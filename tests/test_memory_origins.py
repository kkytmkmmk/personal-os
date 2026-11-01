import unittest
from tools.audit_memory_origins import original_roles
from tools.audit_memory_origins import find_candidates
from tests.test_requirements_cycle import isolated_personal_os, add_entry
import app


class MemoryOriginTests(unittest.TestCase):
    def test_unlabelled_assistant_paragraph_is_resolved_from_raw(self):
        raw = "user: 用語を教えて\n\nassistant: 説明です。\n\n年収の例示。"
        self.assertEqual(original_roles("年収の例示。", raw), {"assistant"})

    def test_user_and_assistant_duplicate_is_ambiguous(self):
        raw = "user: 私の希望です\n\nassistant: 私の希望です"
        self.assertEqual(original_roles("私の希望です", raw), {"user", "assistant"})

    def test_exchange_is_not_assistant_only(self):
        raw = "user: 私は温泉が好き\n\nassistant: 了解"
        self.assertEqual(original_roles(raw, raw), {"user", "assistant"})

    def test_missing_and_unattributed_are_not_proven_assistant(self):
        self.assertEqual(original_roles("not present", "user: メモ"), set())
        self.assertEqual(original_roles("私の資産", "私の資産"), {"unknown"})

    def test_assistant_paragraph_with_user_in_text_is_not_a_role_boundary(self):
        raw = "assistant: The user asked a question.\n\nHere is an example."
        self.assertEqual(original_roles("Here is an example.", raw), {"assistant"})

    def test_audit_preserves_explicit_user_confirmation(self):
        with isolated_personal_os():
            entry = add_entry("Synthetic legacy", "assistant: 私はSample銘柄を保有する例です。", "chatgpt-export")
            stored = app.save_structured_facts(entry, [{"category": "finance", "type": "note", "summary": "Sample example", "details": {}}], "legacy", "sample")
            fact_id = stored[0]["id"]
            with app.db() as connection:
                connection.execute("UPDATE facts SET retrieval_eligibility='eligible' WHERE id=?", (fact_id,))
                connection.execute("UPDATE fact_reviews SET reason='legacy auto' WHERE fact_id=?", (fact_id,))
            self.assertIn(fact_id, find_candidates())
            with app.db() as connection:
                connection.execute("UPDATE fact_reviews SET reason='ユーザー確認済み' WHERE fact_id=?", (fact_id,))
            self.assertNotIn(fact_id, find_candidates())


if __name__ == "__main__": unittest.main()
