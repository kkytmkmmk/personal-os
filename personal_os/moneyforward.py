"""Read-only integration with the local mf-dashboard SQLite database.

Money Forward remains the source of truth.  Personal OS never writes to this
database and does not expose its filesystem path or account identifiers in API
responses.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


REQUIRED_TABLES = {
    "groups",
    "group_accounts",
    "accounts",
    "asset_categories",
    "holdings",
    "daily_snapshots",
    "holding_values",
    "transactions",
    "asset_history",
    "asset_history_categories",
}


def _unavailable(status: str, *, configured: bool, message: str) -> dict[str, object]:
    return {
        "source": "Money Forward ME",
        "configured": configured,
        "connected": False,
        "read_only": True,
        "status": status,
        "message": message,
    }


def _read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _current_group_id(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        """SELECT id FROM groups
           ORDER BY CASE WHEN is_current=1 THEN 0 ELSE 1 END,
                    COALESCE(last_scraped_at,updated_at,created_at) DESC
           LIMIT 1"""
    ).fetchone()
    return str(row["id"]) if row else None


def _legacy_projection(database_path: str | Path | None, *, transaction_limit: int = 50) -> dict[str, object]:
    """Return a bounded local projection without modifying the source DB."""
    if not database_path or not str(database_path).strip():
        return _unavailable("not_configured", configured=False, message="Money Forward連携は未設定です。")

    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        return _unavailable("missing", configured=True, message="Money ForwardのローカルDBが見つかりません。")

    limit = max(1, min(int(transaction_limit), 200))
    try:
        with closing(_read_only_connection(path)) as connection:
            tables = {
                str(row["name"])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            missing = sorted(REQUIRED_TABLES - tables)
            if missing:
                return _unavailable(
                    "incompatible",
                    configured=True,
                    message="Money Forward DBの形式が対応していません。",
                )

            group_id = _current_group_id(connection)
            if not group_id:
                return _unavailable("empty", configured=True, message="連携済みグループがありません。")

            group = connection.execute(
                "SELECT last_scraped_at,updated_at FROM groups WHERE id=?", (group_id,)
            ).fetchone()
            history_rows = connection.execute(
                """SELECT id,date,total_assets,change FROM asset_history
                   WHERE group_id=? ORDER BY date DESC,id DESC LIMIT 400""",
                (group_id,),
            ).fetchall()
            latest_history = history_rows[0] if history_rows else None
            breakdown: dict[str, float] = {}
            if latest_history:
                for row in connection.execute(
                    """SELECT category_name,amount FROM asset_history_categories
                       WHERE asset_history_id=? ORDER BY ABS(amount) DESC""",
                    (latest_history["id"],),
                ):
                    breakdown[str(row["category_name"] or "その他")] = float(row["amount"] or 0)

            snapshot = connection.execute(
                """SELECT id,group_id,date,refresh_completed,updated_at FROM daily_snapshots
                   WHERE group_id=?
                   ORDER BY date DESC,refresh_completed DESC,id DESC LIMIT 1""",
                (group_id,),
            ).fetchone()
            holdings: list[dict[str, object]] = []
            if snapshot:
                holding_rows = connection.execute(
                    """SELECT h.name,h.code,h.type,ac.name AS category,hv.amount,hv.quantity,
                              hv.unit_price,hv.avg_cost_price,hv.daily_change,
                              hv.unrealized_gain,hv.unrealized_gain_pct
                       FROM holding_values hv
                       JOIN holdings h ON h.id=hv.holding_id
                       JOIN accounts a ON a.id=h.account_id
                       JOIN group_accounts ga ON ga.account_id=a.id AND ga.group_id=?
                       JOIN daily_snapshots ds ON ds.id=hv.snapshot_id AND ds.group_id=ga.group_id
                       LEFT JOIN asset_categories ac ON ac.id=h.category_id
                       WHERE hv.snapshot_id=? AND COALESCE(h.is_active,1)=1
                         AND COALESCE(a.is_active,1)=1
                       ORDER BY ABS(COALESCE(hv.amount,0)) DESC,h.id DESC LIMIT 200""",
                    (group_id, snapshot["id"]),
                ).fetchall()
                holdings = [
                    {
                        "name": row["name"],
                        "code": row["code"],
                        "type": row["type"],
                        "category": row["category"] or row["type"] or "その他",
                        "amount": row["amount"],
                        "quantity": row["quantity"],
                        "unit_price": row["unit_price"],
                        "avg_cost_price": row["avg_cost_price"],
                        "daily_change": row["daily_change"],
                        "unrealized_gain": row["unrealized_gain"],
                        "unrealized_gain_pct": row["unrealized_gain_pct"],
                    }
                    for row in holding_rows
                ]

            transaction_filter = """t.account_id IN (
                SELECT account_id FROM group_accounts WHERE group_id=?
            ) AND COALESCE(t.is_excluded_from_calculation,0)=0
              AND COALESCE(t.is_transfer,0)=0"""
            recent_rows = connection.execute(
                f"""SELECT t.date,t.category,t.sub_category,t.description,t.amount,t.type
                     FROM transactions t WHERE {transaction_filter}
                     ORDER BY t.date DESC,t.id DESC LIMIT ?""",
                (group_id, limit),
            ).fetchall()
            recent_transactions = [
                {
                    "date": row["date"],
                    "category": row["category"],
                    "sub_category": row["sub_category"],
                    "description": row["description"],
                    "amount": row["amount"],
                    "type": row["type"],
                }
                for row in recent_rows
            ]
            monthly_rows = connection.execute(
                f"""SELECT substr(t.date,1,7) AS month,
                            SUM(CASE WHEN t.type='income' THEN ABS(COALESCE(t.amount,0)) ELSE 0 END) AS income,
                            SUM(CASE WHEN t.type='expense' THEN ABS(COALESCE(t.amount,0)) ELSE 0 END) AS expense
                     FROM transactions t WHERE {transaction_filter}
                       AND t.type IN ('income','expense')
                     GROUP BY substr(t.date,1,7) ORDER BY month DESC LIMIT 13""",
                (group_id,),
            ).fetchall()
            monthly_cashflow = [
                {
                    "month": row["month"],
                    "income": float(row["income"] or 0),
                    "expense": float(row["expense"] or 0),
                    "balance": float(row["income"] or 0) - float(row["expense"] or 0),
                }
                for row in reversed(monthly_rows)
            ]
            transaction_count = connection.execute(
                f"SELECT COUNT(*) FROM transactions t WHERE {transaction_filter}", (group_id,)
            ).fetchone()[0]

            last_synced_at = None
            for candidate in (
                group["last_scraped_at"] if group else None,
                snapshot["updated_at"] if snapshot else None,
                group["updated_at"] if group else None,
            ):
                if candidate:
                    last_synced_at = candidate
                    break

            return {
                "source": "Money Forward ME",
                "configured": True,
                "connected": True,
                "read_only": True,
                "status": "connected",
                "message": "ローカルDBへ読み取り専用で接続しています。",
                "last_synced_at": last_synced_at,
                "snapshot_date": snapshot["date"] if snapshot else None,
                "refresh_completed": bool(snapshot["refresh_completed"]) if snapshot else None,
                "total_assets": float(latest_history["total_assets"]) if latest_history else None,
                "breakdown": breakdown,
                "holdings_count": len(holdings),
                "holdings": holdings,
                "transaction_count": int(transaction_count),
                "recent_transactions": recent_transactions,
                "monthly_cashflow": monthly_cashflow,
                "asset_history": [
                    {
                        "date": row["date"],
                        "total_assets": float(row["total_assets"] or 0),
                        "change": float(row["change"] or 0),
                    }
                    for row in reversed(history_rows)
                ],
            }
    except (OSError, sqlite3.Error, ValueError):
        return _unavailable(
            "error",
            configured=True,
            message="Money Forward DBを読み込めませんでした。更新処理の完了後に再試行してください。",
        )


def moneyforward_projection(database_path: str | Path | None, *, transaction_limit: int = 50,
                            snapshot_directory: Path | None = None) -> dict[str, object]:
    """Choose the newest complete source; never mix dates or source totals."""
    legacy = _legacy_projection(database_path, transaction_limit=transaction_limit)
    if snapshot_directory is not None:
        from personal_os.moneyforward_sync import connector_projection
        try:
            connector = connector_projection(snapshot_directory)
        except (OSError, ValueError):
            legacy["sync_error"] = "更新データを読み込めませんでした。再度取り込んでください。"
            return legacy
        legacy_dates = [str(legacy.get("snapshot_date") or "")]
        legacy_dates += [str(item["date"]) for item in legacy.get("asset_history", [])]
        if connector and (not legacy.get("connected") or connector["snapshot_date"] >= max(legacy_dates)):
            return connector
    return legacy
