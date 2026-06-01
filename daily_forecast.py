"""
고성 당일 풍속 예보 - 매일 KST 09:30 발송.
단기예보 API로 오늘 09~24시 풍속 흐름을 Slack #gs-routine 에 보냄.
"""
import os
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
FCST_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"

DIRS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
        "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
DIR_KR = {"N": "북", "NNE": "북북동", "NE": "북동", "ENE": "동북동",
          "E": "동", "ESE": "동남동", "SE": "남동", "SSE": "남남동",
          "S": "남", "SSW": "남남서", "SW": "남서", "WSW": "서남서",
          "W": "서", "WNW": "서북서", "NW": "북서", "NNW": "북북서"}


def deg_to_dir(deg):
    idx = int((float(deg) + 11.25) // 22.5) % 16
    return DIR_KR[DIRS[idx]]


def latest_base(now):
    bases = [2, 5, 8, 11, 14, 17, 20, 23]
    h = now.hour if now.minute >= 15 else now.hour - 1
    valid = [b for b in bases if b <= h]
    if valid:
        return now.strftime("%Y%m%d"), f"{max(valid):02d}00"
    y = now - datetime.timedelta(days=1)
    return y.strftime("%Y%m%d"), "2300"


def fetch_fcst(base_date, base_time):
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
    r = requests.get(FCST_URL, params=params, timeout=30)
    r.raise_for_status()
    return r.json()["response"]["body"]["items"]["item"]


def post_slack(text):
    r = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
    r.raise_for_status()


def main():
    now = datetime.datetime.now(KST)
    today = now.strftime("%Y%m%d")
    base_date, base_time = latest_base(now)

    try:
        items = fetch_fcst(base_date, base_time)
    except Exception as e:
        post_slack(f"{SLACK_USER_MENTION} ⚠️ [고성 당일 예보] KMA API 호출 실패: {e}")
        return

    by_time = {}
    for it in items:
        if it["fcstDate"] != today:
            continue
        t = it["fcstTime"]
        by_time.setdefault(t, {})[it["category"]] = it["fcstValue"]

    rows = []
    for t in sorted(by_time.keys()):
        if int(t) < 900:
            continue
        d = by_time[t]
        if "WSD" not in d:
            continue
        wsd = float(d["WSD"])
        vec = deg_to_dir(d["VEC"]) if "VEC" in d else "-"
        tmp = d.get("TMP", "-")
        rows.append((t, wsd, vec, tmp))

    if not rows:
        post_slack(f"{SLACK_USER_MENTION} ⚠️ [고성 당일 예보] 응답에서 오늘 예보 없음 (base={base_date} {base_time})")
        return

    max_wsd = max(r[1] for r in rows)
    over_rows = [r for r in rows if r[1] >= THRESHOLD_AVG_WIND]

    lines = [f"{SLACK_USER_MENTION} 🌬️ *고성 오늘의 풍속 예보 ({today[4:6]}/{today[6:8]})*", ""]
    lines.append("```")
    lines.append("시각   풍속        풍향      기온")
    for t, wsd, vec, tmp in rows:
        mark = " 🚨" if wsd >= WARN_BASE else (" ⚠️" if wsd >= THRESHOLD_AVG_WIND else "")
        lines.append(f"{t[:2]}시   {wsd:>4.1f} m/s   {vec:<5}   {tmp:>3}℃{mark}")
    lines.append("```")
    lines.append("")
    lines.append(f"*오늘 최고 풍속 전망*: `{max_wsd:.1f} m/s`")
    if over_rows:
        first_t = over_rows[0][0]
        lines.append(f"⚠️ *임계값(12.6m/s) 초과 시점*: {len(over_rows)}건 (첫 시점 {first_t[:2]}시)")
        lines.append("→ 루프탑 가구 사전 결박/이동 검토 권장")
    else:
        lines.append("_임계값 초과 없음 — 일반 운영 OK_")
    lines.append("")
    lines.append(f"_발표: {base_date[:4]}-{base_date[4:6]}-{base_date[6:8]} {base_time[:2]}시 단기예보 · 격자({NX},{NY})_")
    post_slack("\n".join(lines))
    print(f"[OK] 당일 예보 발송. 최고 {max_wsd} m/s, 임계값 초과 {len(over_rows)}건.")


if __name__ == "__main__":
    main()
