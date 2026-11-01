"""Resume a full, backed-up local-only conversation repair and analysis run."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from tools.improve_imported_data import select_entries


def emit(stage, **fields):
    print(json.dumps({"stage": stage, "at": app.now(), **fields}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--analyze", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Reuse the last verified daily backup for this interrupted run")
    args = parser.parse_args()
    pending = select_entries(100)
    emit("planned", legacy_batch=len(pending), analysis=args.analyze)
    if not args.apply:
        return
    if app.extraction_providers() != ["local"] or app.cloud_fallback_allowed("import"):
        raise ValueError("This maintenance runner requires local extraction only and no import cloud fallback")
    backup = app.backup_database(force=not args.resume)
    backup_reference = str(backup) if backup else app.setting("last_backup_path", "")
    if not backup_reference or not app.verify_backup(backup_reference)["valid"]:
        raise ValueError("Backup verification failed")
    emit("backup_verified", backup=Path(backup_reference).name)
    if not app.acquire_analysis_lock():
        raise ValueError("An analysis worker is active")
    repaired = quarantined = analyzed = saved = 0
    try:
        while True:
            ids = select_entries(100)
            if not ids:
                break
            with app.db() as connection:
                connection.execute("UPDATE analysis_locks SET acquired_at=? WHERE lock_name=?",
                                   (app.now(), "gemini-import-analysis"))
            result = app.prepare_conversation_reanalysis(entry_ids=ids, queue_jobs=False)
            repaired += result["documents"]
            quarantined += result["quarantined"]
            emit("repairing", repaired=repaired, quarantined=quarantined)
        queued = app.queue_analysis_jobs()
        emit("repair_complete", repaired=repaired, quarantined=quarantined, queued=queued,
             pending=app.pending_import_count())
        if args.analyze:
            # Keep attempts as audit history, including previously failed jobs.
            app.requeue_analysis_jobs("failed")
            for retry_round in range(3):
                while app.runnable_analysis_count():
                    with app.db() as connection:
                        connection.execute("UPDATE analysis_locks SET acquired_at=? WHERE lock_name=?",
                                           (app.now(), "gemini-import-analysis"))
                    result = app._analyze_imported_conversations(1, "local")
                    analyzed += result["analyzed"]
                    saved += result["facts_saved"]
                    emit("analyzing", analyzed=analyzed, facts_processed=saved,
                         remaining=result["remaining"], errors=len(result["errors"]), retry_round=retry_round)
                    if not result["analyzed"] and not result["errors"]:
                        break
                if not app.analysis_job_summary()["failed"]:
                    break
                if retry_round < 2:
                    app.requeue_analysis_jobs("failed")
            summary = app.analysis_job_summary()
            if summary["pending"] or summary["running"] or summary["failed"]:
                emit("incomplete", pending=summary["pending"], running=summary["running"], failed=summary["failed"])
                raise RuntimeError("Analysis remains incomplete; jobs and errors are retained for retry")
        with app.db() as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError("Post-maintenance integrity check failed")
        emit("completed", repaired=repaired, quarantined=quarantined, analyzed=analyzed,
             facts_processed=saved, integrity=integrity)
    finally:
        app.release_analysis_lock()


if __name__ == "__main__": main()
