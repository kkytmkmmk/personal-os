"""Bounded, backed-up provenance repair; old raw text/chunks/Facts stay intact."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


def select_entries(limit, entry_ids=None):
    parameters = [app.CHUNK_VERSION]
    restriction = ""
    if entry_ids:
        restriction = " AND e.id IN (" + ",".join("?" for _ in entry_ids) + ")"
        parameters.extend(entry_ids)
    parameters.append(limit)
    with app.db() as connection:
        return [r["id"] for r in connection.execute(
            """SELECT e.id FROM entries e JOIN documents d ON d.legacy_entry_id=e.id
               WHERE e.source='chatgpt-export'
                 AND NOT EXISTS(SELECT 1 FROM chunks c WHERE c.document_id=d.id
                                AND c.is_active=1 AND c.segment_version=?)""" + restriction +
            " ORDER BY d.source_created_at DESC,e.id DESC LIMIT ?", parameters)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--entry-id", type=int, action="append", help="Optionally target specific source entries")
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("limit must be 1..100")
    entries = select_entries(args.limit, args.entry_id)
    print(json.dumps({"selected_documents": len(entries), "apply": args.apply}), flush=True)
    if not entries or not args.apply:
        return
    backup = app.backup_database(force=True)
    if not app.verify_backup(str(backup))["valid"]:
        raise ValueError("Pre-repair backup could not be verified")
    print(json.dumps({"backup_verified": True, "backup": backup.name}), flush=True)
    if not app.acquire_analysis_lock():
        raise ValueError("An analysis worker is active; pause it and retry")
    try:
        with app.db() as connection:
            placeholders = ",".join("?" for _ in entries)
            running = connection.execute(
                f"SELECT COUNT(*) FROM analysis_jobs j JOIN documents d ON d.id=j.document_id WHERE d.legacy_entry_id IN ({placeholders}) AND j.status='running'", entries
            ).fetchone()[0]
            if running:
                raise ValueError("Selected documents have running analysis jobs")
        repaired = app.prepare_conversation_reanalysis(entry_ids=entries, queue_jobs=False)
        repaired["queued"] = app.queue_analysis_jobs()
    finally:
        app.release_analysis_lock()
    print(json.dumps({"completed": repaired}), flush=True)


if __name__ == "__main__": main()
