"""Execution regressions: input declarations never count as verification evidence."""
from __future__ import annotations

from unittest import mock

from scripts import sprint_auto_execute as execute
from scripts import sprint_auto_state as state
from scripts.test_task_runner import RepositoryCase


class ExecuteTests(RepositoryCase):
    def test_scope_requires_approval_and_rejects_ancestor(self):
        for files, scopes in [(["src/a.py"], []), (["src"], ["src/a.py"]), (["src/../outside"], ["src/"])]:
            with self.subTest(files=files):
                self.assertFalse(execute.check_scope_conformity(files, scopes)[0])
        self.assertTrue(execute.check_scope_conformity(["src/a.py"], ["src/"])[0])

    def test_declared_success_does_not_bypass_failed_checks(self):
        checkpoint = state.RunState(run_name="test", runner="local-worker")
        self.config["checks"] = [["false"]]
        task = {"key": "result", "task_id": "1.1", "spec_name": "result", "scope": ["src/"],
                "checks_green": True, "red_flags": 0, "tracker_done": True}
        with mock.patch.object(execute, "tracker", return_value='{"uid":"T-test", "depends_on":[]}'):
            reports = execute.execute_task_loop(self.root, "1", [task], checkpoint, ["src/"], self.config)
        self.assertFalse(reports[0]["accept"])
        self.assertEqual(reports[0]["status"], "checks_failed")
        self.assertEqual(checkpoint.completed_tasks, {})

    def test_cross_sprint_task_is_rejected_before_running_coder(self):
        checkpoint = state.RunState(run_name="test")
        task = {"key": "result", "task_id": "2.1", "spec_name": "result", "scope": ["src/"]}
        reports = execute.execute_task_loop(self.root, "1", [task], checkpoint, ["src/"], self.config)
        self.assertFalse(reports[0]["accept"])
        self.assertFalse((self.root / "src/result.txt").exists())
