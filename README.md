# 고성 풍속 자동 알림 시스템

매 30분마다 기상청에서 강원도 고성 풍속을 가져와, **강풍주의보 기준의 90%**(평균풍속 12.6m/s)에 도달하거나 6시간 내 예보가 초과하면 Slack `#gs-routine` 채널로 자동 알림을 보냅니다.

루프탑 가구 결박/이동 등 사전 조치 타이밍 놓치지 않도록 만든 시스템입니다.

---

## 동작 방식 한눈에

```
GitHub Actions (매 30분 cron)
       ↓
  Python 스크립트 실행
       ↓
  기상청 API 호출 (현재 풍속 + 6시간 예보)
       ↓
  12.6 m/s 이상이면 → Slack 알림 (본인 멘션)
  미만이면 → 조용히 종료
```

---

## 초기 셋업 (1회만)

### 1단계 — 기상청 API 키 받기 (1~2일 소요)

1. [공공데이터포털](https://www.data.go.kr/) 회원가입
2. 상단 검색창에 **"기상청_단기예보 ((구)_동네예보) 조회서비스"** 검색
3. 해당 API 페이지 → **활용신청** 버튼 클릭
4. 사용 목적: "고성 풍속 모니터링 알림" 등 자유 입력
5. 보통 즉시 또는 1영업일 내 승인 → "마이페이지 > 데이터활용 > Open API > 인증키" 에서 **일반 인증키 (Decoding)** 복사

### 2단계 — Slack Incoming Webhook 만들기

1. Slack 워크스페이스 접속 → 좌측 상단 워크스페이스 이름 클릭 → **도구 및 설정 > 워크스페이스 설정**
2. 좌측 **앱 관리** > 검색창에 **"Incoming Webhooks"** 입력 후 추가
3. **Slack에 추가** > 채널 선택: **#gs-routine** (C0AJ8CL7XPV)
4. 발급된 **Webhook URL** 복사 (형식: `https://hooks.slack.com/services/T.../B.../...`)

> 비공개 채널이라 권한 안 되면 워크스페이스 관리자에게 요청 필요.

### 3단계 — 격자 좌표 확인

기상청은 위경도 대신 격자 번호(nx, ny)를 사용합니다.

- 기본값: `nx=87, ny=131` (강원 고성군 토성면 부근)
- 맹그로브 고성 정확한 위치로 바꾸려면 [기상청 단기예보 격자 좌표 조회](https://www.data.go.kr/data/15084084/openapi.do) 또는 격자 변환 사이트에서 확인

### 4단계 — GitHub 저장소 만들기

1. [GitHub](https://github.com/) 로그인 → 우측 상단 `+` > **New repository**
2. 이름: `gs-wind-alert` / **Private** 체크 / Create
3. 이 폴더의 파일들을 업로드:
   - `check_wind.py`
   - `requirements.txt`
   - `.gitignore`
   - `.github/workflows/wind-check.yml`
   - `README.md`

   업로드 방법: GitHub repo 페이지 > **uploading an existing file** 링크 클릭 > 드래그 앤 드롭

   > 폴더 구조 유지를 위해 `.github/workflows/wind-check.yml` 은 경로 그대로 올려야 합니다. Web UI에서 파일 추가 시 파일명에 `/` 를 입력하면 자동으로 폴더가 됩니다.

### 5단계 — Secrets 등록

repo 페이지 > **Settings** > 좌측 **Secrets and variables > Actions** > **New repository secret**:

| Name | Value |
|------|-------|
| `KMA_API_KEY` | 1단계에서 받은 기상청 일반 인증키(Decoding) |
| `SLACK_WEBHOOK_URL` | 2단계에서 받은 Slack Webhook URL |
| `SLACK_USER_MENTION` | `<@U0AG0G63PTR>` (본인 Slack 사용자 ID) |

같은 페이지 **Variables** 탭에서 (Secrets 아님):

| Name | Value |
|------|-------|
| `KMA_NX` | `87` (또는 3단계 확인 값) |
| `KMA_NY` | `131` (또는 3단계 확인 값) |

### 6단계 — 작동 확인

1. repo 페이지 > 상단 **Actions** 탭
2. 좌측 **고성 풍속 자동 알림** 워크플로 선택
3. 우측 **Run workflow** 버튼 클릭 (수동 실행)
4. 실행 결과 확인 — 녹색 체크 = 성공
5. Slack 채널 확인 — 풍속이 낮으면 알림 없음(정상), 임계값 넘으면 메시지 도착

이후로는 30분마다 자동으로 돕니다.

---

## 알림 예시

```
@예지 🌬️ 고성 강풍 알림 — 루프탑 가구 결박/이동 검토

현재 풍속: 13.2 m/s (관측 05/31 02시 기준)
→ ⚠️ 임계값(12.6m/s) 초과 — 루프탑 점검 필요

향후 6시간 예보 중 임계값 초과 시각:
  • 05/31 03:00 ⚠️ 13.5 m/s
  • 05/31 04:00 🚨 14.8 m/s
  • 05/31 05:00 🚨 15.2 m/s

기준: 강풍주의보 14m/s × 90% = 12.6m/s
관측지점: 격자(87,131) · 출처: 기상청 동네예보 API
```

---

## 임계값/주기 바꾸고 싶을 때

`check_wind.py` 상단:

```python
THRESHOLD_AVG_WIND = 12.6  # 알림 임계값 (m/s)
WARN_BASE = 14.0           # 🚨 표시되는 강풍주의보 기준
```

`.github/workflows/wind-check.yml`:

```yaml
- cron: "*/30 * * * *"  # 30 → 15(15분), 60(1시간) 등
```

---

## 알아두면 좋은 점

- **GitHub Actions 무료 한도**: private repo 월 2,000분. 매 30분 실행이면 월 720분 → 안전.
- **Cron 정확도**: GitHub Actions cron은 부하에 따라 ±5~15분 지연 가능. 즉시 대응이 중요한 시간대(태풍 등)는 수동 `Run workflow` 병행 권장.
- **순간풍속**: 현재 시스템은 KMA 동네예보의 **평균풍속**만 모니터링. 순간풍속(20m/s 기준)까지 정밀하게 보려면 AWS 분(分)자료 API를 추가해야 함 → v2에서 검토.
- **데이터 출처**: [기상청 단기예보 조회서비스](https://www.data.go.kr/data/15084084/openapi.do)
