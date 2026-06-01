"""
고성 풍속 모니터링 - 기상청 동네예보 API → 임계값 초과 시 Slack 알림.
"""
import os
import sys
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


def ncst_base(now):
    target = now - datetime.timedelta(hours=1) if now.minute < 40 else now
    return target.strftime("%Y%m%d"), target.strftime("%H00")


def fcst_base(now):
    target = now - datetime.timedelta(hours=1) if now.minute < 45 else now
    return target.strftime("%Y%m%d"), target.strftime("%H30")


def fetch_ncst(base_date, base_time):
    params = {
        "serviceKey": KMA_API_KEY,
        "numOfRows": "100",
        "pageNo": "1",
        "dataType": "JSON",
        "base_date": base_date,
        "base_time": base_time,
        "nx": NX,
        "ny": NY,
    }
    r = requests.get(NCST_URL, params=params, timeout=30)
    r.raise_for_status()
    items = r.json()["response"]["body"]["items"]["item"]
    return {it["category"]: float(it["obsrValue"]) for it in items}


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
    items = r.json()["response"]["body"]["items"]["item"]
    out = {}
    for it in items:
        if it["category"] != "WSD":
            continue
        ts = f'{it["fcstDate"]} {it["fcstTime"]}'
        out[ts] = float(it["fcstValue"])
    return out


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

    nb_date, nb_time = ncst_base(now)
    try:
        ncst = fetch_ncst(nb_date, nb_time)
    except Exception as e:
        post_slack(f"⚠️ [고성 풍속 모니터링] KMA 실황 API 호출 실패: {e}")
        sys.exit(1)
    current = ncst.get("WSD", 0.0)

    fb_date, fb_time = fcst_base(now)
    try:
        fcst = fetch_fcst(fb_date, fb_time)
    except Exception:
        fcst = {}
    fcst_over = [(ts, v) for ts, v in fcst.items() if v >= THRESHOLD_AVG_WIND]

    if current < THRESHOLD_AVG_WIND and not fcst_over:
        print(f"[OK] 현재 풍속 {current} m/s, 향후 6시간 예보 임계값 미만. 알림 생략.")
        return

    post_slack(build_message(now, current, fcst_over))
    print(f"[ALERT] 현재 {current} m/s, 예보 초과 {len(fcst_over)}건. Slack 전송 완료.")


if __name__ == "__main__":
    main()
