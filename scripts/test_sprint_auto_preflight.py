"""Tests for scripts/sprint_auto_preflight.py.

Run: PYTHONPATH=scripts python3 -m unittest test_sprint_auto_preflight
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
from scripts import sprint_auto_preflight as sap


class TestCollectPreflight(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "tasks").mkdir()
        self.root = self.tmp

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unreviewed_goal_returns_question(self):
        result = sap.collect_preflight(self.root, {"goal": "test"}, {})
        ids = [q["id"] for q in result["questions"]]
        self.assertIn("goal_review", ids)

    def test_missing_answers_exits_2_probe(self):
        proc = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).resolve().parent / "sprint_auto_preflight.py"),
             "preflight-probe", "--goal", "Inspect hashing behavior"],
            capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.assertEqual(proc.returncode, 2)
        data = json.loads(proc.stdout)
        self.assertTrue(len(data["questions"]) > 0)
        # no writes: stdout contains the full unresolved batch
        self.assertIn("goal_review", [q["id"] for q in data["questions"]])

    def test_forbidden_env_spec_lint_off(self):
        os.environ["SPEC_LINT"] = "off"
        try:
            result = sap.collect_preflight(self.root, {"goal": "g"}, {})
            self.assertTrue(any("SPEC_LINT" in b for b in result["blockers"]))
        finally:
            del os.environ["SPEC_LINT"]

    def test_forbidden_env_accept_checks(self):
        os.environ["ACCEPT_CHECKS_CMD"] = "exit 0"
        try:
            result = sap.collect_preflight(self.root, {"goal": "g"}, {})
            self.assertTrue(any("ACCEPT_CHECKS_CMD" in b for b in result["blockers"]))
        finally:
            del os.environ["ACCEPT_CHECKS_CMD"]

    def test_forbidden_env_verify_cmds(self):
        for var in ("JUNIE_VERIFY_CMD", "KILO_VERIFY_CMD", "CODEX_VERIFY_CMD"):
            os.environ[var] = "true"
            try:
                result = sap.collect_preflight(self.root, {"goal": "g"}, {})
                self.assertTrue(any(var in b for b in result["blockers"]), var)
            finally:
                del os.environ[var]

    def test_allowed_scopes_empty_returns_question(self):
        result = sap.collect_preflight(self.root, {"goal": "g"}, {"goal_reviewed": True})
        ids = [q["id"] for q in result["questions"]]
        self.assertIn("allowed_scopes", ids)

    def test_invalid_scope_blocker(self):
        result = sap.collect_preflight(self.root, {"goal": "g"}, {
            "goal_reviewed": True,
            "allowed_scopes": ["../escape", "/abs"],
        })
        self.assertTrue(any("invalid scope" in b for b in result["blockers"]))

    def test_delegated_default(self):
        answers = {
            "goal_reviewed": True,
            "allowed_scopes": ["src/"],
            "decisions": [{"id": "runner", "delegated": True, "safe_default": "junie", "rationale": "default"}],
        }
        result = sap.collect_preflight(self.root, {"goal": "g"}, answers)
        self.assertTrue(any(d["id"] == "runner" for d in result["delegated_defaults"]))

    def test_missing_policy_returns_questions(self):
        result = sap.collect_preflight(self.root, {"goal": "g"}, {
            "goal_reviewed": True,
            "allowed_scopes": ["src/"],
        })
        ids = [q["id"] for q in result["questions"]]
        self.assertIn("runner", ids)

    def test_approval_scope_structure(self):
        result = sap.collect_preflight(self.root, {"goal": "my goal"}, {
            "goal_reviewed": True,
            "allowed_scopes": ["src/"],
            "policy": {"runner": "junie", "serial": True, "push": False},
        })
        scope = result["approval_scope"]
        self.assertEqual(scope["goal"], "my goal")
        self.assertTrue(scope["goal_reviewed"])
        self.assertEqual(scope["allowed_scopes"], ["src/"])

    def test_no_writes_to_root(self):
        before = set(self.root.rglob("*"))
        sap.collect_preflight(self.root, {"goal": "g"}, {})
        after = set(self.root.rglob("*"))
        self.assertEqual(before, after)

    def test_git_staged_generated_blocker(self):
        # create a git repo with staged generated file
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.root, check=True)
        (self.root / "tasks").mkdir(exist_ok=True)
        (self.root / "tasks" / "tasks.snapshot.json").write_text("{}")
        subprocess.run(["git", "add", "tasks/tasks.snapshot.json"], cwd=self.root, check=True)
        result = sap.collect_preflight(self.root, {"goal": "g"}, {})
        self.assertTrue(any("staged generated" in b for b in result["blockers"]))

    def test_sqlite_ro_mode(self):
        db = self.root / "tasks" / "tasks.db"
        db.parent.mkdir(exist_ok=True)
        import sqlite3
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE nodes (id TEXT)")
        conn.commit()
        conn.close()
        # should not raise
        result = sap.collect_preflight(self.root, {"goal": "g"}, {})
        self.assertIsInstance(result, dict)


if __name__ == "__main__":
    unittest.main()
