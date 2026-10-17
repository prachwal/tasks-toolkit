#!/usr/bin/env python3
"""Sprint plan tooling for the Kilo planner agent (see knowledge/local-planner-design.md).

  sprint_plan.py extract  <raw.txt> <plan.json>   pull the JSON plan out of the model's answer
  sprint_plan.py validate <plan.json> [--no-lint] [--specs-written] schema, flow rules and spec-lint; errors on stderr, exit 4
  sprint_plan.py specs    <plan.json>             write each task's spec to tmp/tasks/<spec_name>.md
  sprint_plan.py preview  <plan.json>             human-readable summary
  sprint_plan.py apply    <plan.json>             import through tasks.py (add-sprint, add, link spec|scope)

Exit codes: 0 ok, 1 usage/IO, 4 invalid plan. Only `apply` writes the tracker, and only through tasks.py.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SPEC_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
KEY = re.compile(r"^[A-Za-z0-9_-]+$")
CLEAN_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")
FORBIDDEN = (".git", ".githooks", ".claude", ".junie", ".kilo", "tmp", "kilo.json", ".mcp.json",
             "tasks/tasks.db", "tasks/.lock")
MAX_TASKS = 12


class PlanError(Exception):
    """Raised for an unusable plan or model answer."""


def extract_plan(text: str) -> dict:
    """Return the plan object: the first fenced json block, else the outermost {...}."""
    fence = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    candidate = fence.group(1) if fence else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise PlanError("no JSON object found in the model answer")
        candidate = text[start:end + 1]
    try:
        plan = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise PlanError(f"the plan is not valid JSON: {exc}") from exc
    if not isinstance(plan, dict):
        raise PlanError("the plan must be a JSON object")
    return plan


def _path_problem(path: object) -> str | None:
    """Why a scope/path is not allowed, or None."""
    if not isinstance(path, str) or not path:
        return "must be a non-empty string"
    if not CLEAN_PATH.match(path) or path.startswith("/") or ".." in path.split("/"):
        return "must be a clean repo-relative path ([A-Za-z0-9._/-], no '..')"
    if any(seg.startswith("-") or not seg for seg in path.split("/")):
        return "has an empty segment or one starting with '-'"
    for bad in FORBIDDEN:
        if path == bad or path.startswith(bad + "/"):
            return f"touches the forbidden path {bad}"
    return None


def validate_plan(plan: dict, lint: bool = True, specs_written: bool = False) -> list[str]:
    """Return every violation of the flow rules (empty list = valid).

    With specs_written (apply time) the spec files must already exist; otherwise (plan time) they must not."""
    errors: list[str] = []
    sprint = plan.get("sprint")
    if not isinstance(sprint, dict):
        return ["sprint: missing object"]
    if not isinstance(sprint.get("slug"), str) or not SLUG.match(sprint["slug"]):
        errors.append("sprint.slug: lowercase words joined by single hyphens")
    for field in ("title", "goal"):
        if not isinstance(sprint.get(field), str) or not sprint[field].strip():
            errors.append(f"sprint.{field}: required")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return errors + ["tasks: a non-empty list is required"]
    if len(tasks) > MAX_TASKS:
        errors.append(f"tasks: at most {MAX_TASKS}, got {len(tasks)}")
    seen_keys: set[str] = set()
    seen_specs: set[str] = set()
    for index, task in enumerate(tasks, 1):
        label = f"task #{index}"
        if not isinstance(task, dict):
            errors.append(f"{label}: must be an object")
            continue
        key = task.get("key")
        if not isinstance(key, str) or not KEY.match(key):
            errors.append(f"{label}: key must match {KEY.pattern}")
        elif key in seen_keys:
            errors.append(f"{label}: duplicate key '{key}'")
        label = f"task '{key}'"
        title = task.get("title")
        if not isinstance(title, str) or not title.strip() or title.endswith(".") or re.match(r"^\w+(\(.*\))?:", title):
            errors.append(f"{label}: title required, no trailing period, no conventional-commit prefix")
        name = task.get("spec_name")
        if not isinstance(name, str) or not SPEC_NAME.match(name):
            errors.append(f"{label}: spec_name must match {SPEC_NAME.pattern}")
        elif name in seen_specs:
            errors.append(f"{label}: duplicate spec_name '{name}'")
        elif (ROOT / "tmp" / "tasks" / f"{name}.md").exists() != specs_written:
            errors.append(f"{label}: tmp/tasks/{name}.md " + ("is missing" if specs_written else "already exists; pick another spec_name"))
        if not isinstance(task.get("spec"), str) or not task["spec"].strip():
            errors.append(f"{label}: spec text required")
        scope = task.get("scope")
        if not isinstance(scope, list) or not scope:
            errors.append(f"{label}: at least one scope path is required")
        else:
            errors += [f"{label}: scope '{p}' {why}" for p in scope if (why := _path_problem(p))]
        deps = task.get("depends_on", [])
        if not isinstance(deps, list) or any(d not in seen_keys for d in deps):
            errors.append(f"{label}: depends_on must list earlier task keys")
        if isinstance(key, str):
            seen_keys.add(key)
        if isinstance(name, str):
            seen_specs.add(name)
    if lint and not errors:
        errors += _lint_specs(tasks)
    return errors


def _lint_specs(tasks: list[dict]) -> list[str]:
    """Run scripts/spec-lint.sh on every spec (written to a temp dir, not to tmp/tasks)."""
    import tempfile
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for task in tasks:
            path = Path(tmp) / f"{task['spec_name']}.md"
            path.write_text(task["spec"].rstrip() + "\n", encoding="utf-8")
            result = subprocess.run(["bash", str(ROOT / "scripts" / "spec-lint.sh"), str(path)],
                                    capture_output=True, text=True, check=False)
            if result.returncode != 0:
                problems += [f"task '{task['key']}': {line}" for line in result.stderr.strip().splitlines()]
    return problems


def write_specs(plan: dict) -> list[Path]:
    """Write every task's spec to tmp/tasks/<spec_name>.md."""
    out = ROOT / "tmp" / "tasks"
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for task in plan["tasks"]:
        path = out / f"{task['spec_name']}.md"
        path.write_text(task["spec"].rstrip() + "\n", encoding="utf-8")
        paths.append(path)
    return paths


def preview(plan: dict) -> str:
    """Summary shown before the human confirms."""
    sprint = plan["sprint"]
    lines = [f"Sprint {sprint['slug']}: {sprint['title']}", f"Goal: {sprint['goal']}", ""]
    for task in plan["tasks"]:
        deps = f" after {','.join(task['depends_on'])}" if task.get("depends_on") else ""
        lines.append(f"  [{task['key']}] {task['title']}{deps}")
        lines.append(f"       scope: {', '.join(task['scope'])}   spec: tmp/tasks/{task['spec_name']}.md")
    return "\n".join(lines)


def _tasks_py(*args: str) -> str:
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "tasks.py"), *args], capture_output=True,
                            text=True, check=False)
    if result.returncode != 0:
        raise PlanError(f"tasks.py {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def apply_plan(plan: dict) -> dict[str, str]:
    """Create the sprint and its tasks through tasks.py; returns {task key: tracker id}."""
    sprint = plan["sprint"]
    out = _tasks_py("add-sprint", sprint["title"], "--goal", sprint["goal"])
    match = re.search(r"added sprint (\d+)", out)
    if not match:
        raise PlanError(f"cannot read the sprint id from: {out.strip()}")
    sprint_id = match.group(1)
    ids: dict[str, str] = {}
    for task in plan["tasks"]:
        args = ["add", sprint_id, task["title"]]
        if task.get("description"):
            args += ["-d", task["description"]]
        if task.get("depends_on"):
            args += ["--after", ",".join(ids[k] for k in task["depends_on"])]
        match = re.search(r"added (\d+(?:\.\d+)+)", _tasks_py(*args))
        if not match:
            raise PlanError(f"cannot read the id of task '{task['key']}'")
        ids[task["key"]] = match.group(1)
        _tasks_py("link", ids[task["key"]], "spec", f"tmp/tasks/{task['spec_name']}.md")
        for path in task["scope"]:
            _tasks_py("link", ids[task["key"]], "scope", path)
    return ids


def main(argv: list[str]) -> int:
    """CLI entry point."""
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 1
    command, target = argv[1], Path(argv[2])
    try:
        if command == "extract":
            if len(argv) < 4:
                print("usage: sprint_plan.py extract <raw.txt> <plan.json>", file=sys.stderr)
                return 1
            plan = extract_plan(target.read_text(encoding="utf-8"))
            Path(argv[3]).write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            return 0
        plan = json.loads(target.read_text(encoding="utf-8"))
        if command == "validate":
            errors = validate_plan(plan, lint="--no-lint" not in argv, specs_written="--specs-written" in argv)
            for error in errors:
                print(f"plan: {error}", file=sys.stderr)
            return 4 if errors else 0
        if command == "specs":
            for path in write_specs(plan):
                print(path.relative_to(ROOT))
        elif command == "preview":
            print(preview(plan))
        elif command == "apply":
            for key, task_id in apply_plan(plan).items():
                print(f"{key} -> {task_id}")
        else:
            print(f"unknown command '{command}'", file=sys.stderr)
            return 1
    except PlanError as exc:
        print(f"plan: {exc}", file=sys.stderr)
        return 4
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
