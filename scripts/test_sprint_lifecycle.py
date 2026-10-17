"""End-to-end sprint tests with real Git, SQLite and deterministic local coders."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scripts import sprint_auto as lifecycle
from scripts import sprint_auto_state as state
from scripts import task_runner as runner
from scripts.test_task_runner import SPEC, RepositoryCase


class LifecycleTests(RepositoryCase):
    def setUp(self):
        super().setUp()
        self.answers = {
            "goal_reviewed": True, "allowed_scopes": ["src/"],
            "policy": {"runner": "local-worker", "serial": True, "push": False, "fallback": "none",
                       "retries": 0, "accept_flags": False, "reset": False, "clean": False},
        }
        self.plan = {"sprint": {"slug": "result", "title": "Write result", "goal": "Verify execution"},
                     "tasks": [{"key": "result", "title": "Write verified result", "scope": ["src/result.txt"],
                                "spec_name": "result", "spec": SPEC, "depends_on": []}]}
        self.plan_path = self.root / "tmp" / "plan.json"
        self.plan_path.write_text(json.dumps(self.plan))
        self.answers_path = self.root / "tmp" / "answers.json"
        self.answers_path.write_text(json.dumps(self.answers))

    def run_lifecycle(self, resume=False):
        env = {k: v for k, v in os.environ.items() if k != "TASKS_ROOT"}
        return subprocess.run(
            [sys.executable, str(Path(lifecycle.__file__)), "--yolo-force", "--goal", "Verify execution",
             "--run-name", "result", "--root", str(self.root), "--answers", str(self.answers_path),
             "--plan", str(self.plan_path), *(["--resume"] if resume else [])], capture_output=True, text=True, env=env, timeout=30,
        )

    def test_complete_sprint_updates_main_tracker_and_checkpoint(self):
        proc = self.run_lifecycle()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["status"], "finished")
        self.assertEqual((self.root / "src/result.txt").read_text(), "verified")
        checkpoint = state.read_checkpoint(state.state_path(self.root, "result"))
        self.assertEqual(checkpoint.operation_intent, "finished")
        self.assertEqual(checkpoint.verified_commit, runner.git(self.root, "rev-parse", "main"))
        import sqlite3
        with sqlite3.connect(self.root / "tasks/tasks.db") as db:
            self.assertEqual(db.execute("SELECT status FROM nodes WHERE id='1.1'").fetchone()[0], "done")
        self.assertEqual(runner.git(self.root, "status", "--porcelain"), "")

    def test_failed_checks_preserve_worktree_and_main(self):
        self.config["checks"] = [[sys.executable, "-c", "raise SystemExit(1)"]]
        (self.root / runner.CONFIG_FILE).write_text(json.dumps(self.config))
        self.commit()
        before = runner.git(self.root, "rev-parse", "main")
        proc = self.run_lifecycle()
        self.assertNotEqual(proc.returncode, 0)
        report = json.loads(proc.stdout)
        self.assertEqual(report["status"], "execution_failed")
        self.assertEqual(runner.git(self.root, "rev-parse", "main"), before)
        self.assertTrue((Path(report["worktree"]) / "src/result.txt").exists())

    def test_finish_rejects_main_moved_after_verification(self):
        runner.git(self.root, "branch", "sprint/result")
        wt = self.root / "tmp/worktrees/result"
        runner.git(self.root, "worktree", "add", str(wt), "sprint/result")
        report = runner.execute_task(wt, self.spec, "result", "local-worker", ["src/"], self.config)
        checkpoint = state.RunState(run_name="result", initial_main_sha=self.base,
                                    verified_commit=report["commit"], allowed_scopes=["src/"],
                                    expected_refs={"worktree": str(wt), "sprint_branch": "sprint/result"})
        (self.root / "new-main").write_text("independent change")
        self.commit()
        with self.assertRaisesRegex(runner.RunnerError, "main moved"):
            lifecycle.finish(self.root, checkpoint, self.config)
        self.assertFalse((self.root / "src/result.txt").exists())

    def test_no_checks_stops_before_tracker_import(self):
        (self.root / runner.CONFIG_FILE).unlink()
        self.commit()
        proc = self.run_lifecycle()
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.root / "tasks/tasks.db").exists())

    def test_finished_run_resume_does_not_duplicate_tasks_or_commits(self):
        first = self.run_lifecycle()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        before = runner.git(self.root, "rev-parse", "main")
        second = self.run_lifecycle(resume=True)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertTrue(json.loads(second.stdout)["replay"])
        self.assertEqual(runner.git(self.root, "rev-parse", "main"), before)
        import sqlite3
        with sqlite3.connect(self.root / "tasks/tasks.db") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM nodes").fetchone()[0], 2)

    def test_resume_rejects_changed_approval(self):
        first = self.run_lifecycle()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.answers["allowed_scopes"].append("other/")
        self.answers_path.write_text(json.dumps(self.answers))
        second = self.run_lifecycle(resume=True)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("answers changed", second.stderr)

    def test_resume_rejects_dirty_worktree_without_discarding_it(self):
        first = self.run_lifecycle()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        worktree = Path(json.loads(first.stdout)["worktree"])
        (worktree / "src/result.txt").write_text("manual work")
        second = self.run_lifecycle(resume=True)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("contains changes", second.stderr)
        self.assertEqual((worktree / "src/result.txt").read_text(), "manual work")

    def test_lifecycle_uses_configured_default_when_policy_omits_runner(self):
        self.config["default_runner"] = "local-worker"
        (self.root / runner.CONFIG_FILE).write_text(json.dumps(self.config))
        self.commit()
        del self.answers["policy"]["runner"]
        self.answers_path.write_text(json.dumps(self.answers))
        proc = self.run_lifecycle()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        checkpoint = state.read_checkpoint(state.state_path(self.root, "result"))
        self.assertEqual(checkpoint.runner, "local-worker")
        self.assertEqual(json.loads(proc.stdout)["results"][0]["runner"], "local-worker")

    def test_delegated_runner_choice_is_used_by_execution(self):
        del self.answers["policy"]["runner"]
        self.answers["decisions"] = [{"id": "runner", "delegated": True,
                                     "safe_default": "local-worker", "rationale": "project adapter"}]
        self.answers_path.write_text(json.dumps(self.answers))
        proc = self.run_lifecycle()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        checkpoint = state.read_checkpoint(state.state_path(self.root, "result"))
        self.assertEqual(checkpoint.runner, "local-worker")

    def test_unconfigured_policy_runner_stops_before_import(self):
        self.answers["policy"]["runner"] = "unconfigured"
        self.answers_path.write_text(json.dumps(self.answers))
        proc = self.run_lifecycle()
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.root / "tasks/tasks.db").exists())
