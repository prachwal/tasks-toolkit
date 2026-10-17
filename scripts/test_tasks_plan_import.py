"""Tests for scripts/tasks_plan_import.py.

Run: PYTHONPATH=scripts python3 -m unittest test_tasks_plan_import
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tasks
import tasks_storage
import tasks_plan_import as tpi


class ImportPlanTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["VIBE_TASKS_ROOT"] = str(self.tmp)
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "tasks_init.py"),
             "--path", str(self.tmp / "tasks" / "tasks.db"), "--project", "T"],
            check=True, capture_output=True,
        )
        self.root = self.tmp
        self.plan = {
            "sprint": {"slug": "test-import", "title": "Test import sprint", "goal": "Test"},
            "tasks": [
                {"key": "a", "title": "Add a", "description": "first",
                 "depends_on": [], "scope": ["scripts/a.sh"],
                 "spec_name": "test-a",
                 "spec": "# Task: a\n\n## Goal\nDo a.\n\n## Files\n- EDIT: scripts/a.sh\n\n## Tests\nscripts/test_a.sh\n\n## Acceptance\n`bash scripts/test_a.sh`\n\n## Live check\n`bash scripts/a.sh`\n"},
                {"key": "b", "title": "Add b", "description": "second",
                 "depends_on": ["a"], "scope": ["scripts/b.sh"],
                 "spec_name": "test-b",
                 "spec": "# Task: b\n\n## Goal\nDo b.\n\n## Files\n- EDIT: scripts/b.sh\n\n## Tests\nscripts/test_b.sh\n\n## Acceptance\n`bash scripts/test_b.sh`\n\n## Live check\n`bash scripts/b.sh`\n"},
            ],
        }
        self.plan_path = self.tmp / "plan.json"
        self.plan_path.write_text(json.dumps(self.plan))

    def tearDown(self):
        import shutil
        if "VIBE_TASKS_ROOT" in os.environ:
            del os.environ["VIBE_TASKS_ROOT"]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_import(self, run_id="run-1", plan_path=None):
        pp = plan_path or self.plan_path
        return subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent / "scripts" / "tasks.py"),
             "import-plan", str(pp), "--run-id", run_id],
            capture_output=True, text=True,
            env={**os.environ, "VIBE_TASKS_ROOT": str(self.tmp)},
            cwd=str(Path(__file__).resolve().parent.parent),
        )

    def test_complete_import(self):
        proc = self._run_import()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        receipt = json.loads(proc.stdout)
        self.assertFalse(receipt["replay"])
        self.assertIn("sprint_id", receipt)
        self.assertIn("a", receipt["task_map"])
        self.assertIn("b", receipt["task_map"])

    def test_replay(self):
        self._run_import()
        proc = self._run_import()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        receipt = json.loads(proc.stdout)
        self.assertTrue(receipt["replay"])

    def test_replay_after_materialization_failure(self):
        self._run_import()
        view = self.tmp / "tasks" / "README.md"
        if view.exists():
            view.unlink()
        proc = self._run_import()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        receipt = json.loads(proc.stdout)
        self.assertTrue(receipt["replay"])
        self.assertTrue(view.exists())

    def test_changed_plan_rejection(self):
        self._run_import()
        changed = json.loads(self.plan_path.read_text())
        changed["tasks"][0]["title"] = "Changed title"
        changed_path = self.tmp / "plan2.json"
        changed_path.write_text(json.dumps(changed))
        proc = self._run_import(run_id="run-1", plan_path=changed_path)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("reused with different content", proc.stderr)

    def test_no_reinsert_on_replay(self):
        # redirect module constants so tasks.connect() hits the test db
        tasks.DATA_FILE = self.tmp / "tasks" / "tasks.db"
        tasks.VIEW_DIR = self.tmp / "tasks"
        tasks.README_FILE = tasks.VIEW_DIR / "README.md"
        tasks.VIEW_FILE = tasks.README_FILE
        tasks.LOCK_FILE = self.tmp / "tasks" / ".lock"
        tasks.SNAPSHOT_FILE = self.tmp / "tasks" / "tasks.snapshot.json"
        tasks.RECOVERY_DIR = self.tmp / "tasks" / "removed"
        tasks_storage.DATA_FILE = tasks.DATA_FILE
        tasks_storage.VIEW_DIR = tasks.VIEW_DIR
        tasks_storage.README_FILE = tasks.README_FILE
        tasks_storage.VIEW_FILE = tasks.VIEW_FILE
        tasks_storage.LOCK_FILE = tasks.LOCK_FILE
        tasks_storage.SNAPSHOT_FILE = tasks.SNAPSHOT_FILE
        tasks_storage.RECOVERY_DIR = tasks.RECOVERY_DIR
        self._run_import()
        conn = tasks.connect()
        count_before = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        conn.close()
        self._run_import()
        conn = tasks.connect()
        count_after = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        conn.close()
        self.assertEqual(count_before, count_after)


if __name__ == "__main__":
    unittest.main()
