from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import app
from tests.test_requirements_cycle import isolated_personal_os, add_entry


class StartupRecoveryTests(unittest.TestCase):
    def running_job(self):
        add_entry('Synthetic recovery', 'user: 私はSample温泉が好きです。', 'chatgpt-export')
        app._queue_analysis_jobs_for_provider('local')
        with app.db() as connection:
            row = connection.execute("SELECT id FROM analysis_jobs WHERE status='pending' LIMIT 1").fetchone()
            self.assertIsNotNone(row)
            connection.execute("UPDATE analysis_jobs SET status='running',started_at=? WHERE id=?", (app.now(), row['id']))
        return row['id']

    def test_fresh_worker_is_not_reset_by_server_startup(self):
        with isolated_personal_os(), patch.object(app, 'RUNTIME_LEASED', True):
            job = self.running_job()
            self.assertTrue(app.acquire_analysis_lock())
            self.assertEqual(app.recover_interrupted_analysis(), 0)
            with app.db() as connection:
                self.assertEqual(connection.execute('SELECT status FROM analysis_jobs WHERE id=?', (job,)).fetchone()[0], 'running')
                self.assertEqual(connection.execute('SELECT count(*) FROM analysis_locks').fetchone()[0], 1)

    def test_stale_lock_and_interrupted_job_are_recovered(self):
        with isolated_personal_os(), patch.object(app, 'RUNTIME_LEASED', True):
            job = self.running_job()
            app.acquire_analysis_lock()
            stale = (datetime.now(timezone.utc).astimezone() - timedelta(minutes=16)).isoformat(timespec='seconds')
            with app.db() as connection:
                connection.execute('UPDATE analysis_locks SET acquired_at=?', (stale,))
            self.assertEqual(app.recover_interrupted_analysis(), 1)
            with app.db() as connection:
                self.assertEqual(connection.execute('SELECT status FROM analysis_jobs WHERE id=?', (job,)).fetchone()[0], 'pending')
                self.assertEqual(connection.execute('SELECT count(*) FROM analysis_locks').fetchone()[0], 0)

    def test_no_runtime_lease_cannot_recover_other_workers(self):
        with isolated_personal_os(), patch.object(app, 'RUNTIME_LEASED', False):
            self.running_job()
            self.assertEqual(app.recover_interrupted_analysis(), 0)


if __name__ == '__main__':
    unittest.main()
