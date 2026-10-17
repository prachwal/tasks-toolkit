"""Prepare approved plans and branches for the automated sprint lifecycle (Sprint 12, task 12.4).

Advances an approved new run through planning, validated spec materialization,
atomic tracker import and creation/reconciliation of its exact sprint branch.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import sprint_auto_preflight as sap
from scripts import sprint_auto_state as sas
from scripts import tasks  # noqa: E402
from scripts import tasks_plan_import as tpi
from scripts.runner_config import RunnerError, is_protected, protected_paths  # noqa: E402
from scripts.task_runner import scope_conformity as check_scope_conformity  # noqa: E402


def _run_git(root: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _main_sha(root: Path) -> str:
    return _run_git(root, "rev-parse", "HEAD") or _run_git(root, "rev-parse", "main")


def prepare_run(root: Path, request: dict, answers: dict, run_name: str,
                plan_path: Path | None = None) -> dict:
    with tasks.using_root(root):
        return _prepare_run(root, request, answers, run_name, plan_path)


def _prepare_run(root: Path, request: dict, answers: dict, run_name: str,
                plan_path: Path | None = None) -> dict:
    """Prepare a run: preflight recheck, plan, import, branch.

    Returns a dict with keys: status, preflight, state, plan, import_receipt, branch.
    Does not run agents or create artifacts before approval.
    """
    root = Path(root).resolve()
    result: dict = {"status": "started", "run_name": run_name}

    if sas.state_path(root, run_name).exists():
        return {"status": "run_exists", "message": "checkpoint exists; use sprint-auto.sh --resume"}

    # 1. Preflight recheck (read-only, no writes)
    preflight = sap.collect_preflight(root, request, answers)
    result["preflight"] = preflight
    if preflight["blockers"]:
        result["status"] = "blocked"
        return result
    if preflight["questions"]:
        result["status"] = "needs_answers"
        return result

    # 2. Obtain exclusive lock
    lock = sas.acquire_lock(root, run_name)
    with lock:
        # Recheck preflight under lock
        preflight2 = sap.collect_preflight(root, request, answers)
        if preflight2["blockers"] or preflight2["questions"]:
            result["status"] = "blocked_after_lock"
            result["preflight"] = preflight2
            return result

        # 3. Bind state
        git_common = Path(_run_git(root, "rev-parse", "--git-common-dir") or (root / ".git"))
        if not git_common.is_absolute():
            git_common = root / git_common
        main_sha = _main_sha(root)
        answers_fp = json.dumps(answers, sort_keys=True)
        state = sas.bind_state(
            root, git_common, run_name, request.get("goal", ""),
            answers_fp, answers.get("allowed_scopes", []),
            main_sha, preflight2["approval_scope"]["runner"], "", 120,
        )
        state.plan_fingerprint = ""
        result["state"] = state.to_dict()

        # 4. Plan: use existing plan if provided, else invoke planner
        if plan_path and Path(plan_path).exists():
            plan = json.loads(Path(plan_path).read_text())
        else:
            # Fresh planning attempt — invoke plan-sprint.sh
            plan_file = root / "tmp" / "plans" / f"{run_name}.json"
            if not plan_file.exists():
                # No existing plan — would invoke planner here
                result["status"] = "needs_plan"
                result["message"] = "No plan found; supply --plan with a validated sprint plan"
                return result
            plan = json.loads(plan_file.read_text())

        # Validate plan structure
        sprint_info = plan.get("sprint", {})
        if not isinstance(sprint_info, dict):
            result["status"] = "invalid_plan"
            result["message"] = "plan.sprint must be an object"
            return result
        if not sprint_info.get("slug") or not sprint_info.get("title"):
            result["status"] = "invalid_plan"
            result["message"] = "plan missing sprint slug/title"
            return result
        # slug format: lowercase words joined by single hyphens
        import re
        if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", sprint_info["slug"]):
            result["status"] = "invalid_plan"
            result["message"] = f"invalid sprint slug: {sprint_info['slug']!r}"
            return result
        tasks_list = plan.get("tasks", [])
        if not isinstance(tasks_list, list) or not tasks_list:
            result["status"] = "invalid_plan"
            result["message"] = "plan has no tasks"
            return result

        try:
            protected = protected_paths(root)
        except RunnerError as exc:
            result.update(status="invalid_plan", message=str(exc))
            return result

        # Validate each task
        keys = set()
        for i, t in enumerate(tasks_list):
            if not isinstance(t, dict):
                result["status"] = "invalid_plan"
                result["message"] = f"task {i} is not an object"
                return result
            if not t.get("key") or not t.get("title"):
                result["status"] = "invalid_plan"
                result["message"] = f"task {i} missing key/title"
                return result
            key = t["key"]
            if key in keys:
                result["status"] = "invalid_plan"
                result["message"] = f"duplicate task key: {key}"
                return result
            keys.add(key)
            # spec_name format
            spec_name = t.get("spec_name", "")
            if not isinstance(spec_name, str) or not re.fullmatch(r"^[a-z0-9][a-z0-9-]*$", spec_name):
                result["status"] = "invalid_plan"
                result["message"] = f"invalid spec_name: {spec_name!r}"
                return result
            if not isinstance(t.get("spec"), str) or not t["spec"].strip():
                result.update(status="invalid_plan", message=f"task {key} needs spec text")
                return result
            if not isinstance(t.get("scope"), list) or not t["scope"]:
                result.update(status="invalid_plan", message=f"task {key} needs at least one scope")
                return result
            # scope validation: no forbidden paths, no generated tracker paths
            for scope in t.get("scope", []):
                if not isinstance(scope, str) or ".." in scope or scope.startswith("/"):
                    result["status"] = "invalid_plan"
                    result["message"] = f"invalid scope: {scope!r}"
                    return result
                if is_protected(scope, protected):
                    result["status"] = "invalid_plan"
                    result["message"] = f"forbidden scope: {scope!r}"
                    return result
                if scope.startswith("tasks/") and (scope.endswith(".md") or scope.endswith(".json")):
                    result["status"] = "invalid_plan"
                    result["message"] = f"generated tracker path in scope: {scope!r}"
                    return result

        # Validate dependencies: must reference earlier keys
        seen_keys = set()
        for t in tasks_list:
            for dep in t.get("depends_on", []):
                if dep not in seen_keys:
                    result["status"] = "invalid_plan"
                    result["message"] = f"task {t['key']} depends on unknown/forward key: {dep}"
                    return result
            seen_keys.add(t["key"])

        # Check overlapping scopes (tasks with overlapping scopes must be ordered by dependency)
        scope_map = {t["key"]: set(t.get("scope", [])) for t in tasks_list}
        for i, t1 in enumerate(tasks_list):
            for t2 in tasks_list[i + 1:]:
                if t1["key"] in t2.get("depends_on", []):
                    continue
                if t2["key"] in t1.get("depends_on", []):
                    continue
                overlap = scope_map[t1["key"]] & scope_map[t2["key"]]
                if overlap:
                    result["status"] = "invalid_plan"
                    result["message"] = f"overlapping scopes between unordered tasks {t1['key']} and {t2['key']}: {overlap}"
                    return result

        # Bind plan fingerprint
        state.plan_fingerprint = tpi.canonical_fingerprint(plan)
        result["plan"] = plan

        # 5. Validate scopes against approval
        allowed = set(answers.get("allowed_scopes", []))
        for t in plan.get("tasks", []):
            for scope in t.get("scope", []):
                if allowed and not check_scope_conformity([scope], list(allowed))[0]:
                    result["status"] = "scope_violation"
                    result["message"] = f"scope {scope!r} not in allowed scopes"
                    return result

        # 6. Atomic import
        run_id = f"{run_name}"
        receipt_holder = {}

        def action(conn):
            r = tpi.import_plan(conn, plan, run_id, root)
            receipt_holder.update(r)
            return ""

        tasks.mutate(action)
        result["import_receipt"] = receipt_holder

        # 7. Sprint branch creation/reconciliation
        sprint_info = plan["sprint"]
        sprint_n = None
        # Try to extract sprint number from existing tracker
        # For new sprints, we need to determine the number
        sprint_id = receipt_holder.get("sprint_id", "")
        # Extract number from sprint_id (e.g., "14" -> 14)
        try:
            sprint_n = int(sprint_id.split(".")[0])
        except (ValueError, AttributeError):
            sprint_n = 1

        slug = sprint_info["slug"]
        branch_name = f"sprint/{sprint_n}-{slug}"
        existing_branch = _run_git(root, "rev-parse", "--verify", branch_name)
        if existing_branch:
            # Reconcile: verify ownership (branch must point to approved main SHA or descendant)
            branch_sha = _run_git(root, "rev-parse", branch_name)
            # Check if branch is at or ahead of main_sha
            merge_base = _run_git(root, "merge-base", main_sha, branch_sha)
            if merge_base == main_sha:
                result["branch"] = {"name": branch_name, "action": "reconciled", "sha": branch_sha}
            else:
                result["status"] = "branch_ownership_unproven"
                result["message"] = f"branch {branch_name} does not descend from approved main SHA"
                return result
        else:
            # Create new branch from approved main SHA (plain git; worktree.sh removed)
            create_result = subprocess.run(
                ["git", "branch", branch_name, main_sha],
                capture_output=True, text=True, cwd=str(root),
            )
            if create_result.returncode != 0:
                result["status"] = "branch_creation_failed"
                result["message"] = create_result.stderr or create_result.stdout
                return result
            result["branch"] = {"name": branch_name, "action": "created", "sha": main_sha}

        # Write checkpoint
        state.expected_refs = {"sprint_branch": branch_name, "sprint_sha": result["branch"]["sha"]}
        sas.write_checkpoint(sas.state_path(root, run_name), state)
        result["status"] = "prepared"
        return result


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Prepare approved plans and branches")
    parser.add_argument("--root", default=".")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--answers", required=True)
    parser.add_argument("--plan", default=None)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    answers = json.loads(Path(args.answers).read_text())
    request = {"goal": args.goal}
    result = prepare_run(root, request, answers, args.run_name, args.plan)
    print(json.dumps(result, indent=2))
    if result["status"] in ("blocked", "blocked_after_lock", "needs_answers"):
        return 4 if "blocked" in result["status"] else 2
    if result["status"] in ("needs_plan", "invalid_plan", "scope_violation"):
        return 1
    return 0 if result["status"] == "prepared" else 1


if __name__ == "__main__":
    sys.exit(main())
