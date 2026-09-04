"""
고성 풍속 모니터링 - 기상청 동네예보 API → 임계값 초과 시 Slack 알림.

일시 오류(429/5xx/타임아웃/호출량 초과)는 실행 안에서 재시도로 자가 회복하고,
재시도까지 실패해도 '일시적'이면 조용히 건너뛴다(실패표시·Slack 알림 X, 다음 주기 회복).
기상청 자료 공백(resultCode 03 NO_DATA)은 우리 쪽 오류가 아니므로 초단기예보로 대체 판단하고,
공백이 길어질 때만 1회 알린다. 진짜 오류(키·요청·응답구조)만 즉시 Slack 으로 알린다.
"""
import os
import re
import sys
import json
import time
import datetime
import requests
import wthr_warn

KMA_API_KEY = os.environ["KMA_API_KEY"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
SLACK_USER_MENTION = os.environ.get("SLACK_USER_MENTION", "<@U0AG0G63PTR>")

NX = int(os.environ.get("KMA_NX", "87"))
NY = int(os.environ.get("KMA_NY", "131"))

THRESHOLD_AVG_WIND = 12.6
WARN_BASE = 14.0

KST = datetime.timezone(datetime.timedelta(hours=9))
NCST_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtNcst"
FCST_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtFcst"

# 일시 오류로 보고 재시도할 조건
RETRY_HTTP = {429, 500, 502, 503, 504}
RETRY_RESULT_CODES = {"22"}   # LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS (호출량 일시 초과)
BACKOFF = [3, 8, 20]          # 재시도 간 대기(초). 총 최대 4회 시도.

NO_DATA_CODE = "03"           # NO_DATA — 기상청이 해당 시각 자료를 아직 안 올렸거나 누락한 상태
# 실황 공백이 이만큼 이어지면 그때 1회만 알린다(30분마다 같은 알림이 반복되는 것을 막기 위함).
NCST_GAP_ALERT_AFTER = datetime.timedelta(hours=2)


def mask_key(e):
    """에러 메시지에 섞여 나오는 serviceKey 값을 가려 Slack 노출 방지."""
    return re.sub(r"(serviceKey=)[^&\s]+", r"\1***", str(e))


class TransientAPIError(Exception):
    """일시적(429/5xx/타임아웃/호출량 초과) 오류 — 다음 주기에 자동 회복 기대. 알림하지 않음."""


class NoDataError(Exception):
    """기상청에 해당 시각 자료가 없음(resultCode 03). 우리 쪽 오류가 아니라 기상청 공백이므로,
    재시도해도 소용없고 즉시 대체 수단(초단기예보)으로 판단을 이어간다."""


def _call(url, base_date, base_time, num_rows):
    """기상청 API 호출 + 일시오류 재시도. 성공 시 item 리스트 반환.
    일시오류가 재시도까지 실패하면 TransientAPIError, 그 외(키/요청/구조 오류)는 원 예외 전파."""
    params = {
        "serviceKey": KMA_API_KEY,
        "numOfRows": num_rows,
        "pageNo": "1",
        "dataType": "JSON",
        "base_date": base_date,
        "base_time": base_time,
        "nx": NX,
        "ny": NY,
    }
    last = None
    for attempt in range(len(BACKOFF) + 1):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code in RETRY_HTTP:
                raise TransientAPIError(f"HTTP {r.status_code}")
            r.raise_for_status()
            body = r.json()["response"]
            code = body["header"]["resultCode"]
            if code != "00":
                msg = f'resultCode {code} ({body["header"].get("resultMsg", "")})'
                if code in RETRY_RESULT_CODES:
                    raise TransientAPIError(msg)
                if code == NO_DATA_CODE:
                    raise NoDataError(msg)       # 기상청 자료 공백 → 재시도 무의미, 상위에서 대체 판단
                raise RuntimeError(msg)          # 키오류·무효요청 등 → 진짜 오류(전파)
            return body["body"]["items"]["item"]
        except (requests.Timeout, requests.ConnectionError) as e:
            last = TransientAPIError(mask_key(e))
        except TransientAPIError as e:
            last = e
        # 여기 도달 = 일시오류. 남은 시도가 있으면 대기 후 재시도, 없으면 포기.
        if attempt < len(BACKOFF):
            time.sleep(BACKOFF[attempt])
        else:
            raise last


def fetch_ncst(base_date, base_time):
    items = _call(NCST_URL, base_date, base_time, "100")
    return {it["category"]: float(it["obsrValue"]) for it in items}


def fetch_fcst(base_date, base_time):
    items = _call(FCST_URL, base_date, base_time, "1000")
    out = {}
    for it in items:
        if it["category"] != "WSD":
            continue
        ts = f'{it["fcstDate"]} {it["fcstTime"]}'
        out[ts] = float(it["fcstValue"])
    return out


def ncst_base(now):
    target = now - datetime.timedelta(hours=1) if now.minute < 40 else now
    return target.strftime("%Y%m%d"), target.strftime("%H00")


def fcst_base(now):
    target = now - datetime.timedelta(hours=1) if now.minute < 45 else now
    return target.strftime("%Y%m%d"), target.strftime("%H30")


def post_slack(text):
    r = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
    r.raise_for_status()


# ── 실행 간 상태(state) ────────────────────────────────────────────────────
# 30분마다 도는 실행끼리 기억을 넘기는 유일한 수단. 워크플로우가 '내용이 바뀐 경우에만' 커밋한다.
#   active            : 현재 발효 중인 기상특보 목록 (반복 알림 방지용)
#   ncst_gap_since    : 실황 자료 공백이 시작된 시각 (ISO)
#   ncst_gap_alerted  : 그 공백에 대해 이미 알림을 보냈는지
WARN_STATE_FILE = "warn_state.json"


def load_state():
    try:
        with open(WARN_STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
            return s if isinstance(s, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def state_fingerprint(s):
    """커밋할 만한 '의미 있는' 변화만 추린 값. updated 시각은 매 실행 바뀌므로 제외한다
    (포함하면 30분마다 무의미한 커밋이 쌓인다)."""
    return (list(s.get("active", [])), s.get("ncst_gap_since"), bool(s.get("ncst_gap_alerted")))


def save_state(s, now):
    s["updated"] = now.strftime("%Y-%m-%d %H:%M KST")
    with open(WARN_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


def flush_state(state, before, now):
    """의미 있는 변화가 있을 때만 파일에 쓴다. 중간에 종료하는 경로에서도 반드시 거쳐야 한다."""
    if state_fingerprint(state) != before:
        save_state(state, now)


def gap_started_at(state):
    v = state.get("ncst_gap_since")
    if not v:
        return None
    try:
        return datetime.datetime.fromisoformat(v)
    except (TypeError, ValueError):
        return None    # 상태파일이 깨졌으면 공백을 지금부터 새로 센다


# ── 기상특보 변화 감지 (호우/대설/강풍/풍랑/태풍/폭염/한파) ────────────────
# 현재 발효 특보를 이전 상태와 비교해, '새 발효/해제'가 있을 때만 알린다(반복 알림 방지).
# 이 블록의 어떤 오류도 풍속 감시를 막지 않는다.


def build_warn_message(now, added, removed, current):
    lines = [f"{SLACK_USER_MENTION} 🌧️ *[맹그로브 고성] 기상특보 변경* ({now.strftime('%m/%d %H:%M')})", ""]
    if added:
        lines.append(f"🚨 *새로 발효*: {', '.join(added)} — 시설 대비 점검")
    if removed:
        lines.append(f"✅ 해제: {', '.join(removed)}")
    lines.append("")
    lines.append(f"*현재 발효 중*: {', '.join(current) if current else '없음'}")
    lines.append("_출처: 기상청 기상특보 (강원)_")
    return "\n".join(lines)


def check_warnings(now, state):
    """현재 고성 특보를 이전 상태와 비교 → 변화 시에만 알림 + 상태 갱신(파일 쓰기는 main 이 담당)."""
    try:
        goseong, all_active = wthr_warn.get_goseong_warnings(KMA_API_KEY)
    except wthr_warn.TransientWarnError as e:
        print(f"[특보][SKIP] 일시 오류: {wthr_warn.mask_key(e)}")
        return
    except Exception as e:
        print(f"[특보][ERR] {wthr_warn.mask_key(e)}")
        return
    print(f"[특보] 강원 전체 활성: {all_active}")  # 고성 표기 관찰용 로그
    prev = list(state.get("active", []))
    added = [k for k in goseong if k not in prev]
    removed = [k for k in prev if k not in goseong]
    if added or removed:
        post_slack(build_warn_message(now, added, removed, goseong))
        state["active"] = goseong
        print(f"[특보] 변화 감지 → 알림. added={added} removed={removed} 현재={goseong}")
    else:
        print(f"[특보] 변화 없음. 현재={goseong}")


def safe_check_warnings(now, state):
    """특보 로직의 어떤 실패도 풍속 감시에 영향 주지 않도록 최종 방어."""
    try:
        check_warnings(now, state)
    except Exception as e:
        print(f"[특보][FATAL-무시] {wthr_warn.mask_key(e)}")


def build_message(now, current, fcst_over):
    """current 가 None 이면 실황 자료 공백 — 예보만으로 판단한 알림."""
    lines = [f"{SLACK_USER_MENTION} 🌬️ *고성 강풍 알림 — 루프탑 가구 결박/이동 검토*", ""]
    if current is None:
        lines.append("*현재 풍속*: 기상청 실황 자료 공백 — 예보만으로 판단")
        lines.append("→ ⚠️ *향후 예보가 임계값 초과* — 루프탑 점검 필요")
    else:
        lines.append(f"*현재 풍속*: `{current:.1f} m/s` (관측 {now.strftime('%m/%d %H시')} 기준)")
        if current >= WARN_BASE:
            lines.append("→ 🚨 *강풍주의보 기준 초과* — 즉시 조치 필요")
        elif current >= THRESHOLD_AVG_WIND:
            lines.append("→ ⚠️ *임계값(12.6m/s) 초과* — 루프탑 점검 필요")
        else:
            lines.append("→ 현재는 임계값 미만이지만 향후 예보가 초과")
    lines.append("")
    if fcst_over:
        lines.append("*향후 6시간 예보 중 임계값 초과 시각*:")
        for ts, v in sorted(fcst_over):
            d, t = ts.split()
            mark = "🚨" if v >= WARN_BASE else "⚠️"
            lines.append(f"  • {d[4:6]}/{d[6:8]} {t[:2]}:{t[2:]} {mark} `{v:.1f} m/s`")
        lines.append("")
    lines.append("_기준: 강풍주의보 14m/s × 90% = 12.6m/s_")
    lines.append(f"_관측지점: 격자({NX},{NY}) · 출처: 기상청 동네예보 API_")
    return "\n".join(lines)


def main():
    now = datetime.datetime.now(KST)
    state = load_state()
    before = state_fingerprint(state)

    # 0) 기상특보 변화 감지 — 풍속과 독립. 실패해도 아래 풍속 감시는 그대로 진행.
    safe_check_warnings(now, state)

    # 1) 실황(현재 풍속) — 감시의 핵심 신호. 없으면 current=None 으로 두고 예보로 대체 판단.
    nb_date, nb_time = ncst_base(now)
    current = None
    ncst_gap_reason = None
    try:
        ncst = fetch_ncst(nb_date, nb_time)
        current = ncst.get("WSD", 0.0)
    except TransientAPIError as e:
        # 재시도까지 실패했지만 일시적 → 조용히 건너뜀(실패표시·알림 X). 다음 주기가 회복함.
        print(f"[SKIP] 실황 API 일시 오류(재시도 실패): {mask_key(e)}. 이번 주기 건너뜀.")
        flush_state(state, before, now)
        return
    except NoDataError as e:
        # 기상청 자료 공백 — 우리 오류가 아니므로 실패 처리하지 않는다. 예보로 감시를 이어간다.
        ncst_gap_reason = mask_key(e)
        print(f"[NO_DATA] 실황 {nb_date} {nb_time} 자료 없음: {ncst_gap_reason}. 예보로 대체 판단.")
    except Exception as e:
        # 진짜 오류(키·요청·응답구조) → 알림 후 실패.
        post_slack(f"⚠️ [고성 풍속 모니터링] KMA 실황 API 오류(일시적 아님, 점검 필요): {mask_key(e)}")
        flush_state(state, before, now)
        sys.exit(1)

    # 2) 예보(향후 6시간) — 실황이 있을 땐 보조, 실황 공백일 땐 유일한 감시 수단.
    fb_date, fb_time = fcst_base(now)
    fcst = {}
    try:
        fcst = fetch_fcst(fb_date, fb_time)
    except TransientAPIError as e:
        print(f"[WARN] 예보 API 일시 오류(재시도 실패): {mask_key(e)}. 예보 없이 판단.")
    except NoDataError as e:
        print(f"[WARN] 예보 {fb_date} {fb_time} 자료 없음: {mask_key(e)}. 예보 없이 판단.")
    except Exception as e:
        # 실황이 멀쩡하면 예보 오류만 따로 알린다. 실황도 공백이면 아래 공백 경보로 합쳐 알린다.
        print(f"[ERR] 예보 API 오류: {mask_key(e)}")
        if current is not None:
            post_slack(f"⚠️ [고성 풍속 모니터링] KMA 예보 API 오류(점검 필요) — 현재 풍속 {current} m/s 는 정상 수집됨: {mask_key(e)}")

    # 3) 실황 공백 추적 — 짧은 공백은 조용히 넘기고, 길어지면 그 공백당 딱 1회만 알린다.
    if current is not None:
        had_gap = state.pop("ncst_gap_since", None)
        had_alert = state.pop("ncst_gap_alerted", False)
        if had_gap and had_alert:
            post_slack(f"✅ [고성 풍속 모니터링] 기상청 실황 자료 복구 — 현재 풍속 {current:.1f} m/s. 정상 감시 재개.")
    else:
        since = gap_started_at(state)
        if since is None:
            since = now
            state["ncst_gap_since"] = now.isoformat()
        elapsed = now - since
        if elapsed >= NCST_GAP_ALERT_AFTER and not state.get("ncst_gap_alerted"):
            state["ncst_gap_alerted"] = True
            if fcst:
                detail = f"현재는 초단기예보로 대체 감시 중(향후 6시간 최고 `{max(fcst.values()):.1f} m/s`)."
            else:
                detail = "*예보 자료도 받지 못해 강풍 감시가 완전히 멈춰 있음 — 확인 필요.*"
            post_slack(
                f"{SLACK_USER_MENTION} ⚠️ [고성 풍속 모니터링] 기상청 실황 자료 공백이 "
                f"{elapsed.total_seconds() / 3600:.1f}시간째 ({ncst_gap_reason}). {detail}"
            )
            print(f"[GAP-ALERT] 실황 공백 {elapsed}. Slack 1회 통보.")

    # 4) 판단 — 실황이 없으면 예보만으로 본다.
    fcst_over = [(ts, v) for ts, v in fcst.items() if v >= THRESHOLD_AVG_WIND]
    if (current is None or current < THRESHOLD_AVG_WIND) and not fcst_over:
        print(f"[OK] 현재 풍속 {current if current is not None else '공백'}, "
              f"향후 6시간 예보 임계값 미만. 알림 생략.")
        flush_state(state, before, now)
        return

    post_slack(build_message(now, current, fcst_over))
    print(f"[ALERT] 현재 {current if current is not None else '공백'}, "
          f"예보 초과 {len(fcst_over)}건. Slack 전송 완료.")
    flush_state(state, before, now)


if __name__ == "__main__":
    main()
