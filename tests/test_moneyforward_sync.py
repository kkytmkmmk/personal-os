import copy
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
from personal_os.moneyforward import moneyforward_projection
from personal_os.moneyforward_sync import FORMAT, import_snapshot, connector_projection, validate_snapshot
from tests import test_moneyforward_integration as legacy_tests
from tests.test_requirements_cycle import isolated_personal_os


def sample(day="2026-07-31"):
    return {"format": FORMAT, "fetched_at": "2026-08-01T12:00:00+09:00",
            "balances": {"currency": "JPY", "balances": [{"date": day, "categories": [
                {"name": "預金", "amount": 120000, "accounts": [{"name": "Sample account", "amount": 120000}]},
                {"name": "借入", "amount": -20000, "accounts": [{"name": "Sample loan", "amount": -20000}]}]}]},
            "holdings": {"currency": "JPY", "holdings": [{"name": "Sample deposit", "institution_name": "Sample bank",
                           "account_name": "Sample account", "asset_category": "預金", "current_value": 120000}]},
            "household_summaries": {"household_summaries": [{"month": "2026-07", "income": [{"category_name": "給与", "value_in_jpy": 200000}],
                                      "expenses": [{"category_name": "食費", "value_in_jpy": 3000}]}]},
            "transactions": {"currency": "JPY", "transactions": [{"date": "2026-07-21", "description": "Sample meal",
                                  "amount": -3000, "type": "expense", "category": "食費"}]}}


class MoneyForwardSyncTests(unittest.TestCase):
    def test_import_is_idempotent_and_gross_net_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(import_snapshot(root, sample())["imported"])
            self.assertTrue(import_snapshot(root, sample())["duplicate"])
            self.assertEqual(len(list(root.glob("*.json"))), 1)
            result = connector_projection(root)
            self.assertEqual(result["total_assets"], 120000)
            self.assertEqual(result["liabilities"], 20000)
            self.assertEqual(result["net_assets"], 100000)
            self.assertEqual(result["recent_transactions"][0]["amount"], 3000)
            self.assertEqual(result["monthly_cashflow"][0]["balance"], 197000)

    def test_old_import_does_not_replace_newer_balance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            import_snapshot(root, sample("2026-07-31"))
            older = sample("2026-07-01")
            older["fetched_at"] = "2026-08-02T12:00:00+09:00"
            import_snapshot(root, older)
            self.assertEqual(connector_projection(root)["snapshot_date"], "2026-07-31")

    def test_same_day_newer_fetch_wins_and_history_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            import_snapshot(root, sample())
            newer = sample()
            newer["fetched_at"] = "2026-08-02T12:00:00+09:00"
            newer["balances"]["balances"][0]["categories"][0]["amount"] = 130000
            newer["balances"]["balances"][0]["categories"][0]["accounts"][0]["amount"] = 130000
            import_snapshot(root, newer)
            result = connector_projection(root)
            self.assertEqual(result["total_assets"], 130000)
            self.assertEqual(len(result["asset_history"]), 1)

    def test_projection_selects_complete_newest_source_and_never_writes_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy.db"
            legacy_tests.MoneyForwardIntegrationTests().create_source(legacy)
            old_bytes = legacy.read_bytes()
            import_snapshot(root / "snapshots", sample("2026-07-01"))
            self.assertNotIn("source_kind", moneyforward_projection(legacy, snapshot_directory=root / "snapshots"))
            import_snapshot(root / "snapshots", sample())
            result = moneyforward_projection(legacy, snapshot_directory=root / "snapshots")
            self.assertEqual(result["source_kind"], "connector_snapshot")
            self.assertEqual(result["holdings_count"], 1)
            self.assertEqual(result["transaction_count"], 1)
            self.assertEqual(legacy.read_bytes(), old_bytes)

    def test_invalid_data_never_creates_snapshot(self):
        variations = []
        for value in (True, float("nan"), float("inf"), "120000"):
            bundle = sample()
            bundle["balances"]["balances"][0]["categories"][0]["amount"] = value
            variations.append(bundle)
        mismatch = sample()
        mismatch["balances"]["balances"][0]["categories"][0]["amount"] = 1
        variations += [mismatch, {"format": "wrong"}, []]
        future = sample("2999-01-01")
        variations.append(future)
        for bundle in variations:
            with self.subTest(bundle_type=type(bundle).__name__), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    import_snapshot(Path(directory), bundle)
                self.assertFalse(list(Path(directory).glob("*.json")))

    def test_duplicate_days_and_unsupported_currency_rejected(self):
        bundle = sample()
        bundle["balances"]["balances"] *= 2
        with self.assertRaises(ValueError): validate_snapshot(bundle)
        bundle = sample()
        bundle["holdings"]["currency"] = "USD"
        with self.assertRaises(ValueError): validate_snapshot(bundle)

    def test_auth_gate_applies_before_import(self):
        handler = object.__new__(app.Handler)
        handler.path = "/api/integrations/moneyforward/import"
        handler._authorize = lambda *_: False
        with patch.object(app, "import_moneyforward_snapshot", side_effect=AssertionError("Must not import")):
            self.assertIsNone(handler.do_POST())

    def test_api_import_survives_reopen_and_fact_count_does_not_change(self):
        with isolated_personal_os():
            handler = object.__new__(app.Handler)
            handler.path = "/api/integrations/moneyforward/import"
            handler._authorize = lambda *_: True
            payload = json.dumps(sample()).encode()
            handler.headers = {"Content-Length": str(len(payload))}
            handler.rfile = BytesIO(payload)
            handler.send_json = lambda value, status=200: (value, status)
            value, status = handler.do_POST()
            self.assertEqual(status, 201)
            self.assertEqual(value["status"], "completed")
            reopened = app.domain_projection("money")
            self.assertEqual(reopened["summary"]["total_assets"], 120000)
            with app.db() as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM facts").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT count(*) FROM finance_transactions").fetchone()[0], 0)
            backup = app.backup_database(force=True)
            verified = app.verify_backup(str(backup))
            self.assertTrue(verified["valid"])
            import zipfile
            with zipfile.ZipFile(backup) as archive:
                self.assertTrue(any(name.startswith("attachments/integrations/moneyforward/") for name in archive.namelist()))

    def test_corrupt_cache_reports_failure_without_hiding_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "corrupt.json").write_text("broken", encoding="utf-8")
            result = moneyforward_projection(None, snapshot_directory=root)
            self.assertIn("sync_error", result)
            self.assertFalse(result["connected"])


if __name__ == "__main__": unittest.main()
