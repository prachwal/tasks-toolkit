"""Markdown rendering for the task tracker view.

Extracted from tasks.py to keep the monolith small and testable.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Import from tasks_model and tasks_storage
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from tasks_model import *  # noqa: E402, F401, F403
from tasks_storage import *  # noqa: E402, F401, F403


def counts(nodes) -> dict[str, int]:
    """Count nodes by status."""
    result = {s: 0 for s in STATUSES}
    for n in nodes:
        result[n["status"]] += 1
    return result


def bar(done: int, total: int, width: int = 10) -> str:
    """Render a progress bar."""
    filled = round(width * done / total) if total else 0
    return "█" * filled + "░" * (width - filled)


def render_node(node: dict, indent: int, lines: list[str]) -> None:
    """Render a single node (and children) into lines."""
    status = effective(node)
    box = "x" if status == "done" else " "
    parts = [f"{'  ' * indent}- [{box}] **{node['id']}** {node['title']} — {BADGE[status]}"]
    if node["children"]:
        d, t = progress(node)
        parts.append(f"({d}/{t})")
    if node["assignee"]:
        parts.append(f"· @{node['assignee']}")
    if node["depends_on"]:
        parts.append("· after " + ", ".join(node["depends_on"]))
    kinds = " · ".join(sorted({link["kind"] for link in node.get("links", [])}))
    if kinds:
        parts.append(f"· [{kinds}]")
    lines.append(" ".join(parts))
    pad = "  " * (indent + 1)
    if status == "blocked" and node["reason"]:
        lines.append(f"{pad}- ⛔ **Blocked:** {node['reason']}")
    if node["description"]:
        for text in node["description"].splitlines():
            stripped = text.strip()
            if not stripped:
                continue
            lines.append(f"{pad}{text}" if stripped.startswith(("- ", "* ")) else f"{pad}- {text}")
    for note in node["notes"]:
        lines.append(f"{pad}- 📝 {note}")
    for child in node["children"]:
        render_node(child, indent + 1, lines)


def slugify(text: str) -> str:
    """Lowercase kebab slug for a sprint file name; falls back to 'sprint'."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "sprint"


def sprint_view_path(sprint: dict) -> Path:
    """Path of the per-sprint detail file inside the view directory."""
    return VIEW_DIR / f"sprint-{sprint['id']}-{slugify(sprint['title'])}.md"


def _sprint_body(sprint: dict) -> list[str]:
    """Body shared by the README's current sprint and the per-sprint file."""
    d, t = progress(sprint)
    lines: list[str] = []
    if sprint.get("goal"):
        lines += [f"**Goal:** {sprint['goal']}", ""]
    lines += [f"Progress: {d}/{t} `{bar(d, t)}`", ""]
    for note in sprint["notes"]:
        lines.append(f"📝 {note}")
    if sprint["notes"]:
        lines.append("")
    for task in sprint["children"]:
        render_node(task, 0, lines)
    lines.append("")
    return lines


def render_sprint_file(sprint: dict) -> str:
    """The per-sprint detail file (`sprint-<n>-<slug>.md`)."""
    lines = [f"# Sprint {sprint['id']} — {sprint['title']} {BADGE[effective(sprint)]}", ""]
    lines += _sprint_body(sprint)
    return "\n".join(lines).rstrip() + "\n"


def render_readme(data: dict) -> str:
    """The view index: overall + the current sprint inline + links to the per-sprint files."""
    sprints = data["sprints"]
    all_leaves = [leaf for s in sprints for leaf in leaves(s)]
    c = counts(all_leaves)
    actionable = [leaf for leaf in all_leaves if leaf["status"] != "cancelled"]
    done, total = c["done"], len(actionable)
    pct = round(100 * done / total) if total else 0
    lines = [
        f"# Tasks — {data['project']}",
        "",
        "> Generated from `tasks/tasks.db` by `scripts/tasks.py` after every change. Do not edit by hand.",
        "> Per-sprint detail lives in the `sprint-*.md` files next to this README.",
        "",
        f"**Overall:** {done}/{total} done ({pct}%) `{bar(done, total)}` · "
        f"🔄 {c['in_progress']} in progress · ⛔ {c['blocked']} blocked · "
        f"🚫 {c['cancelled']} cancelled · 🔎 {c['review']} review · ⬜ {c['none']} not started",
        "",
        "Status legend: ⬜ none (brak) · 🔄 in progress (w trakcie) · ⛔ blocked (bloker) · ✅ done (gotowe) · "
        "🚫 cancelled (anulowane). Parent status is derived from its children: any blocked → blocked, "
        "all done or cancelled → done, any started or finished → in progress, otherwise none.",
        "",
    ]
    if sprints:
        lines += ["## Sprints", ""]
        for s in sprints:
            d, t = progress(s)
            lines.append(f"- [Sprint {s['id']} — {s['title']}]({sprint_view_path(s).name}) — "
                         f"{BADGE[effective(s)]} `{bar(d, t)}` {d}/{t}")
        lines.append("")
    blockers = [n for s in sprints for n in leaves(s) if n["status"] == "blocked"]
    if blockers:
        lines += ["## ⛔ Blockers", ""]
        lines += [f"- **{n['id']}** {n['title']} — {n['reason']}" for n in blockers]
        lines.append("")
    if not sprints:
        lines += ["_No sprints yet. Start with `scripts/tasks.py add-sprint \"Title\"`._", ""]
    return "\n".join(lines).rstrip() + "\n"


def render(data: dict) -> str:
    """The main generated file: the README index (single-document entry point)."""
    return render_readme(data)


def expected_view(data: dict) -> dict:
    """{path: text} for every generated view file (the README index + one file per sprint)."""
    files = {README_FILE: render_readme(data)}
    for s in data["sprints"]:
        files[sprint_view_path(s)] = render_sprint_file(s)
    return files