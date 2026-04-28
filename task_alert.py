import os
import requests
from datetime import datetime, timedelta, timezone

# ─── 設定 ───────────────────────────────────────────────────
BACKLOG_SPACE   = "wni"
BACKLOG_PROJECT = "BRAND_ENTRY"
BACKLOG_API_KEY = os.environ["BACKLOG_API_KEY"]
SLACK_WEBHOOK   = os.environ["SLACK_WEBHOOK_URL"]
DASHBOARD_URL   = "https://wxhub.wni.co.jp/artifacts/a928bf65-e09c-4644-a4ea-854d78bb0bd9"
JST = timezone(timedelta(hours=9))

# ─── Backlog API ─────────────────────────────────────────────
def get_project_id():
    url = f"https://{BACKLOG_SPACE}.backlog.jp/api/v2/projects/{BACKLOG_PROJECT}"
    res = requests.get(url, params={"apiKey": BACKLOG_API_KEY})
    res.raise_for_status()
    return res.json()["id"]

def fetch_issues():
    url = f"https://{BACKLOG_SPACE}.backlog.jp/api/v2/issues"
    params = {
        "apiKey": BACKLOG_API_KEY,
        "projectId[]": get_project_id(),
        "statusId[]": [1, 2, 3],
        "count": 100,
    }
    res = requests.get(url, params=params)
    res.raise_for_status()
    return res.json()

# ─── タスク集計 ──────────────────────────────────────────────
def classify_issues(issues):
    today    = datetime.now(JST).date()
    tomorrow = today + timedelta(days=1)
    in_5days = today + timedelta(days=5)
    members  = {}
    overdue_count = 0

    for issue in issues:
        assignee = issue.get("assignee")
        if not assignee:
            continue
        name = assignee["name"]
        due_str = issue.get("dueDate")
        if name not in members:
            members[name] = {"overdue": 0, "today": 0, "tomorrow": 0, "in5": 0, "total": 0}
        members[name]["total"] += 1
        if due_str:
            due = datetime.fromisoformat(due_str[:10]).date()
            if due < today:
                members[name]["overdue"] += 1
                overdue_count += 1
            elif due == today:
                members[name]["today"] += 1
            elif due == tomorrow:
                members[name]["tomorrow"] += 1
            elif due <= in_5days:
                members[name]["in5"] += 1

    return members, overdue_count

# ─── Slack Block Kit 生成 ────────────────────────────────────
def build_slack_blocks(members, overdue_count):
    today_str    = datetime.now(JST).strftime("%Y/%m/%d")
    total_issues = sum(d["total"] for d in members.values())
    total_overdue = sum(d["overdue"] for d in members.values())

    red, yellow, green = [], [], []
    for name, d in sorted(members.items()):
        urgent = d["overdue"] + d["today"]
        if d["overdue"] > 0 or urgent >= 3:
            red.append((name, d))
        elif d["today"] > 0 or d["tomorrow"] > 0 or d["in5"] > 0:
            yellow.append((name, d))
        else:
            green.append((name, d))

    def member_line(name, d):
        parts = []
        if d["overdue"] > 0: parts.append(f"期限切れ:{d['overdue']}件")
        if d["today"]   > 0: parts.append(f"今日:{d['today']}件")
        if d["tomorrow"]> 0: parts.append(f"明日:{d['tomorrow']}件")
        if d["in5"]     > 0: parts.append(f"5日以内:{d['in5']}件")
        detail = " / ".join(parts) if parts else "期限迫りなし"
        return f"• {name}　{detail}"

    def member_line_green(name):
        return f"• {name}"

    blocks = [
        # タイトル
        {"type": "header", "text": {"type": "plain_text", "text": "⚠️ 今日・5日以内の〆切タスク確認"}},
        # 日付・チーム（2列）
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"📅 *{today_str} 朝*"},
            {"type": "mrkdwn", "text": "👥 *Land CS チーム*"}
        ]},
        {"type": "divider"},
        # サマリー
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*サマリー*\n期限切れ：*{total_overdue}件* ／ 担当者数：*{len(members)}名*"}},
        {"type": "divider"},
    ]

    if red:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"🔴 *緊急（{len(red)}名）*\n" + "\n".join(member_line(n, d) for n, d in red)}})

    if yellow:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"🟡 *注意（{len(yellow)}名）*\n" + "\n".join(member_line(n, d) for n, d in yellow)}})

    if green:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"🟢 *順調（{len(green)}名）*\n" + "\n".join(member_line_green(n) for n, d in green)}})

    blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn",
         "text": f"📊 集計対象: {total_issues}件　｜　🔴{len(red)}名　🟡{len(yellow)}名　🟢{len(green)}名"}
    ]})
    blocks.append({"type": "actions", "elements": [
        {"type": "button",
         "text": {"type": "plain_text", "text": "📋 詳細ダッシュボードを見る"},
         "style": "primary",
         "url": DASHBOARD_URL}
    ]})

    return blocks

# ─── Slack 投稿 ──────────────────────────────────────────────
def post_to_slack(blocks):
    res = requests.post(SLACK_WEBHOOK, json={"blocks": blocks})
    res.raise_for_status()
    print("✅ Slack投稿成功")

# ─── メイン ──────────────────────────────────────────────────
def main():
    print("Backlog APIからデータ取得中...")
    issues = fetch_issues()
    print(f"取得件数: {len(issues)}件")
    members, overdue_count = classify_issues(issues)
    blocks = build_slack_blocks(members, overdue_count)
    post_to_slack(blocks)

if __name__ == "__main__":
    main()
