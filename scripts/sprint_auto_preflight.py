#!/usr/bin/env python3
"""Read-only approval preflight for the automated sprint lifecycle (Sprint 12).

Collects a batch of unresolved decisions, inspects repository state without
mutating anything, and returns a JSON report.  No files, locks, bytecode or
artifacts are created by this module.

Usage:
    python3 -B scripts/sprint_auto_preflight.py preflight-probe --goal '...'
    python3 -B scripts/sprint_auto_preflight.py collect --root . --answers answers.json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

FORBIDDEN_ENV = {
    "SPEC_LINT": "off",
    "ACCEPT_CHECKS_CMD": None,  # any non-empty value is forbidden
    "JUNIE_VERIFY_CMD": None,
    "KILO_VERIFY_CMD": None,
    "CODEX_VERIFY_CMD": None,
}

GENERATED_PATHS = {
    "tasks/tasks.snapshot.json",
    "tasks/README.md",
}

SAFE_DEFAULTS = {
    "runner": "codex",
    "serial": True,
    "fallback": "none",
    "retries": 0,
    "timeout": 120,
    "push": False,
    "accept_flags": False,
    "reset": False,
    "clean": False,
}


def _git(root: Path, *args: str) -> str:
    """Run a read-only git command; return stdout (empty on failure)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=30,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _check_env() -> list[str]:
    """Return list of forbidden-env blockers."""
    blockers: list[str] = []
    for key, forbidden_val in FORBIDDEN_ENV.items():
        val = os.environ.get(key, "")
        if not val:
            continue
        if forbidden_val is None or val.strip().lower() == forbidden_val:
            blockers.append(f"forbidden env: {key}={val!r}")
    return blockers


def _inspect_git(root: Path) -> tuple[list[str], list[str]]:
    """Return (blockers, questions) from read-only git inspection."""
    blockers: list[str] = []
    questions: list[str] = []

    # Merge / rebase / cherry-pick in progress?
    git_dir = Path(_git(root, "rev-parse", "--git-dir") or ".git")
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    for marker in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD"):
        if (git_dir / marker).exists():
            blockers.append(f"git operation in progress: {marker}")

    # Staged changes
    staged = _git(root, "diff", "--cached", "--name-only")
    if staged:
        staged_files = [f for f in staged.splitlines() if f.strip()]
        # Distinguish staged generated files (hard blocker) from other staged
        for f in staged_files:
            if f in GENERATED_PATHS or f.startswith("tasks/sprint-") and f.endswith(".md"):
                blockers.append(f"staged generated file: {f}")
        if any(f not in GENERATED_PATHS for f in staged_files):
            blockers.append("staged (non-generated) changes present")

    # Unstaged changes
    unstaged = _git(root, "diff", "--name-only")
    if unstaged:
        for f in unstaged.splitlines():
            f = f.strip()
            if not f:
                continue
            if f in GENERATED_PATHS or (f.startswith("tasks/sprint-") and f.endswith(".md")):
                # narrowly verified generated working-tree change — allowed but noted
                pass
            else:
                blockers.append(f"unstaged change: {f}")

    # Untracked files (excluding gitignored)
    untracked = _git(root, "ls-files", "--others", "--exclude-standard")
    if untracked:
        for f in untracked.splitlines():
            f = f.strip()
            if f:
                blockers.append(f"untracked file: {f}")

    # Worktrees
    wt_list = _git(root, "worktree", "list", "--porcelain")
    if wt_list:
        wt_count = sum(1 for line in wt_list.splitlines() if line.startswith("worktree "))
        if wt_count > 1:
            questions.append(f"multiple worktrees detected ({wt_count})")

    # Current branch must be main for fresh runs
    branch = _git(root, "symbolic-ref", "--short", "HEAD")
    if branch and branch != "main":
        questions.append(f"not on main branch (on {branch})")

    return blockers, questions


def _inspect_sqlite(root: Path) -> list[str]:
    """Read-only SQLite inspection; return blockers."""
    blockers: list[str] = []
    db = root / "tasks" / "tasks.db"
    if not db.exists():
        return blockers  # no db is fine for preflight
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        conn.execute("SELECT 1 FROM nodes LIMIT 1")
        conn.close()
    except (sqlite3.Error, OSError) as exc:
        blockers.append(f"sqlite read-only open failed: {exc}")
    return blockers


def _resolve_delegated(decision: dict) -> tuple[object, bool]:
    """Return (resolved_value, was_delegated) for a decision."""
    if decision.get("answer") is not None:
        return decision["answer"], False
    if decision.get("delegated") and decision.get("safe_default") is not None:
        return decision["safe_default"], True
    return None, False


def collect_preflight(root: Path, request: dict, answers: dict | None = None) -> dict:
    """Collect the preflight report.  Read-only: no writes anywhere."""
    answers = answers or {}
    questions: list[dict] = []
    delegated_defaults: list[dict] = []
    blockers: list[str] = []
    approval_scope: dict = {}

    # --- environment checks ---
    blockers.extend(_check_env())
    git_blockers, git_questions = _inspect_git(root)
    blockers.extend(git_blockers)
    for q in git_questions:
        questions.append({"id": "git_state", "prompt": q, "answer": None,
                          "delegated": False, "safe_default": None, "rationale": ""})
    blockers.extend(_inspect_sqlite(root))

    # --- goal review ---
    goal = request.get("goal", "")
    goal_reviewed = answers.get("goal_reviewed", False)
    if not goal_reviewed:
        questions.append({
            "id": "goal_review",
            "prompt": f"Has the goal been reviewed? Goal: {goal!r}",
            "answer": None,
            "delegated": False,
            "safe_default": False,
            "rationale": "Goal must be explicitly reviewed before automation.",
        })

    # --- allowed scopes ---
    allowed_scopes = answers.get("allowed_scopes", [])
    if not allowed_scopes:
        questions.append({
            "id": "allowed_scopes",
            "prompt": "Which scopes (repo-relative paths) may this run touch?",
            "answer": None,
            "delegated": False,
            "safe_default": [],
            "rationale": "At least one scope is required.",
        })
    else:
        # validate scopes are clean repo-relative paths
        for scope in allowed_scopes:
            if not isinstance(scope, str) or ".." in scope or scope.startswith("/"):
                blockers.append(f"invalid scope: {scope!r}")

    # --- policy decisions ---
    decisions = answers.get("decisions", [])
    decision_map = {d.get("id"): d for d in decisions if isinstance(d, dict)}

    policy_items = [
        ("runner", "Which runner to use?", "codex"),
        ("serial", "Run tasks serially?", True),
        ("fallback", "Fallback runner on failure?", "none"),
        ("retries", "Max retries per task?", 0),
        ("push", "Push to remote?", False),
        ("accept_flags", "Accept red flags?", False),
        ("reset", "Allow reset?", False),
        ("clean", "Allow clean?", False),
    ]

    for pid, prompt, default in policy_items:
        d = decision_map.get(pid, {})
        val, delegated = _resolve_delegated(d)
        if val is None and not delegated:
            # check if explicitly answered
            if pid in answers.get("policy", {}):
                val = answers["policy"][pid]
                delegated = False
            else:
                safe = d.get("safe_default", default)
                if d.get("delegated"):
                    delegated_defaults.append({"id": pid, "value": safe, "rationale": d.get("rationale", "")})
                    val = safe
                    delegated = True
                else:
                    questions.append({
                        "id": pid,
                        "prompt": prompt,
                        "answer": None,
                        "delegated": False,
                        "safe_default": default,
                        "rationale": "",
                    })
                    continue
        if delegated:
            delegated_defaults.append({"id": pid, "value": val, "rationale": d.get("rationale", "")})

    # --- build approval scope ---
    policy = answers.get("policy", {})
    approval_scope = {
        "goal": goal,
        "goal_reviewed": goal_reviewed,
        "allowed_scopes": allowed_scopes,
        "policy": policy,
        "runner": policy.get("runner", "codex"),
        "serial": policy.get("serial", True),
        "fallback": policy.get("fallback", "none"),
        "push": policy.get("push", False),
    }

    return {
        "questions": questions,
        "delegated_defaults": delegated_defaults,
        "blockers": blockers,
        "approval_scope": approval_scope,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only approval preflight")
    sub = parser.add_subparsers(dest="command")

    probe = sub.add_parser("preflight-probe", help="Probe with a goal, no answers")
    probe.add_argument("--goal", required=True)

    coll = sub.add_parser("collect", help="Collect preflight from answers file")
    coll.add_argument("--root", default=".")
    coll.add_argument("--answers", required=True)
    coll.add_argument("--request", default=None)

    args = parser.parse_args()

    if args.command == "preflight-probe":
        request = {"goal": args.goal}
        result = collect_preflight(Path("."), request, {})
        print(json.dumps(result, indent=2))
        if result["questions"]:
            return 2
        if result["blockers"]:
            return 4
        return 0

    if args.command == "collect":
        root = Path(args.root).resolve()
        answers = json.loads(Path(args.answers).read_text())
        request = {}
        if args.request:
            request = json.loads(Path(args.request).read_text())
        result = collect_preflight(root, request, answers)
        print(json.dumps(result, indent=2))
        if result["questions"]:
            return 2
        if result["blockers"]:
            return 4
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
