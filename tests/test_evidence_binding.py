import json
import unittest
from unittest.mock import patch

import app
from personal_os.evidence_quotes import bind_quote
from tests.test_requirements_cycle import isolated_personal_os, add_entry


def personal_fact():
    return {"category": "travel", "type": "preference", "asset": "Sample温泉", "amount": None,
            "currency": None, "date": None, "summary": "Sample温泉が好き", "confidence": 0.95,
            "evidence_quote": "私はSample温泉が好きです。", "evidence_strength": "explicit",
            "personal_relevance": True, "details": {}}


class EvidenceBindingTests(unittest.TestCase):
    def test_model_whitespace_is_rebound_to_exact_source_but_numbers_are_not_changed(self):
        source = "私は毎月12345円を積み立てています。"
        self.assertEqual(bind_quote("私は毎月 12345 円を積み立てています。", source), source)
        self.assertIsNone(bind_quote("私は毎月54321円を積み立てています。", source))
        self.assertIsNone(bind_quote("私は毎月12345万円を積み立てています。", source))

    def test_native_worker_preserves_quote_and_drops_ungrounded_candidate(self):
        with isolated_personal_os():
            add_entry("Synthetic preference", "user: 私はSample温泉が好きです。\n\nassistant: AI_ONLY_MARKER", "chatgpt-export")
            app._queue_analysis_jobs_for_provider("local")
            valid = personal_fact()
            invalid = dict(valid, evidence_quote="原文にない推測", asset="Imaginary place")
            with patch.object(app, "extract_with_model", return_value=[valid, invalid]) as extractor:
                result = app._analyze_imported_conversations(1, "local")
            self.assertNotIn("AI_ONLY_MARKER", extractor.call_args.args[0])
            self.assertEqual(result["facts_saved"], 1)
            with app.db() as connection:
                self.assertEqual(connection.execute("SELECT quote FROM fact_evidence").fetchone()[0], valid["evidence_quote"])
                self.assertEqual(connection.execute("SELECT count(*) FROM facts").fetchone()[0], 1)

    def test_reanalysis_repairs_paraphrase_without_duplicating_fact(self):
        with isolated_personal_os():
            entry = add_entry("Synthetic preference", "user: 私はSample温泉が好きです。", "chatgpt-export")
            fact = personal_fact()
            first = app.save_structured_facts(entry, [fact], "local", "sample")[0]
            with app.db() as connection:
                connection.execute("UPDATE fact_evidence SET quote='要約であって引用ではない' WHERE fact_id=?", (first["id"],))
                self.assertEqual(app.apply_memory_quality_to_fact(connection, first["id"])["retrieval_eligibility"], "pending")
            second = app.save_structured_facts(entry, [fact], "local", "sample")[0]
            self.assertEqual(first["id"], second["id"])
            with app.db() as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM facts").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT quote FROM fact_evidence WHERE fact_id=?", (first["id"],)).fetchone()[0], fact["evidence_quote"])
                self.assertEqual(connection.execute("SELECT retrieval_eligibility FROM facts WHERE id=?", (first["id"],)).fetchone()[0], "eligible")

    def test_identical_history_replay_is_deduped_but_distinct_occurrences_are_kept(self):
        with isolated_personal_os():
            fact = {"category": "lifestyle", "type": "event", "asset": "散歩", "summary": "私は散歩した。",
                    "confidence": 0.95, "evidence_quote": "私は散歩した。", "personal_relevance": True, "details": {}}
            first_entry = add_entry("Synthetic event A", "user: 私は散歩した。", "chatgpt-export")
            second_entry = add_entry("Synthetic event B", "user: 私は散歩した。", "chatgpt-export")
            first = app.save_structured_facts(first_entry, [fact], "local", "sample")[0]
            replay = app.save_structured_facts(first_entry, [fact], "local", "sample")[0]
            other = app.save_structured_facts(second_entry, [fact], "local", "sample")[0]
            self.assertEqual(first["id"], replay["id"])
            self.assertNotEqual(first["id"], other["id"])


if __name__ == "__main__": unittest.main()
