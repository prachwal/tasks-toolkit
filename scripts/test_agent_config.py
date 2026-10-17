"""Validation tests for the agent instruction configuration (task 5.5).

Every linked/imported instruction file must exist, each tool config must point to an existing file,
and the files must stay within their size budgets. Run:
`python3 -m unittest discover -s scripts -p "test_*.py"`.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(rel: str) -> str:
    """Return the UTF-8 text of a repo-relative file."""
    return (ROOT / rel).read_text(encoding="utf-8")


class LinkTests(unittest.TestCase):
    def test_agents_md_links_resolve(self):
        """Every `agents/...md` path mentioned in AGENTS.md exists."""
        refs = re.findall(r"`(agents/[A-Za-z0-9._/-]+\.md)`", read("AGENTS.md"))
        self.assertTrue(refs, "AGENTS.md mentions no agents/ files")
        for ref in sorted(set(refs)):
            self.assertTrue((ROOT / ref).is_file(), f"AGENTS.md links missing file: {ref}")

    def test_claude_md_imports_resolve(self):
        """Every @import in CLAUDE.md exists."""
        imports = re.findall(r"^@(\S+)$", read("CLAUDE.md"), re.MULTILINE)
        self.assertTrue(imports, "CLAUDE.md has no @imports")
        for ref in imports:
            self.assertTrue((ROOT / ref).is_file(), f"CLAUDE.md imports missing file: {ref}")

    def test_common_files_exist(self):
        """The shared detail files the dispatcher relies on are present."""
        for name in ("layout", "git-and-hooks", "task-tracking", "branching", "delegation-flow",
                     "mcp-and-permissions"):
            self.assertTrue((ROOT / "agents" / "common" / f"{name}.md").is_file(), name)


class ConfigTests(unittest.TestCase):
    def test_kilo_instructions_exist(self):
        """Every file in kilo.json instructions exists."""
        for ref in json.loads(read("kilo.json"))["instructions"]:
            self.assertTrue((ROOT / ref).is_file(), f"kilo.json instructions missing: {ref}")

    def test_junie_guidelines_location_exists(self):
        """`guidelines-location` resolves, relative to .junie/, to the generated file."""
        loc = json.loads(read(".junie/config.json"))["guidelines-location"]
        target = (ROOT / ".junie" / loc).resolve()
        self.assertTrue(target.is_file(), f"junie guidelines-location missing: {loc}")
        self.assertTrue(target.name.endswith(".generated.md"), target.name)

    def test_generated_guidelines_in_sync(self):
        """agents_sync.py --check passes (generated file matches its sources)."""
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "agents_sync.py"), "--check"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class SizeTests(unittest.TestCase):
    def test_agents_md_is_slim(self):
        """AGENTS.md stays a dispatcher (<= 60 lines, excluding auto-generated gitnexus section)."""
        content = read("AGENTS.md")
        lines = content.splitlines()
        filtered = []
        in_gitnexus = False
        for line in lines:
            if "<!-- gitnexus:start -->" in line:
                in_gitnexus = True
                continue
            if "<!-- gitnexus:end -->" in line:
                in_gitnexus = False
                continue
            if not in_gitnexus:
                filtered.append(line)
        self.assertLessEqual(len(filtered), 60, "AGENTS.md is too long")

    def test_common_files_within_budget(self):
        """No shared detail file grows past 80 lines."""
        for path in (ROOT / "agents" / "common").glob("*.md"):
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertLessEqual(len(lines), 80, f"{path.name} too long: {len(lines)} lines")

    def test_run_md_per_tool(self):
        """Each runner has a RUN.md."""
        for tool in ("junie", "kilo", "codex"):
            self.assertTrue((ROOT / "agents" / tool / "RUN.md").is_file(), f"agents/{tool}/RUN.md")


if __name__ == "__main__":
    unittest.main()
