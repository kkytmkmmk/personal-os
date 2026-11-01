import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class StartupPortTests(unittest.TestCase):
    def run_app(self, database, port):
        environment = dict(os.environ, PERSONAL_OS_ENV='production', PERSONAL_OS_PORT=str(port), PERSONAL_OS_HOST='127.0.0.1', PERSONAL_OS_DB_PATH=str(database))
        return subprocess.run([sys.executable, str(ROOT/'app.py')], env=environment, cwd=ROOT, capture_output=True, text=True, timeout=15)

    def test_old_port_is_rejected_before_database_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp)/'must-not-create.db'
            result = self.run_app(database, 8787)
            self.assertEqual(result.returncode, 2)
            self.assertIn('fixed port 8788', result.stdout)
            self.assertFalse(database.exists())

    def test_occupied_port_does_not_mutate_database(self):
        with socket.socket() as listener:
            try:
                listener.bind(('127.0.0.1', 8788))
                listener.listen()
            except OSError:
                self.skipTest('Production port already in use; do not touch the live service')
            with tempfile.TemporaryDirectory() as temp:
                database = Path(temp)/'must-not-create.db'
                result = self.run_app(database, 8788)
                self.assertEqual(result.returncode, 1)
                self.assertIn('database was not opened', result.stdout)
                self.assertFalse(database.exists())


if __name__ == '__main__':
    unittest.main()
