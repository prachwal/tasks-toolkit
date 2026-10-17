"""Run summaries accept adapter identities independently of provider or model."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts import run_summary


class RunSummaryTests(unittest.TestCase):
    def summary(self, folder: Path, runner: str, **extra):
        target = folder / "summary.json"
        fields = {"name": "task", "runner": runner, "model": "custom-model", "task_id": "1.1",
                  "patch": "", "started": "10", "finished": "13", "agent_exit": "0",
                  "verify_exit": "0", "files_changed": "1", "verdict": "verified"} | extra
        with contextlib.redirect_stderr(io.StringIO()):
            code = run_summary.main(["run_summary.py", str(target), *[f"{key}={value}" for key, value in fields.items()]])
        return code, json.loads(target.read_text()) if target.exists() else None

    def test_unknown_provider_and_model_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            code, report = self.summary(Path(folder), "private-agent-42", agent="reviewer")
        self.assertEqual(code, 0)
        self.assertEqual(report["runner"], "private-agent-42")
        self.assertEqual(report["model"], "custom-model")
        self.assertEqual(report["agent"], "reviewer")
        self.assertEqual(report["duration_s"], 3)

    def test_agent_field_has_no_provider_specific_requirements(self):
        for name, extra in [("junie", {"agent": "custom"}), ("kilo", {})]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                code, _ = self.summary(Path(folder), name, **extra)
                self.assertEqual(code, 0)

    def test_invalid_runner_does_not_write_summary(self):
        for name in ("", "../escape", "two words", "none"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                code, report = self.summary(Path(folder), name)
                self.assertEqual(code, 2)
                self.assertIsNone(report)
