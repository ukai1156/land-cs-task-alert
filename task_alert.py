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
# フィルタリング設定（運用中に変更する可能性あり）
# ─────────────────────────────────────────
FILTER_DAYS      = 14        # 対象期間：今日から前後N日（例：14 = 前後2週間）
EXCLUDE_KEYWORDS = [         # タイトルに含まれる場合に除外するキーワード
    "受注エントリー：防災気象情報の体系整理対応",
]

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
        "parentChild": 1,
    }
    return backlog_get("/issues", params)


def is_target_issue(issue: dict) -> bool:
    """チケットがフィルタリング条件を満たすか判定する"""
    # 除外キーワードチェック
    title = issue.get("summary", "")
    for keyword in EXCLUDE_KEYWORDS:
        if keyword in title:
            return False

    # 期限日チェック（前後N日以内）
    due_raw = issue.get("dueDate")
    if due_raw is None:
        return False  # 期限なしは対象外
    try:
        due = date.fromisoformat(due_raw[:10])
    except ValueError:
        return False
    date_from = TODAY - timedelta(days=FILTER_DAYS)
    date_to   = TODAY + timedelta(days=FILTER_DAYS)
    return date_from <= due <= date_to

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


def determine_signal(overdue: int, today: int, soon: int, has_deadline: bool) -> str:
    """信号色を決定する"""
    if overdue >= 1 or today >= 1:
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
        member["signal"] = determine_signal(member["overdue"], member["today"], soon_total, deadline_count > 0)
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

    dashboard_url = "https://ukai1156.github.io/land-cs-task-alert/"

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<style>
  * {{ margin:0;padding:0;box-sizing:border-box; }}
  body {{ background:#1a1a2e;font-family:"Hiragino Sans","Yu Gothic","Segoe UI",sans-serif;width:800px;padding:20px; }}
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
  <div style="border-top:1px solid #2d3748;padding-top:14px;font-size:12px;color:#718096;">
    🗂 詳細はこちら →
    <span style="color:#63b3ed;text-decoration:underline;">{dashboard_url}</span>
    <span style="margin-left:16px;color:#4a5568;">PROJECT: {PROJECT_KEY}</span>
  </div>

</body></html>"""

# ─────────────────────────────────────────
# ダッシュボードHTML生成
# ─────────────────────────────────────────

def generate_dashboard_html(members_data: list, summary: dict) -> str:
    """詳細ダッシュボードのHTMLを生成（3タブ構成）"""

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
        border_color = {"red": "#ef4444", "yellow": "#eab308", "green": "#22c55e"}.get(signal, "#22c55e")
        dot_color    = {"red": "#ef4444", "yellow": "#eab308", "green": "#22c55e"}.get(signal, "#22c55e")
        badge_label  = {"red": "緊急", "yellow": "注意", "green": "順調"}.get(signal, "順調")
        total = member["total"]

        # チケット行（期限切れは赤色表示）
        ticket_rows = ""
        for t in member["tickets"]:
            due_display = t["due_date"]
            due_class = ""
            if "超過" in due_display:
                due_class = "style='color:#ef4444;font-weight:bold;'"
            elif due_display == "今日":
                due_class = "style='color:#ef4444;font-weight:bold;'"
            elif due_display == "明日" or due_display == "1日後":
                due_class = "style='color:#f97316;font-weight:bold;'"
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
    chart_height     = max(300, len(sorted_members) * 44)

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

  /* ── ヘッダー ── */
  .page-header {{ background:#1e3a8a;padding:18px 32px 14px; }}
  .page-title {{ font-size:22px;font-weight:bold;color:#fff;margin-bottom:4px; }}
  .page-subtitle {{ font-size:13px;color:#93c5fd;margin-bottom:12px; }}
  .header-badges {{ display:flex;gap:8px;flex-wrap:wrap; }}
  .hbadge {{ padding:4px 14px;border-radius:20px;font-size:13px;font-weight:bold;color:#fff; }}
  .hbadge-red    {{ background:#ef4444; }}
  .hbadge-orange {{ background:#f97316; }}
  .hbadge-yellow {{ background:#ca8a04; }}
  .hbadge-green  {{ background:#16a34a; }}

  /* ── タブ ── */
  .tab-nav {{ display:flex;gap:0;background:#fff;border-bottom:2px solid #e2e8f0; }}
  .tab-btn {{ padding:12px 28px;border:none;background:transparent;color:#64748b;font-size:14px;cursor:pointer;font-family:inherit;border-bottom:3px solid transparent;transition:all 0.2s;display:flex;align-items:center;gap:6px; }}
  .tab-btn:hover {{ color:#1e3a8a;background:#f8fafc; }}
  .tab-btn.active {{ color:#1e3a8a;border-bottom-color:#1e3a8a;font-weight:bold;background:#eff6ff; }}
  .tab-content {{ display:none;padding:28px 32px; }}
  .tab-content.active {{ display:block; }}

  /* ── タブ1 ── */
  .tab1-heading {{ font-size:17px;font-weight:bold;color:#1e293b;margin-bottom:20px;padding-left:12px;border-left:4px solid #1e3a8a; }}
  .section-label {{ display:inline-flex;align-items:center;gap:8px;padding:5px 16px;border-radius:20px;font-size:14px;font-weight:bold;color:#fff;margin-bottom:12px; }}
  .section-label-red    {{ background:#ef4444; }}
  .section-label-orange {{ background:#f97316; }}
  .section-count {{ font-size:13px;font-weight:normal;margin-left:2px; }}

  /* ── テーブル共通 ── */
  .ticket-table {{ width:100%;border-collapse:collapse;margin-bottom:28px;font-size:13px;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08); }}
  .ticket-table th {{ padding:10px 14px;text-align:left;color:#374151;font-weight:600;border-bottom:1px solid #e2e8f0; }}
  .today-table th    {{ background:#fff1f2; }}
  .tomorrow-table th {{ background:#fff7ed; }}
  .inner-table th    {{ background:#f8fafc; }}
  .ticket-table td {{ padding:9px 14px;border-bottom:1px solid #f1f5f9;color:#374151; }}
  .ticket-table tr:last-child td {{ border-bottom:none; }}
  .ticket-table tr:hover td {{ background:#f8fafc; }}
  .due-today    {{ color:#ef4444;font-weight:bold; }}
  .due-tomorrow {{ color:#f97316;font-weight:bold; }}
  .empty-msg {{ text-align:center;color:#94a3b8;padding:24px;font-size:15px;margin-bottom:24px;background:#fff;border-radius:8px; }}

  /* ── タブ2 ── */
  .tab2-heading {{ font-size:17px;font-weight:bold;color:#1e293b;margin-bottom:16px;padding-left:12px;border-left:4px solid #1e3a8a; }}
  .filter-bar {{ display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap;align-items:center; }}
  .filter-btn {{ padding:6px 18px;border:1px solid #e2e8f0;background:#fff;color:#64748b;border-radius:20px;cursor:pointer;font-size:13px;font-family:inherit;transition:all 0.2s;display:flex;align-items:center;gap:5px; }}
  .filter-btn:hover {{ background:#f1f5f9; }}
  .filter-btn.active {{ background:#1e3a8a;border-color:#1e3a8a;color:#fff;font-weight:bold; }}
  .filter-dot {{ width:10px;height:10px;border-radius:50%;display:inline-block; }}
  .member-count-text {{ font-size:13px;color:#64748b;margin-bottom:12px; }}

  .accordion-card {{ background:#fff;border-radius:8px;margin-bottom:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08); }}
  .accordion-header {{ padding:12px 16px;cursor:pointer;display:flex;align-items:center;gap:8px;flex-wrap:wrap; }}
  .accordion-header:hover {{ background:#f8fafc; }}
  .signal-dot {{ width:10px;height:10px;border-radius:50%;flex-shrink:0; }}
  .signal-label {{ font-size:13px;font-weight:bold;flex-shrink:0; }}
  .member-name {{ font-weight:bold;font-size:15px;color:#1e293b;flex-shrink:0; }}
  .total-count {{ font-size:13px;color:#64748b;flex-shrink:0; }}
  .count-badge {{ padding:2px 8px;border-radius:10px;font-size:11px;font-weight:bold; }}
  .overdue-badge {{ background:#fee2e2;color:#ef4444; }}
  .today-badge   {{ background:#ffedd5;color:#f97316; }}
  .soon-badge    {{ background:#dbeafe;color:#2563eb; }}
  .ok-badge      {{ background:#f1f5f9;color:#64748b; }}
  .chevron {{ color:#94a3b8;margin-left:auto;transition:transform 0.2s;flex-shrink:0; }}
  .chevron.open {{ transform:rotate(180deg); }}
  .accordion-body {{ padding:0 16px 16px;border-top:1px solid #f1f5f9; }}

  /* ── タブ3 ── */
  .tab3-heading {{ font-size:17px;font-weight:bold;color:#1e293b;margin-bottom:6px;padding-left:12px;border-left:4px solid #1e3a8a; }}
  .tab3-subtext {{ font-size:13px;color:#64748b;margin-bottom:16px; }}
  /* ワークロードグラフ：縦スクロール対応 */
  .chart-wrapper {{
    background:#fff;
    border-radius:10px;
    padding:20px;
    box-shadow:0 1px 3px rgba(0,0,0,0.08);
    overflow-y: auto;
  }}
  .chart-container {{
    position: relative;
    width: 100%;
    height: {chart_height}px;
  }}

  /* ── フッター ── */
  .page-footer {{ text-align:right;color:#64748b;font-size:12px;padding:20px 32px 28px; }}
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
  <div class="tab1-heading">⚡ 今日・明日〆切タスク</div>
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
  <div class="tab2-heading">👥 メンバー別タスク一覧</div>
  <div class="filter-bar">
    <button class="filter-btn active" onclick="filterMembers('all',this)">全員</button>
    <button class="filter-btn" onclick="filterMembers('red',this)"><span class="filter-dot" style="background:#ef4444;"></span>緊急</button>
    <button class="filter-btn" onclick="filterMembers('yellow',this)"><span class="filter-dot" style="background:#eab308;"></span>注意</button>
    <button class="filter-btn" onclick="filterMembers('green',this)"><span class="filter-dot" style="background:#22c55e;"></span>順調</button>
  </div>
  <div class="member-count-text" id="member-count-text">{total_members}名を表示中（全{total_members}名）</div>
  <div id="accordion-list">{accordion_cards}</div>
</div>

<!-- タブ3: ワークロード -->
<div id="tab3" class="tab-content">
  <div class="tab3-heading">📊 メンバー別ワークロード（積み上げ横棒グラフ）</div>
  <div class="tab3-subtext">タスク数の多い順に並べています。</div>
  <div class="chart-wrapper">
    <div class="chart-container">
      <canvas id="workloadChart"></canvas>
    </div>
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
        {{ label: '期限切れ', data: {chart_overdue_js}, backgroundColor: '#dc2626' }},
        {{ label: '今日〆切', data: {chart_today_js},   backgroundColor: '#f97316' }},
        {{ label: '5日以内',  data: {chart_soon_js},    backgroundColor: '#ca8a04' }},
        {{ label: '6日以上',  data: {chart_ok_js},      backgroundColor: '#16a34a' }},
      ],
    }},
    options: {{
      indexAxis: 'y',
      responsive: true,
      barThickness: 20,
      plugins: {{
        legend: {{
          position: 'top',
          align: 'start',
          reverse: true,
          labels: {{ boxWidth: 14, color: '#374151', padding: 16 }}
        }},
        tooltip: {{
          callbacks: {{
            title: (items) => items[0].label,
            label: () => null,
            afterBody: (items) => {{
              const idx = items[0].dataIndex;
              const ds  = items[0].chart.data.datasets;
              // datasets順: 期限切れ[0], 今日〆切[1], 5日以内[2], 6日以上[3]
              return [
                '期限切れ: ' + ds[0].data[idx] + '件',
                '今日〆切: ' + ds[1].data[idx] + '件',
                '5日以内: '  + ds[2].data[idx] + '件',
                '6日以上: '  + ds[3].data[idx] + '件',
              ];
            }},
          }},
          backgroundColor: '#fff',
          titleColor: '#1e293b',
          bodyColor: '#374151',
          borderColor: '#e2e8f0',
          borderWidth: 1,
          padding: 12,
        }},
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

    # 3. フィルタリング（前後{FILTER_DAYS}日・除外キーワード）
    issues = [i for i in issues if is_target_issue(i)]
    print(f"フィルタリング後: {len(issues)} 件")

    # 4. 担当者別集計
    results = aggregate_by_assignee(issues)
    print(f"対象メンバー数: {len(results)} 名")

    # 5. サマリー集計
    summary = build_summary(results, len(issues))

    # 6. Slack通知画像生成 → スクリーンショット → Slack投稿
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
                f"📊 詳細ダッシュボード: https://ukai1156.github.io/land-cs-task-alert/")
        )
    except Exception as e:
        print(f"⚠️ Slack画像投稿エラー: {e}")

    # 7. ダッシュボードHTML生成
    dashboard_html = generate_dashboard_html(results, summary)
    with open("dashboard.html", "w", encoding="utf-8") as f:
        f.write(dashboard_html)
    print("ダッシュボードHTML生成完了: dashboard.html")

    # 8. ログ出力
    print(f"\n📝 生成日時: {summary['generated_at']}")
    print(f"期限切れ: {summary['overdue']}件 / 今日: {summary['today']}件 / 5日以内: {summary['soon']}件")
    print("✅ 処理完了")


if __name__ == "__main__":
    main()
