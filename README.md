# タスクアラート自動化ツール

Land CSチーム向けのBacklogタスク期限アラートをSlackに自動投稿するツールです。

## 概要

GitHub Actionsを使用して、毎朝8:00 JST（月〜金）にBacklogのタスク期限状況を取得し、
担当者別の信号機サマリーをSlack Block Kit形式で投稿します。

## アーキテクチャ

```
GitHub Actions (Scheduler)
    ↓ 毎朝 8:00 JST（月〜金）
task_alert.py
    ↓ Backlog API
    タスク取得（親チケットのみ）
    ↓ 判定ロジック
    🔴 要対応：期限切れ1件以上
    🟡 注意  ：5日以内3件以上（🔴以外）
    🟢 順調  ：期限タスク1件以上（🔴🟡以外）
    ↓ Slack Webhook
    Block Kit形式で投稿
```

## ファイル構成

```
.
├── .github/
│   └── workflows/
│       └── task_alert.yml   # GitHub Actions ワークフロー定義
├── task_alert.py            # メインスクリプト
├── requirements.txt         # 依存ライブラリ
└── README.md                # このファイル
```

## セットアップ手順

### 1. リポジトリ作成

GitHubで新しいPrivateリポジトリを作成し、以下のファイルをアップロードします。

```
task_alert.py
requirements.txt
README.md
.github/workflows/task_alert.yml
```

### 2. GitHub Secrets の登録

リポジトリの **Settings → Secrets and variables → Actions** から以下を登録します。

| Secret名 | 内容 |
|---|---|
| `BACKLOG_API_KEY` | BacklogのAPIキー（個人設定 → API から発行） |
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook URL |

### 3. 環境変数の設定（ローカル実行時）

ローカルで動作確認する場合は `.env` ファイルを作成します（`.gitignore` に追加すること）。

```env
BACKLOG_API_KEY=your_backlog_api_key
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/xxx/yyy/zzz
BACKLOG_SPACE=wni
BACKLOG_PROJECT_KEY=BRAND_ENTRY
```

### 4. 動作確認

GitHub Actionsの **Actions タブ → Task Alert → Run workflow** から手動実行して動作を確認します。

## 通知仕様

### Slack投稿形式

現在：**Slack Block Kit（テキスト形式）**
将来：**Playwright によるHTML→スクリーンショット画像投稿**（予定）

### サマリーカード（4項目）

| 項目 | 内容 |
|---|---|
| 🚨 期限切れ | 期限切れタスク件数 |
| 📅 今日〆切 | 本日が期限のタスク件数 |
| ⏰ 5日以内 | 5日以内に期限が来るタスク件数 |
| 👥 対象メンバー | 期限タスクを持つメンバー数 |

### 判定ロジック

| ステータス | 条件 |
|---|---|
| 🔴 要対応 | 期限切れタスクが1件以上 |
| 🟡 注意 | 5日以内のタスクが3件以上（🔴以外） |
| 🟢 順調 | 期限タスクが1件以上（🔴🟡以外） |
| （除外） | 期限タスクが0件のメンバーは表示しない |

### 取得対象

- **現在**：親チケットのみ
- **将来**：子チケット含む（予定）

## 属人化対策

- チームメンバーをCollaboratorとして追加し、複数人でリポジトリを管理します。
- 将来的にはWNI組織アカウント（github.com/wni）への移行を推奨します。

## 注意事項

- GitHub Actionsの無料枠は月2,000分です。毎朝1回の実行（約1〜2分）であれば十分です。
- APIキー・Webhook URLは必ずGitHub Secretsで管理し、スクリプト本体には含めないでください。
- `.env` ファイルは `.gitignore` に追加し、リポジトリにコミットしないでください。
