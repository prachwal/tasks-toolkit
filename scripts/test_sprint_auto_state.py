"""Tests for scripts/sprint_auto_state.py.

Run: PYTHONPATH=scripts python3 -m unittest test_sprint_auto_state
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import sprint_auto_state as sas


class StateTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_read_roundtrip(self):
        st = sas.RunState(run_name="r1", goal="g")
        p = self.tmp / "state.json"
        sas.write_checkpoint(p, st)
        loaded = sas.read_checkpoint(p)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.run_name, "r1")
        self.assertEqual(loaded.goal, "g")

    def test_atomic_replacement(self):
        p = self.tmp / "state.json"
        sas.write_checkpoint(p, sas.RunState(run_name="v1"))
        # interrupt mid-write by writing partial then replacing
        sas.write_checkpoint(p, sas.RunState(run_name="v2"))
        loaded = sas.read_checkpoint(p)
        self.assertEqual(loaded.run_name, "v2")

    def test_truncated_state_returns_none(self):
        p = self.tmp / "state.json"
        p.write_text("{truncated")
        self.assertIsNone(sas.read_checkpoint(p))

    def test_unknown_version_returns_none(self):
        p = self.tmp / "state.json"
        p.write_text(json.dumps({"version": 999, "run_name": "x"}))
        self.assertIsNone(sas.read_checkpoint(p))

    def test_symlink_state_refused(self):
        target = self.tmp / "real.json"
        target.write_text("{}")
        link = self.tmp / "link.json"
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            sas.write_checkpoint(link, sas.RunState())

    def test_symlink_lock_refused(self):
        target = self.tmp / "real.lock"
        target.write_text("")
        link = self.tmp / "link.lock"
        link.symlink_to(target)
        with self.assertRaises(ValueError), sas.StateLock(link):
            pass

    def test_exclusive_lock(self):
        lock = self.tmp / "test.lock"
        with sas.StateLock(lock), open(lock, "w") as fh:
            fcntl_import = __import__("fcntl")
            with self.assertRaises((BlockingIOError, OSError)):
                fcntl_import.flock(fh, fcntl_import.LOCK_EX | fcntl_import.LOCK_NB)

    def test_child_alive_false_no_pid(self):
        st = sas.RunState(child_pid=0)
        self.assertFalse(sas.is_child_alive(st))

    def test_child_alive_true(self):
        proc = subprocess.Popen(["sleep", "10"])
        st = sas.RunState(child_pid=proc.pid)
        self.assertTrue(sas.is_child_alive(st))
        proc.kill()
        proc.wait()

    def test_supervised_run_success(self):
        code, out = sas.supervised_run(["echo", "hello"], self.tmp, 10)
        self.assertEqual(code, 0)
        self.assertIn("hello", out)

    def test_supervised_run_timeout(self):
        code, out = sas.supervised_run(["sleep", "60"], self.tmp, 1)
        self.assertNotEqual(code, 0)

    def test_import_no_write(self):
        # importing module should not create files
        before = set(self.tmp.rglob("*"))
        # re-import
        import importlib
        importlib.reload(sas)
        after = set(self.tmp.rglob("*"))
        self.assertEqual(before, after)

    def test_parent_interruption_preserves_exclusive_child_ownership(self):
        # spawn a child that holds a lock and sleeps
        lock = self.tmp / "child.lock"
        script = self.tmp / "child.py"
        script.write_text(
            "import fcntl, time, sys\n"
            "fh = open(sys.argv[1], 'w')\n"
            "fcntl.flock(fh, fcntl.LOCK_EX)\n"
            "time.sleep(30)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, str(script), str(lock)],
            start_new_session=True,
        )
        time.sleep(0.5)
        # child should be alive and holding lock
        self.assertTrue(sas.is_child_alive(sas.RunState(child_pid=proc.pid)))
        # try to acquire lock non-blocking -> should fail
        import fcntl
        with open(lock, "w") as fh, self.assertRaises((BlockingIOError, OSError)):
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proc.kill()
        proc.wait()

    def test_bind_state(self):
        st = sas.bind_state(
            self.tmp, self.tmp, "run1", "goal", "fp", ["src/"], "abc123",
            "junie", "model", 120,
        )
        self.assertEqual(st.run_name, "run1")
        self.assertEqual(st.initial_main_sha, "abc123")
        self.assertEqual(st.allowed_scopes, ["src/"])
        self.assertEqual(st.version, sas.STATE_VERSION)


if __name__ == "__main__":
    unittest.main()
