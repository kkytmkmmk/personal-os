# 検証用DB運用

## 目的

本番の `data/personal_os.db` はChatGPT履歴・原文・Fact・Decisionを含む正本です。起動時Migrationや解析Workerの検証を本番DBで行うと、全履歴の監査やLLM推論が発生し、時間・GPU・APIコストを消費します。

`tools/create_verification_db.py` はSQLite online backupで正本をコピーし、コピー側だけを次の規模へ縮小します。

- 原文エントリ: 既定8件
- Document: 選択エントリに対応するものだけ
- chunk: 1エントリあたり既定2件。FactのEvidence chunkを優先し、必要な場合はinactiveの旧Evidenceも保持
- 解析Job: 既定20件
- Fact、レビュー、訂正履歴: 選択Factに対応するものだけ
- 添付画像: 選択エントリに対応するものだけ

原本、未選択の会話、画像、legacyテーブルは本番DBに残り、検証DBは再生成できます。

## 作成と起動

```powershell
python tools/create_verification_db.py `
  --source data/personal_os.db `
  --output data/verification/personal_os_verification.db `
  --entries 12 --chunks-per-entry 3 --analysis-jobs 20

.\tools\start_verification.ps1
```

検証環境は `data/verification/personal_os_verification.db` と固定ポート `8877`、本番環境は `data/personal_os.db` と固定ポート `8788` を使用します。起動スクリプトが環境変数を設定・整理するため、DBとポートの組み合わせを取り違えません。

## 正本への反映

1. 検証DBで `PRAGMA integrity_check` が `ok` になることを確認する。
2. `tools/start_verification.ps1` で検証環境を起動し、Migration、解析Job、Fact品質監査、UI操作、テストを検証DBで実行する。
3. コード・Schema変更だけを本番へ反映し、本番DBの事前バックアップを作成する。
4. 本番起動後に件数・Job状態・バックアップ整合性を確認する。

検証DBのFactや解析結果を本番へ自動マージしません。検証で作ったデータは検証専用であり、正本のデータ消失や重複を防ぎます。

## 読取性能と品質の回帰確認

```powershell
python tools/run_memory_quality_benchmark.py
python tools/benchmark_analysis_queue.py --db data/verification/personal_os_verification.db
python tools/benchmark_retrieval.py --db data/verification/personal_os_verification.db
python tools/check_secrets.py
```

Retrieval benchmarkはOneDrive/WindowsによるSQLiteファイルの初回hydrateを測定外にするため、読取専用のwarmupを1回行います。これは常用時の検索退行を検出する開発用budgetで、起動時間のSLOではありません。本番反映前は自動テスト、`PRAGMA integrity_check`、完全`.posbackup`の検証も併せて行います。
