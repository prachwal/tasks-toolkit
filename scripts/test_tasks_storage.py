"""Tests for scripts/tasks_storage.py.

Run: PYTHONPATH=scripts python3 -m unittest test_tasks_storage
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from scripts import tasks_storage as ts


class StorageTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["TASKS_TESTING"] = "1"
        ts.DATA_FILE = self.tmp / "tasks" / "tasks.db"
        ts.VIEW_DIR = self.tmp / "tasks"
        ts.README_FILE = ts.VIEW_DIR / "README.md"
        ts.VIEW_FILE = ts.README_FILE
        ts.LOCK_FILE = self.tmp / "tasks" / ".lock"
        ts.SNAPSHOT_FILE = self.tmp / "tasks" / "tasks.snapshot.json"
        ts.RECOVERY_DIR = self.tmp / "tasks" / "removed"
        ts.BACKUP_DIR = self.tmp / "backup.d"
        (self.tmp / "tasks").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_connect_creates_db(self):
        conn = ts.connect()
        self.assertTrue(ts.DATA_FILE.exists())
        conn.close()

    def test_load_empty(self):
        conn = ts.connect()
        data = ts.load(conn)
        self.assertEqual(data["sprints"], [])
        conn.close()

    def test_snapshot_json_deterministic(self):
        data = {"version": 2, "project": "T", "sprints": []}
        j1 = ts.snapshot_json(data)
        j2 = ts.snapshot_json(data)
        self.assertEqual(j1, j2)
        self.assertTrue(j1.endswith("\n"))

    def test_write_atomic(self):
        path = self.tmp / "test.txt"
        ts._write_atomic(path, "hello")
        self.assertEqual(path.read_text(), "hello")

    def test_testing_guard_blocks_real_path(self):
        import os
        os.environ["TASKS_TESTING"] = "1"
        path = Path("/home/prachwal/src/emulators/vibe-emulator-001/tasks/tasks.snapshot.json")
        with self.assertRaises(ts.TaskError):
            ts._testing_guard(path)


if __name__ == "__main__":
    unittest.main()
