"""Tests for scripts/sprint_auto_resume.py.

Run: PYTHONPATH=scripts python3 -m unittest test_sprint_auto_resume
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import sprint_auto_resume as sar
from scripts import sprint_auto_state as sas


class ResumeTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reconcile_no_checkpoint(self):
        result = sar.reconcile_checkpoint(self.tmp, "nonexistent")
        self.assertEqual(result["status"], "no_checkpoint")

    def test_reconcile_basic(self):
        state = sas.RunState(run_name="r1", operation_intent="execute_task:a")
        sas.write_checkpoint(self.tmp / "state.json", state)
        # Need state at the right path
        sp = sas.state_path(self.tmp, "r1")
        sp.parent.mkdir(parents=True, exist_ok=True)
        sas.write_checkpoint(sp, state)
        result = sar.reconcile_checkpoint(self.tmp, "r1")
        self.assertEqual(result["status"], "reconciled")
        self.assertIn("resume task execution", result["actions"][0] if result["actions"] else "")

    def test_reconcile_child_running(self):
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        state = sas.RunState(run_name="r2", child_pid=proc.pid)
        sp = sas.state_path(self.tmp, "r2")
        sp.parent.mkdir(parents=True, exist_ok=True)
        sas.write_checkpoint(sp, state)
        result = sar.reconcile_checkpoint(self.tmp, "r2")
        self.assertEqual(result["status"], "child_running")
        self.assertTrue(result["child_alive"])
        proc.kill()
        proc.wait()

    def test_finish_guard_ok(self):
        ok, reason = sar.finish_guard(self.tmp, "r1", "", "")
        self.assertFalse(ok)

    def test_recognize_existing_merge_no_branch(self):
        # No git repo → False
        self.assertFalse(sar.recognize_existing_merge(self.tmp, "nonexistent"))

    def test_recognize_existing_merge_with_git(self):
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.tmp, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=self.tmp, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.tmp, check=True)
        (self.tmp / "f.txt").write_text("x")
        subprocess.run(["git", "add", "-A"], cwd=self.tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.tmp, check=True)
        # Create and merge a branch
        subprocess.run(["git", "checkout", "-q", "-b", "sprint/1-test"], cwd=self.tmp, check=True)
        (self.tmp / "s.txt").write_text("sprint")
        subprocess.run(["git", "add", "-A"], cwd=self.tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "sprint work"], cwd=self.tmp, check=True)
        subprocess.run(["git", "checkout", "-q", "main"], cwd=self.tmp, check=True)
        subprocess.run(["git", "merge", "--no-ff", "sprint/1-test", "-m", "merge"], cwd=self.tmp, check=True)
        self.assertTrue(sar.recognize_existing_merge(self.tmp, "sprint/1-test"))

    def test_reconcile_merge_exists(self):
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.tmp, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=self.tmp, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.tmp, check=True)
        (self.tmp / "f.txt").write_text("x")
        subprocess.run(["git", "add", "-A"], cwd=self.tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.tmp, check=True)
        subprocess.run(["git", "checkout", "-q", "-b", "sprint/1-test"], cwd=self.tmp, check=True)
        (self.tmp / "s.txt").write_text("sprint")
        subprocess.run(["git", "add", "-A"], cwd=self.tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "sprint work"], cwd=self.tmp, check=True)
        subprocess.run(["git", "checkout", "-q", "main"], cwd=self.tmp, check=True)
        subprocess.run(["git", "merge", "--no-ff", "sprint/1-test", "-m", "merge"], cwd=self.tmp, check=True)
        state = sas.RunState(run_name="r3", operation_intent="finish", expected_refs={"sprint_branch": "sprint/1-test"})
        sp = sas.state_path(self.tmp, "r3")
        sp.parent.mkdir(parents=True, exist_ok=True)
        sas.write_checkpoint(sp, state)
        result = sar.reconcile_checkpoint(self.tmp, "r3")
        self.assertTrue(result["merge_exists"])
        self.assertIn("post-merge verification", " ".join(result["actions"]))


if __name__ == "__main__":
    unittest.main()
