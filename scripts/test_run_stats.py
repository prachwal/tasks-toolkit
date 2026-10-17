"""Tests for scripts/run_stats.py (temp dirs only; never reads the real tmp/runs)."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "run_stats.py"


def run(runs_dir, *extra):
    return subprocess.run([sys.executable, str(SCRIPT), "--runs-dir", str(runs_dir), *extra],
                          capture_output=True, text=True)


class RunStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def status(self, name, line):
        (self.dir / f"{name}.status").write_text(line + "\n", encoding="utf-8")

    def test_status_fallback_and_unfinished(self):
        self.status("a", "[03:00] done - junie exit 0, verification PASSED model=m runner=junie")
        self.status("b", "[10:00] done - kilo exit 0, verification PASSED model=m runner=kilo")
        self.status("c", "[02:00] junie running - 0 file(s) changed model=m runner=junie")
        data = json.loads(run(self.dir, "--json").stdout)
        self.assertEqual(data["junie"]["runs"], 2)
        self.assertEqual(data["junie"]["verified"], 1)
        self.assertEqual(data["junie"]["unfinished"], 1)
        self.assertEqual(data["junie"]["median_s"], 180)
        self.assertEqual(data["kilo"]["median_s"], 600)
        self.assertEqual(data["ALL"]["rework_rate"], 0.33)

    def test_summary_wins_over_status_and_verdicts(self):
        self.status("x", "[01:00] done - junie exit 0, verification PASSED model=m runner=junie")
        (self.dir / "x.summary.json").write_text(json.dumps(
            {"name": "x", "runner": "junie", "model": "m", "duration_s": 99, "verdict": "verify_failed"}))
        self.status("y", "[01:00] done - kilo exit 3, verification FAILED model=m runner=kilo")
        data = json.loads(run(self.dir, "--json").stdout)
        self.assertEqual(data["junie"]["verify_failed"], 1)
        self.assertEqual(data["junie"]["median_s"], 99)
        self.assertEqual(data["kilo"]["agent_failed"], 1)

    def test_text_table_and_missing_dir(self):
        self.status("a", "[03:00] done - junie exit 0, verification PASSED model=m runner=junie")
        out = run(self.dir)
        self.assertEqual(out.returncode, 0)
        self.assertIn("junie", out.stdout)
        self.assertIn("slowest:", out.stdout)
        self.assertEqual(run(self.dir / "nope").returncode, 1)

    def test_empty_dir(self):
        out = run(self.dir)
        self.assertEqual(out.returncode, 0)

    def test_legacy_status_supports_arbitrary_runner_names(self):
        self.status("private", "[01:30] done - private-agent-42 exit 0, verification PASSED")
        data = json.loads(run(self.dir, "--json").stdout)
        self.assertEqual(data["private-agent-42"]["verified"], 1)
        self.assertEqual(data["private-agent-42"]["median_s"], 90)


if __name__ == "__main__":
    unittest.main()
