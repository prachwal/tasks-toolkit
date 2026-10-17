"""Worktree MR, merge and accept — thin Python wrapper delegating to worktree.sh.

Provides a Python interface for new, commit, patch, mr, merge, accept, remove.
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


def new_task(root: Path, name: str, task_id: str = "") -> tuple[int, str, str]:
    args = ["new", name]
    if task_id:
        args += ["--task-id", task_id]
    return run_worktree(root, *args)


def commit_task(root: Path, name: str, runner: str = "codex", model: str = "",
                task_id: str = "", title: str = "") -> tuple[int, str, str]:
    args = ["commit", name, "--runner", runner]
    if model:
        args += ["--model", model]
    if task_id:
        args += ["--task-id", task_id]
    if title:
        args += ["--title", title]
    return run_worktree(root, *args)


def create_mr(root: Path, name: str, remote: bool = False) -> tuple[int, str, str]:
    args = ["mr", name]
    if remote:
        args.append("--remote")
    return run_worktree(root, *args)


def merge_task(root: Path, name: str, accept_flags: bool = False, auto: bool = False) -> tuple[int, str, str]:
    args = ["merge", name]
    if accept_flags:
        args.append("--accept-flags")
    if auto:
        args.append("--auto")
    return run_worktree(root, *args)


def accept_task(root: Path, name: str, links_only: bool = False, task_id: str = "") -> tuple[int, str, str]:
    args = ["accept", name]
    if links_only:
        args.append("--links-only")
    if task_id:
        args += ["--task-id", task_id]
    return run_worktree(root, *args)


def remove_task(root: Path, name: str) -> tuple[int, str, str]:
    return run_worktree(root, "remove", name)


def abandon_task(root: Path, name: str) -> tuple[int, str, str]:
    return run_worktree(root, "abandon", name)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Worktree MR, merge and accept")
    sub = parser.add_subparsers(dest="command")
    n = sub.add_parser("new")
    n.add_argument("name")
    n.add_argument("--task-id", default="")
    c = sub.add_parser("commit")
    c.add_argument("name")
    c.add_argument("--runner", default="codex")
    c.add_argument("--model", default="")
    c.add_argument("--task-id", default="")
    c.add_argument("--title", default="")
    m = sub.add_parser("mr")
    m.add_argument("name")
    m.add_argument("--remote", action="store_true")
    mg = sub.add_parser("merge")
    mg.add_argument("name")
    mg.add_argument("--accept-flags", action="store_true")
    mg.add_argument("--auto", action="store_true")
    a = sub.add_parser("accept")
    a.add_argument("name")
    a.add_argument("--links-only", action="store_true")
    a.add_argument("--task-id", default="")
    r = sub.add_parser("remove")
    r.add_argument("name")
    ab = sub.add_parser("abandon")
    ab.add_argument("name")
    args = parser.parse_args()

    root = Path(".").resolve()
    if args.command == "new":
        rc, out, err = new_task(root, args.name, args.task_id)
    elif args.command == "commit":
        rc, out, err = commit_task(root, args.name, args.runner, args.model, args.task_id, args.title)
    elif args.command == "mr":
        rc, out, err = create_mr(root, args.name, args.remote)
    elif args.command == "merge":
        rc, out, err = merge_task(root, args.name, args.accept_flags, args.auto)
    elif args.command == "accept":
        rc, out, err = accept_task(root, args.name, args.links_only, args.task_id)
    elif args.command == "remove":
        rc, out, err = remove_task(root, args.name)
    elif args.command == "abandon":
        rc, out, err = abandon_task(root, args.name)
    else:
        parser.print_help()
        return 1
    if out:
        print(out)
    if err:
        print(err, file=__import__("sys").stderr)
    return rc


if __name__ == "__main__":
    import sys
    sys.exit(main())
