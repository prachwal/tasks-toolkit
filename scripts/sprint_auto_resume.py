"""Resume and finish owned sprints for the automated sprint lifecycle (Sprint 12, task 12.7).

Reconciles interrupted operations from checkpoint, tracker receipts and Git history.
Never recovers by resetting, cleaning, abandoning or blindly rerunning the agent.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sprint_auto_state as sas


def _run_git(root: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def reconcile_checkpoint(root: Path, run_name: str) -> dict:
    """Reconcile interrupted operations from checkpoint, tracker, and git history.

    Returns a dict with: status, state, child_alive, merge_exists, actions.
    """
    root = Path(root).resolve()
    state = sas.read_checkpoint(sas.state_path(root, run_name))
    if state is None:
        return {"status": "no_checkpoint", "actions": ["create new state"]}

    actions = []
    result = {"state": state.to_dict(), "actions": actions}

    # Check child process
    child_alive = sas.is_child_alive(state)
    result["child_alive"] = child_alive
    if child_alive:
        actions.append("child still running — wait or forward signal")
        result["status"] = "child_running"
        return result

    # Check if main merge already exists
    sprint_branch = None
    if state.expected_refs:
        sprint_branch = state.expected_refs.get("sprint_branch")
    merge_exists = False
    if sprint_branch:
        # Check if sprint branch is merged into main
        main_sha = _run_git(root, "rev-parse", "main")
        merge_base = _run_git(root, "merge-base", "main", sprint_branch)
        if merge_base and main_sha:
            # If merge-base equals sprint branch HEAD, it's merged
            sprint_head = _run_git(root, "rev-parse", sprint_branch)
            if merge_base == sprint_head:
                merge_exists = True
                actions.append("main merge already exists — skip merge")
    result["merge_exists"] = merge_exists

    # Determine next action based on operation_intent
    intent = state.operation_intent
    if intent.startswith("execute_task:"):
        actions.append(f"resume task execution from: {intent}")
    elif intent == "merge_sprint":
        if merge_exists:
            actions.append("post-merge verification needed")
        else:
            actions.append("merge sprint to main needed")
    elif intent == "finish":
        if merge_exists:
            actions.append("post-merge verification needed")
        else:
            actions.append("finish: merge + verify needed")

    # Check operation results
    if state.command_results:
        last = state.command_results[-1]
        if last.get("exit_code", 0) != 0:
            actions.append(f"last command failed: {last.get('error', 'unknown')}")

    # Expected-ref finish guard
    if state.expected_refs:
        expected_sprint = state.expected_refs.get("sprint_sha")
        if expected_sprint:
            actual = _run_git(root, "rev-parse", sprint_branch or "main")
            if actual and actual != expected_sprint and not merge_exists:
                actions.append("WARNING: sprint ref mismatch — expected %s got %s" % (expected_sprint[:8], actual[:8]))

    result["status"] = "reconciled"
    return result


def finish_guard(root: Path, run_name: str, expected_sprint_sha: str,
                 expected_main_sha: str) -> tuple[bool, str]:
    """Guard before finishing: verify refs match expectations.

    Returns (ok, reason).
    """
    root = Path(root).resolve()
    sprint_sha = _run_git(root, "rev-parse", "main")  # after merge, main should have sprint
    if expected_main_sha and sprint_sha != expected_main_sha:
        # main moved — check if it's the sprint merge
        merge_base = _run_git(root, "merge-base", "main", expected_sprint_sha) if expected_sprint_sha else ""
        if merge_base != expected_sprint_sha:
            return False, f"main ref mismatch: expected merge of {expected_sprint_sha[:8]}"
    return True, "finish guard passed"


def recognize_existing_merge(root: Path, sprint_branch: str) -> bool:
    """Check if sprint branch is already merged into main."""
    merge_base = _run_git(root, "merge-base", "main", sprint_branch)
    sprint_head = _run_git(root, "rev-parse", sprint_branch)
    return merge_base == sprint_head and merge_base != ""


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Resume and finish owned sprints")
    sub = parser.add_subparsers(dest="command")

    rec = sub.add_parser("reconcile", help="Reconcile interrupted run")
    rec.add_argument("--root", default=".")
    rec.add_argument("--run-name", required=True)

    fg = sub.add_parser("finish-guard", help="Guard before finishing")
    fg.add_argument("--root", default=".")
    fg.add_argument("--run-name", required=True)
    fg.add_argument("--expected-sprint-sha", default="")
    fg.add_argument("--expected-main-sha", default="")

    args = parser.parse_args()

    if args.command == "reconcile":
        root = Path(args.root).resolve()
        result = reconcile_checkpoint(root, args.run_name)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] in ("reconciled", "no_checkpoint") else 1

    if args.command == "finish-guard":
        root = Path(args.root).resolve()
        ok, reason = finish_guard(root, args.run_name, args.expected_sprint_sha, args.expected_main_sha)
        print(json.dumps({"ok": ok, "reason": reason}))
        return 0 if ok else 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
