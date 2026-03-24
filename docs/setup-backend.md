# Backend 初期設定
- GitHub Secrets `(dev)` に保存
  - `AWS_ROLE_ARN`: 作成した IAM Role の ARN
  - `ARTIFACT_BUCKET`: `<project>-artifact-<account>`
- GitHub Variables `(dev)` に保存
  - `PRODUCT`: `<project>`
  - `AWS_REGION`: `ap-northeast-1`
- メモ
  - Actions に Lambda のファイル名を記述しないとエラーになる
