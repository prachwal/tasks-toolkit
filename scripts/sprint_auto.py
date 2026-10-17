"""Prepare, execute and fast-forward a verified sprint into an unchanged main."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import sprint_auto_execute as execute
from scripts import sprint_auto_prepare as prepare
from scripts import sprint_auto_state as sas
from scripts import task_runner as runner
from scripts.tasks_plan_import import canonical_fingerprint


def finish(root: Path, state: sas.RunState, config: dict) -> dict:
    worktree = Path(state.expected_refs["worktree"])
    branch = state.expected_refs["sprint_branch"]
    verified = state.verified_commit
    if not verified or runner.git(root, "rev-parse", branch) != verified:
        raise runner.RunnerError("sprint branch no longer matches the verified commit")
    if runner.git(worktree, "status", "--porcelain") or runner.git(root, "status", "--porcelain"):
        raise runner.RunnerError("both worktrees must be clean before finishing")
    if runner.git(root, "symbolic-ref", "--short", "HEAD") != "main":
        raise runner.RunnerError("finish requires main to be checked out")
    actual_main = runner.git(root, "rev-parse", "main")
    if actual_main == verified and state.operation_intent in ("merge_sprint", "finished"):
        state.operation_intent = "finished"
        sas.write_checkpoint(sas.state_path(root, state.run_name), state)
        return {"status": "finished", "commit": verified, "replay": True}
    if actual_main != state.initial_main_sha:
        raise runner.RunnerError("main moved since approval; review and prepare a new run")
    if runner.git(root, "merge-base", "main", verified) != actual_main:
        raise runner.RunnerError("sprint does not descend from approved main")
    files = runner.changed_files(worktree, actual_main)
    ok, violations = runner.scope_conformity(files, state.allowed_scopes)
    if not ok:
        raise runner.RunnerError(f"sprint scope violation: {violations}")
    checks = runner.run_checks(worktree, config)
    if not checks or any(c["exit_code"] for c in checks):
        raise runner.RunnerError("final sprint checks failed")
    if runner.git(worktree, "status", "--porcelain") or runner.git(worktree, "rev-parse", "HEAD") != verified:
        raise runner.RunnerError("checks changed the verified worktree")
    if runner.git(root, "status", "--porcelain"):
        raise runner.RunnerError("main worktree changed during final checks")
    if runner.git(root, "rev-parse", "main") != actual_main or runner.git(root, "rev-parse", branch) != verified:
        raise runner.RunnerError("refs changed during final checks")
    state.operation_intent = "merge_sprint"
    sas.write_checkpoint(sas.state_path(root, state.run_name), state)
    # Merge the immutable verified commit, with no reset, force update or push.
    runner.git(root, "merge", "--ff-only", verified)
    state.operation_intent = "finished"
    sas.write_checkpoint(sas.state_path(root, state.run_name), state)
    return {"status": "finished", "commit": verified, "checks": checks}


def run(root: Path, goal: str, name: str, answers: dict, plan: Path) -> dict:
    if not runner.NAME.fullmatch(name):
        raise runner.RunnerError("invalid run name")
    config = runner.load_config(root)
    policy = answers.get("policy", {})
    if policy.get("fallback", "none") != "none" or policy.get("retries", 0) != 0:
        raise runner.RunnerError("automated sprints require fallback=none and retries=0")
    for flag in ("push", "accept_flags", "reset", "clean"):
        if policy.get(flag, False):
            raise runner.RunnerError(f"automated sprints do not support {flag}=true")
    if policy.get("serial", True) is not True:
        raise runner.RunnerError("automated sprints execute serially")
    runner_command = runner.runner_command(config, policy.get("runner", "codex"))
    if not runner_command:
        raise runner.RunnerError("runner command missing")
    prepared = prepare.prepare_run(root, {"goal": goal}, answers, name, plan)
    if prepared["status"] != "prepared":
        return prepared
    with sas.acquire_lock(root, name):
        state = sas.read_checkpoint(sas.state_path(root, name))
        if state is None:
            raise runner.RunnerError("prepared checkpoint missing")
        branch = prepared["branch"]["name"]
        worktree = root / "tmp" / "worktrees" / name
        if worktree.exists():
            raise runner.RunnerError("worktree already exists; inspect checkpoint before resuming")
        state.config_fingerprint = canonical_fingerprint(config)
        state.tracker_uids = prepared["import_receipt"]
        state.expected_refs = {"sprint_branch": branch, "sprint_sha": prepared["branch"]["sha"], "worktree": str(worktree)}
        state.operation_intent = "create_worktree"
        sas.write_checkpoint(sas.state_path(root, name), state)
        runner.git(root, "worktree", "add", str(worktree), branch)
        state.owned_artifacts.append(str(worktree))
        sas.write_checkpoint(sas.state_path(root, name), state)
        mapping = prepared["import_receipt"]["task_map"]
        task_list = [task | {"task_id": mapping[task["key"]]["id"]} for task in prepared["plan"]["tasks"]]
        results = execute.execute_task_loop(root, prepared["import_receipt"]["sprint_id"], task_list, state,
                                            answers["allowed_scopes"], config)
        if len(results) != len(task_list) or not all(r["accept"] for r in results):
            return {"status": "execution_failed", "results": results, "worktree": str(worktree)}
        result = finish(root, state, config)
        result.update(results=results, worktree=str(worktree))
        return result


def resume(root: Path, goal: str, name: str, answers: dict, plan_path: Path) -> dict:
    if not runner.NAME.fullmatch(name):
        raise runner.RunnerError("invalid run name")
    with sas.acquire_lock(root, name):
        state = sas.read_checkpoint(sas.state_path(root, name))
        if state is None or state.repo_root != str(root) or state.run_name != name:
            raise runner.RunnerError("checkpoint identity mismatch or missing checkpoint")
        config = runner.load_config(root)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if state.goal != goal or state.answers_fingerprint != json.dumps(answers, sort_keys=True):
            raise runner.RunnerError("goal or answers changed since approval")
        if state.config_fingerprint != canonical_fingerprint(config) or state.plan_fingerprint != canonical_fingerprint(plan):
            raise runner.RunnerError("configuration or plan changed since approval")
        if sas.is_child_alive(state):
            raise runner.RunnerError("previous runner still active")
        worktree = Path(state.expected_refs["worktree"])
        if runner.git(worktree, "symbolic-ref", "--short", "HEAD") != state.expected_refs["sprint_branch"]:
            raise runner.RunnerError("worktree branch identity mismatch")
        if runner.git(worktree, "rev-parse", "HEAD") != state.expected_refs["sprint_sha"]:
            raise runner.RunnerError("worktree ref changed; inspect the interrupted task before resuming")
        if runner.git(worktree, "status", "--porcelain"):
            raise runner.RunnerError("interrupted worktree contains changes; inspect them before resuming")
        mapping = state.tracker_uids["task_map"]
        task_list = [task | {"task_id": mapping[task["key"]]["id"]} for task in plan["tasks"]]
        results = execute.execute_task_loop(root, state.tracker_uids["sprint_id"], task_list, state,
                                            state.allowed_scopes, config)
        if len(results) != len(task_list) or not all(r["accept"] for r in results):
            return {"status": "execution_failed", "results": results, "worktree": str(worktree)}
        return finish(root, state, config) | {"results": results, "worktree": str(worktree)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yolo-force", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--goal", default="")
    parser.add_argument("--run-name", default="")
    parser.add_argument("--answers")
    parser.add_argument("--plan")
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    for value, message in ((args.yolo_force, "--yolo-force is required"), (args.goal, "--goal is required"),
                           (args.run_name, "--run-name is required")):
        if not value:
            print(message, file=sys.stderr)
            return 1
    try:
        root = Path(args.root).resolve()
        os.environ["TASKS_ROOT"] = str(root)
        answers_path = Path(args.answers) if args.answers else root / "tmp" / "plans" / f"{args.run_name}.answers.json"
        plan = Path(args.plan) if args.plan else root / "tmp" / "plans" / f"{args.run_name}.json"
        answers = json.loads(answers_path.read_text(encoding="utf-8"))
        action = resume if args.resume else run
        result = action(root, args.goal, args.run_name, answers, plan)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "finished" else 1
    except (runner.RunnerError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"sprint-auto: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
