"""Execute sprint tasks and accept only commits verified by repository checks."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import sprint_auto_state as sas
from scripts import task_runner as runner

RUNNER_DEFAULT = "codex"
check_scope_conformity = runner.scope_conformity


def verify_tracker_done_insufficient(task_id: str) -> bool:
    return True


def auto_accept_decision(scope_ok: bool, checks_green: bool, red_flags: int,
                         tracker_done: bool) -> tuple[bool, str]:
    if not scope_ok:
        return False, "scope violation"
    if not checks_green:
        return False, "checks failed"
    if red_flags > 0:
        return False, f"{red_flags} red flag(s)"
    if not tracker_done:
        return False, "tracker not done"
    return True, "all criteria met"


def tracker(root: Path, *args: str) -> str:
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("tasks.py")), *args],
        cwd=root, env={**os.environ, "TASKS_ROOT": str(root)},
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode:
        raise runner.RunnerError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def execute_task_loop(root: Path, sprint_id: str, task_list: list[dict],
                      state: sas.RunState, allowed_scopes: list[str], config: dict | None = None) -> list[dict]:
    """Stop at the first rejected task; declared JSON verification flags are ignored."""
    config = config if config is not None else runner.load_config(root)
    worktree = Path(state.expected_refs.get("worktree", root)).resolve()
    results = []
    for task in task_list:
        key, task_id = task.get("key", ""), task.get("task_id", "")
        result = {"key": key, "task_id": task_id, "accept": False}
        scopes = task.get("scope", [])
        ok, violations = check_scope_conformity(scopes, allowed_scopes)
        if not ok or not scopes:
            result.update(status="scope_violation", scope_violations=violations)
            results.append(result)
            break
        try:
            if not task_id.startswith(sprint_id + ".") or task_id.count(".") != 1:
                raise runner.RunnerError("task does not belong to the target sprint")
            node = json.loads(tracker(root, "show", task_id))
            if isinstance(node, list):
                node = node[0]
            # Dependencies are checked against the live tracker, never input flags.
            for dependency in node.get("depends_on", []):
                dep = json.loads(tracker(root, "show", dependency))
                if isinstance(dep, list):
                    dep = dep[0]
                if dep.get("effective_status", dep.get("status")) != "done":
                    raise runner.RunnerError(f"dependency {dependency} is not done")
            if key in state.completed_tasks:
                saved = state.completed_tasks[key]
                if saved.get("task_id") != task_id or saved.get("uid") != node["uid"]:
                    raise runner.RunnerError("checkpoint task identity mismatch")
                if runner.git(worktree, "merge-base", "HEAD", saved["commit"]) != saved["commit"]:
                    raise runner.RunnerError("verified task commit is missing")
                results.append(saved)
                continue
            state.operation_intent = f"execute_task:{key}"
            sas.write_checkpoint(sas.state_path(root, state.run_name), state)
            tracker(root, "status", task_id, "in_progress")
            spec = root / "tmp" / "tasks" / f"{task['spec_name']}.md"
            result.update(runner.execute_task(worktree, spec, task["spec_name"], state.runner, scopes, config))
            runner.write_report(root, task["spec_name"], result)
            if result["accept"]:
                tracker(root, "link", task_id, "commit", result["commit"])
                tracker(root, "status", task_id, "done")
                result.update(status="accepted", uid=node["uid"])
                state.completed_tasks[key] = result
                state.verified_commit = result["commit"]
                state.expected_refs["sprint_sha"] = result["commit"]
                sas.write_checkpoint(sas.state_path(root, state.run_name), state)
            else:
                tracker(root, "status", task_id, "blocked", "--reason", result["status"])
        except (runner.RunnerError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            result.update(accept=False, status="rejected", error=str(exc))
        results.append(result)
        if not result["accept"]:
            break
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--sprint-id", required=True)
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--answers", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    state = sas.read_checkpoint(sas.state_path(root, args.run_name))
    if state is None:
        parser.error("prepare the run before execution; checkpoint missing")
    answers = json.loads(Path(args.answers).read_text())
    task_list = json.loads(Path(args.tasks).read_text())
    try:
        with sas.acquire_lock(root, args.run_name):
            results = execute_task_loop(root, args.sprint_id, task_list, state, answers.get("allowed_scopes", []))
        print(json.dumps({"results": results}, indent=2))
        return 0 if results and len(results) == len(task_list) and all(r["accept"] for r in results) else 1
    except (runner.RunnerError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
