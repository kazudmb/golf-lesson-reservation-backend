## 目次
- [全体像](#全体像)
- [Backend 実装について](#backend-実装について)
- [ゴルフレッスン自動予約](#ゴルフレッスン自動予約)

## 全体像
Infra の README を参照してください。

## Backend 実装について

基本は **1 API = 1 Lambda**。共通処理は `common/` から import します。

### ディレクトリ構成
```
{project}/
  common/                      # 共有ユーティリティ（各 Lambda に同梱）
    time_utils.py
  <function>/                  # 1 API = 1 ディレクトリ
    main.py                    # handler(event, context)
    requirements.txt           # 追加依存がある場合だけ記載
  ...                          # 今後増える場合は同様に追加
```

## ゴルフレッスン自動予約
- 実装: `auto_reserve_lesson/`
- セットアップ: `docs/lesson-reservation-automation.md`
