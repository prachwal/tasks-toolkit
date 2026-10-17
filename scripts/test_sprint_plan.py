"""Planner regressions from outside the toolkit's installation directory."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import sprint_plan

SPEC = """## Goal
Write the public result.

## Files
- NEW public/result.txt

## Tests
Check the result contents.

## Acceptance
Run `cat public/result.txt`.

## Live check
Run `cat public/result.txt`.
"""


class SprintPlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        installed = self.directory / "toolkit" / "scripts"
        shutil.copytree(Path(sprint_plan.__file__).parent, installed, ignore=shutil.ignore_patterns("__pycache__"))
        self.script = installed / "sprint_plan.py"
        self.root = self.directory / "project"
        self.root.mkdir()
        (self.root / ".tasks-toolkit.json").write_text(json.dumps({"protected_paths": ["private"]}))
        self.plan = {"sprint": {"slug": "project-root", "title": "Project root", "goal": "Test the planner"},
                     "tasks": [{"key": "a", "title": "Write result", "spec_name": "project-result",
                                "spec": SPEC, "scope": ["public/result.txt"]}]}
        self.plan_file = self.directory / "plan.json"

    def cli(self, command, *args, cwd=None, override=None):
        self.plan_file.write_text(json.dumps(self.plan))
        env = {k: v for k, v in os.environ.items() if k not in ("TASKS_ROOT", "PYTHONPATH")}
        if override:
            env["TASKS_ROOT"] = str(override)
        return subprocess.run(
            [sys.executable, str(self.script), command, str(self.plan_file), *args],
            cwd=cwd or self.root, env=env, capture_output=True, text=True, timeout=30,
        )

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def init_git(self):
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        (self.root / ".gitignore").write_text("tmp/\ntasks/\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "fixture")

    def test_validation_reads_callers_project_configuration(self):
        self.plan["tasks"][0]["scope"] = ["private/data.txt"]
        proc = self.cli("validate", "--no-lint")
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertIn("protected path private", proc.stderr)

    def test_dot_prefixed_scope_cannot_bypass_protection(self):
        self.plan["tasks"][0]["scope"] = ["./private/data.txt"]
        proc = self.cli("validate", "--no-lint")
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertIn("protected path private", proc.stderr)

    def test_dot_prefixed_protected_configuration_is_effective(self):
        (self.root / ".tasks-toolkit.json").write_text('{"protected_paths": ["./private"]}')
        self.plan["tasks"][0]["scope"] = ["private/data.txt"]
        proc = self.cli("validate", "--no-lint")
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertIn("protected path private", proc.stderr)

    def test_invalid_protected_configuration_reports_validation_error(self):
        (self.root / ".tasks-toolkit.json").write_text('{"protected_paths": ["../private"]}')
        proc = self.cli("validate", "--no-lint")
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertIn("protected_paths", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_subdirectory_and_worktree_use_main_project_configuration(self):
        self.init_git()
        subdir = self.root / "nested"
        subdir.mkdir()
        worktree = self.directory / "worktree"
        self.git("worktree", "add", "-q", "-b", "worker", str(worktree))
        # Make it possible to detect whether the worktree's local config is incorrectly used.
        (worktree / ".tasks-toolkit.json").write_text('{"protected_paths": []}')
        self.plan["tasks"][0]["scope"] = ["private/data.txt"]
        for location in (subdir, worktree):
            with self.subTest(location=location):
                proc = self.cli("validate", "--no-lint", cwd=location)
                self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
                self.assertIn("protected path private", proc.stderr)

    def test_explicit_root_is_resolved_at_call_time(self):
        self.plan["tasks"][0]["scope"] = ["private/data.txt"]
        with mock.patch.dict(os.environ, {"TASKS_ROOT": str(self.root)}):
            errors = sprint_plan.validate_plan(self.plan, lint=False)
        self.assertTrue(any("protected path private" in error for error in errors), errors)

    def test_specs_and_tracker_use_override_while_helpers_use_installed_code(self):
        unrelated = self.directory / "unrelated"
        unrelated.mkdir()
        proc = self.cli("validate", cwd=unrelated, override=self.root)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = self.cli("specs", cwd=unrelated, override=self.root)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        spec = self.root / "tmp/tasks/project-result.md"
        self.assertEqual(spec.read_text(), SPEC)
        self.assertFalse((unrelated / "tmp").exists())
        proc = self.cli("validate", "--specs-written", cwd=unrelated, override=self.root)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = self.cli("apply", cwd=unrelated, override=self.root)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        with sqlite3.connect(self.root / "tasks/tasks.db") as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM nodes").fetchone()[0], 2)
        self.assertFalse((unrelated / "tasks").exists())
