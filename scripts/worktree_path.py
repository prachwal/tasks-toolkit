"""Path validation for worktree.sh (extracted to Python for testability).

Thin wrapper that delegates to worktree.sh's bash implementation.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

GENERATED_PATHS = [
    "tasks/tasks.snapshot.json",
    "tasks/README.md",
    "tasks/sprint-*.md",
    "TASKS.md",
]


def is_generated_path(p: str) -> bool:
    """Check if a path matches a generated file pattern."""
    import fnmatch
    for gp in GENERATED_PATHS:
        if fnmatch.fnmatch(p, gp):
            return True
    return False


def path_ok(p: str) -> tuple[bool, str]:
    """Validate a repo-relative path. Returns (ok, reason)."""
    if not p:
        return False, "empty path"
    import re
    if re.search(r"[^A-Za-z0-9._/\-]", p):
        return False, f"path '{p}' contains forbidden characters"
    if p.startswith("/"):
        return False, f"path '{p}' must not start with /"
    segments = p.split("/")
    for seg in segments:
        if not seg:
            return False, f"path '{p}' contains empty segment (//)"
        if seg == "..":
            return False, f"path '{p}' contains '..' segment"
        if seg.startswith("-"):
            return False, f"path '{p}' has segment starting with '-': {seg}"
    for d in (".git", ".githooks", ".claude", ".junie", ".kilo", "tmp"):
        if p == d or p.startswith(d + "/") or ("/" + d + "/") in ("/" + p + "/") or p.endswith("/" + d):
            return False, f"path '{p}' is inside protected directory {d}/"
    for f in ("kilo.json", ".mcp.json", "tasks/tasks.db", "tasks/.lock"):
        if p == f:
            return False, f"path '{p}' is a protected file"
    if is_generated_path(p):
        return False, f"path '{p}' is a generated file"
    return True, "ok"


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Path validation for worktree")
    parser.add_argument("--check", required=True, help="Path to validate")
    args = parser.parse_args()
    ok, reason = path_ok(args.check)
    if ok:
        print("path-ok")
        return 0
    print(reason, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
