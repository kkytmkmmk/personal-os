"""Validated, immutable Money Forward ME connector snapshots; no network calls.

Snapshots live under the app's attachment root so full backups include them.
They remain reference data, never LLM-extracted Facts or actual trading orders.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

FORMAT = "moneyforward-me-snapshot-v1"
MAX_BYTES = 8 * 1024 * 1024


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("金額は有限の数値にしてください。")
    return value


def text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise ValueError("項目名または日付の形式が正しくありません。")
    return value


def day(value):
    result = date.fromisoformat(text(value))
    if result.isoformat() != value or result > datetime.now().astimezone().date():
        raise ValueError("未来日または不正な日付は取り込めません。")
    return result


def validate_snapshot(bundle: dict) -> dict:
    try:
        if bundle["format"] != FORMAT:
            raise ValueError("Money Forward ME用の更新JSONではありません。")
        fetched = datetime.fromisoformat(bundle["fetched_at"])
        if fetched.tzinfo is None or fetched > datetime.now(timezone.utc):
            raise ValueError("取得日時には過去または現在のタイムゾーン付き日時が必要です。")
        balances = bundle["balances"]
        holdings = bundle["holdings"]
        cashflow = bundle["household_summaries"]
        if balances["currency"] != "JPY" or holdings["currency"] != "JPY":
            raise ValueError("JPY以外の金額は対応していません。")
        if not isinstance(balances["balances"], list) or not 1 <= len(balances["balances"]) <= 12:
            raise ValueError("残高は1〜12日分にしてください。")
        dates = set()
        for balance in balances["balances"]:
            day(balance["date"])
            if balance["date"] in dates:
                raise ValueError("残高の日付が重複しています。")
            dates.add(balance["date"])
            categories = balance["categories"]
            if not isinstance(categories, list) or not categories or len(categories) > 100:
                raise ValueError("残高の内訳がありません。")
            names = set()
            for category in categories:
                name = text(category["name"])
                if name in names:
                    raise ValueError("残高のカテゴリが重複しています。")
                names.add(name)
                amount = number(category["amount"])
                accounts = category["accounts"]
                if not isinstance(accounts, list) or len(accounts) > 1000:
                    raise ValueError("口座内訳の形式が正しくありません。")
                for account in accounts:
                    text(account["name"])
                    number(account["amount"])
                if abs(sum(account["amount"] for account in accounts) - amount) > 1:
                    raise ValueError("カテゴリ残高と口座内訳が一致しません。")
        if not isinstance(holdings["holdings"], list) or len(holdings["holdings"]) > 10000:
            raise ValueError("保有明細は10,000件以内にしてください。")
        for holding in holdings["holdings"]:
            for key in ("name", "account_name", "institution_name", "asset_category"):
                text(holding[key])
            number(holding["current_value"])
        summaries = cashflow["household_summaries"]
        if not isinstance(summaries, list) or len(summaries) > 24:
            raise ValueError("月次収支は24か月以内にしてください。")
        months = set()
        for summary in summaries:
            month = text(summary["month"])
            if len(month) != 7 or month in months:
                raise ValueError("収支の月が不正または重複しています。")
            day(month + "-01")
            months.add(month)
            for kind in ("income", "expenses"):
                if not isinstance(summary[kind], list) or len(summary[kind]) > 200:
                    raise ValueError("月次収支の内訳が正しくありません。")
                for item in summary[kind]:
                    text(item["category_name"])
                    if number(item["value_in_jpy"]) < 0:
                        raise ValueError("月次収支の金額は0以上にしてください。")
        transactions = bundle.get("transactions")
        if transactions is not None:
            if transactions["currency"] != "JPY" or len(transactions["transactions"]) > 500:
                raise ValueError("入出金はJPY・500件以内にしてください。")
            for item in transactions["transactions"]:
                day(item["date"])
                number(item["amount"])
                if item["type"] not in {"income", "expense"}:
                    raise ValueError("振替や未分類の入出金は取り込めません。")
                text(item["description"])
        return bundle
    except (KeyError, TypeError, AttributeError, OverflowError) as error:
        raise ValueError("更新JSONの必須項目または形式が正しくありません。") from error


def import_snapshot(directory: Path, bundle: dict) -> dict:
    validate_snapshot(bundle)
    encoded = json.dumps(bundle, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise ValueError("更新JSONは8MB以内にしてください。")
    digest = hashlib.sha256(encoded).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{digest}.json"
    duplicate = target.exists()
    if not duplicate:
        descriptor, temporary = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    projection = connector_projection(directory)
    return {"imported": not duplicate, "duplicate": duplicate,
            "snapshot_date": projection["snapshot_date"], "last_synced_at": projection["last_synced_at"],
            "holdings_count": len(bundle["holdings"]["holdings"]), "status": "completed",
            "message": "Money Forward MEの更新データを反映しました。" if not duplicate else "同じ更新データは反映済みです。"}


def connector_projection(directory: Path) -> dict | None:
    candidates = []
    for path in directory.glob("*.json"):
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("保存済み更新JSONのサイズが不正です。")
        bundle = validate_snapshot(json.loads(path.read_text(encoding="utf-8")))
        for balance in bundle["balances"]["balances"]:
            candidates.append((balance["date"], bundle["fetched_at"], balance, bundle))
    if not candidates:
        return None
    _, _, latest, bundle = max(candidates, key=lambda item: (item[0], datetime.fromisoformat(item[1])))
    breakdown, liability_breakdown = {}, {}
    for category in latest["categories"]:
        breakdown[category["name"]] = sum(max(0, account["amount"]) for account in category["accounts"])
        liability_breakdown[category["name"]] = sum(max(0, -account["amount"]) for account in category["accounts"])
    breakdown = {key: value for key, value in breakdown.items() if value}
    liability_breakdown = {key: value for key, value in liability_breakdown.items() if value}
    assets, liabilities = sum(breakdown.values()), sum(liability_breakdown.values())
    history = {}
    for _, fetched, balance, _ in sorted(candidates, key=lambda item: datetime.fromisoformat(item[1])):
        values = [account["amount"] for category in balance["categories"] for account in category["accounts"]]
        history[balance["date"]] = {"date": balance["date"], "total_assets": sum(max(0, v) for v in values),
                                   "net_assets": sum(values), "change": None}
    flows = [{"month": s["month"], "income": sum(i["value_in_jpy"] for i in s["income"]),
              "expense": sum(i["value_in_jpy"] for i in s["expenses"])}
             for s in bundle["household_summaries"]["household_summaries"]]
    for flow in flows:
        flow["balance"] = flow["income"] - flow["expense"]
    holdings = [{"name": h["name"], "category": h["asset_category"], "amount": h["current_value"]}
                for h in bundle["holdings"]["holdings"]]
    transactions = [{"date": t["date"], "description": t["description"], "category": t.get("category"),
                     "amount": abs(t["amount"]), "type": t["type"]}
                    for t in (bundle.get("transactions") or {}).get("transactions", [])]
    return {"source": "Money Forward ME", "source_kind": "connector_snapshot", "configured": True,
            "connected": True, "read_only": True, "status": "connected", "last_synced_at": bundle["fetched_at"],
            "snapshot_date": latest["date"], "balance_date": latest["date"], "refresh_completed": True,
            "holdings_fetched_at": bundle["fetched_at"], "total_assets": assets, "liabilities": liabilities,
            "net_assets": assets - liabilities, "breakdown": breakdown, "liability_breakdown": liability_breakdown,
            "holdings_count": len(holdings), "holdings": holdings, "transaction_count": len(transactions),
            "recent_transactions": sorted(transactions, key=lambda t: t["date"], reverse=True),
            "monthly_cashflow": sorted(flows, key=lambda f: f["month"]),
            "asset_history": [history[key] for key in sorted(history)],
            "message": "接続プラグインから取得した更新データを表示しています。"}
