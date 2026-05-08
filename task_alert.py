#!/usr/bin/env python3
"""
タスクアラート自動化ツール
Land CSチーム向け Backlog タスク期限アラート Slack 投稿スクリプト

実行環境: GitHub Actions (ubuntu-22.04)
実行スケジュール: 毎朝 8:00 JST（月〜金）
通知形式: Playwright によるHTML→スクリーンショット → Slack Files API で画像投稿
取得対象: S3に配置された issues.json（開発チームが毎朝8:00に更新）
"""

import os
import json
import urllib.request
import urllib.parse
import tempfile
import subprocess
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

# ─────────────────────────────────────────
# 設定
# ─────────────────────────────────────────
SLACK_BOT_TOKEN  = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL_ID = os.environ["SLACK_CHANNEL_ID"]
PROJECT_KEY      = os.environ.get("BACKLOG_PROJECT_KEY", "BRAND_ENTRY")
DASHBOARD_URL    = os.environ.get("DASHBOARD_URL", "")
ISSUES_JSON_URL  = os.environ.get(
    "ISSUES_JSON_URL",
    "https://d3epywq9jpw7qy.cloudfront.net/ukai/issues.json?t=" + Date.now()"
)

JST           = ZoneInfo("Asia/Tokyo")
TODAY         = date.today()
ALERT_DAYS    = 5
CAUTION_COUNT = 3

# ─────────────────────────────────────────
# S3 から issues.json を取得
# ─────────────────────────────────────────

# ─────────────────────────────────────────
# テスト用ダミーデータ（S3が403の場合のフォールバック）
# ─────────────────────────────────────────
FALLBACK_ISSUES = [
    {"summary": "【テスト】LP制作対応",        "assignee": {"name": "田中 太郎"}, "dueDate": None,         "status": {"name": "未着手"}},
    {"summary": "【テスト】バナー修正依頼",      "assignee": {"name": "田中 太郎"}, "dueDate": "2026-05-03", "status": {"name": "処理中"}},
    {"summary": "【テスト】原稿チェック",        "assignee": {"name": "鈴木 花子"}, "dueDate": "2026-05-04", "status": {"name": "未着手"}},
    {"summary": "【テスト】入稿データ確認",      "assignee": {"name": "鈴木 花子"}, "dueDate": "2026-05-07", "status": {"name": "処理中"}},
    {"summary": "【テスト】クライアント確認",    "assignee": {"name": "佐藤 次郎"}, "dueDate": "2026-04-30", "status": {"name": "処理中"}},
    {"summary": "【テスト】修正対応",            "assignee": {"name": "佐藤 次郎"}, "dueDate": "2026-05-03", "status": {"name": "未着手"}},
    {"summary": "【テスト】素材整理",            "assignee": {"name": "山田 三郎"}, "dueDate": "2026-05-08", "status": {"name": "未着手"}},
    {"summary": "【テスト】スケジュール調整",    "assignee": {"name": "山田 三郎"}, "dueDate": "2026-05-10", "status": {"name": "処理中"}},
    {"summary": "【テスト】レポート作成",        "assignee": {"name": "伊藤 四郎"}, "dueDate": "2026-05-05", "status": {"name": "未着手"}},
    {"summary": "【テスト】ミーティング準備",    "assignee": {"name": "伊藤 四郎"}, "dueDate": "2026-05-06", "status": {"name": "処理中"}},
]

def fetch_issues() -> list:
    """S3に配置された issues.json を取得して返す。
    403エラーの場合はテスト用ダミーデータを使用する。
    """
    try:
        with urllib.request.urlopen(ISSUES_JSON_URL) as resp:
            issues = json.loads(resp.read().decode("utf-8"))
        print(f"✅ issues.json 取得完了: {len(issues)} 件")
        return issues
    except Exception as e:
        error_msg = str(e)
        if "403" in error_msg or "Forbidden" in error_msg:
            print(f"⚠️ S3アクセス制限（403）のため、テスト用ダミーデータを使用します")
            print(f"   エラー詳細: {error_msg}")
            print(f"   ダミーデータ件数: {len(FALLBACK_ISSUES)} 件")
            return FALLBACK_ISSUES
        else:
            raise RuntimeError(f"issues.json の取得に失敗しました: {e}")

def classify_issue(due_date) -> str:
    """期限日から状態を分類する"""
    if due_date is None:
        return "none"
    if isinstance(due_date, str):
        try:
            due = date.fromisoformat(due_date[:10])
        except ValueError:
            return "none"
    else:
        due = due_date
    delta = (due - TODAY).days
    if delta < 0:
        return "overdue"
    elif delta == 0:
        return "today"
    elif delta == 1:
        return "tomorrow"
    elif delta <= ALERT_DAYS:
        return "soon"
    else:
        return "ok"


def determine_signal(overdue: int, soon: int, has_deadline: bool) -> str:
    """信号色を決定する"""
    if overdue >= 1:
        return "red"
    if soon >= CAUTION_COUNT:
        return "yellow"
    if has_deadline:
        return "green"
    return "none"

# ─────────────────────────────────────────
# データ集計
# ─────────────────────────────────────────

def _due_date_display(due_date_str) -> str:
    """期限日を表示用文字列に変換"""
    if not due_date_str:
        return "期限なし"
    try:
        due = date.fromisoformat(due_date_str[:10])
    except ValueError:
        return str(due_date_str)
    delta = (due - TODAY).days
    if delta < 0:
        return f"{abs(delta)}日超過"
    elif delta == 0:
        return "今日"
    elif delta == 1:
        return "明日"
    else:
        return f"{delta}日後"


def aggregate_by_assignee(issues: list) -> list:
    """担当者別にタスクを集計"""
    assignee_map: dict = {}
    for issue in issues:
        assignee = issue.get("assignee")
        name = assignee.get("name", "不明") if assignee else "未割り当て"
        if name not in assignee_map:
            assignee_map[name] = {
                "name": name,
                "overdue": 0, "today": 0, "tomorrow": 0,
                "soon": 0, "ok": 0, "none": 0, "total": 0,
                "tickets": [],
            }
        due_raw = issue.get("dueDate")
        classification = classify_issue(due_raw)
        assignee_map[name][classification] += 1
        assignee_map[name]["total"] += 1
        assignee_map[name]["tickets"].append({
            "title": issue.get("summary", ""),
            "due_date": _due_date_display(due_raw),
            "status": classification,
        })

    results = []
    for member in assignee_map.values():
        deadline_count = (
            member["overdue"] + member["today"] + member["tomorrow"]
            + member["soon"] + member["ok"]
        )
        if deadline_count == 0:
            continue
        soon_total = member["overdue"] + member["today"] + member["tomorrow"] + member["soon"]
        member["signal"] = determine_signal(member["overdue"], soon_total, deadline_count > 0)
        results.append(member)

    signal_order = {"red": 0, "yellow": 1, "green": 2, "none": 3}
    results.sort(key=lambda x: signal_order.get(x["signal"], 99))
    return results


def build_summary(results: list, total_issues: int) -> dict:
    """サマリー辞書を構築"""
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    now_jst = datetime.now(tz=JST)
    generated_at = now_jst.strftime(f"%Y/%m/%d（{weekdays_ja[now_jst.weekday()]}）")
    return {
        "overdue":       sum(r["overdue"] for r in results),
        "today":         sum(r["today"]   for r in results),
        "soon":          sum(r["soon"] + r["tomorrow"] for r in results),
        "members":       len(results),
        "total_issues":  total_issues,
        "total_members": len(results),
        "total_tickets": sum(r["total"] for r in results),
        "generated_at":  generated_at,
    }

# ─────────────────────────────────────────
# Slack通知HTML生成
# ─────────────────────────────────────────

def generate_slack_html(results: list, summary: dict) -> str:
    """Slack投稿用の通知カード画像HTMLを生成"""
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    today_str = TODAY.strftime(f"%Y/%m/%d（{weekdays_ja[TODAY.weekday()]}）")

    # メンバーカード生成
    def member_card(member):
        signal = member["signal"]
        icon = {"red": "🔴", "yellow": "🟡", "green": "🟢"}.get(signal, "⚪")
        if signal == "green":
            detail = f"5日以内{member['soon']}件"
        else:
            parts = []
            if member["overdue"] > 0:
                parts.append(f"期限切れ{member['overdue']}件")
            if member["today"] > 0:
                parts.append(f"今日{member['today']}件")
            if member["soon"] > 0:
                parts.append(f"5日以内{member['soon']}件")
            detail = "・".join(parts) or "対象タスクなし"
        return f"""
        <div style="background:#16213e;border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:4px;">
          <div style="font-size:14px;font-weight:700;color:#e2e8f0;">{icon} {member['name']}</div>
          <div style="font-size:12px;color:#a0aec0;">{detail}</div>
        </div>"""

    # セクション生成
    def member_section(emoji, label, members):
        if not members:
            return ""
        cards = "".join(member_card(m) for m in members)
        return f"""
        <div style="margin-bottom:20px;">
          <div style="font-size:15px;font-weight:700;color:#e2e8f0;margin-bottom:10px;">
            {emoji} {label}
            <span style="font-size:12px;color:#718096;font-weight:400;">（{len(members)}名）</span>
          </div>
          <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;">
            {cards}
          </div>
        </div>"""

    red_members    = [m for m in results if m["signal"] == "red"]
    yellow_members = [m for m in results if m["signal"] == "yellow"]
    green_members  = [m for m in results if m["signal"] == "green"]

    sections = (
        member_section("🔴", "緊急メンバー（期限切れまたは今日〆切あり）", red_members) +
        member_section("🟡", "注意メンバー（5日以内3件以上）", yellow_members) +
        member_section("🟢", "順調メンバー（期限タスク1件以上・緊急/注意以外）", green_members)
    )

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<style>
  * {{ margin:0;padding:0;box-sizing:border-box; }}
  body {{ background:#1a1a2e;font-family:"Noto Sans CJK JP","Hiragino Sans","Yu Gothic","Segoe UI",sans-serif;width:800px;padding:20px; }}
</style></head><body>

  <!-- ヘッダー -->
  <div style="background:#16213e;border-radius:12px;border-left:4px solid #f1c40f;padding:16px 20px;margin-bottom:20px;">
    <div style="font-size:20px;font-weight:800;color:#ffffff;margin-bottom:6px;">⚠ 今日・5日以内の〆切タスク確認</div>
    <div style="font-size:13px;color:#a0aec0;">🗂 Land CS チーム　📅 {today_str} 朝 8:00</div>
  </div>

  <!-- サマリーカード -->
  <div style="display:flex;gap:12px;margin-bottom:24px;">
    <div style="background:#16213e;border-radius:14px;border-left:4px solid #e74c3c;padding:18px 20px;flex:1;">
      <div style="font-size:22px;margin-bottom:6px;">🚨</div>
      <div style="font-size:12px;color:#a0aec0;margin-bottom:6px;">期限切れ</div>
      <div style="font-size:32px;font-weight:800;color:#e74c3c;">{summary['overdue']}</div>
    </div>
    <div style="background:#16213e;border-radius:14px;border-left:4px solid #e74c3c;padding:18px 20px;flex:1;">
      <div style="font-size:22px;margin-bottom:6px;">🔴</div>
      <div style="font-size:12px;color:#a0aec0;margin-bottom:6px;">今日〆切</div>
      <div style="font-size:32px;font-weight:800;color:#e74c3c;">{summary['today']}</div>
    </div>
    <div style="background:#16213e;border-radius:14px;border-left:4px solid #718096;padding:18px 20px;flex:1;">
      <div style="font-size:22px;margin-bottom:6px;">🟡</div>
      <div style="font-size:12px;color:#a0aec0;margin-bottom:6px;">5日以内</div>
      <div style="font-size:32px;font-weight:800;color:#e2e8f0;">{summary['soon']}</div>
    </div>
    <div style="background:#16213e;border-radius:14px;border-left:4px solid #718096;padding:18px 20px;flex:1;">
      <div style="font-size:22px;margin-bottom:6px;">👥</div>
      <div style="font-size:12px;color:#a0aec0;margin-bottom:6px;">担当者数</div>
      <div style="font-size:32px;font-weight:800;color:#e2e8f0;">{summary['members']}</div>
    </div>
  </div>

  <!-- メンバーセクション -->
  {sections}

  <!-- フッター -->
</body></html>"""

# ─────────────────────────────────────────
# Playwright スクリーンショット
# ─────────────────────────────────────────

def take_screenshot(html_content: str, output_path: str) -> None:
    """Python Playwright APIでHTMLをスクリーンショットとして保存"""
    from playwright.sync_api import sync_playwright

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", delete=False, encoding="utf-8"
    ) as f:
        f.write(html_content)
        tmp_html = f.name

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"file://{tmp_html}")
        page.set_viewport_size({"width": 860, "height": 100})
        page.wait_for_timeout(500)
        body_height = page.evaluate("document.body.scrollHeight")
        page.set_viewport_size({"width": 860, "height": body_height})
        page.screenshot(path=output_path, full_page=True)
        browser.close()

    os.unlink(tmp_html)

# ─────────────────────────────────────────
# Slack Files API 投稿
# ─────────────────────────────────────────

def post_image_to_slack(image_path: str, title: str, initial_comment: str) -> None:
    """Slack Files API v2 で画像を投稿"""
    file_size = os.path.getsize(image_path)
    filename  = os.path.basename(image_path)

    # Step1: アップロードURL取得
    params = urllib.parse.urlencode({"filename": filename, "length": file_size, "token": SLACK_BOT_TOKEN}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/files.getUploadURLExternal", data=params,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        res1 = json.loads(resp.read().decode())
    if not res1.get("ok"):
        raise RuntimeError(f"getUploadURLExternal 失敗: {res1}")
    upload_url = res1["upload_url"]
    file_id    = res1["file_id"]

    # Step2: ファイルアップロード
    with open(image_path, "rb") as f:
        file_data = f.read()
    req2 = urllib.request.Request(upload_url, data=file_data,
        headers={"Content-Type": "application/octet-stream"}, method="POST")
    with urllib.request.urlopen(req2) as resp:
        resp.read()

    # Step3: 投稿完了
    payload = json.dumps({"files": [{"id": file_id, "title": title}],
        "channel_id": SLACK_CHANNEL_ID, "initial_comment": initial_comment}).encode()
    req3 = urllib.request.Request(
        "https://slack.com/api/files.completeUploadExternal", data=payload,
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": f"Bearer {SLACK_BOT_TOKEN}"}, method="POST")
    with urllib.request.urlopen(req3) as resp:
        res3 = json.loads(resp.read().decode())
    if not res3.get("ok"):
        raise RuntimeError(f"completeUploadExternal 失敗: {res3}")
    print(f"✅ Slack投稿完了: {title}")

# ─────────────────────────────────────────
# メイン処理
# ─────────────────────────────────────────

def main() -> None:
    print("🚀 タスクアラートスクリプト開始")

    # 1. S3から issues.json を取得
    issues = fetch_issues()
    print(f"取得チケット数: {len(issues)} 件")

    # 2. 担当者別集計
    results = aggregate_by_assignee(issues)
    print(f"対象メンバー数: {len(results)} 名")

    # 3. サマリー集計
    summary = build_summary(results, len(issues))

    # 4. Slack通知画像生成 → スクリーンショット → Slack投稿
    slack_html = generate_slack_html(results, summary)
    screenshot_path = "/tmp/task_alert.png"
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    today_str = TODAY.strftime(f"%Y/%m/%d（{weekdays_ja[TODAY.weekday()]}）")
    try:
        take_screenshot(slack_html, screenshot_path)
        post_image_to_slack(
            image_path=screenshot_path,
            title=f"タスク期限アラート｜{today_str}",
            initial_comment=(
                f"📋 *タスク期限アラート｜{today_str}*\n"
                f"🔴 期限切れ: {summary['overdue']}件　"
                f"🟠 今日〆切: {summary['today']}件　"
                f"🟡 5日以内: {summary['soon']}件　"
                f"👥 対象: {summary['members']}名\n"
                f"📊 詳細ダッシュボード: {DASHBOARD_URL}")
        )
    except Exception as e:
        print(f"⚠️ Slack画像投稿エラー: {e}")

    # 5. ログ出力
    print(f"\n📝 生成日時: {summary['generated_at']}")
    print(f"期限切れ: {summary['overdue']}件 / 今日: {summary['today']}件 / 5日以内: {summary['soon']}件")
    print("✅ 処理完了")


if __name__ == "__main__":
    main()
