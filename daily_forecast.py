"""
[맹그로브 고성] 당일 풍속 예보 - 매일 KST 09:10 발송.
오늘 09시~24시 + 내일 00시~09시 풍속을 3시간 구간으로 묶어 Slack #gs-routine 에 보냄.
"""
import os
import datetime
import unicodedata
import requests

KMA_API_KEY = os.environ["KMA_API_KEY"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
SLACK_USER_MENTION = os.environ.get("SLACK_USER_MENTION", "<@U0AG0G63PTR>")

NX = int(os.environ.get("KMA_NX", "87"))
NY = int(os.environ.get("KMA_NY", "131"))

# 기상청 강풍 기준 (평균 풍속)
WARN_BASE = 14.0   # 강풍주의보
ALERT_BASE = 21.0  # 강풍경보

KST = datetime.timezone(datetime.timedelta(hours=9))
FCST_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"

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
        return f"> 🚨 *최고 풍속 {max_wsd:.1f} m/s — 강풍경보 기준 → 강풍 대비 점검 진행*"
    if max_wsd >= WARN_BASE:
        return f"> ⚠️ *최고 풍속 {max_wsd:.1f} m/s — 강풍주의보 기준 → 강풍 대비 점검 진행*"
    return f"> 📊 *최고 풍속 {max_wsd:.1f} m/s — 상시 정비 진행*"


def main():
    now = datetime.datetime.now(KST)
    today_str = now.strftime("%Y%m%d")
    tomorrow_str = (now + datetime.timedelta(days=1)).strftime("%Y%m%d")
    md = f"{today_str[4:6]}/{today_str[6:8]}"
    base_date, base_time = latest_base(now)

    try:
        items = fetch_fcst(base_date, base_time)
    except Exception as e:
        post_slack(f"{SLACK_USER_MENTION} ⚠️ *[맹그로브 고성] 풍속 예보 발송 실패*\n\nKMA API 호출 오류: {e}")
        return

    today_groups = group_3h(collect(items, today_str), GROUPS_TODAY)
    tomorrow_groups = group_3h(collect(items, tomorrow_str), GROUPS_TOMORROW)

    if not today_groups and not tomorrow_groups:
        post_slack(f"{SLACK_USER_MENTION} ⚠️ *[맹그로브 고성] 풍속 예보 데이터 없음* (base={base_date} {base_time})")
        return

    all_groups = today_groups + tomorrow_groups
    max_wsd = max(g["wsd_max"] for g in all_groups)

    lines = [
        f"{SLACK_USER_MENTION} *[맹그로브 고성] 오늘의 풍속 예보 ({md})*",
        "",
        summary_line(max_wsd),
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
