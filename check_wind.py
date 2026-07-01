"""
고성 풍속 모니터링 - 기상청 동네예보 API → 임계값 초과 시 Slack 알림.

일시 오류(429/5xx/타임아웃/호출량 초과)는 실행 안에서 재시도로 자가 회복하고,
재시도까지 실패해도 '일시적'이면 조용히 건너뛴다(실패표시·Slack 알림 X, 다음 주기 회복).
진짜 오류(키·요청·응답구조)만 Slack 으로 알린다.
"""
import os
import re
import sys
import time
import datetime
import requests

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


def mask_key(e):
    """에러 메시지에 섞여 나오는 serviceKey 값을 가려 Slack 노출 방지."""
    return re.sub(r"(serviceKey=)[^&\s]+", r"\1***", str(e))


class TransientAPIError(Exception):
    """일시적(429/5xx/타임아웃/호출량 초과) 오류 — 다음 주기에 자동 회복 기대. 알림하지 않음."""


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


def build_message(now, current, fcst_over):
    lines = [f"{SLACK_USER_MENTION} 🌬️ *고성 강풍 알림 — 루프탑 가구 결박/이동 검토*", ""]
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

    # 1) 실황(현재 풍속) — 감시의 핵심 신호.
    nb_date, nb_time = ncst_base(now)
    try:
        ncst = fetch_ncst(nb_date, nb_time)
    except TransientAPIError as e:
        # 재시도까지 실패했지만 일시적 → 조용히 건너뜀(실패표시·알림 X). 다음 주기가 회복함.
        print(f"[SKIP] 실황 API 일시 오류(재시도 실패): {mask_key(e)}. 이번 주기 건너뜀.")
        return
    except Exception as e:
        # 진짜 오류(키·요청·응답구조) → 알림 후 실패.
        post_slack(f"⚠️ [고성 풍속 모니터링] KMA 실황 API 오류(일시적 아님, 점검 필요): {mask_key(e)}")
        sys.exit(1)
    current = ncst.get("WSD", 0.0)

    # 2) 예보(향후 6시간) — 없어도 현재 풍속 감시는 계속.
    fb_date, fb_time = fcst_base(now)
    try:
        fcst = fetch_fcst(fb_date, fb_time)
    except TransientAPIError as e:
        print(f"[WARN] 예보 API 일시 오류(재시도 실패): {mask_key(e)}. 예보 없이 현재풍속만 판단.")
        fcst = {}
    except Exception as e:
        post_slack(f"⚠️ [고성 풍속 모니터링] KMA 예보 API 오류(점검 필요) — 현재 풍속 {current} m/s 는 정상 수집됨: {mask_key(e)}")
        fcst = {}
    fcst_over = [(ts, v) for ts, v in fcst.items() if v >= THRESHOLD_AVG_WIND]

    if current < THRESHOLD_AVG_WIND and not fcst_over:
        print(f"[OK] 현재 풍속 {current} m/s, 향후 6시간 예보 임계값 미만. 알림 생략.")
        return

    post_slack(build_message(now, current, fcst_over))
    print(f"[ALERT] 현재 {current} m/s, 예보 초과 {len(fcst_over)}건. Slack 전송 완료.")


if __name__ == "__main__":
    main()
