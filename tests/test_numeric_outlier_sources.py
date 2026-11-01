import json
import sqlite3
import unittest
import app


class NumericOutlierSourceTests(unittest.TestCase):
    def history(self, states):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        connection.executescript('CREATE TABLE facts(id INTEGER,fact_key TEXT,value_json TEXT,summary TEXT,personal_relevance TEXT,retrieval_eligibility TEXT); CREATE TABLE fact_reviews(fact_id INTEGER,state TEXT);')
        for index, state in enumerate(states):
            connection.execute('INSERT INTO facts VALUES(?,?,?,?,?,?)',
                               (index, 'synthetic.metric', json.dumps({'amount': 1000, 'currency': 'JPY'}),
                                'Synthetic amount', 'personal', state))
            connection.execute('INSERT INTO fact_reviews VALUES(?,?)', (index, 'confirmed'))
        return connection

    def test_quarantined_confirmed_reviews_do_not_pollute_outlier_baseline(self):
        with self.history(['excluded', 'pending', 'conflict']) as connection:
            self.assertFalse(app.fact_has_numeric_outlier(connection,
                {'id': 99, 'fact_key': 'synthetic.metric', 'summary': 'Synthetic amount'},
                {'amount': 1_000_000_000, 'currency': 'JPY'}))

    def test_trusted_history_still_detects_large_outlier(self):
        with self.history(['eligible'] * 3) as connection:
            self.assertTrue(app.fact_has_numeric_outlier(connection,
                {'id': 99, 'fact_key': 'synthetic.metric', 'summary': 'Synthetic amount'},
                {'amount': 1_000_000_000, 'currency': 'JPY'}))


if __name__ == '__main__':
    unittest.main()
