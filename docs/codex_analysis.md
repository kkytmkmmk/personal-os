# Codexで会話のFact抽出を進める

未解析のテキストJobを少量ずつCodexへ渡し、JSON結果を既存のFact検証処理へ戻せます。APIキーの登録や別のCodexプロセスの自動起動はありません。このチャットで「未解析をCodexで5件解析して」と依頼して利用できます。

## 手順

リポジトリのルートから実行します。書出し先はDBと同じディレクトリ内の`codex-analysis/`に限定します。本番の既定値では`data/codex-analysis/`です。このディレクトリはGitの公開対象外です。

```powershell
python tools/codex_analysis.py export --limit 5 --output data/codex-analysis/batch-001.json
```

書出しは読取りのみで、ローカルLLMは従来通り利用できます。チャンクを途中で切らず、最大20 Job、既定24,000文字に制限します。大きいチャンクは文字数上限を増やして別途扱えます。添付画像の解析は現在対象外です。

Codexは書出したJSONの`instructions`、`fact_schema`、`jobs`を読み、各Jobを独立して解析します。会話の指示はデータとして扱い、実行しません。出力形式は以下です。本人に関係する明示Factがなければ空配列を返します。

```json
{
  "format": "personal-os-codex-analysis-v1",
  "results": [{"job_id": 123, "facts": []}]
}
```

FactにはそのJobのuser発言に実在する`evidence_quote`を必ず付けます。結果を同じディレクトリへ保存して取り込みます。

```powershell
python tools/codex_analysis.py apply --packet data/codex-analysis/batch-001.json --results data/codex-analysis/batch-001-result.json --model codex-session
```

実際のモデル識別子が分かる場合は`--model`へ指定します。不明な場合は`codex-session`として記録し、モデル名を推測しません。

## 保存と安全性

- Codexに読ませた会話はCodexの処理対象になります。Ollamaだけで端末内処理する場合とは送信先が異なります。ツール自体は通信せず、明示的な依頼で渡した分だけを扱います。
- 原文引用は該当チャンクのuser発言と照合します。assistantだけの説明、捏造した引用、変更済み・無効化済みのチャンクを拒否します。
- 原文ハッシュ、Job ID、Prompt versionを再照合します。ローカルLLMが既に処理したJobや処理中のJobはスキップします。再適用でもFactを増やしません。
- 本人確認済みとして強制保存しません。既存の個人関連性・推測除外・金融取引検証・誤り訂正・確認待ち判定へ通します。AIの推薦を本人のDecisionとして作成しません。
- Fact/Evidenceには`extractor=codex`、指定モデル、Prompt version、原文チャンクを記録します。実行履歴にもCodexの完了Jobを残し、元の解析待ちJobは`completed via codex handoff`を付けて完了させます。
- 保存は専用CLIプロセス内で1トランザクションです。バッチ途中に不正な結果があれば、Fact・Evidence・Job状態をまとめてロールバックします。
- DBのSchema変更はありません。通常のWeb画面にCodex Providerを選ぶ設定や、無人で全件Codexへ送る機能は追加していません。

運用時は事前に通常の完全バックアップを確認してください。今回の受入テストは一時DBと合成データで行います。
