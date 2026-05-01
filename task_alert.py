#!/usr/bin/env python3
"""
タスクアラート自動化ツール
Land CSチーム向け Backlog タスク期限アラート Slack 投稿スクリプト

実行環境: GitHub Actions (ubuntu-latest)
実行スケジュール: 毎朝 8:00 JST（月〜金）
通知形式: Playwright によるHTML→スクリーンショット → Slack Files API で画像投稿
アウトプット②: 詳細ダッシュボードHTML（dashboard.html）を生成
取得対象: 親チケットのみ
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
BACKLOG_API_KEY  = os.environ["BACKLOG_API_KEY"]
SLACK_BOT_TOKEN  = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL_ID = os.environ["SLACK_CHANNEL_ID"]
BACKLOG_SPACE    = os.environ.get("BACKLOG_SPACE", "wni")
PROJECT_KEY      = os.environ.get("BACKLOG_PROJECT_KEY", "BRAND_ENTRY")

JST           = ZoneInfo("Asia/Tokyo")
TODAY         = date.today()
ALERT_DAYS    = 5
CAUTION_COUNT = 3

# ─────────────────────────────────────────
# Backlog API
# ─────────────────────────────────────────

def backlog_get(path: str, params: dict) -> list:
    """ページネーション対応 Backlog GET リクエスト"""
    base_url = f"https://{BACKLOG_SPACE}.backlog.jp/api/v2{path}"
    all_results = []
    offset = 0
    count = 100
    while True:
        query_params = {**params, "apiKey": BACKLOG_API_KEY, "count": count, "offset": offset}
        encoded = urllib.parse.urlencode(query_params, doseq=True)
        url = f"{base_url}?{encoded}"
        with urllib.request.urlopen(url) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, list):
            return data
        all_results.extend(data)
        if len(data) < count:
            break
        offset += count
    return all_results


def get_project_id(project_key: str) -> int:
    """プロジェクトキーからプロジェクトIDを取得"""
    projects = backlog_get("/projects", {})
    for p in projects:
        if p.get("projectKey") == project_key:
            return p["id"]
    raise ValueError(f"プロジェクト '{project_key}' が見つかりません")


def fetch_issues(project_id: int) -> list:
    """未完了の親チケットのみ取得"""
    params = {
        "projectId[]": project_id,
        "statusId[]": [1, 2, 3],
        "parentChild": 2,
    }
    return backlog_get("/issues", params)

# ─────────────────────────────────────────
# 判定ロジック
# ─────────────────────────────────────────

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
# Slack Block Kit 構築
# ─────────────────────────────────────────

def build_blocks(results: list, summary: dict) -> list:
    """Slack Block Kit のブロックリストを構築"""
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    today_str = TODAY.strftime(f"%Y/%m/%d（{weekdays_ja[TODAY.weekday()]}）")
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📋 タスク期限アラート｜{today_str}", "emoji": True}},
        {"type": "divider"},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*🔴 期限切れ*\n{summary['overdue']} 件"},
            {"type": "mrkdwn", "text": f"*🟠 今日〆切*\n{summary['today']} 件"},
            {"type": "mrkdwn", "text": f"*🟡 5日以内*\n{summary['soon']} 件"},
            {"type": "mrkdwn", "text": f"*👥 対象メンバー*\n{summary['members']} 名"},
        ]},
        {"type": "divider"},
    ]
    for member in results:
        icon = {"red": "🔴", "yellow": "🟡", "green": "🟢"}.get(member["signal"], "⚪")
        parts = [f"{icon} *{member['name']}*"]
        if member["overdue"] > 0:
            parts.append(f"期限切れ: {member['overdue']}件")
        if member["today"] > 0:
            parts.append(f"今日: {member['today']}件")
        if member["tomorrow"] > 0:
            parts.append(f"明日: {member['tomorrow']}件")
        if member["soon"] > 0:
            parts.append(f"5日以内: {member['soon']}件")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "　".join(parts)}})
    blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": f"対象プロジェクト: {PROJECT_KEY}　|　取得チケット数: {summary['total_issues']} 件　|　生成日時: {summary['generated_at']}"}]})
    return blocks

# ─────────────────────────────────────────
# Slack通知HTML生成
# ─────────────────────────────────────────

def generate_slack_html(results: list, summary: dict) -> str:
    """Slack投稿用の通知カード画像HTMLを生成（WNIデザインシステム準拠）"""
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    today_str = TODAY.strftime(f"%Y/%m/%d（{weekdays_ja[TODAY.weekday()]}）")

    # ── メンバーカード生成 ──
    def member_card(member):
        signal = member["signal"]
        # WNIデザインシステム準拠カラー
        dot_color  = {"red": "#f64d00", "yellow": "#ca8a04", "green": "#16a34a"}.get(signal, "#16a34a")
        label      = {"red": "危険", "yellow": "注意", "green": "安全"}.get(signal, "安全")
        # 詳細テキスト
        if signal == "green":
            parts = []
            if member["soon"] + member["tomorrow"] > 0:
                parts.append(f"5日以内 {member['soon'] + member['tomorrow']}件")
            if member["ok"] > 0:
                parts.append(f"余裕 {member['ok']}件")
            detail = "・".join(parts) or "対象タスクなし"
        else:
            parts = []
            if member["overdue"] > 0:
                parts.append(f"期限切れ {member['overdue']}件")
            if member["today"] > 0:
                parts.append(f"今日 {member['today']}件")
            if member["soon"] + member["tomorrow"] > 0:
                parts.append(f"5日以内 {member['soon'] + member['tomorrow']}件")
            detail = "・".join(parts) or "対象タスクなし"
        return f"""<div style="background:#ffffff;border:1px solid #dfe4f0;border-left:3px solid {dot_color};border-radius:8px;padding:10px 12px;display:flex;flex-direction:column;gap:3px;">
          <div style="display:flex;align-items:center;gap:6px;">
            <span style="width:8px;height:8px;border-radius:50%;background:{dot_color};flex-shrink:0;display:inline-block;"></span>
            <span style="font-size:13px;font-weight:700;color:#101010;">{member['name']}</span>
            <span style="font-size:11px;font-weight:700;color:{dot_color};margin-left:2px;">{label}</span>
          </div>
          <div style="font-size:12px;color:#303030;padding-left:14px;">{detail}</div>
        </div>"""

    # ── セクション生成 ──
    def member_section(label, color, members):
        if not members:
            return ""
        cards = "".join(member_card(m) for m in members)
        return f"""<div style="margin-bottom:16px;">
          <div style="font-size:13px;font-weight:700;color:{color};margin-bottom:8px;padding-left:2px;">{label}（{len(members)}名）</div>
          <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;">{cards}</div>
        </div>"""

    red_members    = [m for m in results if m["signal"] == "red"]
    yellow_members = [m for m in results if m["signal"] == "yellow"]
    green_members  = [m for m in results if m["signal"] == "green"]

    sections = (
        member_section("🚨 緊急（期限切れ・今日〆切あり）", "#f64d00", red_members) +
        member_section("⚠️ 注意（5日以内3件以上）",         "#ca8a04", yellow_members) +
        member_section("✅ 順調（期限タスクあり）",           "#16a34a", green_members)
    )

    dashboard_url = "https://ukai1156.github.io/land-cs-task-alert/"

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<style>
  * {{ margin:0;padding:0;box-sizing:border-box; }}
  body {{
    font-family: Arial,"Droid Sans",Roboto,
      "Hiragino Kaku Gothic ProN","ヒラギノ角ゴ ProN",
      "Hiragino Kaku Gothic Pro","ヒラギノ角ゴ Pro",
      ヒラギノ角ゴシック,"Hiragino Sans",
      メイリオ,Meiryo,游ゴシック体,YuGothic,
      "ＭＳ Ｐゴシック",sans-serif;
    background:#f9f9f9;
    width:820px;
    padding:0;
  }}
</style></head><body>

  <!-- ヘッダー -->
  <div style="background:#0c419a;border-bottom:3px solid #3569c0;padding:14px 20px 12px;">
    <div style="font-size:18px;font-weight:700;color:#ffffff;margin-bottom:4px;">📋 タスク期限アラート</div>
    <div style="font-size:12px;color:#93c5fd;">🗂 Land CS チーム　📅 {today_str} 朝 8:00</div>
  </div>

  <!-- サマリーカード -->
  <div style="display:flex;gap:8px;padding:14px 20px;background:#ffffff;border-bottom:1px solid #dfe4f0;">
    <div style="background:#f9f9f9;border:1px solid #dfe4f0;border-left:3px solid #f64d00;border-radius:8px;padding:12px 16px;flex:1;text-align:center;">
      <div style="font-size:11px;color:#303030;margin-bottom:4px;font-weight:700;">🚨 期限切れ</div>
      <div style="font-size:28px;font-weight:700;color:#f64d00;line-height:1;">{summary['overdue']}</div>
    </div>
    <div style="background:#f9f9f9;border:1px solid #dfe4f0;border-left:3px solid #e07b00;border-radius:8px;padding:12px 16px;flex:1;text-align:center;">
      <div style="font-size:11px;color:#303030;margin-bottom:4px;font-weight:700;">🔴 今日〆切</div>
      <div style="font-size:28px;font-weight:700;color:#e07b00;line-height:1;">{summary['today']}</div>
    </div>
    <div style="background:#f9f9f9;border:1px solid #dfe4f0;border-left:3px solid #ca8a04;border-radius:8px;padding:12px 16px;flex:1;text-align:center;">
      <div style="font-size:11px;color:#303030;margin-bottom:4px;font-weight:700;">🟡 5日以内</div>
      <div style="font-size:28px;font-weight:700;color:#ca8a04;line-height:1;">{summary['soon']}</div>
    </div>
    <div style="background:#f9f9f9;border:1px solid #dfe4f0;border-left:3px solid #3569c0;border-radius:8px;padding:12px 16px;flex:1;text-align:center;">
      <div style="font-size:11px;color:#303030;margin-bottom:4px;font-weight:700;">👥 担当者数</div>
      <div style="font-size:28px;font-weight:700;color:#3569c0;line-height:1;">{summary['members']}</div>
    </div>
  </div>

  <!-- メンバーセクション -->
  <div style="padding:16px 20px;background:#f9f9f9;">
    {sections}
  </div>

  <!-- フッター -->
  <div style="background:#ffffff;border-top:1px solid #dfe4f0;padding:10px 20px;display:flex;align-items:center;justify-content:space-between;">
    <div style="font-size:12px;color:#303030;">
      📊 詳細ダッシュボード →
      <span style="color:#3569c0;text-decoration:underline;">{dashboard_url}</span>
    </div>
    <div style="font-size:11px;color:#94a3b8;">PROJECT: {PROJECT_KEY}</div>
  </div>

</body></html>"""


# ─────────────────────────────────────────
# ダッシュボードHTML生成
# ─────────────────────────────────────────

def generate_dashboard_html(members_data: list, summary: dict) -> str:
    """詳細ダッシュボードのHTMLを生成（3タブ構成・WNIデザインシステム準拠）"""

    # 日付：日本語形式
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    today_str_ja = TODAY.strftime(f"%Y/%m/%d") + f"（{weekdays_ja[TODAY.weekday()]}）"

    # ── タブ1: 今日・明日〆切チケット収集 ──
    today_rows = ""
    tomorrow_rows = ""
    today_count = 0
    tomorrow_count = 0
    for member in members_data:
        for ticket in member["tickets"]:
            due = ticket["due_date"]
            title = ticket["title"].replace("<", "&lt;").replace(">", "&gt;")
            name  = member["name"].replace("<", "&lt;").replace(">", "&gt;")
            if due == "今日":
                today_rows += f"<tr><td>{name}</td><td>{title}</td><td class='due-today'>今日</td></tr>"
                today_count += 1
            elif due == "明日":
                tomorrow_rows += f"<tr><td>{name}</td><td>{title}</td><td class='due-tomorrow'>1日後</td></tr>"
                tomorrow_count += 1

    today_section = (
        f"<table class='ticket-table today-table'><thead><tr><th>担当者</th><th>タスク名</th><th>〆切日</th></tr></thead><tbody>{today_rows}</tbody></table>"
        if today_rows else "<p class='empty-msg'>🎉 ありません</p>"
    )
    tomorrow_section = (
        f"<table class='ticket-table tomorrow-table'><thead><tr><th>担当者</th><th>タスク名</th><th>〆切日</th></tr></thead><tbody>{tomorrow_rows}</tbody></table>"
        if tomorrow_rows else "<p class='empty-msg'>🎉 ありません</p>"
    )

    # ── タブ2: アコーディオンカード ──
    accordion_cards = ""
    total_members = len(members_data)
    for i, member in enumerate(members_data):
        signal = member["signal"]
        # WNIデザインシステム準拠カラー
        border_color = {"red": "#f64d00", "yellow": "#ca8a04", "green": "#16a34a"}.get(signal, "#16a34a")
        dot_color    = {"red": "#f64d00", "yellow": "#ca8a04", "green": "#16a34a"}.get(signal, "#16a34a")
        badge_label  = {"red": "危険", "yellow": "注意", "green": "安全"}.get(signal, "安全")
        total = member["total"]

        # チケット行（期限切れは赤色表示）
        ticket_rows = ""
        for t in member["tickets"]:
            due_display = t["due_date"]
            due_class = ""
            if "超過" in due_display:
                due_class = "style='color:#f64d00;font-weight:700;'"
            elif due_display == "今日":
                due_class = "style='color:#f64d00;font-weight:700;'"
            elif due_display == "明日":
                due_class = "style='color:#e07b00;font-weight:700;'"
            ticket_rows += f"<tr><td>{t['title'].replace('<','&lt;').replace('>','&gt;')}</td><td {due_class}>{due_display}</td></tr>"

        # 件数バッジ（0件は非表示）
        badges = ""
        if member["overdue"] > 0:
            badges += f"<span class='count-badge overdue-badge'>期限切れ{member['overdue']}件</span>"
        if member["today"] > 0:
            badges += f"<span class='count-badge today-badge'>今日{member['today']}件</span>"
        if member["soon"] + member["tomorrow"] > 0:
            badges += f"<span class='count-badge soon-badge'>5日以内{member['soon'] + member['tomorrow']}件</span>"
        if member["ok"] > 0:
            badges += f"<span class='count-badge ok-badge'>余裕{member['ok']}件</span>"

        # 最初の1件はデフォルトで開く
        body_display = "block" if i == 0 else "none"
        chevron_class = "chevron open" if i == 0 else "chevron"

        accordion_cards += f"""
        <div class="accordion-card" data-signal="{signal}" style="border-left:4px solid {border_color};">
          <div class="accordion-header" onclick="toggleAccordion(this)">
            <span class="signal-dot" style="background:{dot_color};"></span>
            <span class="signal-label" style="color:{dot_color};">{badge_label}</span>
            <span class="member-name">{member['name']}</span>
            <span class="total-count">計{total}件</span>
            {badges}
            <span class="{chevron_class}">▼</span>
          </div>
          <div class="accordion-body" style="display:{body_display};">
            <table class="ticket-table inner-table"><thead><tr><th>チケット名</th><th>〆切</th></tr></thead>
            <tbody>{ticket_rows}</tbody></table>
          </div>
        </div>"""

    # ── タブ3: Chart.jsデータ ──
    sorted_members   = sorted(members_data, key=lambda x: x["total"], reverse=True)
    chart_labels_js  = json.dumps([m["name"] for m in sorted_members], ensure_ascii=False)
    chart_overdue_js = json.dumps([m["overdue"] for m in sorted_members])
    chart_today_js   = json.dumps([m["today"]   for m in sorted_members])
    chart_soon_js    = json.dumps([m["soon"] + m["tomorrow"] for m in sorted_members])
    chart_ok_js      = json.dumps([m["ok"]     for m in sorted_members])
    chart_height     = max(300, len(sorted_members) * 28)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Land CS チーム タスクダッシュボード</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  /* ── WNIデザインシステム CSS Custom Properties ── */
  :root {{
    --color-primary:    #3569c0;
    --color-dark-blue:  #0c419a;
    --color-danger:     #f64d00;
    --color-warning:    #ca8a04;
    --color-success:    #16a34a;
    --color-def:        #303030;
    --color-heading:    #101010;
    --color-link:       #3569c0;
    --color-bg-gray01:  #f9f9f9;
    --color-bg-gray02:  #eef1f8;
    --color-border:     #dfe4f0;
    --radius-base:      8px;
    --shadow-base:      0px 2px 4px 0px rgba(0,0,0,.25);
  }}

  * {{ margin:0;padding:0;box-sizing:border-box; }}
  body {{
    font-family: Arial,"Droid Sans",Roboto,
      "Hiragino Kaku Gothic ProN","ヒラギノ角ゴ ProN",
      "Hiragino Kaku Gothic Pro","ヒラギノ角ゴ Pro",
      ヒラギノ角ゴシック,"Hiragino Sans",
      メイリオ,Meiryo,游ゴシック体,YuGothic,
      "ＭＳ Ｐゴシック",sans-serif;
    background: var(--color-bg-gray01);
    color: var(--color-def);
    font-size: 15px;
  }}

  /* ── ヘッダー ── */
  .page-header {{
    background: var(--color-dark-blue);
    padding: 16px 30px 14px;
    border-bottom: 3px solid var(--color-primary);
  }}
  .page-title {{
    font-size: 22px;
    font-weight: 700;
    color: #fff;
    margin-bottom: 4px;
    line-height: 1.3;
  }}
  .page-subtitle {{
    font-size: 13px;
    color: #93c5fd;
    margin-bottom: 12px;
  }}
  .header-badges {{
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }}
  .hbadge {{
    padding: 3px 12px;
    border-radius: 20px;
    font-size: 13px;
    font-weight: 700;
    color: #fff;
  }}
  .hbadge-red    {{ background: var(--color-danger); }}
  .hbadge-orange {{ background: #e07b00; }}
  .hbadge-yellow {{ background: var(--color-warning); }}
  .hbadge-green  {{ background: var(--color-success); }}

  /* ── タブナビ ── */
  .tab-nav {{
    display: flex;
    background: #fff;
    border-bottom: 2px solid var(--color-border);
    padding: 0 30px;
  }}
  .tab-btn {{
    padding: 12px 24px;
    border: none;
    background: transparent;
    color: #64748b;
    font-size: 14px;
    font-weight: 400;
    cursor: pointer;
    font-family: inherit;
    border-bottom: 3px solid transparent;
    margin-bottom: -2px;
    transition: all 0.15s;
    display: flex;
    align-items: center;
    gap: 6px;
  }}
  .tab-btn:hover {{
    color: var(--color-primary);
    background: var(--color-bg-gray01);
  }}
  .tab-btn.active {{
    color: var(--color-primary);
    border-bottom-color: var(--color-primary);
    font-weight: 700;
    background: var(--color-bg-gray02);
  }}
  .tab-content {{ display: none; padding: 24px 30px; }}
  .tab-content.active {{ display: block; }}

  /* ── セクション見出し共通 ── */
  .section-heading {{
    font-size: 18px;
    font-weight: 700;
    color: var(--color-heading);
    margin-bottom: 16px;
    padding-left: 12px;
    border-left: 4px solid var(--color-primary);
    line-height: 1.4;
  }}

  /* ── タブ1 ── */
  .section-label {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 14px;
    border-radius: 20px;
    font-size: 13px;
    font-weight: 700;
    color: #fff;
    margin-bottom: 10px;
  }}
  .section-label-red    {{ background: var(--color-danger); }}
  .section-label-orange {{ background: #e07b00; }}
  .section-count {{ font-size: 12px; font-weight: 400; margin-left: 2px; }}

  /* ── テーブル共通 ── */
  .ticket-table {{
    width: 100%;
    border-collapse: collapse;
    margin-bottom: 24px;
    font-size: 14px;
    background: #fff;
    border-radius: var(--radius-base);
    overflow: hidden;
    box-shadow: var(--shadow-base);
    border: 1px solid var(--color-border);
  }}
  .ticket-table th {{
    padding: 10px 14px;
    text-align: left;
    color: var(--color-def);
    font-weight: 700;
    font-size: 13px;
    border-bottom: 1px solid var(--color-border);
  }}
  .today-table th    {{ background: #fff1f2; }}
  .tomorrow-table th {{ background: #fff7ed; }}
  .inner-table th    {{ background: var(--color-bg-gray02); }}
  .ticket-table td {{
    padding: 9px 14px;
    border-bottom: 1px solid var(--color-bg-gray01);
    color: var(--color-def);
    font-size: 14px;
  }}
  .ticket-table tr:last-child td {{ border-bottom: none; }}
  .ticket-table tr:hover td {{ background: var(--color-bg-gray01); }}
  .due-today    {{ color: var(--color-danger); font-weight: 700; }}
  .due-tomorrow {{ color: #e07b00; font-weight: 700; }}
  .empty-msg {{
    text-align: center;
    color: #94a3b8;
    padding: 24px;
    font-size: 15px;
    margin-bottom: 20px;
    background: #fff;
    border-radius: var(--radius-base);
    border: 1px solid var(--color-border);
  }}

  /* ── タブ2 フィルター ── */
  .filter-bar {{
    display: flex;
    gap: 8px;
    margin-bottom: 10px;
    flex-wrap: wrap;
    align-items: center;
  }}
  .filter-btn {{
    padding: 5px 16px;
    border: 1px solid var(--color-border);
    background: #fff;
    color: var(--color-def);
    border-radius: 20px;
    cursor: pointer;
    font-size: 13px;
    font-family: inherit;
    transition: all 0.15s;
    display: flex;
    align-items: center;
    gap: 5px;
  }}
  .filter-btn:hover {{ background: var(--color-bg-gray01); }}
  .filter-btn.active {{
    background: var(--color-primary);
    border-color: var(--color-primary);
    color: #fff;
    font-weight: 700;
  }}
  .filter-dot {{ width: 9px; height: 9px; border-radius: 50%; display: inline-block; }}
  .member-count-text {{ font-size: 13px; color: #64748b; margin-bottom: 10px; }}

  /* ── アコーディオン ── */
  .accordion-card {{
    background: #fff;
    border-radius: var(--radius-base);
    margin-bottom: 6px;
    overflow: hidden;
    box-shadow: var(--shadow-base);
    border: 1px solid var(--color-border);
  }}
  .accordion-header {{
    padding: 11px 16px;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }}
  .accordion-header:hover {{ background: var(--color-bg-gray01); }}
  .signal-dot {{ width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }}
  .signal-label {{ font-size: 13px; font-weight: 700; flex-shrink: 0; }}
  .member-name {{ font-weight: 700; font-size: 15px; color: var(--color-heading); flex-shrink: 0; }}
  .total-count {{ font-size: 13px; color: #64748b; flex-shrink: 0; }}
  .count-badge {{ padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 700; }}
  .overdue-badge {{ background: #fee2e2; color: var(--color-danger); }}
  .today-badge   {{ background: #ffedd5; color: #e07b00; }}
  .soon-badge    {{ background: var(--color-bg-gray02); color: var(--color-primary); }}
  .ok-badge      {{ background: var(--color-bg-gray01); color: #64748b; border: 1px solid var(--color-border); }}
  .chevron {{ color: #94a3b8; margin-left: auto; transition: transform 0.2s; flex-shrink: 0; font-size: 12px; }}
  .chevron.open {{ transform: rotate(180deg); }}
  .accordion-body {{
    padding: 0 16px 14px;
    border-top: 1px solid var(--color-border);
  }}

  /* ── タブ3 ── */
  .chart-container {{
    background: #fff;
    border-radius: var(--radius-base);
    padding: 20px;
    box-shadow: var(--shadow-base);
    border: 1px solid var(--color-border);
  }}
  .tab3-subtext {{ font-size: 13px; color: #64748b; margin-bottom: 16px; }}

  /* ── フッター ── */
  .page-footer {{
    text-align: right;
    color: #94a3b8;
    font-size: 12px;
    padding: 16px 30px 24px;
    border-top: 1px solid var(--color-border);
    background: #fff;
    margin-top: 8px;
  }}
</style>
</head>
<body>

<!-- ヘッダー -->
<div class="page-header">
  <div class="page-title">🏢 Land CS チーム タスクダッシュボード</div>
  <div class="page-subtitle">{today_str_ja} — 全{summary['total_members']}名 / {summary['total_tickets']}件のタスク</div>
  <div class="header-badges">
    <span class="hbadge hbadge-red">期限切れ {summary['overdue']}件</span>
    <span class="hbadge hbadge-orange">今日〆切 {summary['today']}件</span>
    <span class="hbadge hbadge-yellow">5日以内 {summary['soon']}件</span>
    <span class="hbadge hbadge-green">全{summary['total_members']}名・{summary['total_tickets']}件</span>
  </div>
</div>

<!-- タブナビ -->
<div class="tab-nav">
  <button class="tab-btn active" onclick="switchTab('tab1',this)">⚡ 今日・明日〆切</button>
  <button class="tab-btn" onclick="switchTab('tab2',this)">👥 メンバー別タスク</button>
  <button class="tab-btn" onclick="switchTab('tab3',this)">📊 ワークロード</button>
</div>

<!-- タブ1: 今日・明日〆切 -->
<div id="tab1" class="tab-content active">
  <div class="section-heading">⚡ 今日・明日〆切タスク</div>
  <div style="margin-bottom:8px;">
    <span class="section-label section-label-red">🔴 今日〆切<span class="section-count">{today_count}件</span></span>
  </div>
  {today_section}
  <div style="margin-bottom:8px;">
    <span class="section-label section-label-orange">🟠 明日〆切<span class="section-count">{tomorrow_count}件</span></span>
  </div>
  {tomorrow_section}
</div>

<!-- タブ2: メンバー別タスク -->
<div id="tab2" class="tab-content">
  <div class="section-heading">👥 メンバー別タスク一覧</div>
  <div class="filter-bar">
    <button class="filter-btn active" onclick="filterMembers('all',this)">全員</button>
    <button class="filter-btn" onclick="filterMembers('red',this)"><span class="filter-dot" style="background:#f64d00;"></span>緊急</button>
    <button class="filter-btn" onclick="filterMembers('yellow',this)"><span class="filter-dot" style="background:#ca8a04;"></span>注意</button>
    <button class="filter-btn" onclick="filterMembers('green',this)"><span class="filter-dot" style="background:#16a34a;"></span>順調</button>
  </div>
  <div class="member-count-text" id="member-count-text">{total_members}名を表示中（全{total_members}名）</div>
  <div id="accordion-list">{accordion_cards}</div>
</div>

<!-- タブ3: ワークロード -->
<div id="tab3" class="tab-content">
  <div class="section-heading">📊 メンバー別ワークロード（積み上げ横棒グラフ）</div>
  <div class="tab3-subtext">タスク数の多い順に並べています。</div>
  <div class="chart-container">
    <canvas id="workloadChart" height="{chart_height}"></canvas>
  </div>
</div>

<div class="page-footer">※ データはBacklog「{PROJECT_KEY}」プロジェクトから取得（{summary['generated_at']}時点）。</div>

<script>
  function switchTab(tabId, btn) {{
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.getElementById(tabId).classList.add('active');
    btn.classList.add('active');
  }}
  function toggleAccordion(header) {{
    const body = header.nextElementSibling;
    const chevron = header.querySelector('.chevron');
    const isOpen = body.style.display !== 'none';
    body.style.display = isOpen ? 'none' : 'block';
    chevron.classList.toggle('open', !isOpen);
  }}
  function filterMembers(signal, btn) {{
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    let visible = 0;
    const total = document.querySelectorAll('.accordion-card').length;
    document.querySelectorAll('.accordion-card').forEach(card => {{
      const show = (signal === 'all' || card.dataset.signal === signal);
      card.style.display = show ? 'block' : 'none';
      if (show) visible++;
    }});
    document.getElementById('member-count-text').textContent =
      visible + '名を表示中（全' + total + '名）';
  }}

  // Chart.js ワークロードグラフ
  const ctx = document.getElementById('workloadChart').getContext('2d');
  new Chart(ctx, {{
    type: 'bar',
    data: {{
      labels: {chart_labels_js},
      datasets: [
        {{ label: '5日以内',  data: {chart_soon_js},    backgroundColor: '#ca8a04' }},
        {{ label: '6日以上',  data: {chart_ok_js},      backgroundColor: '#16a34a' }},
        {{ label: '今日〆切', data: {chart_today_js},   backgroundColor: '#e07b00' }},
        {{ label: '期限切れ', data: {chart_overdue_js}, backgroundColor: '#f64d00' }},
      ],
    }},
    options: {{
      indexAxis: 'y',
      responsive: true,
      datasets: {{
        bar: {{
          barThickness: 14,
          categoryPercentage: 0.5,
        }},
      }},
      plugins: {{
        legend: {{
          position: 'top',
          align: 'start',
          labels: {{ boxWidth: 13, color: '#303030', padding: 16, font: {{ size: 13 }} }}
        }},
        tooltip: {{
          callbacks: {{
            title: (items) => items[0].label,
            label: () => null,
            afterBody: (items) => {{
              const idx = items[0].dataIndex;
              const ds  = items[0].chart.data.datasets;
              return [
                '期限切れ: ' + ds[3].data[idx] + '件',
                '今日〆切: ' + ds[2].data[idx] + '件',
                '5日以内: '  + ds[0].data[idx] + '件',
                '6日以上: '  + ds[1].data[idx] + '件',
              ];
            }},
          }},
          backgroundColor: '#fff',
          titleColor: '#101010',
          bodyColor: '#303030',
          borderColor: '#dfe4f0',
          borderWidth: 1,
          padding: 12,
          titleFont: {{ size: 13, weight: 'bold' }},
          bodyFont: {{ size: 13 }},
        }},
      }},
      scales: {{
        x: {{
          stacked: true,
          position: 'top',
          ticks: {{ color: '#64748b', font: {{ size: 12 }} }},
          grid: {{ color: '#eef1f8' }},
        }},
        y: {{
          stacked: true,
          ticks: {{ color: '#303030', font: {{ size: 12 }} }},
          grid: {{ display: false }},
        }},
      }},
    }},
  }});
</script>
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

    # 1. プロジェクトID取得
    project_id = get_project_id(PROJECT_KEY)
    print(f"プロジェクトID: {project_id}")

    # 2. チケット取得
    issues = fetch_issues(project_id)
    print(f"取得チケット数: {len(issues)} 件")

    # 3. 担当者別集計
    results = aggregate_by_assignee(issues)
    print(f"対象メンバー数: {len(results)} 名")

    # 4. サマリー集計
    summary = build_summary(results, len(issues))

    # 5. Slack通知画像生成 → スクリーンショット → Slack投稿
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
                f"👥 対象: {summary['members']}名\n"    f"📊 詳細ダッシュボード: https://ukai1156.github.io/land-cs-task-alert/")
        )
    except Exception as e:
        print(f"⚠️ Slack画像投稿エラー: {e}")

    # 6. ダッシュボードHTML生成
    dashboard_html = generate_dashboard_html(results, summary)
    with open("dashboard.html", "w", encoding="utf-8") as f:
        f.write(dashboard_html)
    print("ダッシュボードHTML生成完了: dashboard.html")

    # 7. ログ出力
    print(f"\n📝 生成日時: {summary['generated_at']}")
    print(f"期限切れ: {summary['overdue']}件 / 今日: {summary['today']}件 / 5日以内: {summary['soon']}件")
    print("✅ 処理完了")


if __name__ == "__main__":
    main()