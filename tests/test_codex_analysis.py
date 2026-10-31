import copy
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import app
from tests.test_requirements_cycle import isolated_personal_os, add_entry
from tools import codex_analysis as bridge


class CodexAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.context = isolated_personal_os()
        self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.provider = patch.object(app, "extraction_providers", return_value=["local"])
        self.provider.start()
        self.addCleanup(self.provider.stop)

    def pending(self, text="user: 私は旅行では温泉を優先します。"):
        entry = add_entry("Synthetic travel preference", text, "ai-ingest")
        with app.db() as connection:
            chunk = connection.execute("SELECT c.* FROM chunks c JOIN documents d ON d.id=c.document_id WHERE d.legacy_entry_id=? AND c.is_active=1", (entry,)).fetchone()
            connection.execute(
                """INSERT INTO analysis_jobs(document_id,provider,model,prompt_version,content_hash,
                   status,job_kind,source_chunk_id,created_at,updated_at)
                   VALUES(?,'local',?,?,?,'pending','chunk',?,?,?)""",
                (chunk["document_id"], app.provider_model("local", "extract"), app.PROMPT_VERSION,
                 bridge.source_hash(f"{chunk['id']}\n{chunk['text']}"), chunk["id"], app.now(), app.now()),
            )
        return entry

    def answer(self, packet):
        return {"format": bridge.FORMAT, "results": [{"job_id": item["job_id"], "facts": [{
            "category": "travel", "type": "preference", "asset": "温泉", "amount": None,
            "currency": None, "date": None, "summary": "旅行では温泉を優先する",
            "confidence": 0.95, "details": {}, "evidence_quote": "私は旅行では温泉を優先します。",
            "evidence_strength": "explicit", "personal_relevance": True, "subject_scope": "self",
            "entity_type": "place",
        }]} for item in packet["jobs"]]}

    def test_export_is_read_only_and_contains_shared_prompt(self):
        self.pending()
        with patch.object(app, "extract_with_model", side_effect=AssertionError("No LLM allowed")):
            packet = bridge.export_packet()
        self.assertEqual(len(packet["jobs"]), 1)
        self.assertEqual(packet["prompt_version"], app.PROMPT_VERSION)
        self.assertIn("user:", packet["instructions"])
        with app.db() as connection:
            self.assertEqual(connection.execute("SELECT status FROM analysis_jobs").fetchone()[0], "pending")
            self.assertEqual(connection.execute("SELECT count(*) FROM facts").fetchone()[0], 0)

    def test_success_has_evidence_provenance_and_is_idempotent(self):
        self.pending()
        packet = bridge.export_packet()
        answer = self.answer(packet)
        result = bridge.apply_results(packet, answer, "codex-session")
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["facts_saved"], 1)
        with app.db() as connection:
            fact = connection.execute("SELECT * FROM facts").fetchone()
            self.assertEqual(fact["extractor"], "codex")
            self.assertEqual(fact["extractor_model"], "codex-session")
            self.assertEqual(fact["source_chunk_id"], packet["jobs"][0]["source_chunk_id"])
            self.assertGreater(connection.execute("SELECT count(*) FROM fact_evidence WHERE fact_id=?", (fact["id"],)).fetchone()[0], 0)
            quote = connection.execute("SELECT quote FROM fact_evidence WHERE fact_id=?", (fact["id"],)).fetchone()[0]
            self.assertEqual(quote, answer["results"][0]["facts"][0]["evidence_quote"])
            self.assertEqual(connection.execute("SELECT status FROM analysis_jobs WHERE provider='codex'").fetchone()[0], "completed")
        again = bridge.apply_results(packet, answer, "codex-session")
        self.assertEqual(again["skipped"], 1)
        self.assertEqual(again["facts_saved"], 0)
        self.assertEqual(app.analysis_job_summary()["pending"], 0)

    def test_running_or_completed_job_is_not_stolen(self):
        for state in ("running", "completed"):
            with self.subTest(state=state):
                self.pending()
                packet = bridge.export_packet(limit=1)
                with app.db() as connection:
                    connection.execute("UPDATE analysis_jobs SET status=? WHERE id=?", (state, packet["jobs"][0]["job_id"]))
                result = bridge.apply_results(packet, self.answer(packet), "codex-session")
                self.assertEqual(result["skipped"], 1)
                self.assertEqual(result["facts_saved"], 0)

    def test_changed_source_or_inactive_chunk_rejected(self):
        self.pending()
        packet = bridge.export_packet()
        chunk_id = packet["jobs"][0]["source_chunk_id"]
        for sql in ("UPDATE chunks SET text='changed' WHERE id=?", "UPDATE chunks SET is_active=0 WHERE id=?"):
            with app.db() as connection:
                connection.execute(sql, (chunk_id,))
            with self.assertRaises(ValueError):
                bridge.apply_results(packet, self.answer(packet), "codex-session")

    def test_fabricated_or_assistant_only_quote_rejected(self):
        self.pending("user: 今日は散歩しました。\nassistant: 私は旅行では温泉を優先します。")
        packet = bridge.export_packet()
        for quote in ("存在しない発言", "私は旅行では温泉を優先します。"):
            answer = self.answer(packet)
            answer["results"][0]["facts"][0]["evidence_quote"] = quote
            with self.assertRaises(ValueError):
                bridge.apply_results(packet, answer, "codex-session")

    def test_complete_batch_rolls_back_if_later_result_invalid(self):
        self.pending()
        self.pending()
        packet = bridge.export_packet()
        answer = self.answer(packet)
        answer["results"][-1]["facts"][0]["evidence_quote"] = "fabricated"
        original_db = app.db
        with self.assertRaises(ValueError):
            bridge.apply_results(packet, answer, "codex-session")
        self.assertIs(app.db, original_db)
        with app.db() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM facts").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM analysis_jobs WHERE status='pending'").fetchone()[0], 2)

    def test_empty_fact_output_completes_without_inventing_memory(self):
        self.pending()
        packet = bridge.export_packet()
        answer = self.answer(packet)
        answer["results"][0]["facts"] = []
        result = bridge.apply_results(packet, answer, "codex-session")
        self.assertEqual((result["completed"], result["facts_saved"]), (1, 0))

    def test_inference_is_excluded(self):
        self.pending()
        packet = bridge.export_packet()
        answer = self.answer(packet)
        answer["results"][0]["facts"][0]["evidence_strength"] = "inferred"
        result = bridge.apply_results(packet, answer, "codex-session")
        self.assertEqual((result["excluded"], result["facts_saved"]), (1, 0))

    def test_stale_prompt_duplicate_missing_and_foreign_ids_rejected(self):
        self.pending()
        packet = bridge.export_packet()
        original = self.answer(packet)
        for items in ([], original["results"] * 2, [{"job_id": -1, "facts": []}]):
            with self.assertRaises(ValueError):
                bridge.apply_results(packet, {"format": bridge.FORMAT, "results": items}, "codex-session")
        stale = copy.deepcopy(packet)
        stale["prompt_version"] = "obsolete"
        with self.assertRaises(ValueError):
            bridge.apply_results(stale, original, "codex-session")

    def test_invalid_numbers_rejected(self):
        self.pending()
        packet = bridge.export_packet()
        for value in (True, float("nan"), float("inf"), "0.9", -0.1, 1.1):
            answer = self.answer(packet)
            answer["results"][0]["facts"][0]["confidence"] = value
            with self.assertRaises(ValueError):
                bridge.apply_results(packet, answer, "codex-session")

    def test_private_path_and_packet_budget(self):
        self.pending("user: " + "Synthetic long source " * 100)
        self.assertEqual(bridge.export_packet(max_chars=1000)["jobs"], [])
        for value in ("packet.json", "docs/packet.json", str(app.DB_PATH.parent / "codex-analysis" / ".." / "escape.json")):
            with self.assertRaises(ValueError):
                bridge.private_path(value)
        accepted = app.DB_PATH.parent / "codex-analysis" / "packet.json"
        self.assertEqual(bridge.private_path(str(accepted)), accepted.resolve())

    def test_cli_export_apply_persists_in_separate_verification_processes(self):
        app.save_setting("extract_provider", "local")
        app.save_setting("local_llm_base_url", "http://127.0.0.1:11434/v1")
        self.pending()
        directory = app.DB_PATH.parent / "codex-analysis"
        directory.mkdir()
        packet_path = directory / "packet.json"
        result_path = directory / "result.json"
        environment = dict(os.environ, PERSONAL_OS_ENV="verification", PERSONAL_OS_DB_PATH=str(app.DB_PATH))
        command = [sys.executable, str(app.ROOT / "tools" / "codex_analysis.py")]
        exported = subprocess.run(command + ["export", "--output", str(packet_path)],
                                  env=environment, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(exported.stdout)["exported"], 1)
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        result_path.write_text(json.dumps(self.answer(packet)), encoding="utf-8")
        applied = subprocess.run(command + ["apply", "--packet", str(packet_path),
                                 "--results", str(result_path), "--model", "codex-session"],
                                 env=environment, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(applied.stdout)["completed"], 1)
        with app.db() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM facts WHERE extractor='codex'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
