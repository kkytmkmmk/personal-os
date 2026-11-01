import json
import unittest
from unittest.mock import patch
import app
from tests.test_requirements_cycle import isolated_personal_os, add_entry
from tools.refresh_search_index import process_batch, pending


class SearchIndexRefreshTests(unittest.TestCase):
    def test_batch_is_bounded_and_idempotent(self):
        with isolated_personal_os():
            add_entry('Synthetic index A', '私はSample温泉が好きです。')
            add_entry('Synthetic index B', '私はSample旅行を検討しています。')
            self.assertEqual(pending(), 2)
            self.assertEqual(process_batch(1), 1)
            self.assertEqual(pending(), 1)
            self.assertEqual(process_batch(128), 1)
            self.assertEqual(process_batch(), 0)
            with app.db() as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM embeddings').fetchone()[0], 2)
                vector = json.loads(connection.execute('SELECT vector_json FROM embeddings LIMIT 1').fetchone()[0])
                self.assertEqual(len(vector), app.EMBEDDING_DIMENSIONS)

    def test_failed_calculation_is_retained_for_retry(self):
        with isolated_personal_os():
            add_entry('Synthetic retry', '私はSample温泉が好きです。')
            with patch.object(app, 'local_embedding', side_effect=ValueError('synthetic failure')):
                with self.assertRaises(RuntimeError):
                    process_batch()
            self.assertEqual(pending(), 1)
            self.assertEqual(process_batch(), 1)
            self.assertEqual(pending(), 0)

    def test_running_job_owned_by_another_worker_is_not_claimed(self):
        with isolated_personal_os():
            add_entry('Synthetic active worker', '私はSample温泉が好きです。')
            with app.db() as connection:
                connection.execute("UPDATE embedding_jobs SET state='running'")
            self.assertEqual(process_batch(), 0)


if __name__ == '__main__':
    unittest.main()
