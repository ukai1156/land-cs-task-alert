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
    """Slack投稿用通知カード画像HTMLを生成"""
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    today_str = TODAY.strftime(f"%Y/%m/%d ({weekdays_ja[TODAY.weekday()]})")

    # メンバー行HTML生成（緊急・注意のみ）
    red_rows = ""
    yellow_rows = ""

    for member in results:
        signal = member["signal"]
        if signal == "green":
            continue  # ← 順調メンバーはスキップ

        name = member["name"]
        overdue = member["overdue"]
        today_count = member["today"]
        soon = member["soon"] + member["tomorrow"]

        row = f"""
        <div style="display:flex;align-items:center;padding:8px 12px;
                    border-left:4px solid {'#ef4444' if signal=='red' else '#eab308'};
                    background:#2a2d31;margin-bottom:6px;border-radius:4px;">
          <span style="font-size:16px;margin-right:8px;">{'🔴' if signal=='red' else '🟡'}</span>
          <span style="color:#e8e8e8;font-weight:bold;flex:1;">{name}</span>
          <span style="color:#aaa;font-size:12px;margin-right:8px;">{'危険' if signal=='red' else '注意'}</span>
          <span style="color:#ef4444;font-size:12px;margin-right:6px;">期限切れ {overdue}件</span>
          <span style="color:#f97316;font-size:12px;margin-right:6px;">今日 {today_count}件</span>
          <span style="color:#3b82f6;font-size:12px;">5日以内 {soon}件</span>
        </div>"""

        if signal == "red":
            red_rows += row
        else:
            yellow_rows += row

    # セクション組み立て（緊急・注意のみ）
    sections_html = ""
    if red_rows:
        sections_html += f"""
        <div style="margin-bottom:16px;">
          <div style="font-size:13px;color:#ef4444;font-weight:bold;margin-bottom:8px;">🚨 要対応</div>
          {red_rows}
        </div>"""
    if yellow_rows:
        sections_html += f"""
        <div style="margin-bottom:16px;">
          <div style="font-size:13px;color:#eab308;font-weight:bold;margin-bottom:8px;">🟡 注意</div>
          {yellow_rows}
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<style>
  * {{ margin:0;padding:0;box-sizing:border-box; }}
  body {{ background:#1a1d21;font-family:"Hiragino Sans","Yu Gothic",sans-serif;width:800px;padding:24px;color:#e8e8e8; }}
</style></head><body>

  <!-- ヘッダー -->
  <div style="background:linear-gradient(135deg,#1e3a5f,#0f2744);border-radius:12px;padding:20px 24px;margin-bottom:20px;border:1px solid #2a4a7f;">
    <div style="font-size:22px;font-weight:bold;color:#fff;">📋 タスク期限アラート</div>
    <div style="color:#aaa;font-size:13px;margin-top:6px;">{today_str}</div>
  </div>

  <!-- サマリーカード4枚 -->
  <div style="display:flex;gap:12px;margin-bottom:20px;">
    <div style="flex:1;padding:16px;border-radius:10px;background:#3d1f1f;text-align:center;border:1px solid #5a2a2a;">
      <div style="font-size:12px;color:#ccc;margin-bottom:6px;">🚨 期限切れ</div>
      <div style="font-size:30px;font-weight:bold;color:#ef4444;">{summary['overdue']}</div>
    </div>
    <div style="flex:1;padding:16px;border-radius:10px;background:#3d2a1f;text-align:center;border:1px solid #5a3a1a;">
      <div style="font-size:12px;color:#ccc;margin-bottom:6px;">🟠 今日〆切</div>
      <div style="font-size:30px;font-weight:bold;color:#f97316;">{summary['today']}</div>
    </div>
    <div style="flex:1;padding:16px;border-radius:10px;background:#3d361f;text-align:center;border:1px solid #5a4a1a;">
      <div style="font-size:12px;color:#ccc;margin-bottom:6px;">🟡 5日以内</div>
      <div style="font-size:30px;font-weight:bold;color:#eab308;">{summary['soon']}</div>
    </div>
    <div style="flex:1;padding:16px;border-radius:10px;background:#1f2e3d;text-align:center;border:1px solid #1a3a5a;">
      <div style="font-size:12px;color:#ccc;margin-bottom:6px;">👥 対象メンバー</div>
      <div style="font-size:30px;font-weight:bold;color:#3b82f6;">{summary['members']}</div>
    </div>
  </div>

  <!-- メンバー別アラート（緊急・注意のみ） -->
  <div style="font-size:13px;color:#888;margin-bottom:10px;">メンバー別アラート状況</div>
  {sections_html}

</body></html>"""


# ─────────────────────────────────────────
# ダッシュボードHTML生成
# ─────────────────────────────────────────

def generate_dashboard_html(members_data: list, summary: dict) -> str:
    """詳細ダッシュボードのHTMLを生成（3タブ構成）"""

    # タブ1: 今日・明日〆切チケット収集
    today_rows = ""
    tomorrow_rows = ""
    for member in members_data:
        for ticket in member["tickets"]:
            due = ticket["due_date"]
            title = ticket["title"].replace("<", "&lt;").replace(">", "&gt;")
            name  = member["name"].replace("<", "&lt;").replace(">", "&gt;")
            if due == "今日":
                today_rows += f"<tr><td>{name}</td><td>{title}</td><td style='color:#ef4444;font-weight:bold;'>今日</td></tr>"
            elif due == "明日":
                tomorrow_rows += f"<tr><td>{name}</td><td>{title}</td><td style='color:#f97316;font-weight:bold;'>明日</td></tr>"

    today_section = (
        f"<table class='ticket-table'><thead><tr><th>担当者</th><th>タスク名</th><th>〆切日</th></tr></thead><tbody>{today_rows}</tbody></table>"
        if today_rows else "<p class='empty-msg'>🎉 ありません</p>"
    )
    tomorrow_section = (
        f"<table class='ticket-table'><thead><tr><th>担当者</th><th>タスク名</th><th>〆切日</th></tr></thead><tbody>{tomorrow_rows}</tbody></table>"
        if tomorrow_rows else "<p class='empty-msg'>🎉 ありません</p>"
    )

    # タブ2: アコーディオンカード
    accordion_cards = ""
    for member in members_data:
        signal = member["signal"]
        border_color = {"red": "#ef4444", "yellow": "#eab308", "green": "#22c55e"}.get(signal, "#22c55e")
        badge_label  = {"red": "🔴 危険", "yellow": "🟡 注意", "green": "🟢 安全"}.get(signal, "🟢 安全")
        ticket_rows = "".join(
            f"<tr><td>{t['title'].replace('<','&lt;').replace('>','&gt;')}</td><td>{t['due_date']}</td></tr>"
            for t in member["tickets"]
        )
        accordion_cards += f"""
        <div class="accordion-card" data-signal="{signal}" style="border-left:4px solid {border_color};">
          <div class="accordion-header" onclick="toggleAccordion(this)">
            <span class="badge" style="background:{border_color};">{badge_label}</span>
            <span class="member-name">{member['name']}</span>
            <span class="count-badge overdue-badge">期限切れ{member['overdue']}件</span>
            <span class="count-badge today-badge">今日{member['today']}件</span>
            <span class="count-badge soon-badge">5日以内{member['soon'] + member['tomorrow']}件</span>
            <span class="count-badge ok-badge">余裕{member['ok']}件</span>
            <span class="chevron">▼</span>
          </div>
          <div class="accordion-body" style="display:none;">
            <table class="ticket-table"><thead><tr><th>チケット名</th><th>〆切</th></tr></thead>
            <tbody>{ticket_rows}</tbody></table>
          </div>
        </div>"""

    # タブ3: Chart.jsデータ
    sorted_members  = sorted(members_data, key=lambda x: x["total"], reverse=True)
    chart_labels_js = json.dumps([m["name"] for m in sorted_members], ensure_ascii=False)
    chart_overdue_js = json.dumps([m["overdue"] for m in sorted_members])
    chart_today_js   = json.dumps([m["today"]   for m in sorted_members])
    chart_soon_js    = json.dumps([m["soon"] + m["tomorrow"] for m in sorted_members])
    chart_ok_js      = json.dumps([m["ok"]     for m in sorted_members])
    chart_height     = max(300, len(sorted_members) * 40)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Land CS チーム タスクダッシュボード</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  * {{ margin:0;padding:0;box-sizing:border-box; }}
  body {{ font-family:"Hiragino Sans","Yu Gothic","Meiryo",sans-serif;background:#f1f5f9;color:#1e293b; }}
  .page-header {{ background:#1e3a8a;padding:20px 32px;display:flex;align-items:center;flex-wrap:wrap;gap:12px; }}
  .page-title {{ font-size:22px;font-weight:bold;color:#fff;flex:1; }}
  .header-badge {{ background:rgba(255,255,255,0.2);border-radius:20px;padding:4px 14px;font-size:13px;color:#fff; }}
  .tab-nav {{ display:flex;gap:0;background:#fff;border-bottom:2px solid #e2e8f0; }}
  .tab-btn {{ padding:12px 28px;border:none;background:transparent;color:#64748b;font-size:14px;cursor:pointer;font-family:inherit;border-bottom:3px solid transparent;transition:all 0.2s; }}
  .tab-btn:hover {{ color:#1e3a8a; }}
  .tab-btn.active {{ color:#1e3a8a;border-bottom-color:#1e3a8a;font-weight:bold; }}
  .tab-content {{ display:none;padding:28px 32px; }}
  .tab-content.active {{ display:block; }}
  .section-heading {{ font-size:15px;font-weight:bold;margin-bottom:12px;padding-left:10px;border-left:4px solid; }}
  .section-heading.red {{ color:#ef4444;border-color:#ef4444; }}
  .section-heading.orange {{ color:#f97316;border-color:#f97316; }}
  .ticket-table {{ width:100%;border-collapse:collapse;margin-bottom:28px;font-size:13px;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08); }}
  .ticket-table th {{ background:#f8fafc;padding:10px 14px;text-align:left;color:#64748b;font-weight:600;border-bottom:1px solid #e2e8f0; }}
  .ticket-table td {{ padding:9px 14px;border-bottom:1px solid #f1f5f9;color:#374151; }}
  .ticket-table tr:hover td {{ background:#f8fafc; }}
  .empty-msg {{ text-align:center;color:#94a3b8;padding:24px;font-size:15px;margin-bottom:24px;background:#fff;border-radius:8px; }}
  .filter-bar {{ display:flex;gap:8px;margin-bottom:18px;flex-wrap:wrap; }}
  .filter-btn {{ padding:6px 18px;border:1px solid #e2e8f0;background:#fff;color:#64748b;border-radius:20px;cursor:pointer;font-size:13px;font-family:inherit;transition:all 0.2s; }}
  .filter-btn:hover {{ background:#f1f5f9; }}
  .filter-btn.active {{ background:#1e3a8a;border-color:#1e3a8a;color:#fff;font-weight:bold; }}
  .accordion-card {{ background:#fff;border-radius:8px;margin-bottom:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08); }}
  .accordion-header {{ padding:12px 16px;cursor:pointer;display:flex;align-items:center;gap:10px;flex-wrap:wrap; }}
  .accordion-header:hover {{ background:#f8fafc; }}
  .badge {{ padding:3px 10px;border-radius:12px;font-size:12px;color:#fff;font-weight:bold; }}
  .member-name {{ font-weight:bold;font-size:15px;flex:1;color:#1e293b; }}
  .count-badge {{ padding:2px 8px;border-radius:10px;font-size:11px;font-weight:bold; }}
  .overdue-badge {{ background:#fee2e2;color:#ef4444; }}
  .today-badge {{ background:#ffedd5;color:#f97316; }}
  .soon-badge {{ background:#dbeafe;color:#2563eb; }}
  .ok-badge {{ background:#f1f5f9;color:#64748b; }}
  .chevron {{ color:#94a3b8;margin-left:auto;transition:transform 0.2s; }}
  .chevron.open {{ transform:rotate(180deg); }}
  .accordion-body {{ padding:0 16px 16px;border-top:1px solid #f1f5f9; }}
  .chart-container {{ background:#fff;border-radius:10px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,0.08); }}
  .page-footer {{ text-align:right;color:#94a3b8;font-size:11px;padding:16px 32px 24px; }}
</style>
</head>
<body>
<div class="page-header">
  <div class="page-title">🏢 Land CS チーム タスクダッシュボード</div>
  <span class="header-badge">🔴 期限切れ {summary['overdue']}件</span>
  <span class="header-badge">🟠 今日〆切 {summary['today']}件</span>
  <span class="header-badge">🟡 5日以内 {summary['soon']}件</span>
  <span class="header-badge">👥 全{summary['total_members']}名 {summary['total_tickets']}件</span>
</div>
<div class="tab-nav">
  <button class="tab-btn active" onclick="switchTab('tab1',this)">⚡ 今日・明日〆切</button>
  <button class="tab-btn" onclick="switchTab('tab2',this)">👥 メンバー別タスク</button>
  <button class="tab-btn" onclick="switchTab('tab3',this)">📊 ワークロード</button>
</div>
<div id="tab1" class="tab-content active">
  <div class="section-heading red">🔴 今日〆切</div>
  {today_section}
  <div class="section-heading orange">🟠 明日〆切</div>
  {tomorrow_section}
</div>
<div id="tab2" class="tab-content">
  <div class="filter-bar">
    <button class="filter-btn active" onclick="filterMembers('all',this)">全員</button>
    <button class="filter-btn" onclick="filterMembers('red',this)">🔴 危険</button>
    <button class="filter-btn" onclick="filterMembers('yellow',this)">🟡 注意</button>
    <button class="filter-btn" onclick="filterMembers('green',this)">🟢 順調</button>
  </div>
  <div id="accordion-list">{accordion_cards}</div>
</div>
<div id="tab3" class="tab-content">
  <div class="chart-container">
    <canvas id="workloadChart" height="{chart_height}"></canvas>
  </div>
</div>
<div class="page-footer">※ データはBacklog「BRANDエントリー」プロジェクトから取得（{summary['generated_at']}時点）。</div>
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
    document.querySelectorAll('.accordion-card').forEach(card => {{
      card.style.display = (signal === 'all' || card.dataset.signal === signal) ? 'block' : 'none';
    }});
  }}
  const ctx = document.getElementById('workloadChart').getContext('2d');
  new Chart(ctx, {{
    type: 'bar',
    data: {{
      labels: {chart_labels_js},
      datasets: [
        {{ label: '期限切れ', data: {chart_overdue_js}, backgroundColor: '#dc2626' }},
        {{ label: '今日〆切', data: {chart_today_js},   backgroundColor: '#f97316' }},
        {{ label: '5日以内',  data: {chart_soon_js},    backgroundColor: '#d97706' }},
        {{ label: '6日以上',  data: {chart_ok_js},      backgroundColor: '#16a34a' }},
      ],
    }},
    options: {{
      indexAxis: 'y',
      responsive: true,
      plugins: {{
        legend: {{ position: 'top', align: 'start', labels: {{ boxWidth: 14, color: '#374151' }} }},
      }},
      scales: {{
        x: {{ stacked: true, position: 'top', ticks: {{ color: '#64748b' }}, grid: {{ color: '#f1f5f9' }} }},
        y: {{ stacked: true, ticks: {{ color: '#374151' }}, grid: {{ display: false }} }},
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