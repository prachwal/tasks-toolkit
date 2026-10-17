"""Tests for scripts/sprint-auto.sh.

Run: bash scripts/test_sprint_auto.sh (if exists) or manual verification.
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sprint-auto.sh"


class SprintAutoTests(unittest.TestCase):

    def test_requires_yolo_force(self):
        proc = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--yolo-force is required", proc.stderr)

    def test_requires_goal(self):
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--yolo-force"], capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--goal is required", proc.stderr)

    def test_requires_run_name(self):
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--yolo-force", "--goal", "test"], capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--run-name is required", proc.stderr)

    def test_unknown_option(self):
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--yolo-force", "--goal", "test", "--run-name", "r1", "--bogus"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unrecognized arguments", proc.stderr)


if __name__ == "__main__":
    unittest.main()
