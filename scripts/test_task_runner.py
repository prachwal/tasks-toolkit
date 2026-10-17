"""Integration tests using local Python coders, real Git repositories and checks."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import task_runner as runner

SPEC = """## Goal
Write the requested result.

## Files
- NEW src/result.txt

## Tests
Check the exact file contents.

## Acceptance
Run `python3 check.py`.

## Live check
Run `cat src/result.txt`.
"""


class RepositoryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        runner.git(self.root, "init", "-q", "-b", "main")
        runner.git(self.root, "config", "user.email", "test@example.com")
        runner.git(self.root, "config", "user.name", "Test")
        (self.root / ".gitignore").write_text("tmp/\ntasks/\n__pycache__/\n")
        (self.root / "coder.py").write_text(
            "import sys\nfrom pathlib import Path\nsys.stdin.read()\n"
            "Path('src').mkdir(exist_ok=True)\nPath('src/result.txt').write_text('verified')\n"
        )
        (self.root / "check.py").write_text(
            "from pathlib import Path\nassert Path('src/result.txt').read_text() == 'verified'\n"
        )
        self.config = {"checks": [[sys.executable, "check.py"]],
                       "runners": {"local-worker": [sys.executable, "coder.py"]}, "timeout": 10}
        (self.root / runner.CONFIG_FILE).write_text(json.dumps(self.config))
        self.commit()
        self.base = runner.git(self.root, "rev-parse", "HEAD")
        self.spec = self.root / "tmp" / "tasks" / "result.md"
        self.spec.parent.mkdir(parents=True)
        self.spec.write_text(SPEC)

    def commit(self):
        runner.git(self.root, "add", "-A")
        runner.git(self.root, "commit", "-qm", "fixture")

    def change_coder(self, code):
        (self.root / "coder.py").write_text(code)
        self.commit()
        self.base = runner.git(self.root, "rev-parse", "HEAD")

    def execute(self):
        return runner.execute_task(self.root, self.spec, "result", "local-worker", ["src/"], self.config)


class RunnerTests(RepositoryCase):
    def test_real_checks_precede_commit(self):
        report = self.execute()
        self.assertTrue(report["accept"], report)
        self.assertEqual(report["files"], ["src/result.txt"])
        self.assertNotEqual(report["commit"], self.base)
        self.assertEqual(runner.git(self.root, "status", "--porcelain"), "")

    def test_failed_check_never_commits(self):
        self.config["checks"] = [[sys.executable, "-c", "raise SystemExit(1)"]]
        report = self.execute()
        self.assertFalse(report["accept"])
        self.assertEqual(runner.git(self.root, "rev-parse", "HEAD"), self.base)
        self.assertTrue((self.root / "src/result.txt").exists())

    def test_actual_untracked_out_of_scope_file_rejected(self):
        self.change_coder("from pathlib import Path\nPath('outside.txt').write_text('unexpected')\n")
        report = self.execute()
        self.assertFalse(report["accept"])
        self.assertEqual(report["scope_violations"], ["outside.txt"])
        self.assertEqual(runner.git(self.root, "rev-parse", "HEAD"), self.base)

    def test_deleted_file_is_checked_against_scope(self):
        self.change_coder("from pathlib import Path\nPath('check.py').unlink()\n")
        report = self.execute()
        self.assertFalse(report["accept"])
        self.assertIn("check.py", report["scope_violations"])

    def test_check_mutating_verified_content_is_rejected(self):
        self.config["checks"] = [[sys.executable, "-c", "from pathlib import Path; Path('src/result.txt').write_text('changed')"]]
        report = self.execute()
        self.assertFalse(report["accept"])
        self.assertTrue(report["checks_changed_files"])
        self.assertEqual(runner.git(self.root, "rev-parse", "HEAD"), self.base)

    def test_agent_commit_is_not_trusted(self):
        self.change_coder(
            "from pathlib import Path\nimport subprocess\nPath('src').mkdir()\n"
            "Path('src/result.txt').write_text('verified')\n"
            "subprocess.run(['git','add','src'], check=True)\n"
            "subprocess.run(['git','commit','-qm','unverified'], check=True)\n"
        )
        report = self.execute()
        self.assertFalse(report["accept"])
        self.assertEqual(report["status"], "unexpected_commit")

    def test_missing_check_configuration_is_rejected(self):
        (self.root / runner.CONFIG_FILE).write_text('{"checks": []}')
        with self.assertRaises(runner.RunnerError):
            runner.load_config(self.root)

    def test_runner_failure_preserves_work(self):
        self.change_coder("from pathlib import Path\nPath('partial').write_text('keep')\nraise SystemExit(3)\n")
        report = self.execute()
        self.assertEqual(report["exit_code"], 3)
        self.assertEqual((self.root / "partial").read_text(), "keep")

    def test_timeout_cannot_report_success_even_when_sigterm_exits_zero(self):
        from scripts import sprint_auto_state as state
        code, _ = state.supervised_run(
            [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, lambda *args: exit(0)); time.sleep(30)"],
            self.root, 1,
        )
        self.assertEqual(code, 124)

    def test_configured_junie_and_kilo_adapters_execute(self):
        for name in ("junie", "kilo"):
            with self.subTest(runner=name):
                self.config["runners"][name] = [sys.executable, "coder.py"]
                runner.git(self.root, "branch", name)
                wt = self.root / "tmp" / name
                runner.git(self.root, "worktree", "add", str(wt), name)
                report = runner.execute_task(wt, self.spec, name, name, ["src/"], self.config)
                self.assertTrue(report["accept"], report)
        with self.assertRaises(runner.RunnerError):
            runner.runner_command({"runners": {}}, "junie")

    def test_reports_remain_readable_by_delivery_statistics(self):
        from scripts import run_stats
        report = self.execute()
        runner.write_report(self.root, "result", report)
        stats = run_stats.load_runs(self.root / "tmp/runs")
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["verdict"], "verified")
        self.assertEqual(stats[0]["runner"], "local-worker")

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(Path(runner.__file__)), "result", "--root", str(self.root),
             "--file", str(self.spec), "--scope", "src/", *args],
            cwd=self.root, capture_output=True, text=True, timeout=30,
        )

    def save_config(self):
        (self.root / runner.CONFIG_FILE).write_text(json.dumps(self.config))
        self.commit()

    def test_cli_uses_configured_default_runner(self):
        self.config["default_runner"] = "local-worker"
        self.save_config()
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["runner"], "local-worker")

    def test_cli_override_selects_arbitrary_registered_name(self):
        self.config["default_runner"] = "local-worker"
        self.config["runners"]["private-adapter-42"] = [sys.executable, "coder.py"]
        self.save_config()
        proc = self.run_cli("--runner", "private-adapter-42")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["runner"], "private-adapter-42")

    def test_cli_requires_explicit_selection_without_default(self):
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 3)
        self.assertIn("default_runner", proc.stderr)
        self.assertFalse((self.root / "src/result.txt").exists())

    def test_unregistered_fallback_is_rejected_before_execution(self):
        proc = self.run_cli("--runner", "local-worker", "--fallback", "unregistered")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("runners.unregistered", proc.stderr)
        self.assertFalse((self.root / "src/result.txt").exists())

    def test_configured_fallback_works_with_arbitrary_adapter_names(self):
        self.config["default_runner"] = "broken-adapter"
        self.config["runners"]["broken-adapter"] = [sys.executable, "-c", "raise SystemExit(3)"]
        self.save_config()
        proc = self.run_cli("--fallback", "local-worker")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["runner"], "local-worker")

    def test_default_must_reference_registered_runner(self):
        self.config["default_runner"] = "not-configured"
        (self.root / runner.CONFIG_FILE).write_text(json.dumps(self.config))
        with self.assertRaisesRegex(runner.RunnerError, "runners.not-configured"):
            runner.load_config(self.root)

    def test_provider_names_have_no_implicit_command(self):
        with self.assertRaisesRegex(runner.RunnerError, "runners.codex"):
            runner.runner_command({"runners": {}}, "codex")

    def test_installed_entrypoint_uses_callers_project(self):
        # Run installed scripts from outside their source tree with no TASKS_ROOT.
        env = {k: v for k, v in os.environ.items() if k not in ("TASKS_ROOT", "PYTHONPATH")}
        entry = Path(sys.executable).parent / "tasks-init"
        if not entry.exists():
            self.skipTest("requires an installed package")
        proc = subprocess.run([str(entry)], cwd=self.root, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.root / "tasks/tasks.db").exists())


if __name__ == "__main__":
    unittest.main()
