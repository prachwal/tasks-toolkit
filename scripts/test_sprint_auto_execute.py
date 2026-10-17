"""Tests for scripts/sprint_auto_execute.py.

Run: PYTHONPATH=scripts python3 -m unittest test_sprint_auto_execute
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sprint_auto_execute as sae


class ExecuteTests(unittest.TestCase):

    def test_scope_conformity_ok(self):
        ok, violations = sae.check_scope_conformity(
            ["scripts/a.sh", "src/b.cs"], ["scripts/", "src/"]
        )
        self.assertTrue(ok)
        self.assertEqual(violations, [])

    def test_scope_conformity_violation(self):
        ok, violations = sae.check_scope_conformity(
            ["scripts/a.sh", "/etc/passwd"], ["scripts/"]
        )
        self.assertFalse(ok)
        self.assertIn("/etc/passwd", violations)

    def test_scope_conformity_empty_allowed(self):
        # No allowed scopes → everything conforms (no restriction)
        ok, violations = sae.check_scope_conformity(["any/file.py"], [])
        self.assertTrue(ok)

    def test_tracker_done_insufficient(self):
        self.assertTrue(sae.verify_tracker_done_insufficient("1.1"))

    def test_auto_accept_all_criteria(self):
        accept, reason = sae.auto_accept_decision(True, True, 0, True)
        self.assertTrue(accept)
        self.assertEqual(reason, "all criteria met")

    def test_auto_accept_scope_violation(self):
        accept, reason = sae.auto_accept_decision(False, True, 0, True)
        self.assertFalse(accept)
        self.assertEqual(reason, "scope violation")

    def test_auto_accept_checks_failed(self):
        accept, reason = sae.auto_accept_decision(True, False, 0, True)
        self.assertFalse(accept)
        self.assertEqual(reason, "checks failed")

    def test_auto_accept_red_flags(self):
        accept, reason = sae.auto_accept_decision(True, True, 2, True)
        self.assertFalse(accept)
        self.assertEqual(reason, "2 red flag(s)")

    def test_auto_accept_tracker_not_done(self):
        accept, reason = sae.auto_accept_decision(True, True, 0, False)
        self.assertFalse(accept)
        self.assertEqual(reason, "tracker not done")

    def test_execute_task_loop_all_accepted(self):
        tasks = [
            {"key": "a", "task_id": "1.1", "files": ["scripts/a.sh"], "checks_green": True, "red_flags": 0, "tracker_done": True},
        ]
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        state_path = tmp / "state.json"
        import sprint_auto_state as sas
        state = sas.RunState(run_name="test", allowed_scopes=["scripts/"])
        sas.write_checkpoint(state_path, state)
        state = sas.read_checkpoint(state_path)
        results = sae.execute_task_loop(tmp, "1", tasks, state, ["scripts/"])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["accept"])
        self.assertEqual(results[0]["status"], "accepted")

    def test_execute_task_loop_scope_violation(self):
        tasks = [
            {"key": "a", "task_id": "1.1", "files": ["/etc/passwd"], "checks_green": True, "red_flags": 0, "tracker_done": True},
        ]
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        import sprint_auto_state as sas
        state = sas.RunState(run_name="test2", allowed_scopes=["scripts/"])
        results = sae.execute_task_loop(tmp, "1", tasks, state, ["scripts/"])
        self.assertEqual(results[0]["status"], "scope_violation")
        self.assertFalse(results[0]["accept"])

    def test_execute_task_loop_red_flags(self):
        tasks = [
            {"key": "a", "task_id": "1.1", "files": ["scripts/a.sh"], "checks_green": True, "red_flags": 1, "tracker_done": True},
        ]
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        import sprint_auto_state as sas
        state = sas.RunState(run_name="test3", allowed_scopes=["scripts/"])
        results = sae.execute_task_loop(tmp, "1", tasks, state, ["scripts/"])
        self.assertEqual(results[0]["status"], "rejected")
        self.assertFalse(results[0]["accept"])


if __name__ == "__main__":
    unittest.main()
