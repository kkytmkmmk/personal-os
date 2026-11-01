"""Finish derived local search indexes in bounded, resumable transactions."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


def process_batch(limit=128):
    with app.db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        jobs = connection.execute(
            "SELECT j.chunk_id,c.text FROM embedding_jobs j JOIN chunks c ON c.id=j.chunk_id "
            "WHERE j.state IN ('pending','failed') ORDER BY j.chunk_id LIMIT ?",
            (max(1, min(limit, 128)),),
        ).fetchall()
        connection.executemany("UPDATE embedding_jobs SET state='running',last_error='',updated_at=? WHERE chunk_id=?",
                               [(app.now(), row['chunk_id']) for row in jobs])
    prepared, failures = [], []
    for job in jobs:
        try:
            digest = hashlib.sha256(job['text'].encode('utf-8')).hexdigest()
            vector = app.local_embedding(job['text'])
            prepared.append((job['chunk_id'], app.EMBEDDING_MODEL, app.EMBEDDING_DIMENSIONS,
                             json.dumps(vector), digest, app.now()))
        except (UnicodeError, ValueError) as error:
            failures.append((type(error).__name__, app.now(), job['chunk_id']))
    with app.db() as connection:
        connection.executemany(
            'INSERT INTO embeddings(chunk_id,model,dimensions,vector_json,content_hash,created_at) VALUES(?,?,?,?,?,?) '
            'ON CONFLICT(chunk_id,model,content_hash) DO UPDATE SET vector_json=excluded.vector_json,created_at=excluded.created_at',
            prepared,
        )
        connection.executemany("UPDATE embedding_jobs SET state='completed',last_error='',updated_at=? WHERE chunk_id=?",
                               [(app.now(), row[0]) for row in prepared])
        connection.executemany("UPDATE embedding_jobs SET state='failed',last_error=?,updated_at=? WHERE chunk_id=?", failures)
    if failures:
        raise RuntimeError('Local index generation failed; failed jobs retained for retry')
    return len(prepared)


def pending():
    with app.db() as connection:
        return connection.execute(
            "SELECT count(*) FROM embedding_jobs j JOIN chunks c ON c.id=j.chunk_id WHERE j.state IN ('pending','failed')"
        ).fetchone()[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps({'pending': pending(), 'apply': args.apply}), flush=True)
    if not args.apply:
        return
    backup = app.backup_database(force=False) or app.setting('last_backup_path', '')
    if not backup or not app.verify_backup(str(backup))['valid']:
        raise ValueError('A verified backup is required')
    count = 0
    while processed := process_batch():
        count += processed
        if count % 2048 == 0:
            print(json.dumps({'indexed': count, 'remaining': pending()}), flush=True)
    with app.db() as connection:
        orphans = connection.execute('SELECT count(*) FROM embedding_jobs j WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.id=j.chunk_id)').fetchone()[0]
        running = connection.execute("SELECT count(*) FROM embedding_jobs WHERE state='running'").fetchone()[0]
    print(json.dumps({'indexed': count, 'remaining': pending(), 'running': running, 'historical_orphan_jobs': orphans}), flush=True)
    if pending() or running:
        raise RuntimeError('Index refresh has outstanding jobs')


if __name__ == '__main__':
    main()
