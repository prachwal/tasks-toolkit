"""Run a configured coder and verify its actual Git changes before committing them."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import sprint_auto_state as sas
from scripts.runner_config import (
    CONFIG_FILE as CONFIG_FILE,
)
from scripts.runner_config import (
    RunnerError as RunnerError,
)
from scripts.runner_config import (
    load_config as load_config,
)
from scripts.runner_config import (
    runner_command as runner_command,
)
from scripts.runner_config import (
    select_runner as select_runner,
)

NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RunnerError(result.stderr.strip() or f"git {args[0]} failed")
    return result.stdout if "-z" in args else result.stdout.rstrip("\n")


def scope_conformity(files: list[str], scopes: list[str]) -> tuple[bool, list[str]]:
    """Match repo-relative files to an exact file or directory, never its ancestor."""
    def clean(value: str) -> bool:
        return bool(value) and not value.startswith("/") and ".." not in value.split("/") and value != "."

    violations = [f for f in files if not clean(f) or not any(
        clean(s.rstrip("/")) and (f == s.rstrip("/") or f.startswith(s.rstrip("/") + "/")) for s in scopes
    )]
    return not violations, violations


def changed_files(root: Path, base: str) -> list[str]:
    changed = git(root, "diff", "--no-renames", "--name-only", "-z", base, "--")
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z")
    return sorted(set(filter(None, changed.split("\0") + untracked.split("\0"))))


def content_fingerprint(root: Path, files: list[str]) -> str:
    digest = hashlib.sha256()
    for name in files:
        path = root / name
        digest.update(name.encode())
        if path.is_symlink():
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(path.read_bytes())
            digest.update(str(path.stat().st_mode).encode())
        elif path.exists():
            raise RunnerError(f"unsupported changed path: {name}")
        else:
            digest.update(b"deleted")
    return digest.hexdigest()


def run_command(command: list[str], root: Path, timeout: int, prompt: str | None = None) -> tuple[int, str]:
    return sas.supervised_run(command, root, timeout, input_text=prompt)


def run_checks(root: Path, config: dict) -> list[dict]:
    results = []
    for command in config["checks"]:
        code, output = run_command(command, root, config.get("timeout", 120))
        results.append({"command": command, "exit_code": code, "output": output})
        if code:
            break
    return results


def verify_changes(root: Path, base: str, scopes: list[str], config: dict) -> dict:
    """Inspect actual changes and run the checks captured before invoking the coder."""
    files = changed_files(root, base)
    ok, violations = scope_conformity(files, scopes)
    forbidden = [f for f in files if f == CONFIG_FILE or f == ".git" or f.startswith(".git/")]
    if forbidden:
        ok = False
        violations += forbidden
    if not ok or not files:
        return {"accept": False, "status": "scope_violation" if not ok else "no_changes",
                "files": files, "scope_violations": violations}
    if git(root, "rev-parse", "HEAD") != base:
        # Coders leave uncommitted changes; the verifier owns commits and their identity.
        return {"accept": False, "status": "unexpected_commit", "files": files}
    before = content_fingerprint(root, files)
    code, whitespace = run_command(["git", "diff", "--check", base, "--"], root, 30)
    checks = [] if code else run_checks(root, config)
    unchanged = files == changed_files(root, base) and before == content_fingerprint(root, files)
    head_unchanged = git(root, "rev-parse", "HEAD") == base
    accepted = not code and bool(checks) and all(c["exit_code"] == 0 for c in checks) and unchanged and head_unchanged
    return {"accept": accepted, "status": "verified" if accepted else "checks_failed",
            "files": files, "checks": checks, "whitespace": whitespace,
            "checks_changed_files": not unchanged or not head_unchanged}


def execute_task(root: Path, spec: Path, name: str, runner: str, scopes: list[str], config: dict) -> dict:
    if not NAME.fullmatch(name):
        raise RunnerError("task name must contain only letters, digits, underscores and hyphens")
    if not scopes:
        raise RunnerError("at least one scope is required")
    if git(root, "status", "--porcelain"):
        raise RunnerError("task worktree must be clean before running a coder")
    lint = Path(__file__).with_name("spec-lint.sh")
    lint_env = {k: v for k, v in os.environ.items() if k != "SPEC_LINT"}
    result = subprocess.run(["bash", str(lint), str(spec)], capture_output=True, text=True, env=lint_env)
    if result.returncode:
        return {"accept": False, "status": "invalid_spec", "output": result.stderr, "exit_code": 1}
    command = runner_command(config, runner)
    base = git(root, "rev-parse", "HEAD")
    prompt = spec.read_text(encoding="utf-8") + "\n\nImplement this task. Leave changes uncommitted. Do not change the tracker or runner configuration.\n"
    started = time.monotonic()
    code, output = run_command(command, root, config.get("timeout", 120), prompt)
    report: dict[str, Any] = {"runner": runner, "runner_exit_code": code, "output": output,
              "seconds": round(time.monotonic() - started, 3), "base": base}
    if code:
        return report | {"accept": False, "status": "runner_failed", "exit_code": 3}
    report.update(verify_changes(root, base, scopes, config))
    report["exit_code"] = 0 if report["accept"] else 1
    if report["accept"]:
        git(root, "add", "--", *report["files"])
        git(root, "commit", "-m", f"Implement task {name}")
        report["commit"] = git(root, "rev-parse", "HEAD")
    return report


def write_report(root: Path, name: str, report: dict) -> None:
    """Keep machine-readable verification evidence and delivery statistics together."""
    directory = root / "tmp" / "runs"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    code = report["exit_code"]
    summary = {"name": name, "runner": report.get("runner"), "model": "",
               "duration_s": round(report.get("seconds", 0)),
               "files_changed": len(report.get("files", [])),
               "verdict": "verified" if code == 0 else "agent_failed" if code == 3 else "verify_failed"}
    (directory / f"{name}.summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    with (directory / f"{name}.attempts").open("a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%H:%M:%S')} runner={summary['runner']} exit={code} seconds={summary['duration_s']}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("--file", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--task-id")
    parser.add_argument("--scope", action="append", default=[])
    parser.add_argument("--runner", help="registered runner name (default: configuration default_runner)")
    parser.add_argument("--fallback", default="none", help="registered fallback runner, or none")
    args = parser.parse_args(argv)
    try:
        root = Path(args.root).resolve()
        spec = Path(args.file).resolve()
        config = load_config(root)
        selected = select_runner(config, args.runner)
        if args.fallback != "none":
            runner_command(config, args.fallback)
        scopes = args.scope
        if args.task_id:
            from scripts import tasks
            node = tasks.find(tasks.load(), args.task_id)
            scopes = scopes or [link["value"] for link in node.get("links", []) if link["kind"] == "scope"]
        report = execute_task(root, spec, args.name, selected, scopes, config)
        # A failed coder may have left changes. Never retry over those changes.
        if report["exit_code"] == 3 and args.fallback not in ("none", selected) and not git(root, "status", "--porcelain"):
            report = execute_task(root, spec, args.name, args.fallback, scopes, config)
        write_report(root, args.name, report)
        print(json.dumps(report, indent=2))
        return int(report["exit_code"])
    except (RunnerError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"run-task: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
