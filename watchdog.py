"""
[맹그로브 고성] 강풍 감시(wind-check) 공백 자동 복구 — 매시 정각 실행.
wind-check.yml 이 90분 넘게 안 돌았으면 GitHub API로 재트리거(workflow_dispatch)하고 Slack 알림.
cron-job.org(1차)·wind-check.yml 내장 */30 cron(2차) 이 동시에 죽는 경우를 대비한 3중 백업.
정상일 땐 조용히 종료(알림 없음).
"""
import datetime
import os

import requests

REPO = "yjan-max/gs-wind-alert"
WORKFLOW = "wind-check.yml"
STALE_MIN = 90

SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
SLACK_USER_MENTION = os.environ.get("SLACK_USER_MENTION", "<@U0AG0G63PTR>")
KST = datetime.timezone(datetime.timedelta(hours=9))


def post_slack(text):
    r = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
    r.raise_for_status()


def dispatch(headers):
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches"
    r = requests.post(url, headers=headers, json={"ref": "main"}, timeout=15)
    r.raise_for_status()


def main():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GH_TOKEN 없음 — 점검 불가")
        return
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    runs_url = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/runs?per_page=1"
    r = requests.get(runs_url, headers=headers, timeout=15)
    r.raise_for_status()
    runs = r.json().get("workflow_runs", [])

    if not runs:
        dispatch(headers)
        post_slack(f"{SLACK_USER_MENTION} 🔴 *[맹그로브 고성] 강풍 감시 실행 이력 없음* — watchdog이 재실행 트리거함")
        return

    last = datetime.datetime.strptime(runs[0]["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    age_min = (datetime.datetime.now(datetime.timezone.utc) - last).total_seconds() / 60

    if age_min <= STALE_MIN:
        print(f"정상 — 마지막 실행 {age_min:.0f}분 전")
        return

    last_kst = last.astimezone(KST).strftime("%m/%d %H:%M")
    dispatch(headers)
    post_slack(
        f"{SLACK_USER_MENTION} 🔧 *[맹그로브 고성] 강풍 감시 공백 감지* — 마지막 실행 {last_kst}({age_min / 60:.1f}시간 전)\n"
        f"→ watchdog이 자동으로 재실행 트리거함"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"watchdog 점검 실패(무시): {e}")
