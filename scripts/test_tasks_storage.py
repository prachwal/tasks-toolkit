"""Tests for scripts/tasks_storage.py.

Run: PYTHONPATH=scripts python3 -m unittest test_tasks_storage
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import tasks_storage as ts


class StorageTests(unittest.TestCase):

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        env = mock.patch.dict(os.environ, {"TASKS_TESTING": "1"})
        env.start()
        self.addCleanup(env.stop)
        view = self.tmp / "tasks"
        paths = mock.patch.multiple(
            ts, DATA_FILE=view / "tasks.db", VIEW_DIR=view,
            README_FILE=view / "README.md", VIEW_FILE=view / "README.md",
            LOCK_FILE=view / ".lock", SNAPSHOT_FILE=view / "tasks.snapshot.json",
            RECOVERY_DIR=view / "removed", BACKUP_DIR=self.tmp / "backup.d",
        )
        paths.start()
        self.addCleanup(paths.stop)
        view.mkdir()

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

    def test_testing_guard_blocks_paths_outside_fixture_temp_root(self):
        allowed = self.tmp / "allowed"
        outside = self.tmp / "outside" / "tasks.snapshot.json"
        with mock.patch.object(tempfile, "gettempdir", return_value=str(allowed)):
            ts._testing_guard(allowed / "tasks.snapshot.json")
            with self.assertRaises(ts.TaskError):
                ts._testing_guard(outside)
        self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()
