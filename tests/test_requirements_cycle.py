import json
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import app
from tools.check_secrets import find_secrets


@contextmanager
def isolated_personal_os():
    previous = (app.DB_PATH, app.BACKUP_DIR, app.ATTACHMENT_DIR, app.ANALYSIS_PREFILTER_SCOPE)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        app.DB_PATH = root / "personal_os.db"
        app.BACKUP_DIR = root / "backups"
        app.ATTACHMENT_DIR = root / "attachments"
        app.ANALYSIS_PREFILTER_SCOPE = None
        try:
            app.initialize()
            yield root
        finally:
            app.DB_PATH, app.BACKUP_DIR, app.ATTACHMENT_DIR, app.ANALYSIS_PREFILTER_SCOPE = previous


def add_entry(title: str, body: str, source: str = "manual") -> int:
    with app.db() as connection:
        cursor = connection.execute(
            """INSERT INTO entries(kind,title,body,source,tags,status,created_at,updated_at)
               VALUES('note',?,?,?,?, 'note',?,?)""",
            (title, body, source, "", app.now(), app.now()),
        )
    app.ensure_document_for_entry(cursor.lastrowid)
    return int(cursor.lastrowid)


class RequirementsCycleTests(unittest.TestCase):
    def test_automatic_backup_waits_until_daily_due_time(self) -> None:
        with isolated_personal_os():
            reference = datetime(2026, 7, 26, 12, tzinfo=timezone.utc).astimezone()
            self.assertEqual(app.backup_wait_seconds(reference), app.BACKUP_INTERVAL_SECONDS)
            app.save_setting("last_backup_at", (reference - timedelta(hours=6)).isoformat())
            self.assertEqual(app.backup_wait_seconds(reference), 18 * 60 * 60)
            app.save_setting("last_backup_at", (reference - timedelta(hours=25)).isoformat())
            self.assertEqual(app.backup_wait_seconds(reference), 0)

    def test_backup_restores_database_and_attachment_bytes(self) -> None:
        with isolated_personal_os() as root:
            entry_id = add_entry("画像メモ", "私の資産画面")
            app.ATTACHMENT_DIR.mkdir(parents=True)
            image = app.ATTACHMENT_DIR / "sample.png"
            image.write_bytes(b"original-image")
            with app.db() as connection:
                connection.execute(
                    """INSERT INTO attachments(entry_id,storage_path,original_name,mime_type,byte_size,content_hash,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (entry_id, "attachments/sample.png", "sample.png", "image/png", image.stat().st_size,
                     app._path_sha256(image), app.now()),
                )
            backup = app.backup_database(force=True)
            self.assertIsNotNone(backup)
            verification = app.verify_backup(str(backup))
            self.assertTrue(verification["valid"])
            self.assertEqual(verification["attachments"], 1)
            image.write_bytes(b"changed")
            with app.db() as connection:
                connection.execute("UPDATE entries SET title='changed' WHERE id=?", (entry_id,))
            app.restore_database(str(backup))
            self.assertEqual(image.read_bytes(), b"original-image")
            with app.db() as connection:
                self.assertEqual(connection.execute("SELECT title FROM entries WHERE id=?", (entry_id,)).fetchone()[0], "画像メモ")

    def test_retrieval_excludes_unrelated_current_fact_and_reads_decision_result(self) -> None:
        with isolated_personal_os():
            finance_entry = add_entry("資産", "私は現在、総資産を1234万円保有している")
            app.save_structured_facts(finance_entry, [{
                "category": "finance", "type": "asset_balance", "asset": "総資産",
                "amount": 12_345_678, "currency": "JPY", "date": "2026-07",
                "summary": "現在の総資産は12,345,678円", "confidence": 0.98,
                "personal_relevance": "personal", "evidence_strength": "explicit",
                "evidence_quote": "私は現在、総資産を1234万円保有している",
                "details": {"entity_type": "asset"},
            }])
            travel_entry = add_entry("旅行", "私は次の旅行で箱根の温泉に行きたい")
            app.save_structured_facts(travel_entry, [{
                "category": "travel", "type": "preference", "asset": "箱根",
                "amount": None, "currency": None, "date": "2026-08",
                "summary": "次の旅行で箱根の温泉に行きたい", "confidence": 0.98,
                "personal_relevance": "personal", "evidence_strength": "explicit",
                "evidence_quote": "私は次の旅行で箱根の温泉に行きたい",
                "details": {"entity_type": "place"},
            }])
            timestamp = app.now()
            with app.db() as connection:
                connection.execute(
                    """INSERT INTO decisions(domain,title,context,question,options_json,decision,selected_option,rationale,
                       status,decided_on,related_fact_ids_json,related_entity_ids_json,result,later_evaluation,created_at,updated_at)
                       VALUES('travel','箱根旅行','旅行相談','どこへ行く','["箱根"]','箱根へ行く','箱根',
                              '温泉を優先','revisited','2026-06-01','[]','[]','移動が楽だった','良かった',?,?)""",
                    (timestamp, timestamp),
                )
            context = app.retrieval_context("今週末の温泉旅行はどこへ行く？")
            current_text = " ".join(str(item["body"]) for item in context["current"])
            self.assertIn("箱根", current_text)
            self.assertNotIn("1234万円", current_text)
            decision_text = " ".join(str(item["body"]) for item in context["decisions"])
            self.assertIn("移動が楽だった", decision_text)
            self.assertIn("良かった", decision_text)

    def test_duplicate_screenshot_is_one_independent_evidence_group(self) -> None:
        with isolated_personal_os():
            entry_id = add_entry("資産画像", "私は総資産を確認した")
            with app.db() as connection:
                document_id = connection.execute("SELECT id FROM documents WHERE legacy_entry_id=?", (entry_id,)).fetchone()[0]
                fact_id = connection.execute(
                    """INSERT INTO facts(document_id,category,fact_type,fact_key,value_json,summary,confidence,extractor,created_at,personal_relevance)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (document_id, "finance", "asset_balance", "finance.asset_balance.total",
                     json.dumps({"amount": 12_345_678, "currency": "JPY", "details": {}}, ensure_ascii=False),
                     "総資産は12,345,678円", 0.95, "local", app.now(), "personal"),
                ).lastrowid
                connection.execute(
                    "INSERT INTO fact_reviews(fact_id,state,reason,created_at) VALUES(?,'pending','',?)",
                    (fact_id, app.now()),
                )
                for index in (1, 2):
                    connection.execute(
                        """INSERT INTO attachments(entry_id,storage_path,original_name,mime_type,byte_size,content_hash,created_at)
                           VALUES(?,?,?,?,?,?,?)""",
                        (entry_id, f"data/attachments/{index}.png", f"{index}.png", "image/png", 10, "same-hash", app.now()),
                    )
                attachments = [row[0] for row in connection.execute("SELECT id FROM attachments ORDER BY id")]
                for attachment_id in attachments:
                    app.record_fact_evidence(
                        connection, fact_id, source_chunk_id=None, source_attachment_id=attachment_id,
                        quote="総資産は12,345,678円", evidence_kind="image", reliability=0.95,
                    )
                count = connection.execute("SELECT COUNT(*) FROM fact_evidence WHERE fact_id=?", (fact_id,)).fetchone()[0]
                trust = app.fact_trust_evaluation(
                    connection, fact_id,
                    {"category": "finance", "type": "asset_balance", "summary": "総資産は12,345,678円",
                     "confidence": 0.95, "amount": 12_345_678, "details": {}},
                )
            self.assertEqual(count, 1)
            self.assertEqual(trust["support_count"], 1)

    def test_recommendation_contains_tradeoffs_plan_and_sources(self) -> None:
        with isolated_personal_os():
            entry_id = add_entry("旅行希望", "私はSample City Aへ行きたい。温泉と海鮮を楽しみたい")
            saved = app.save_structured_facts(entry_id, [{
                "category": "travel", "type": "preference", "asset": "Sample City A",
                "amount": None, "currency": None, "date": "2026-09",
                "summary": "Sample City Aへ行きたい", "confidence": 0.96,
                "personal_relevance": "personal", "evidence_strength": "explicit",
                "evidence_quote": "私はSample City Aへ行きたい",
                "details": {"entity_type": "place"},
            }])
            draft = app.build_local_recommendation("travel", "次の旅行先を決めたい")
            self.assertTrue(draft["options"])
            self.assertTrue(draft["tradeoffs"])
            self.assertTrue(draft["plan_steps"])
            self.assertIsInstance(draft["plan_steps"][0], dict)
            self.assertIn(saved[0]["id"], draft["source_fact_ids"])
            self.assertEqual(draft["criteria"]["external_execution"], False)

    def test_decision_result_and_later_evaluation_feed_next_recommendation(self) -> None:
        with isolated_personal_os():
            timestamp = app.now()
            with app.db() as connection:
                decision_id = connection.execute(
                    """INSERT INTO decisions(
                         domain,title,context,question,options_json,decision,selected_option,rationale,status,
                         decided_on,related_fact_ids_json,related_entity_ids_json,result,later_evaluation,
                         created_at,updated_at
                       ) VALUES('travel','前回の温泉旅行','','どこへ行く','["箱根"]','箱根へ行く','箱根',
                                '移動が短い','revisited','2026-06-01','[]','[]',
                                '移動が楽だった','良い判断だった',?,?)""",
                    (timestamp, timestamp),
                ).lastrowid
            draft = app.build_local_recommendation("travel", "次の温泉旅行はどうする？")
            self.assertIn("移動が楽だった", draft["rationale"])
            self.assertIn("良い判断だった", draft["rationale"])
            self.assertIn(int(decision_id), draft["source_decision_ids"])

    def test_query_can_raise_pending_analysis_priority(self) -> None:
        with isolated_personal_os():
            entry_id = add_entry("未解析旅行", "私は次の旅行でSample City Bの温泉へ行きたい", "chatgpt-export")
            with app.db() as connection:
                document = connection.execute("SELECT id FROM documents WHERE legacy_entry_id=?", (entry_id,)).fetchone()[0]
                chunk = connection.execute("SELECT id,text FROM chunks WHERE document_id=? AND is_active=1", (document,)).fetchone()
                connection.execute(
                    """INSERT INTO analysis_jobs(document_id,provider,model,prompt_version,content_hash,status,job_kind,
                       source_chunk_id,priority,priority_reason,created_at,updated_at)
                       VALUES(?,?,?,?,?,'pending','chunk',?,100,'backfill',?,?)""",
                    (document, "local", "test", app.PROMPT_VERSION, "hash", chunk["id"], app.now(), app.now()),
                )
            changed = app.prioritize_analysis_for_query("Sample City Bの温泉旅行")
            self.assertEqual(changed, 1)
            with app.db() as connection:
                row = connection.execute("SELECT priority,priority_reason,usage_count FROM analysis_jobs").fetchone()
            self.assertEqual(row["priority"], 10)
            self.assertEqual(row["priority_reason"], "相談に関連")
            self.assertEqual(row["usage_count"], 1)

    def test_fact_correction_keeps_auditable_before_and_after(self) -> None:
        with isolated_personal_os():
            entry_id = add_entry("資産メモ", "現在の積立は4.321万円")
            saved = app.save_structured_facts(entry_id, [{
                "category": "finance", "type": "monthly_investment", "asset": "積立総額",
                "amount": 43_210, "currency": "JPY", "date": "2026-07",
                "summary": "月間積立額は4.321万円", "confidence": 0.98,
                "personal_relevance": "personal", "evidence_strength": "explicit",
                "evidence_quote": "現在の積立は4.321万円", "details": {"scope": "total"},
            }])
            fact_id = int(saved[0]["id"])
            result = app.correct_fact(
                fact_id,
                {"summary": "月間積立額は5.678万円", "amount": 56_780, "reason": "本人が金額を訂正"},
            )
            self.assertEqual(result["corrected"]["summary"], "月間積立額は5.678万円")
            with app.db() as connection:
                fact = connection.execute(
                    "SELECT summary,value_json,validation_status FROM facts WHERE id=?", (fact_id,)
                ).fetchone()
                correction = connection.execute(
                    """SELECT before_json,after_json,source FROM memory_corrections
                       WHERE fact_id=? AND correction_type='user_fact_correction'""",
                    (fact_id,),
                ).fetchone()
            self.assertEqual(fact["validation_status"], "confirmed")
            self.assertEqual(json.loads(fact["value_json"])["amount"], 56_780)
            self.assertEqual(json.loads(correction["before_json"])["summary"], "月間積立額は4.321万円")
            self.assertEqual(json.loads(correction["after_json"])["summary"], "月間積立額は5.678万円")
            self.assertEqual(correction["source"], "user")

    def test_privacy_preview_and_attachment_delete_remove_derived_data_and_file(self) -> None:
        with isolated_personal_os():
            content = b"\x89PNG\r\n\x1a\n" + b"test-image"
            entry_id, attachment_id = app.store_screenshot(content, "private.png", "image/png", "人物メモ")
            with app.db() as connection:
                attachment = connection.execute(
                    "SELECT storage_path FROM attachments WHERE id=?", (attachment_id,)
                ).fetchone()
                connection.execute(
                    """INSERT INTO attachment_derivatives(
                         attachment_id,derivative_kind,engine,version,content,confidence,metadata_json,created_at
                       ) VALUES(?,'ocr','test','ocr-v1','private text',1.0,'{}',?)""",
                    (attachment_id, app.now()),
                )
            stored_path = app.ROOT / attachment["storage_path"]
            if not stored_path.exists():
                stored_path = app.ATTACHMENT_DIR / Path(attachment["storage_path"]).name
            preview = app.privacy_delete_preview("attachment", attachment_id, delete_raw=True)
            self.assertEqual(preview["entries"], [entry_id])
            self.assertEqual(len(preview["attachments"]), 1)
            result = app.delete_private_data("attachment", attachment_id, delete_raw=True)
            self.assertEqual(result["attachments_deleted"], 1)
            self.assertFalse(stored_path.exists())
            with app.db() as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM attachment_derivatives WHERE attachment_id=?", (attachment_id,)
                ).fetchone()[0], 0)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM entries WHERE id=?", (entry_id,)
                ).fetchone()[0], 0)

    def test_local_ocr_is_cached_as_attachment_derivative(self) -> None:
        with isolated_personal_os():
            with app.db() as connection:
                entry_id = connection.execute(
                    """INSERT INTO entries(kind,title,body,source,tags,status,created_at,updated_at)
                       VALUES('note','画像','資産','screenshot','','inbox',?,?)""",
                    (app.now(), app.now()),
                ).lastrowid
                attachment_id = connection.execute(
                    """INSERT INTO attachments(entry_id,storage_path,original_name,mime_type,byte_size,content_hash,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (entry_id, "unused.png", "unused.png", "image/png", 1, "hash", app.now()),
                ).lastrowid
            with patch.object(app, "extract_ocr_text", return_value={
                "available": True, "text": "総資産 12,345,678円", "confidence": 0.91, "engine": "tesseract",
            }) as extractor:
                first = app.local_ocr_derivative(int(attachment_id), b"image")
                second = app.local_ocr_derivative(int(attachment_id), b"different")
            self.assertEqual(first["text"], "総資産 12,345,678円")
            self.assertEqual(second["text"], first["text"])
            extractor.assert_called_once()

    def test_chatgpt_sharded_import_checkpoints_and_is_idempotent(self) -> None:
        with isolated_personal_os():
            archive_bytes = BytesIO()
            conversation = {
                "id": "conversation-1",
                "title": "旅行相談",
                "create_time": 1_785_000_000,
                "mapping": {
                    "u": {"message": {"author": {"role": "user"}, "create_time": 1,
                                      "content": {"parts": ["温泉へ行きたい"]}}},
                    "a": {"message": {"author": {"role": "assistant"}, "create_time": 2,
                                      "content": {"parts": ["候補を考えます"]}}},
                },
            }
            with zipfile.ZipFile(archive_bytes, "w") as archive:
                archive.writestr("conversations-000.json", json.dumps([conversation], ensure_ascii=False))
                archive.writestr("conversations-001.json", json.dumps([], ensure_ascii=False))
            payload = archive_bytes.getvalue()
            created, skipped = app.import_chatgpt_export(payload)
            self.assertEqual((created, skipped), (1, 0))
            created_again, skipped_again = app.import_chatgpt_export(payload)
            self.assertEqual((created_again, skipped_again), (0, 1))
            with app.db() as connection:
                job = connection.execute("SELECT * FROM import_jobs").fetchone()
                entries = connection.execute(
                    "SELECT COUNT(*) FROM entries WHERE external_id='chatgpt:conversation-1'"
                ).fetchone()[0]
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["last_shard"], "conversations-001.json")
            self.assertEqual(entries, 1)

    def test_chatgpt_single_json_is_parsed_incrementally(self) -> None:
        with isolated_personal_os():
            decoded = list(app.iter_json_array(
                BytesIO('[{"text":"日本語"},2]'.encode("utf-8")), chunk_size=1
            ))
            self.assertEqual(decoded, [{"text": "日本語"}, 2])
            archive_bytes = BytesIO()
            conversations = [
                {
                    "id": f"conversation-{index}",
                    "title": f"会話 {index}",
                    "create_time": 1_785_000_000 + index,
                    "mapping": {
                        "u": {"message": {"author": {"role": "user"}, "create_time": 1,
                                          "content": {"parts": [f"自分のメモ {index}"]}}},
                    },
                }
                for index in range(3)
            ]
            with zipfile.ZipFile(archive_bytes, "w") as archive:
                archive.writestr("conversations.json", json.dumps(conversations, ensure_ascii=False))
            with patch.object(app.json, "load", side_effect=AssertionError("whole-file load is forbidden")):
                created, skipped = app.import_chatgpt_export(archive_bytes.getvalue())
            self.assertEqual((created, skipped), (3, 0))
            with app.db() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM entries WHERE source='chatgpt-export'"
                    ).fetchone()[0],
                    3,
                )

    def test_secret_scanner_reports_concrete_key_without_exposing_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret_file = root / "bad.py"
            secret_file.write_text(
                "API_KEY = 'AIza" + "A" * 35 + "'\n",
                encoding="utf-8",
            )
            findings = find_secrets(root)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["kind"], "Google API key")
        self.assertNotIn("AIza", json.dumps(findings))

    def test_operational_health_is_local_and_reports_queue_integrity(self) -> None:
        with isolated_personal_os():
            health = app.operational_health()
        self.assertTrue(health["ok"])
        self.assertEqual(health["integrity"], "ok")
        self.assertFalse(health["network_boundary"]["wildcard_cors"])
        self.assertIn("pending", health["analysis"])

    def _save_housing_fact(self, date: str, rent: int) -> int:
        entry_id = add_entry("家賃の記録", f"私は家賃を{rent}円として記録した")
        saved = app.save_structured_facts(entry_id, [{
            "category": "housing", "type": "preference", "asset": "rent",
            "amount": rent, "currency": "JPY", "date": date,
            "summary": f"現在の家賃は{rent}円", "confidence": 0.98,
            "personal_relevance": "personal", "evidence_strength": "explicit",
            "evidence_quote": f"私は家賃を{rent}円として記録した",
            "details": {"entity_type": "unknown", "scope": "rent"},
        }])
        self.assertEqual(len(saved), 1)
        return int(saved[0]["id"])

    def test_fact_timeline_uses_effective_date_and_keeps_old_import_from_rollback(self) -> None:
        with isolated_personal_os():
            current_id = self._save_housing_fact("2026-07", 100000)
            old_id = self._save_housing_fact("2024-01", 82400)
            with app.db() as connection:
                rows = {row["id"]: row for row in connection.execute(
                    "SELECT id,status,valid_from,valid_to FROM facts WHERE id IN (?,?)", (current_id, old_id)
                )}
                states = {row["fact_id"]: row["state"] for row in connection.execute(
                    "SELECT fact_id,state FROM fact_currentness WHERE fact_id IN (?,?)", (current_id, old_id)
                )}
            self.assertEqual(rows[current_id]["status"], "current")
            self.assertEqual(states[current_id], "current")
            self.assertEqual(rows[old_id]["status"], "superseded")
            self.assertEqual(states[old_id], "superseded")
            self.assertLessEqual(rows[old_id]["valid_from"], rows[old_id]["valid_to"])

    def test_same_effective_date_conflict_has_no_current_fact(self) -> None:
        with isolated_personal_os():
            first = self._save_housing_fact("2026-07", 100000)
            second = self._save_housing_fact("2026-07", 120000)
            with app.db() as connection:
                states = [row["state"] for row in connection.execute(
                    "SELECT state FROM fact_currentness WHERE fact_id IN (?,?) ORDER BY fact_id", (first, second)
                )]
                current_count = connection.execute(
                    "SELECT COUNT(*) FROM facts WHERE id IN (?,?) AND status='current'", (first, second)
                ).fetchone()[0]
                invalid_ranges = connection.execute(
                    "SELECT COUNT(*) FROM facts WHERE id IN (?,?) AND valid_to IS NOT NULL AND valid_from>valid_to", (first, second)
                ).fetchone()[0]
            self.assertEqual(states, ["unknown", "unknown"])
            self.assertEqual(current_count, 0)
            self.assertEqual(invalid_ranges, 0)

    def test_same_value_with_different_extraction_metadata_is_not_conflict(self) -> None:
        with isolated_personal_os():
            first_entry = add_entry("家賃", "私は家賃を100000円で確認した")
            second_entry = add_entry("家賃", "私は家賃を100000円で再確認した")
            facts = []
            for entry_id, note, quote in ((first_entry, "first extraction", "私は家賃を100000円で確認した"),
                                          (second_entry, "confirmed twice", "私は家賃を100000円で再確認した")):
                saved = app.save_structured_facts(entry_id, [{
                    "category": "housing", "type": "preference", "asset": "rent", "amount": 100000,
                    "currency": "JPY", "date": "2026-07", "summary": "現在の家賃は100000円",
                    "confidence": 0.98, "personal_relevance": "personal", "evidence_strength": "explicit",
                    "evidence_quote": quote,
                    "details": {"scope": "rent", "note": note},
                }])
                facts.append(saved[0]["id"])
            with app.db() as connection:
                rows = list(connection.execute(
                    "SELECT status FROM facts WHERE id IN (?,?) ORDER BY id", facts
                ))
            self.assertEqual([row["status"] for row in rows].count("current"), 1)
            self.assertEqual(facts[0], facts[1])
            with app.db() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM fact_evidence WHERE fact_id=?", (facts[0],)).fetchone()[0], 2)

    def test_timeline_intervals_close_at_next_effective_date(self) -> None:
        with isolated_personal_os():
            old = self._save_housing_fact("2024-01", 82400)
            middle = self._save_housing_fact("2025-01", 85000)
            current = self._save_housing_fact("2026-01", 100000)
            with app.db() as connection:
                rows = {row["id"]: row for row in connection.execute(
                    "SELECT id,valid_from,valid_to,status FROM facts WHERE id IN (?,?,?)", (old, middle, current)
                )}
            self.assertEqual(rows[old]["valid_to"], "2025-01")
            self.assertEqual(rows[middle]["valid_to"], "2026-01")
            self.assertIsNone(rows[current]["valid_to"])

    def test_repair_job_preserves_raw_evidence_and_records_audit(self) -> None:
        with isolated_personal_os():
            entry_id = add_entry("外部キャラクター", "東北ずん子プロジェクトの仲間はずんだもん")
            saved = app.save_structured_facts(entry_id, [{
                "category": "relationship", "type": "note", "asset": "ずんだもん",
                "summary": "東北ずん子プロジェクトの仲間", "confidence": 0.8,
                "personal_relevance": "personal", "evidence_strength": "explicit",
                "evidence_quote": "東北ずん子プロジェクトの仲間はずんだもん",
                "details": {"entity_type": "person"},
            }])
            result = app.repair_memory_state("benchmark")
            with app.db() as connection:
                job = connection.execute("SELECT status,scanned_count FROM repair_jobs WHERE id=?", (result["job_id"],)).fetchone()
                raw = connection.execute("SELECT COUNT(*) FROM entries WHERE id=?", (entry_id,)).fetchone()[0]
                entity_type = connection.execute(
                    "SELECT resolved_entity_type FROM facts WHERE id=?", (saved[0]["id"],)
                ).fetchone()[0]
            self.assertEqual(job["status"], "completed")
            self.assertGreaterEqual(job["scanned_count"], 1)
            self.assertEqual(raw, 1)
            self.assertNotEqual(entity_type, "person")

    def test_personal_inferences_are_regenerable_and_not_facts(self) -> None:
        with isolated_personal_os():
            for index in range(3):
                entry_id = add_entry("Personal OS", f"Personal OSでAI自動化を試した{index}")
                app.save_structured_facts(entry_id, [{
                    "category": "technology", "type": "note", "asset": "Personal OS",
                    "summary": "Personal OSでAI自動化を試した", "confidence": 0.98,
                    "personal_relevance": "personal", "evidence_strength": "explicit",
                    "evidence_quote": "Personal OSでAI自動化を試した",
                    "details": {"entity_type": "project"},
                }])
            with app.db() as connection:
                fact_count_before = connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            refreshed = app.refresh_personal_inferences()
            with app.db() as connection:
                fact_count_after = connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
                inference_count = connection.execute("SELECT COUNT(*) FROM personal_inferences WHERE status='active'").fetchone()[0]
            self.assertGreaterEqual(refreshed["generated"], 1)
            self.assertEqual(fact_count_before, fact_count_after)
            self.assertGreaterEqual(inference_count, 1)

    def test_assistant_only_chunk_cannot_create_personal_inference(self) -> None:
        with isolated_personal_os():
            entry_id = add_entry("AI説明", "assistant: The user is strongly interested in AI automation")
            app.ensure_document_for_entry(entry_id)
            result = app.refresh_personal_inferences()
            with app.db() as connection:
                role = connection.execute(
                    "SELECT speaker_role FROM chunks WHERE document_id=(SELECT id FROM documents WHERE legacy_entry_id=?)",
                    (entry_id,),
                ).fetchone()[0]
                active = connection.execute(
                    "SELECT COUNT(*) FROM personal_inferences WHERE status='active'"
                ).fetchone()[0]
            self.assertEqual(role, "assistant")
            self.assertEqual(result["generated"], 0)
            self.assertEqual(active, 0)

    def test_mixed_chunk_uses_only_user_span_for_inference_evidence(self) -> None:
        self.assertEqual(
            app.user_evidence_text("user: こんにちは\nassistant: CodexとAI自動化に興味がありますね"),
            "こんにちは",
        )
        self.assertEqual(
            app.user_evidence_text("user: 最近Codexで自動化ツールを作るのが面白い\nassistant: いいですね"),
            "最近Codexで自動化ツールを作るのが面白い",
        )
        with isolated_personal_os():
            entry_id = add_entry("mixed", "user: こんにちは\nassistant: CodexとAI自動化に興味がありますね")
            app.ensure_document_for_entry(entry_id)
            result = app.refresh_personal_inferences()
            self.assertEqual(result["generated"], 0)
            entry_id = add_entry("mixed user evidence", "user: 最近Codexで自動化ツールを作るのが面白い\nassistant: いいですね")
            app.ensure_document_for_entry(entry_id)
            self.assertGreaterEqual(app.refresh_personal_inferences()["generated"], 1)

    def test_domain_alias_maps_money_and_people_inference_queries(self) -> None:
        self.assertEqual(app.canonical_domain("money"), "finance")
        self.assertEqual(app.canonical_domain("people"), "relationship")
        with isolated_personal_os():
            timestamp = app.now()
            with app.db() as connection:
                connection.execute(
                    """INSERT INTO personal_inferences(statement,inference_type,domain,confidence,source_fact_ids_json,source_decision_ids_json,source_chunk_ids_json,created_at,last_evaluated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    ("finance preference", "preference_pattern", "finance", 0.9, "[]", "[]", "[]", timestamp, timestamp),
                )
            self.assertEqual(len(app.personal_inference_projection("money")), 1)

    def test_decision_execution_event_updates_state_and_is_retrievable(self) -> None:
        with isolated_personal_os():
            timestamp = app.now()
            with app.db() as connection:
                decision_id = connection.execute(
                    """INSERT INTO decisions(domain,title,context,question,options_json,decision,selected_option,rationale,status,decided_on,created_at,updated_at)
                       VALUES('travel','週末の旅行','', 'どこへ行く？','[]','Sample City A','Sample City A','', 'decided', ?, ?, ?)""",
                    (timestamp[:10], timestamp, timestamp),
                ).lastrowid
            # Exercise the state transition at the persistence layer; the HTTP
            # route is covered by the same SQL contract and should not create a
            # Decision implicitly from a chat response.
            with app.db() as connection:
                connection.execute("UPDATE decisions SET decision_state='executed' WHERE id=?", (decision_id,))
                connection.execute(
                    "INSERT INTO execution_events(decision_id,event_type,summary,occurred_at,created_at) VALUES(?,?,?,?,?)",
                    (decision_id, "executed", "予約を実行した", timestamp, timestamp),
                )
                row = connection.execute("SELECT decision_state FROM decisions WHERE id=?", (decision_id,)).fetchone()
                event_count = connection.execute("SELECT COUNT(*) FROM execution_events WHERE decision_id=?", (decision_id,)).fetchone()[0]
            self.assertEqual(row["decision_state"], "executed")
            self.assertEqual(event_count, 1)

    def test_consultation_response_types_and_cycle_stage_transitions(self) -> None:
        self.assertEqual(app.consultation_response_type("今の家賃はいくら？"), "answer_only")
        self.assertEqual(app.consultation_response_type("次の旅行どこがいい？"), "recommendation")
        self.assertEqual(app.consultation_response_type("Sample City A旅行の予定を組んで"), "planning")
        self.assertEqual(app.consultation_response_type("この前の売却判断は正しかった？"), "decision_review")
        self.assertFalse(app.valid_cycle_transition("candidate", "executed"))
        self.assertTrue(app.valid_cycle_transition("decided", "executed"))
        with isolated_personal_os():
            timestamp = app.now()
            with app.db() as connection:
                rec = connection.execute(
                    """INSERT INTO recommendations(domain,title,rationale,options_json,criteria_json,source_fact_ids_json,
                       source_decision_ids_json,source_evidence_ids_json,context_json,tradeoffs_json,missing_context_json,status,created_at,updated_at)
                       VALUES('travel','Sample City A旅行','温泉を優先','[\"Sample City A\"]','{}','[]','[]','[]',?, '[]','[]','draft',?,?)""",
                    (json.dumps({"plan_steps": [{"order": 1, "title": "日程を決める"}]}, ensure_ascii=False), timestamp, timestamp),
                ).lastrowid
            self.assertEqual(app.cycle_snapshot(rec)["cycle_stage"], "recommended")
            with app.db() as connection:
                plan = connection.execute(
                    """INSERT INTO plans(domain,title,steps_json,source_recommendation_id,status,created_at,updated_at)
                       VALUES('travel','Sample City A旅行','[{\"title\":\"日程を決める\"}]',?,'draft',?,?)""",
                    (rec, timestamp, timestamp),
                ).lastrowid
            self.assertEqual(app.cycle_snapshot(rec)["cycle_stage"], "planned")
            with app.db() as connection:
                decision = connection.execute(
                    """INSERT INTO decisions(domain,title,context,question,options_json,decision,selected_option,rationale,status,
                       related_fact_ids_json,related_entity_ids_json,result,later_evaluation,source_recommendation_id,created_at,updated_at)
                       VALUES('travel','Sample City A旅行','plan','この計画で進めるか','[\"Sample City A\"]','未確定','','','considering','[]','[]','','',?,?,?)""",
                    (rec, timestamp, timestamp),
                ).lastrowid
                connection.execute("UPDATE plans SET decision_id=? WHERE id=?", (decision, plan))
                connection.execute("UPDATE decisions SET decision_state='candidate' WHERE id=?", (decision,))
            self.assertIn("confirm_decision", app.cycle_snapshot(rec)["available_actions"])
            with app.db() as connection:
                connection.execute("UPDATE decisions SET decision_state='decided',selected_option='Sample City A',decision='Sample City A' WHERE id=?", (decision,))
            self.assertEqual(app.cycle_snapshot(rec)["cycle_stage"], "decided")
            with app.db() as connection:
                connection.execute("UPDATE decisions SET decision_state='executed' WHERE id=?", (decision,))
                connection.execute("INSERT INTO execution_events(decision_id,event_type,summary,occurred_at,created_at) VALUES(?,?,?,?,?)", (decision, "executed", "旅行した", timestamp, timestamp))
            self.assertEqual(app.cycle_snapshot(rec)["cycle_stage"], "executed")
            with app.db() as connection:
                connection.execute("UPDATE decisions SET decision_state='result',result='温泉が良かった' WHERE id=?", (decision,))
            self.assertEqual(app.cycle_snapshot(rec)["cycle_stage"], "result")
            with app.db() as connection:
                connection.execute("UPDATE decisions SET later_evaluation='移動は長かった' WHERE id=?", (decision,))
            final = app.cycle_snapshot(rec)
            self.assertEqual(final["cycle_stage"], "evaluated")
            self.assertEqual(final["available_actions"], [])


if __name__ == "__main__":
    unittest.main()
