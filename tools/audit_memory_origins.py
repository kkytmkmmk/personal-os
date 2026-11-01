"""Quarantine only provably assistant-origin Facts; retain every source row."""
import argparse
import bisect
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


def original_roles(fragment, transcript):
    """All exact occurrences must agree; ambiguity is never a deletion reason."""
    if not fragment:
        return set()
    spans = list(re.finditer(r"(?m)^(user|assistant|system|external):", transcript))
    positions = [span.start() for span in spans]
    roles = set()
    start = 0
    for _ in range(100):
        position = transcript.find(fragment, start)
        if position < 0:
            return roles
        index = bisect.bisect_right(positions, position) - 1
        if index < 0:
            roles.add("unknown")
        else:
            roles.add(spans[index].group(1))
        for span in spans[index + 1:]:
            if span.start() >= position + len(fragment):
                break
            roles.add(span.group(1))
        start = position + 1
    return set()  # Too many repeated spans to disambiguate conservatively.


def find_candidates():
    candidates = []
    with app.db() as connection:
        entries = connection.execute(
            """SELECT DISTINCT e.id FROM entries e JOIN documents d ON d.legacy_entry_id=e.id
               JOIN facts f ON f.document_id=d.id
               WHERE e.source='chatgpt-export' AND f.retrieval_eligibility='eligible'"""
        ).fetchall()
        for entry in entries:
            transcript = connection.execute("SELECT body FROM entries WHERE id=?", (entry["id"],)).fetchone()[0]
            facts = connection.execute(
                """SELECT f.id,c.text,r.reason,r.review_note FROM facts f
                   JOIN documents d ON d.id=f.document_id JOIN chunks c ON c.id=f.source_chunk_id
                   LEFT JOIN fact_reviews r ON r.fact_id=f.id
                   WHERE d.legacy_entry_id=? AND f.retrieval_eligibility='eligible'""", (entry["id"],)
            ).fetchall()
            for fact in facts:
                if any(marker in str(fact["reason"] or "") + str(fact["review_note"] or "")
                       for marker in ("ユーザー", "本人確認")):
                    continue
                if original_roles(fact["text"], transcript) != {"assistant"}:
                    continue
                evidence = connection.execute(
                    "SELECT e.quote,c.text FROM fact_evidence e JOIN chunks c ON c.id=e.source_chunk_id WHERE e.fact_id=?",
                    (fact["id"],),
                ).fetchall()
                if any(item["quote"] and item["quote"] in app.user_evidence_text(item["text"])
                       and app.source_role_for_text(item["text"]) in {"user", "mixed"} for item in evidence):
                    continue
                candidates.append(fact["id"])
    return candidates


def repair_audited_source_roles():
    changed = 0
    with app.db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        entries = connection.execute(
            """SELECT DISTINCT e.id FROM entries e JOIN documents d ON d.legacy_entry_id=e.id
               JOIN chunks c ON c.document_id=d.id JOIN facts f ON f.source_chunk_id=c.id
               JOIN memory_corrections m ON m.fact_id=f.id
               WHERE m.correction_type='assistant_origin_quarantine' AND c.speaker_role!='assistant'"""
        ).fetchall()
        for entry in entries:
            raw = connection.execute("SELECT body FROM entries WHERE id=?", (entry["id"],)).fetchone()[0]
            chunks = connection.execute(
                """SELECT DISTINCT c.id,c.text FROM chunks c JOIN documents d ON d.id=c.document_id
                   JOIN facts f ON f.source_chunk_id=c.id JOIN memory_corrections m ON m.fact_id=f.id
                   WHERE d.legacy_entry_id=? AND m.correction_type='assistant_origin_quarantine'
                     AND c.speaker_role!='assistant'""", (entry["id"],)
            ).fetchall()
            for chunk in chunks:
                if original_roles(chunk["text"], raw) == {"assistant"}:
                    connection.execute("UPDATE chunks SET speaker_role='assistant' WHERE id=?", (chunk["id"],))
                    changed += 1
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--repair-audited-roles", action="store_true")
    args = parser.parse_args()
    candidates = find_candidates()
    print(json.dumps({"assistant_only_candidates": len(candidates), "apply": args.apply}), flush=True)
    if not args.apply or (not candidates and not args.repair_audited_roles):
        return
    backup = app.backup_database(force=True)
    if not app.verify_backup(str(backup))["valid"]:
        raise ValueError("Backup verification failed")
    with app.db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        # A user may have confirmed/re-anchored a Fact during the backup.
        # Re-audit under the write reservation before changing any candidate.
        candidates = sorted(set(candidates).intersection(find_candidates()))
        changed = 0
        for fact_id in candidates:
            row = connection.execute("SELECT retrieval_eligibility FROM facts WHERE id=?", (fact_id,)).fetchone()
            if row["retrieval_eligibility"] != "eligible":
                continue
            connection.execute(
                """UPDATE facts SET retrieval_eligibility='pending',validation_status='pending',
                   validation_reason='原文ではAI側の発言のみが根拠。本人Evidenceの再解析待ち',truth_confidence=MIN(COALESCE(truth_confidence,0),0.49)
                   WHERE id=?""", (fact_id,)
            )
            connection.execute(
                "UPDATE chunks SET speaker_role='assistant' WHERE id=(SELECT source_chunk_id FROM facts WHERE id=?)",
                (fact_id,),
            )
            app._record_memory_correction(connection, fact_id=fact_id, entity_id=None,
                correction_type="assistant_origin_quarantine", before={"retrieval_eligibility": "eligible"},
                after={"retrieval_eligibility": "pending"}, reason="原文上の発言者がassistantのみ", source="origin_audit")
            changed += 1
    repaired_roles = repair_audited_source_roles() if args.repair_audited_roles else 0
    print(json.dumps({"quarantined": changed, "source_roles_repaired": repaired_roles, "backup": backup.name}), flush=True)


if __name__ == "__main__": main()
