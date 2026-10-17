"""Worktree sprint lifecycle — thin Python wrapper delegating to worktree.sh.

Provides a Python interface for sprint start/status/finish/run.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run_worktree(root: Path, *args: str) -> tuple[int, str, str]:
    """Run a worktree.sh command; return (exit_code, stdout, stderr)."""
    try:
        r = subprocess.run(
            ["bash", str(root / "scripts" / "worktree.sh"), *args],
            capture_output=True, text=True, timeout=300, cwd=str(root),
        )
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def sprint_start(root: Path, n: int, slug: str, base: str = "main") -> tuple[int, str, str]:
    return run_worktree(root, "sprint", "start", str(n), slug, "--from", base)


def sprint_status(root: Path) -> tuple[int, str, str]:
    return run_worktree(root, "sprint", "status")


def sprint_finish(root: Path, merge: bool = False) -> tuple[int, str, str]:
    args = ["sprint", "finish"]
    if merge:
        args.append("--merge")
    return run_worktree(root, *args)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Worktree sprint lifecycle")
    sub = parser.add_subparsers(dest="command")
    s = sub.add_parser("start")
    s.add_argument("n", type=int)
    s.add_argument("slug")
    s.add_argument("--from", dest="base", default="main")
    st = sub.add_parser("status")
    f = sub.add_parser("finish")
    f.add_argument("--merge", action="store_true")
    args = parser.parse_args()

    root = Path(".").resolve()
    if args.command == "start":
        rc, out, err = sprint_start(root, args.n, args.slug, args.base)
    elif args.command == "status":
        rc, out, err = sprint_status(root)
    elif args.command == "finish":
        rc, out, err = sprint_finish(root, args.merge)
    else:
        parser.print_help()
        return 1
    if out:
        print(out)
    if err:
        print(err, file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
