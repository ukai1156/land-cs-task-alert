#!/usr/bin/env python3
"""
task_alert.py
Land CS チーム タスクアラート自動投稿スクリプト
- Backlog APIからタスクを取得
- Slack Block Kit形式で投稿
- 毎朝8:00 GitHub Actionsで自動実行
"""

import os
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# ─── 設定 ────────────────────────────────────────────────────────────────────

BACKLOG_API_KEY   = os.environ["BACKLOG_API_KEY"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
BACKLOG_BASE_URL  = "https://wni.backlog.jp"
PROJECT_KEY       = "BRAND_ENTRY"
DASHBOARD_URL     = "https://wxhub.wni.co.jp/api/sites/501a2f01-9abe-4574-b32a-7803ecf67855/app-a928bf65-mog22est/index.html"

# 対象ステータスID（未対応・処理中・処理済み）
TARGET_STATUS_IDS = [1, 2, 3]

# 〆切アラート対象日数
ALERT_DAYS = 5

# ─── 日付ユーティリティ ───────────────────────────────────────────────────────

JST = timezone(timedelta(hours=9))

def get_today_jst():
    return datetime.now(JST).date()

def parse_due_date(due_date_str):
    if not due_date_str:
        return None
    return datetime.fromisoformat(due_date_str[:10]).date()

def days_from_today(due_date):
    return (due_date - get_today_jst()).days

def due_label(days):
    if days < 0:
        return f"期限切れ（{abs(days)}日前）"
    if days == 0:
        return "今日〆切"
    if days == 1:
        return "明日〆切"
    return f"{days}日後〆切"

# ─── Backlog API ──────────────────────────────────────────────────────────────

def backlog_get(path, params=None):
    """Backlog APIへGETリクエスト"""
    base = f"{BACKLOG_BASE_URL}/api/v2{path}"
    p = {"apiKey": BACKLOG_API_KEY}
    if params:
        p.update(params)
    url = base + "?" + urllib.parse.urlencode(p, doseq=True)
    with urllib.request.urlopen(url, timeout=30) as res:
        return json.loads(res.read().decode())

def get_project_id():
    """プロジェクトキーからIDを取得"""
    data = backlog_get(f"/projects/{PROJECT_KEY}")
    return data["id"]

def get_members(project_id):
    """プロジェクトメンバー一覧を取得"""
    return backlog_get(f"/projects/{project_id}/users")

def get_issues(project_id, assignee_id):
    """担当者のオープンタスクを取得"""
    params = {
        "projectId[]": project_id,
        "assigneeId[]": assignee_id,
        "statusId[]": TARGET_STATUS_IDS,
        "count": 100,
    }
    return backlog_get("/issues", params)

# ─── タスク分析 ───────────────────────────────────────────────────────────────

def analyze_member_tasks(members, project_id):
    """
    各メンバーのタスクを分析し、アラート対象のみ返す
    Returns: list of dict {name, overdue, today, within5, tasks}
    """
    today = get_today_jst()
    results = []

    for member in members:
        issues = get_issues(project_id, member["id"])
        overdue_tasks = []
        today_tasks   = []
        within5_tasks = []

        for issue in issues:
            due_str = issue.get("dueDate")
            if not due_str:
                continue
            due = parse_due_date(due_str)
            days = days_from_today(due)

            task_info = {
                "name": issue["summary"],
                "days": days,
                "label": due_label(days),
                "url": f"{BACKLOG_BASE_URL}/view/{issue['issueKey']}",
            }

            if days < 0:
                overdue_tasks.append(task_info)
            elif days == 0:
                today_tasks.append(task_info)
            elif days <= ALERT_DAYS:
                within5_tasks.append(task_info)

        # アラート対象タスクがある場合のみ追加
        if overdue_tasks or today_tasks or within5_tasks:
            results.append({
                "name": member["name"],
                "overdue": overdue_tasks,
                "today": today_tasks,
                "within5": within5_tasks,
            })

    return results

# ─── シグナル判定 ─────────────────────────────────────────────────────────────

def get_signal(member_data):
    """🔴🟡🟢 シグナルを返す"""
    if member_data["overdue"] or member_data["today"]:
        return "red"
    if len(member_data["within5"]) >= 3:
        return "yellow"
    return "green"

# ─── Slack Block Kit 生成 ─────────────────────────────────────────────────────

def build_slack_blocks(alert_members, all_members):
    """Slack Block Kit ブロックを生成"""
    today_jst = datetime.now(JST)
    date_str  = today_jst.strftime("%Y/%m/%d（%a）")

    # シグナル別に分類
    red_members    = [m for m in alert_members if get_signal(m) == "red"]
    yellow_members = [m for m in alert_members if get_signal(m) == "yellow"]
    # 順調メンバー：アラート対象外のメンバー
    alert_names    = {m["name"] for m in alert_members}
    green_members  = [m for m in all_members if m["name"] not in alert_names]

    # サマリー集計
    total_overdue = sum(len(m["overdue"]) for m in alert_members)
    total_assignees = len(alert_members)

    blocks = []

    # ── ヘッダー ──
    blocks.append({
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": "⚠️ 今日・5日以内の〆切タスク確認",
            "emoji": True
        }
    })

    # ── 日付・チーム名 ──
    blocks.append({
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": f"📅 *{date_str}*"},
            {"type": "mrkdwn", "text": "🏢 *Land CS チーム*"}
        ]
    })

    # ── サマリー ──
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": f"🚨 *期限切れ：{total_overdue}件*"},
            {"type": "mrkdwn", "text": f"👥 *担当者数：{total_assignees}名*"}
        ]
    })
    blocks.append({"type": "divider"})

    # ── 🔴 緊急セクション ──
    if red_members:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"🔴 *緊急（{len(red_members)}名）*"
            }
        })
        for m in red_members:
            overdue_count = len(m["overdue"])
            within5_count = len(m["today"]) + len(m["within5"])
            detail = f"期限切れ:{overdue_count}件 / 5日以内:{within5_count}件"
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"　• *{m['name']}*　{detail}"
                }
            })

    # ── 🟡 注意セクション ──
    if yellow_members:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"🟡 *注意（{len(yellow_members)}名）*"
            }
        })
        for m in yellow_members:
            overdue_count = len(m["overdue"])
            within5_count = len(m["today"]) + len(m["within5"])
            detail = f"期限切れ:{overdue_count}件 / 5日以内:{within5_count}件"
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"　• *{m['name']}*　{detail}"
                }
            })

    # ── 🟢 順調セクション ──
    if green_members:
        green_names = "　" + "　".join([f"• {m['name']}" for m in green_members])
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"🟢 *順調（{len(green_members)}名）*\n{green_names}"
            }
        })

    blocks.append({"type": "divider"})

    # ── フッター：集計サマリー ──
    total_today   = sum(len(m["today"])   for m in alert_members)
    total_within5 = sum(len(m["within5"]) for m in alert_members)
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                f"📊 *集計サマリー*\n"
                f"期限切れ: *{total_overdue}件* ／ "
                f"今日〆切: *{total_today}件* ／ "
                f"5日以内: *{total_within5}件*"
            )
        }
    })

    # ── フッター：ダッシュボードリンク ──
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "📊 詳細ダッシュボードを見る",
                    "emoji": True
                },
                "url": DASHBOARD_URL,
                "style": "primary"
            }
        ]
    })

    return blocks

# ─── Slack 投稿 ───────────────────────────────────────────────────────────────

def post_to_slack(blocks):
    """Slack Webhookへ投稿"""
    payload = json.dumps({"blocks": blocks}).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.status, res.read().decode()

# ─── メイン ───────────────────────────────────────────────────────────────────

def main():
    print("=== Land CS タスクアラート開始 ===")
    today = get_today_jst()
    print(f"実行日時（JST）: {datetime.now(JST).strftime('%Y/%m/%d %H:%M')}")

    # プロジェクトID取得
    print(f"プロジェクト取得中: {PROJECT_KEY}")
    project_id = get_project_id()
    print(f"  → プロジェクトID: {project_id}")

    # メンバー取得
    print("メンバー一覧取得中...")
    members = get_members(project_id)
    print(f"  → {len(members)}名取得")

    # タスク分析
    print("タスク分析中...")
    alert_members = analyze_member_tasks(members, project_id)
    print(f"  → アラート対象: {len(alert_members)}名")

    # シグナル別集計
    red    = [m for m in alert_members if get_signal(m) == "red"]
    yellow = [m for m in alert_members if get_signal(m) == "yellow"]
    green  = [m for m in members if m["name"] not in {a["name"] for a in alert_members}]
    print(f"  🔴 緊急: {len(red)}名 / 🟡 注意: {len(yellow)}名 / 🟢 順調: {len(green)}名")

    # Block Kit生成
    blocks = build_slack_blocks(alert_members, members)

    # Slack投稿
    print("Slackへ投稿中...")
    status, body = post_to_slack(blocks)
    print(f"  → ステータス: {status} / レスポンス: {body}")

    if status == 200:
        print("✅ 投稿成功！")
    else:
        print("❌ 投稿失敗")
        raise Exception(f"Slack投稿エラー: {status} {body}")

if __name__ == "__main__":
    main()
