# マネーフォワード情報を更新する

資産画面は、既存の読み取り専用ローカルDBと、接続済みマネーフォワード MEプラグインから取得した更新データに対応します。日付が新しい情報を優先し、異なる取得元の合計や内訳を混ぜません。

## このチャットから更新

「マネーフォワードをPersonal OSへ更新して」と依頼します。Codexが接続済みプラグインで残高・保有明細・月次収支を取得し、専用の更新データとして反映します。必要な場合だけ直近の入出金も取得します。プラグインが返すのはMoney Forward MEに取得・保存された情報で、金融機関のリアルタイム残高を保証するものではありません。

Web画面単独からChatGPTのプラグインを自動呼出しする機能はありません。APIキーやプラグインの認証情報をPersonal OSへ保存せず、更新は明示的に取得したデータの取込として行います。

## 資産画面から更新

「資産」→「マネーフォワード連携」→「マネーフォワード情報を更新」を開きます。更新用JSONを選んで「更新データを取り込む」を押します。成功した場合だけ反映完了を表示します。エラー時は既存の表示と選択ファイルを保持して再試行できます。

残高基準日と取得日時を確認してください。月次収支は年月を明記し、古い月の情報を「今月」と表示しません。資産は負債控除前、負債は正の絶対額、純資産は資産から負債を引いた値です。保有明細の評価時点はプラグイン側の最新取得情報で、残高基準日と同一とは限りません。

## Codex向け更新手順

接続済みプラグインの各取得結果の構造化データを、次の形式へまとめます。出力はGit除外対象の`data/imports/`へ保存します。残高は任意の過去日、保有明細は取得時の最新データ、月次収支は指定月のデータです。

```json
{
  "format": "moneyforward-me-snapshot-v1",
  "fetched_at": "2026-08-01T12:00:00+09:00",
  "balances": {"currency": "JPY", "balances": []},
  "holdings": {"currency": "JPY", "holdings": []},
  "household_summaries": {"household_summaries": []}
}
```

上記は構造の説明です。実際の取込には1日分以上のカテゴリ・口座内訳付き残高が必要です。`transactions`は省略できます。指定する場合はプラグインが返す`currency`と`transactions`を格納します。件数上限超過時は期間を狭めて取得し、振替は入れません。

```powershell
python tools/sync_moneyforward.py --input data/imports/moneyforward-update.json --backup
```

既存の`mf-dashboard` DBへの書込みやPersonal OSのSchema変更はありません。更新データは`data/attachments/integrations/moneyforward/`へ原子的に保存し、通常の完全バックアップに含めます。同じデータの再取込は重複しません。古い日付の取込は新しい残高を上書きしません。金融情報をFactや実取引テーブルへ自動複製せず、推論による金額の補完もしません。

APIは認証済みの`POST /api/integrations/moneyforward/import`（JSON、8MB以内）です。`GET /api/integrations/moneyforward`で反映状態、`GET /api/domains/money`で資産画面の表示用情報を確認できます。
