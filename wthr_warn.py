"""
고성(강원도 고성군) 기상특보 조회 — 기상청_기상특보 조회서비스(WthrWrnInfoService).

동네예보 API에는 특보 데이터가 없어 별도 서비스를 쓴다. getPwnStatus(발효 현황)는
'지금 발효 중인 특보' 스냅샷 1건을 광역(강원지방기상청 stnId=105) 단위로 준다.
그 통보문 텍스트에서 '종류 : 지역' 줄을 파싱해, 지역에 '고성'이 든 대상 특보만 골라낸다.

이 모듈은 호출부(daily_forecast / check_wind)에서 try/except로 감싸 쓰며,
여기서 나는 어떤 오류도 기존 풍속 알림을 막지 않도록 설계한다.
"""
import os
import re
import time
import datetime
import requests

KST = datetime.timezone(datetime.timedelta(hours=9))

# 강원지방기상청. 강원도 고성군은 여기 관할 → 경남 고성과 자동 분리됨.
STN_GANGWON = "105"
PWN_URL = "https://apis.data.go.kr/1360000/WthrWrnInfoService/getPwnStatus"

# 운영에 영향 큰 특보만 (사용자 선택). 종류 어간(주의보/경보 앞부분) 기준.
TARGET_STEMS = {"호우", "대설", "강풍", "풍랑", "태풍", "폭염", "한파"}

# 고성이 걸리는 지역 표기. KMA는 시군 단위로 씀(예: "홍천평지") → 고성 육상 특보는 "고성"으로 표기될 것.
# 실제 고성 특보 사례를 로그로 관찰 후, 다른 구역명(예: 강원북부동해안/앞바다)으로 나오면 여기 추가.
REGION_TOKENS = ["고성"]

# 일시 오류로 보고 재시도할 HTTP 상태
RETRY_HTTP = {429, 500, 502, 503, 504}
BACKOFF = [3, 8]  # 총 최대 3회 시도

# "o 호우주의보 : 강원도(춘천)" 같은 줄에서 (종류어간)(레벨) : (지역) 추출
LINE_RE = re.compile(r"([가-힣]{2,8})(주의보|경보)\s*[:：]\s*(.+)")


class TransientWarnError(Exception):
    """일시적(429/5xx/타임아웃) 특보 API 오류 — 다음 주기 회복 기대. 조용히 넘긴다."""


def mask_key(e):
    return re.sub(r"(serviceKey=)[^&\s]+", r"\1***", str(e))


def _fetch_pwn_item(api_key, stn=STN_GANGWON):
    """getPwnStatus 최신 발효현황 1건(dict) 반환. 발효 없음이어도 item 은 옴(필드가 '없음')."""
    params = {
        "serviceKey": api_key,
        "dataType": "JSON",
        "numOfRows": "10",
        "pageNo": "1",
        "stnId": stn,
    }
    last = None
    for attempt in range(len(BACKOFF) + 1):
        try:
            r = requests.get(PWN_URL, params=params, timeout=25)
            if r.status_code in RETRY_HTTP:
                raise TransientWarnError(f"HTTP {r.status_code}")
            r.raise_for_status()
            body = r.json()["response"]
            code = body["header"]["resultCode"]
            if code == "03":            # NO_DATA — 발효현황 자체가 없음(사실상 발효 없음)
                return {}
            if code != "00":
                raise RuntimeError(f'resultCode {code} ({body["header"].get("resultMsg","")})')
            items = body["body"]["items"]["item"]
            return items[0] if isinstance(items, list) else items
        except (requests.Timeout, requests.ConnectionError) as e:
            last = TransientWarnError(mask_key(e))
        except TransientWarnError as e:
            last = e
        if attempt < len(BACKOFF):
            time.sleep(BACKOFF[attempt])
        else:
            raise last


def parse_active(item):
    """발효현황 item 의 모든 문자열 필드에서 '종류 : 지역' 줄을 뽑아 [(kind, region), ...] 반환.
    kind = '호우주의보' 처럼 종류어간+레벨. 대상종류 필터는 아직 안 함(관찰 로그용 전체)."""
    out = []
    for v in item.values():
        if not isinstance(v, str):
            continue
        for raw_line in re.split(r"[\r\n]+", v):
            line = raw_line.strip().lstrip("oㅇ•・ ").strip()
            if not line or "없" in line[:3]:   # "없음" / "없 음"
                continue
            m = LINE_RE.search(line)
            if not m:
                continue
            kind = m.group(1) + m.group(2)      # 예: 호우 + 주의보
            region = m.group(3).strip()
            out.append((kind, region, m.group(1)))
    # 중복 제거(순서 유지)
    seen, uniq = set(), []
    for k, region, stem in out:
        key = (k, region)
        if key not in seen:
            seen.add(key)
            uniq.append((k, region, stem))
    return uniq


def get_goseong_warnings(api_key, stn=STN_GANGWON):
    """반환: (goseong_active, all_active)
    goseong_active = 고성 대상특보 kind 목록(정렬), 예: ['강풍주의보','호우주의보']
    all_active = 관찰용 전체 활성 [(kind, region)] (강원 전체) — 호출부에서 로그로 남길 것.
    실패 시: 일시오류는 TransientWarnError, 그 외는 원 예외 전파(호출부가 처리)."""
    item = _fetch_pwn_item(api_key, stn)
    if not item:
        return [], []
    parsed = parse_active(item)
    all_active = [(k, r) for (k, r, _) in parsed]
    goseong = sorted({
        k for (k, region, stem) in parsed
        if stem in TARGET_STEMS and any(tok in region for tok in REGION_TOKENS)
    })
    return goseong, all_active


if __name__ == "__main__":
    # 로컬 점검용: 현재 강원 발효현황 출력
    key = os.environ.get("KMA_API_KEY", "")
    g, a = get_goseong_warnings(key)
    print("고성 대상특보:", g)
    print("강원 전체 활성:", a)
