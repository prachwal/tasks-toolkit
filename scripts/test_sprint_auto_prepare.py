"""Tests for scripts/sprint_auto_prepare.py.

Run: PYTHONPATH=scripts python3 -m unittest test_sprint_auto_prepare
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import tasks, tasks_storage


class PrepareTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["TASKS_ROOT"] = str(self.tmp)
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "tasks_init.py"),
             "--path", str(self.tmp / "tasks" / "tasks.db"), "--project", "T"],
            check=True, capture_output=True,
        )
        # init git repo
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.tmp, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=self.tmp, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.tmp, check=True)
        (self.tmp / "README.md").write_text("init")
        (self.tmp / ".gitignore").write_text("tmp/\ntasks/*.db\ntasks/.lock\ntasks/removed/\n")
        scripts_dir = self.tmp / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "add", "-A"], cwd=self.tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.tmp, check=True)
        self.root = self.tmp
        self.answers = {
            "goal_reviewed": True,
            "allowed_scopes": ["scripts/"],
            "policy": {"runner": "codex", "serial": True, "push": False,
                       "fallback": "none", "retries": 0, "accept_flags": False,
                       "reset": False, "clean": False},
        }
        self.plan = {
            "sprint": {"slug": "test-prep", "title": "Test prep sprint", "goal": "Test"},
            "tasks": [
                {"key": "a", "title": "Add a", "description": "first",
                 "depends_on": [], "scope": ["scripts/a.sh"],
                 "spec_name": "test-a",
                 "spec": "# Task: a\n\n## Goal\nDo a.\n\n## Files\n- EDIT: scripts/a.sh\n\n## Tests\nscripts/test_a.sh\n\n## Acceptance\n`bash scripts/test_a.sh`\n\n## Live check\n`bash scripts/a.sh`\n"},
            ],
        }
        self.plan_path = self.tmp / "tmp" / "plan.json"
        self.plan_path.parent.mkdir(parents=True, exist_ok=True)
        self.plan_path.write_text(json.dumps(self.plan))

    def tearDown(self):
        import shutil
        if "TASKS_ROOT" in os.environ:
            del os.environ["TASKS_ROOT"]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_prepare(self, run_name="run-1", plan_path=None, goal="Test goal"):
        pp = plan_path or self.plan_path
        ans_path = self.tmp / "tmp" / "answers.json"
        ans_path.parent.mkdir(parents=True, exist_ok=True)
        ans_path.write_text(json.dumps(self.answers))
        return subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "sprint_auto_prepare.py"),
             "--root", str(self.tmp), "--run-name", run_name, "--goal", goal,
             "--answers", str(ans_path), "--plan", str(pp)],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )

    def test_valid_prepare(self):
        proc = self._run_prepare()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["status"], "prepared")
        self.assertIn("import_receipt", result)
        self.assertFalse(result["import_receipt"]["replay"])

    def test_resume_after_import_before_branch(self):
        # First prepare creates sprint + branch
        proc = self._run_prepare()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result1 = json.loads(proc.stdout)
        sprint_id = result1["import_receipt"]["sprint_id"]
        # Verify tracker has the sprint
        tasks.DATA_FILE = self.tmp / "tasks" / "tasks.db"
        tasks_storage.DATA_FILE = tasks.DATA_FILE
        conn = tasks.connect()
        node = conn.execute("SELECT id, title FROM nodes WHERE id=?", (sprint_id,)).fetchone()
        self.assertIsNotNone(node)
        conn.close()

    def test_invalid_plan_missing_title(self):
        bad_plan = json.loads(self.plan_path.read_text())
        bad_plan["sprint"]["title"] = ""
        bad_path = self.tmp / "tmp" / "bad_plan.json"
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        bad_path.write_text(json.dumps(bad_plan))
        proc = self._run_prepare(plan_path=bad_path)
        self.assertNotEqual(proc.returncode, 0)
        result = json.loads(proc.stdout)
        self.assertEqual(result["status"], "invalid_plan")

    def test_scope_violation(self):
        bad_plan = json.loads(self.plan_path.read_text())
        bad_plan["tasks"][0]["scope"] = ["/etc/passwd"]
        bad_path = self.tmp / "tmp" / "bad_scope.json"
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        bad_path.write_text(json.dumps(bad_plan))
        proc = self._run_prepare(plan_path=bad_path)
        # scope violation should be caught (path validation in preflight or scope check)
        # The scope /etc/passwd starts with / which is invalid
        self.assertNotEqual(proc.returncode, 0)

    def test_missing_answers(self):
        self.answers = {}  # no goal_reviewed
        proc = self._run_prepare()
        self.assertNotEqual(proc.returncode, 0)
        result = json.loads(proc.stdout)
        self.assertIn("needs_answers", result["status"])

    def test_no_plan(self):
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "sprint_auto_prepare.py"),
             "--root", str(self.tmp), "--run-name", "run-x", "--goal", "g",
             "--answers", str(self.tmp / "answers.json")],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        # no --plan and no existing plan file → needs_plan
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
