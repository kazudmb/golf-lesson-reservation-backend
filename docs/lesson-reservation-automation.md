# ゴルフレッスン自動予約 Lambda

## 目的
- 1時間ごとに空き状況を確認する
- 当日 18:40 以降の空きが 1 以上あれば予約する
- 当日の予約がすでにある場合はスキップする
- 当日の枠を取る前に、当日より未来の予約はキャンセルする
- 土日はスキップする（ただし祝日は予約対象）
- Google カレンダーに予定がある日はスキップする

## 配置
- Lambda コード: `auto_reserve_lesson/main.py`
- 依存関係: `auto_reserve_lesson/requirements.txt`

## 認証情報
- 会員番号とパスワードは AWS Secrets Manager の `auto-reserve-lesson/credentials` に保存する
- シークレットの JSON 例:
  `{"LESSON_MEMBER_ID":"hoge","LESSON_PASSWORD":"hoge","GOOGLE_CALENDAR_ID":"your-calendar-id@group.calendar.google.com","GOOGLE_SERVICE_ACCOUNT_JSON":"{\"type\":\"service_account\",...}"}`
- シークレット名は `auto-reserve-lesson/credentials` 固定
- Google カレンダー連携を使う場合も同じシークレットに `GOOGLE_CALENDAR_ID` と `GOOGLE_SERVICE_ACCOUNT_JSON` を含める

### Google カレンダー連携（任意）
- Google カレンダーの予定詳細は読まず、その日の `busy` 情報だけで判定する
- `busy` のうち、当日 `17:00` 以降の時間帯に重なるものだけを予約衝突として扱う

## 固定設定
- 予約サイト URL: `https://www.spoon3.jp/reserve/index.php?_action=index&site=smart&s=380`
- 対象打席ラベル: `ジートラック打席`
- 予約対象の最小時刻: `18:40`
- 実行対象時間帯: JST `0:00-18:00`
- HTTP タイムアウト: `20` 秒

## スケジュール設定
運用は EventBridge Scheduler か EventBridge ルールで毎時実行してください。

- 推奨: `rate(1 hour)` で毎時実行
  Lambda 側で JST 0:00-18:00 以外は自動スキップします。

UTC 側で直接絞る場合は、JST 0:00-18:00 相当の cron を設定できます。
- 例: `cron(0 0-9,15-23 * * ? *)`

## 実行結果（handler レスポンス）
- `status=reserved`: 予約完了まで検出
- `status=submitted`: 予約送信は実施（完了文言は未検出）
- `status=skipped`: 条件未達でスキップ

`reason` はスキップ理由を返します（例: `already_reserved_today`, `no_available_slot`）。
