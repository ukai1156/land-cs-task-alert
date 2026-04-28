# Land CS タスクアラート

Backlog APIからタスク情報を取得し、毎朝8時（JST）にSlackへ自動投稿するスクリプトです。

## ファイル構成

```
.
├── task_alert.py
├── requirements.txt
├── .github/
│   └── workflows/
│       └── task_alert.yml
└── README.md
```

## セットアップ手順

### 1. リポジトリをPrivateで作成
### 2. GitHub Secretsを登録
Settings → Secrets and variables → Actions → New repository secret

| Secret名 | 内容 |
|----------|------|
| `BACKLOG_API_KEY` | BacklogのAPIキー |
| `SLACK_WEBHOOK_URL` | SlackのIncoming Webhook URL |

### 3. 動作確認（手動実行）
Actions タブ → 「Land CS タスクアラート」→「Run workflow」

### 4. 自動実行
毎朝8時（JST）に自動でSlack投稿されます。

## 引き継ぎ
- APIキー・Webhook URLはBacklog Wikiにも記録しておくこと
- チームメンバーをCollaboratorとして追加推奨（Settings → Collaborators）

## 作成者
鵜飼 啓之（ukai@wni.com）
