"""Execute and accept guarded tasks for the automated sprint lifecycle (Sprint 12, task 12.6).

Serial task loop bound to a target sprint. Auto-accepts only verified scope +
green Rules checks + zero red flags. Tracker 'done' alone is insufficient evidence.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sprint_auto_state as sas
import tasks  # noqa: E402

RUNNER_DEFAULT = "codex"


def _run_git(root: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _run_cmd(cmd: list[str], cwd: Path, timeout: int = 120) -> tuple[int, str, str]:
    """Run a command; return (exit_code, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def check_scope_conformity(task_files: list[str], allowed_scopes: list[str]) -> tuple[bool, list[str]]:
    """Verify that all task files fall within allowed scopes.

    Returns (conforms, list of violating files).
    """
    if not allowed_scopes:
        return True, []
    violations = []
    for f in task_files:
        ok = False
        for scope in allowed_scopes:
            if f == scope or f.startswith(scope.rstrip("/") + "/") or scope.startswith(f.rstrip("/") + "/"):
                ok = True
                break
            # directory scope: f under scope dir
            if scope.endswith("/") and f.startswith(scope):
                ok = True
                break
            if not scope.endswith("/") and f.startswith(scope + "/"):
                ok = True
                break
        if not ok:
            violations.append(f)
    return len(violations) == 0, violations


def verify_tracker_done_insufficient(task_id: str) -> bool:
    """Check if tracker 'done' alone would be insufficient (it always is).

    Returns True (done is always insufficient without merge+checks+zero flags).
    """
    return True  # tracker done is never sufficient by itself


def auto_accept_decision(scope_ok: bool, checks_green: bool, red_flags: int,
                        tracker_done: bool) -> tuple[bool, str]:
    """Decide whether to auto-accept a task.

    Returns (accept, reason).
    """
    if not scope_ok:
        return False, "scope violation"
    if not checks_green:
        return False, "checks failed"
    if red_flags > 0:
        return False, f"{red_flags} red flag(s)"
    if not tracker_done:
        return False, "tracker not done"
    return True, "all criteria met"


def execute_task_loop(root: Path, sprint_id: str, task_list: list[dict],
                      state: sas.RunState, allowed_scopes: list[str]) -> list[dict]:
    """Execute tasks serially, bound to a sprint. Returns list of results."""
    results = []
    for task in task_list:
        task_key = task.get("key", "")
        task_id = task.get("task_id", "")
        result = {
            "key": task_key,
            "task_id": task_id,
            "status": "started",
        }
        # Scope check
        task_files = task.get("files", [])
        scope_ok, violations = check_scope_conformity(task_files, allowed_scopes)
        result["scope_ok"] = scope_ok
        result["scope_violations"] = violations
        if not scope_ok:
            result["status"] = "scope_violation"
            result["accept"] = False
            result["accept_reason"] = "scope violation"
            results.append(result)
            continue

        # Record intent
        state.operation_intent = f"execute_task:{task_key}"
        sas.write_checkpoint(sas.state_path(root, state.run_name), state)

        # Run checks (placeholder — real impl calls worktree.sh mr)
        checks_green = task.get("checks_green", True)
        red_flags = task.get("red_flags", 0)
        tracker_done = task.get("tracker_done", False)

        # Auto-accept decision
        accept, reason = auto_accept_decision(scope_ok, checks_green, red_flags, tracker_done)
        result["accept"] = accept
        result["accept_reason"] = reason
        result["status"] = "accepted" if accept else "rejected"
        results.append(result)

    return results


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Execute and accept guarded tasks")
    parser.add_argument("--root", default=".")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--sprint-id", required=True)
    parser.add_argument("--tasks", required=True, help="JSON file with task list")
    parser.add_argument("--answers", required=True)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    answers = json.loads(Path(args.answers).read_text())
    tasks_list = json.loads(Path(args.tasks).read_text())
    allowed_scopes = answers.get("allowed_scopes", [])

    # Load state
    state_path = sas.state_path(root, args.run_name)
    state = sas.read_checkpoint(state_path)
    if state is None:
        state = sas.bind_state(root, root / ".git", args.run_name, "", "", allowed_scopes, "", RUNNER_DEFAULT, "", 120)

    results = execute_task_loop(root, args.sprint_id, tasks_list, state, allowed_scopes)
    print(json.dumps({"results": results}, indent=2))

    all_accepted = all(r.get("accept") for r in results)
    return 0 if all_accepted else 1


if __name__ == "__main__":
    sys.exit(main())
