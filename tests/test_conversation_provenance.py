import unittest
from unittest.mock import patch

import app
from personal_os.conversation_chunks import split_conversation
from tests.test_requirements_cycle import isolated_personal_os, add_entry
from tools.codex_analysis import export_packet


class ConversationProvenanceTests(unittest.TestCase):
    def test_blank_lines_do_not_erase_assistant_identity(self):
        chunks = split_conversation("user: 手取りとは？\n\nassistant: 一般説明です。\n\n例示の年収です。\n\n資産の試算です。", 100)
        self.assertTrue(all("例示の年収です" not in app.user_evidence_text(c) for c in chunks))
        self.assertTrue(all("資産の試算です" not in app.user_evidence_text(c) for c in chunks))

    def test_every_long_continuation_has_its_real_role(self):
        chunks = split_conversation("assistant: " + "資産に関する一般説明。" * 80, 160)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(c.startswith("assistant: ") for c in chunks))
        self.assertTrue(all(len(c) <= 160 for c in chunks))
        self.assertTrue(all(app.user_evidence_text(c) == "" for c in chunks))

    def test_long_user_continuation_stays_user(self):
        chunks = split_conversation("user: " + "私の旅行の好みです。" * 80, 160)
        self.assertTrue(all(c.startswith("user: ") for c in chunks))
        self.assertTrue(all(app.user_evidence_text(c) for c in chunks))

    def test_unattributed_legacy_text_is_not_user_evidence(self):
        self.assertTrue(split_conversation("私の資産という例示です。")[0].startswith("external: "))
        self.assertFalse(app.chunk_may_contain_personal_memory("年収が書かれた出所不明の断片"))

    def test_prefilter_uses_user_not_assistant_keywords(self):
        self.assertFalse(app.chunk_may_contain_personal_memory("user: 用語を説明して\n\nassistant: 私の資産はこの額という例です。"))
        self.assertTrue(app.chunk_may_contain_personal_memory("user: 私は温泉旅行が好きです。\n\nassistant: 了解"))

    def test_pronoun_free_travel_wish_is_personal_but_assistant_and_general_text_are_not(self):
        fact = {"category": "travel", "type": "event", "asset": "Sample温泉", "details": {}}
        self.assertEqual(app.classify_personal_relevance(fact, "user: 17時にはホテルに戻りたい。朝は遅め", "place"), "personal")
        self.assertNotEqual(app.classify_personal_relevance(fact, "assistant: ホテルに戻りたいという例です。", "place"), "personal")
        self.assertEqual(app.classify_personal_relevance(fact, "user: 一般的にホテルに戻りたいときはどうする？", "place"), "archive_only")

    def test_selected_repair_preserves_raw_old_chunks_and_other_documents(self):
        with isolated_personal_os():
            with patch.object(app, "CHUNK_VERSION", "conversation-turn-v3"):
                first = add_entry("Synthetic A", "user: 私は温泉が好きです。", "chatgpt-export")
                second = add_entry("Synthetic B", "user: 私は旅行が好きです。", "chatgpt-export")
            with app.db() as connection:
                before = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                raw = connection.execute("SELECT body FROM entries WHERE id=?", (first,)).fetchone()[0]
            result = app.prepare_conversation_reanalysis(entry_ids=[first], queue_jobs=False)
            self.assertEqual(result["documents"], 1)
            with app.db() as connection:
                self.assertGreater(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], before)
                self.assertEqual(connection.execute("SELECT body FROM entries WHERE id=?", (first,)).fetchone()[0], raw)
                other = connection.execute("SELECT c.segment_version FROM chunks c JOIN documents d ON d.id=c.document_id WHERE d.legacy_entry_id=? AND c.is_active=1", (second,)).fetchone()[0]
                self.assertEqual(other, "conversation-turn-v3")
            self.assertEqual(app.prepare_conversation_reanalysis(entry_ids=[], queue_jobs=False)["documents"], 0)

    def test_codex_skips_unattributed_imported_fragments(self):
        with isolated_personal_os(), patch.object(app, "extraction_providers", return_value=["local"]):
            entry = add_entry("Synthetic fragment", "年収の一般例です。", "chatgpt-export")
            with app.db() as connection:
                chunk = connection.execute("SELECT c.* FROM chunks c JOIN documents d ON d.id=c.document_id WHERE d.legacy_entry_id=?", (entry,)).fetchone()
                connection.execute("UPDATE chunks SET text='年収の一般例です。' WHERE id=?", (chunk["id"],))
                connection.execute("""INSERT INTO analysis_jobs(document_id,provider,model,prompt_version,content_hash,status,job_kind,source_chunk_id,created_at,updated_at)
                    VALUES(?,'local',?,?,'sample','pending','chunk',?,?,?)""",
                    (chunk["document_id"], app.provider_model("local", "extract"), app.PROMPT_VERSION, chunk["id"], app.now(), app.now()))
            self.assertEqual(export_packet()["jobs"], [])

    def test_wrong_source_is_rejected_instead_of_reassigned(self):
        with isolated_personal_os():
            entry = add_entry("Synthetic source", "私のメモです。")
            with self.assertRaises(ValueError):
                app.save_structured_facts(entry, [], "codex", "sample", source_chunk_id=999999)

    def test_consultation_keyword_and_semantic_search_exclude_assistant_only_text(self):
        with isolated_personal_os():
            own = add_entry("SampleKeyword user", "user: 私のSampleKeyword UserOnlyMarkerのメモです。", "chatgpt-export")
            other = add_entry("SampleKeyword assistant", "assistant: SampleKeyword AssistantOnlyMarkerの説明です。", "chatgpt-export")
            app.process_embedding_jobs(20)
            semantic = app.semantic_search("SampleKeyword")
            self.assertTrue(any(row["id"] == own for row in semantic))
            self.assertFalse(any(row["id"] == other for row in semantic))
            retrieved = app.relevant_memories("SampleKeyword")
            self.assertTrue(any("UserOnlyMarker" in str(item) for item in retrieved))
            self.assertFalse(any("AssistantOnlyMarker" in str(item) for item in retrieved))

    def test_relevance_correction_can_restore_auto_rejection_but_not_manual_rejection(self):
        with isolated_personal_os():
            quote = "17時にはホテルに戻りたい。"
            entry = add_entry("Synthetic travel request", "user: " + quote, "chatgpt-export")
            saved = app.save_structured_facts(entry, [{"category": "travel", "type": "event", "asset": "Sample旅行",
                "summary": quote, "confidence": 0.95, "evidence_strength": "explicit", "personal_relevance": True,
                "details": {"evidence_quote": quote}}], "codex", "sample")
            fact_id = saved[0]["id"]
            with app.db() as connection:
                connection.execute("UPDATE facts SET personal_relevance='archive_only',retrieval_eligibility='excluded' WHERE id=?", (fact_id,))
                connection.execute("UPDATE fact_reviews SET state='rejected',reason='Personal OSの正本ではない外部情報のため保管のみ',review_note='自動判定' WHERE fact_id=?", (fact_id,))
                self.assertEqual(app.apply_memory_quality_to_fact(connection, fact_id)["retrieval_eligibility"], "eligible")
                connection.execute("UPDATE fact_reviews SET state='rejected',reason='ユーザーによる訂正',review_note='ユーザー' WHERE fact_id=?", (fact_id,))
                self.assertEqual(app.apply_memory_quality_to_fact(connection, fact_id)["retrieval_eligibility"], "excluded")

    def test_quarantined_financial_fact_is_not_an_actual_transaction_total(self):
        with isolated_personal_os():
            entry = add_entry("Synthetic purchase", "user: 私はSample銘柄を1000円で買った。", "ai-ingest")
            fact = {"category": "finance", "type": "transaction", "asset": "Sample銘柄", "amount": 1000,
                    "currency": "JPY", "date": "2026-07-01", "summary": "私はSample銘柄を1000円で買った。",
                    "confidence": 0.99, "details": {"evidence_quote": "私はSample銘柄を1000円で買った。"},
                    "evidence_quote": "私はSample銘柄を1000円で買った。", "evidence_strength": "explicit", "personal_relevance": True}
            saved = app.save_structured_facts(entry, [fact], "codex", "sample")
            with app.db() as connection:
                self.assertTrue(app.eligible_finance_transactions(connection))
                connection.execute("UPDATE facts SET retrieval_eligibility='pending' WHERE id=?", (saved[0]["id"],))
                self.assertEqual(app.eligible_finance_transactions(connection), [])


if __name__ == "__main__": unittest.main()
