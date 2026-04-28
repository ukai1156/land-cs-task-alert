# Land CS タスクアラートシステム

毎朝8:00（JST）にBacklogのタスク状況をSlackへ自動投稿するシステムです。

## 📁 ファイル構成

```
.
├── task_alert.py                    # メインスクリプト
├── requirements.txt                 # 依存ライブラリ（標準ライブラリのみ）
├── README.md                        # このファイル
└── .github/
    └── workflows/
        └── task_alert.yml           # GitHub Actions設定
```

## ⚙️ GitHub Secrets 設定

リポジトリの Settings → Secrets and variables → Actions に以下を登録：

| Secret名           | 内容                        |
|--------------------|-----------------------------|
| BACKLOG_API_KEY    | BacklogのAPIキー             |
| SLACK_WEBHOOK_URL  | SlackのIncoming Webhook URL |

## 🚀 実行スケジュール

- **自動実行**: 毎朝8:00 JST（月〜金）
- **手動実行**: GitHub Actions → Run workflow

## 📊 ダッシュボード

詳細ダッシュボード：
https://wxhub.wni.co.jp/api/sites/501a2f01-9abe-4574-b32a-7803ecf67855/app-a928bf65-mog22est/index.html

## 👥 チームメンバーへの共有

このリポジトリをCollaboratorとして追加し、以下を共有してください：
- BACKLOG_API_KEY
- SLACK_WEBHOOK_URL
