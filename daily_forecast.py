"""
[맹그로브 고성] 당일 풍속 예보 - 매일 KST 09:10 발송.
오늘 09시~24시 + 내일 00시~09시 풍속을 3시간 구간으로 묶어 Slack #gs-routine 에 보냄.
"""
import os
import re
import sys
import time
import datetime
import unicodedata
import requests
import wthr_warn


def mask_key(e):
    """에러 메시지에 섞여 나오는 serviceKey 값을 가려 Slack 노출 방지."""
    return re.sub(r"(serviceKey=)[^&\s]+", r"\1***", str(e))

KMA_API_KEY = os.environ["KMA_API_KEY"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
SLACK_USER_MENTION = os.environ.get("SLACK_USER_MENTION", "")
if not SLACK_USER_MENTION:
    # 멘션이 빠지면 슬랙 알림이 안 떠서 못 보고 지나간다 — 조용히 넘어가지 않고 로그에 남긴다.
    print("[WARN] SLACK_USER_MENTION 미설정 — 멘션 없이 전송됨")

NX = int(os.environ.get("KMA_NX", "87"))
NY = int(os.environ.get("KMA_NY", "131"))

# 기상청 강풍 기준 (평균 풍속)
WARN_BASE = 14.0   # 강풍주의보
ALERT_BASE = 21.0  # 강풍경보

KST = datetime.timezone(datetime.timedelta(hours=9))
FCST_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"

# 일시 오류로 보고 재시도할 조건 (check_wind.py 와 동일 정책)
RETRY_HTTP = {429, 500, 502, 503, 504}
RETRY_RESULT_CODES = {"22"}   # LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS (호출량 일시 초과)
BACKOFF = [3, 8, 20]          # 재시도 간 대기(초). 총 최대 4회 시도.

DIRS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
        "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
DIR_KR = {"N": "북", "NNE": "북북동", "NE": "북동", "ENE": "동북동",
          "E": "동", "ESE": "동남동", "SE": "남동", "SSE": "남남동",
          "S": "남", "SSW": "남남서", "SW": "남서", "WSW": "서남서",
          "W": "서", "WNW": "서북서", "NW": "북서", "NNW": "북북서"}

GROUPS_TODAY = [9, 12, 15, 18, 21]
GROUPS_TOMORROW = [0, 3, 6]

# 표 컬럼 폭 (한글 1글자 = 2폭 기준)
TIME_W = 9
WIND_W = 16
DIR_W = 9
TEMP_W = 8


def disp_len(s):
    n = 0
    for c in s:
        n += 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
    return n


def pad(s, width):
    return s + " " * max(0, width - disp_len(s))


def deg_to_dir(deg):
    idx = int((float(deg) + 11.25) // 22.5) % 16
    return DIR_KR[DIRS[idx]]


def latest_base(now):
    """단기예보 발표시각(02,05,08,11,14,17,20,23) 중 직전 것."""
    bases = [2, 5, 8, 11, 14, 17, 20, 23]
    h = now.hour if now.minute >= 15 else now.hour - 1
    valid = [b for b in bases if b <= h]
    if valid:
        return now.strftime("%Y%m%d"), f"{max(valid):02d}00"
    y = now - datetime.timedelta(days=1)
    return y.strftime("%Y%m%d"), "2300"


class TransientAPIError(Exception):
    """일시적(429/5xx/타임아웃/호출량 초과) 오류 — 재시도로 회복 가능한 종류."""


def fetch_fcst(base_date, base_time):
    """기상청 단기예보 호출 + 일시오류 재시도.

    data.go.kr 이 간헐적으로 연결 타임아웃을 내는데, 1회 호출로 끝내면 그때마다
    '발송 실패' 알림이 떴다. check_wind.py 와 같은 백오프 재시도로 자가 회복시킨다."""
    params = {
        "serviceKey": KMA_API_KEY,
        "numOfRows": "1000",
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
            r = requests.get(FCST_URL, params=params, timeout=30)
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
            print(f"[예보][재시도 {attempt + 1}/{len(BACKOFF)}] {mask_key(last)}")
            time.sleep(BACKOFF[attempt])
        else:
            raise last


def post_slack(text):
    r = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
    r.raise_for_status()


def collect(items, target_date):
    by_time = {}
    for it in items:
        if it["fcstDate"] != target_date:
            continue
        by_time.setdefault(it["fcstTime"], {})[it["category"]] = it["fcstValue"]
    return by_time


def group_3h(by_time, start_hours):
    """3시간 구간별로 풍속 범위/풍향(중간 시점)/기온 범위 집계."""
    out = []
    for s in start_hours:
        wsds, vecs, tmps = [], [], []
        for h in range(s, s + 3):
            d = by_time.get(f"{h:02d}00")
            if not d:
                continue
            if "WSD" in d:
                wsds.append(float(d["WSD"]))
            if "VEC" in d:
                vecs.append(float(d["VEC"]))
            if "TMP" in d:
                try:
                    tmps.append(int(float(d["TMP"])))
                except ValueError:
                    pass
        if not wsds:
            continue
        vec_mid = vecs[len(vecs) // 2] if vecs else None
        out.append({
            "label": f"{s:02d}~{s + 3:02d}시",
            "wsd_min": min(wsds),
            "wsd_max": max(wsds),
            "vec": deg_to_dir(vec_mid) if vec_mid is not None else "-",
            "tmp_min": min(tmps) if tmps else None,
            "tmp_max": max(tmps) if tmps else None,
        })
    return out


def fmt_wsd(g):
    lo, hi = g["wsd_min"], g["wsd_max"]
    if abs(lo - hi) < 0.05:
        return f"{lo:.1f} m/s"
    return f"{lo:.1f}~{hi:.1f} m/s"


def fmt_temp(g):
    lo, hi = g["tmp_min"], g["tmp_max"]
    if lo is None:
        return "-"
    if lo == hi:
        return f"{lo}℃"
    return f"{lo}~{hi}℃"


def fmt_header():
    return pad("시간대", TIME_W) + pad("풍속", WIND_W) + pad("풍향", DIR_W) + pad("기온", TEMP_W)


def fmt_row(g):
    return pad(g["label"], TIME_W) + pad(fmt_wsd(g), WIND_W) + pad(g["vec"], DIR_W) + pad(fmt_temp(g), TEMP_W)


def summary_line(max_wsd):
    if max_wsd >= ALERT_BASE:
        return f"🚨 *최고 풍속 {max_wsd:.1f} m/s — 강풍경보 기준 → 강풍 대비 점검 진행*"
    if max_wsd >= WARN_BASE:
        return f"🚨 *최고 풍속 {max_wsd:.1f} m/s — 강풍주의보 기준 → 강풍 대비 점검 진행*"
    return f"🔔 *최고 풍속 {max_wsd:.1f} m/s — 상시 정비 진행*"


def active_warning_line():
    """현재 발효 중인 고성 기상특보를 한 줄로. 조회 실패해도 풍속 예보엔 영향 없게 여기서 흡수."""
    try:
        goseong, all_active = wthr_warn.get_goseong_warnings(KMA_API_KEY)
        print(f"[특보] 강원 전체 활성: {all_active}")  # 고성 표기 관찰용 로그
        if goseong:
            return f"🚨 *현재 발효 특보(고성): {', '.join(goseong)}* — 시설 대비 점검"
        return "☀️ 현재 발효 중인 기상특보 없음(고성)"
    except wthr_warn.TransientWarnError as e:
        print(f"[특보][SKIP] 일시 오류: {wthr_warn.mask_key(e)}")
        return "⚠️ _기상특보 조회 일시 실패_"
    except Exception as e:
        print(f"[특보][ERR] {wthr_warn.mask_key(e)}")
        return "⚠️ _기상특보 조회 실패 — 기상청 특보 직접 확인 권장_"


def check_wind_watch_alive():
    """30분 강풍 감시(check_wind)가 최근에 돌았는지 GitHub API로 점검 → 멈춰 있으면 슬랙 경고(dead-man).
    daily_forecast 는 외부 트리거(cron-job.org)로 매일 도니, check_wind 트리거(cron-job.org/GitHub cron)가
    죽어도 여기서 하루 1회 잡아낸다. 점검 실패는 무시(데일리 예보 발송엔 영향 없음). 2026-06-29 추가."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        return
    try:
        url = "https://api.github.com/repos/yjan-max/gs-wind-alert/actions/workflows/wind-check.yml/runs?per_page=1"
        r = requests.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, timeout=15)
        r.raise_for_status()
        runs = r.json().get("workflow_runs", [])
        if not runs:
            post_slack(f"{SLACK_USER_MENTION} 🔴 *[맹그로브 고성] 30분 강풍 감시 실행 이력 없음* — 트리거 점검 필요")
            return
        last = datetime.datetime.strptime(runs[0]["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        age_min = (datetime.datetime.now(datetime.timezone.utc) - last).total_seconds() / 60
        if age_min > 90:
            last_kst = last.astimezone(KST).strftime("%m/%d %H:%M")
            hrs = int(age_min // 60)
            post_slack(f"{SLACK_USER_MENTION} 🔴 *[맹그로브 고성] 30분 강풍 감시 멈춤 감지* — 마지막 실행 {last_kst}({hrs}시간 전). cron-job.org/GitHub cron 트리거 점검 필요.")
    except Exception:
        pass


def already_posted_today():
    """오늘(KST) daily-forecast 가 이미 성공 실행됐는지 GitHub API 로 확인 → 백업 트리거의 이중 발송 방지.
    cron-job.org(09:10)가 정상이면 그 run 이 success 로 남고, GitHub cron 백업(09:40)은 이걸 보고 skip.
    cron-job.org 가 죽어 오늘 success 가 없으면 백업이 이어받아 발송(이 함수가 False 반환).
    현재 진행 중인 run 은 conclusion 이 아직 없으므로 자기 자신은 안 셈. 토큰 없으면 판단 불가 → False(그냥 발송)."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        return False
    try:
        url = "https://api.github.com/repos/yjan-max/gs-wind-alert/actions/workflows/daily-forecast.yml/runs?per_page=20"
        r = requests.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, timeout=15)
        r.raise_for_status()
        today = datetime.datetime.now(KST).strftime("%Y-%m-%d")
        for run in r.json().get("workflow_runs", []):
            if run.get("conclusion") != "success":
                continue
            created_kst = datetime.datetime.strptime(run["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc).astimezone(KST).strftime("%Y-%m-%d")
            if created_kst == today:
                return True
        return False
    except Exception:
        return False


def main():
    now = datetime.datetime.now(KST)
    if already_posted_today():
        print("[SKIP] 오늘 이미 예보 발송됨 — 백업 트리거 중복 방지.")
        return
    check_wind_watch_alive()  # 30분 강풍 감시가 죽었는지 먼저 점검(dead-man)
    today_str = now.strftime("%Y%m%d")
    tomorrow_str = (now + datetime.timedelta(days=1)).strftime("%Y%m%d")
    md = f"{today_str[4:6]}/{today_str[6:8]}"
    base_date, base_time = latest_base(now)

    try:
        items = fetch_fcst(base_date, base_time)
    except Exception as e:
        post_slack(f"{SLACK_USER_MENTION} ⚠️ *[맹그로브 고성] 풍속 예보 발송 실패* (재시도 {len(BACKOFF) + 1}회 모두 실패)\n\nKMA API 호출 오류: {mask_key(e)}")
        sys.exit(1)

    today_groups = group_3h(collect(items, today_str), GROUPS_TODAY)
    tomorrow_groups = group_3h(collect(items, tomorrow_str), GROUPS_TOMORROW)

    if not today_groups and not tomorrow_groups:
        post_slack(f"{SLACK_USER_MENTION} ⚠️ *[맹그로브 고성] 풍속 예보 데이터 없음* (base={base_date} {base_time})")
        sys.exit(1)

    all_groups = today_groups + tomorrow_groups
    max_wsd = max(g["wsd_max"] for g in all_groups)

    lines = [
        f"{SLACK_USER_MENTION} *[맹그로브 고성] 오늘의 풍속 예보 ({md})*",
        "",
        summary_line(max_wsd),
        active_warning_line(),
        "",
    ]

    if today_groups:
        lines += ["", "🕐 *오늘의 풍속*", "```", fmt_header()]
        lines += [fmt_row(g) for g in today_groups]
        lines += ["```"]

    if tomorrow_groups:
        lines += ["", "🌅 *내일의 풍속*", "```", fmt_header()]
        lines += [fmt_row(g) for g in tomorrow_groups]
        lines += ["```"]

    lines += [
        "",
        f"_발표: {base_date[:4]}-{base_date[4:6]}-{base_date[6:8]} {base_time[:2]}시 단기예보_",
    ]

    post_slack("\n".join(lines))
    print(f"[OK] 발송. 최고 {max_wsd} m/s, 오늘 {len(today_groups)}구간, 내일 {len(tomorrow_groups)}구간.")


if __name__ == "__main__":
    main()
