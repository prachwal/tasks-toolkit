"""Core data model for the task tracker.

Extracted from tasks.py to keep the monolith small and testable.
"""
from __future__ import annotations

import datetime as dt
import secrets
import sqlite3
from typing import Any

STATUSES = ("none", "in_progress", "blocked", "done", "cancelled", "review")

ALIASES: dict[str, str] = {
    "none": "none", "brak": "none", "todo": "none", "open": "none",
    "in_progress": "in_progress", "in-progress": "in_progress", "w-trakcie": "in_progress",
    "w_trakcie": "in_progress", "doing": "in_progress", "wip": "in_progress", "start": "in_progress",
    "blocked": "blocked", "bloker": "blocked", "block": "blocked",
    "done": "done", "gotowe": "done", "finished": "done",
    "cancelled": "cancelled", "canceled": "cancelled", "anulowane": "cancelled", "anulowany": "cancelled",
    "review": "review", "w-przeglądzie": "review", "w-przegladzie": "review", "przeglad": "review",
}

BADGE: dict[str, str] = {
    "none": "⬜ none", "in_progress": "🔄 in progress", "blocked": "⛔ blocked",
    "done": "✅ done", "cancelled": "🚫 cancelled", "review": "🔎 review",
}

MAX_DEPTH = 3


class TaskError(Exception):
    """A user-facing error (bad id, invalid transition, invalid data)."""

    pass


def today() -> str:
    """Return today's date as ISO string."""
    return dt.date.today().isoformat()


def now_iso() -> str:
    """Local timestamp (second precision) used for audit events."""
    return dt.datetime.now().replace(microsecond=0).isoformat()


def actor() -> str:
    """Whoever is making the change: $TASKS_ACTOR, else $USER, else 'unknown'."""
    import os
    return os.environ.get("TASKS_ACTOR") or os.environ.get("USER") or "unknown"


def new_uid(conn: sqlite3.Connection | None = None) -> str:
    """A fresh immutable uid ('T-' + 4 base36 chars), unique among stored nodes."""
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    existing: set[str] = set()
    if conn is not None:
        existing = {r[0] for r in conn.execute("SELECT uid FROM nodes WHERE uid <> ''")}
    while True:
        uid = "T-" + "".join(secrets.choice(alphabet) for _ in range(4))
        if uid not in existing:
            return uid


def new_node(node_id: str, title: str, **fields: Any) -> dict[str, Any]:
    """Create a new node dict with defaults."""
    node: dict[str, Any] = {
        "id": node_id, "uid": "", "title": title, "description": "", "status": "none",
        "reason": "", "assignee": "", "depends_on": [], "notes": [], "links": [],
        "created": today(), "updated": today(), "started": "", "finished": "", "children": [],
    }
    node.update({k: v for k, v in fields.items() if v is not None})
    return node


def empty_data(project: str = "Project") -> dict[str, Any]:
    """Return an empty tracker data structure."""
    return {"version": 1, "project": project, "sprints": []}


def walk(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Yield every node depth-first in document order."""
    result: list[dict[str, Any]] = []
    for node in nodes:
        result.append(node)
        result.extend(walk(node["children"]))
    return result


def find(data: dict[str, Any], node_id: str) -> dict[str, Any]:
    """Find a node by id or uid."""
    for node in walk(data["sprints"]):
        if node["id"] == node_id or node.get("uid") == node_id:
            return node
    raise TaskError(f"no such id: {node_id}")


def parent_of(data: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    """Find the parent of a node."""
    for node in walk(data["sprints"]):
        if any(c["id"] == node_id for c in node["children"]):
            return node
    return None


def depth(node_id: str) -> int:
    """Return the depth of a node id (1 for sprint, 2 for task, 3 for subtask)."""
    return node_id.count(".") + 1


def effective(node: dict[str, Any]) -> str:
    """Status of a node: stored for leaves, derived from children otherwise."""
    if not node["children"]:
        return str(node["status"])
    states = [effective(c) for c in node["children"]]
    if "blocked" in states:
        return "blocked"
    if all(s == "done" for s in states):
        return "done"
    if all(s == "cancelled" for s in states):
        return "cancelled"
    if all(s in ("done", "cancelled") for s in states):
        return "done"
    if any(s in ("in_progress", "done", "review") for s in states):
        return "in_progress"
    return "none"


def leaves(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Yield all leaf nodes under a node."""
    result: list[dict[str, Any]] = []
    if not node["children"]:
        result.append(node)
    for child in node["children"]:
        result.extend(leaves(child))
    return result


def progress(node: dict[str, Any]) -> tuple[int, int]:
    """Return (done, actionable) leaf count."""
    all_leaves = [n for n in leaves(node) if n["status"] != "cancelled"]
    return sum(1 for n in all_leaves if n["status"] == "done"), len(all_leaves)


def next_child_id(parent: dict[str, Any] | None, data: dict[str, Any]) -> str:
    """Compute the next child id for a parent."""
    siblings = parent["children"] if parent else data["sprints"]
    top = 0
    for s in siblings:
        top = max(top, int(s["id"].rsplit(".", 1)[-1]))
    return f"{parent['id']}.{top + 1}" if parent else str(top + 1)


def normalize_status(value: str) -> str:
    """Normalize a status string to canonical form."""
    try:
        return ALIASES[value.strip().lower()]
    except KeyError as exc:
        raise TaskError(f"unknown status '{value}' (use: none/brak, in_progress/w-trakcie, blocked/bloker, done/gotowe)") from exc


def ready(data: dict[str, Any], node: dict[str, Any]) -> bool:
    """Check if a node is ready (all dependencies done)."""
    for dep in node.get("depends_on", []):
        try:
            dep_node = find(data, dep)
            if effective(dep_node) != "done":
                return False
        except TaskError:
            return False
    return True


LINK_KINDS = ("spec", "patch", "runner", "model", "agent", "commit", "branch", "fixes", "scope")

def validate(data: dict) -> None:
    seen: set[str] = set()
    uids: set[str] = set()
    ids = {n["id"] for n in walk(data["sprints"])}
    for node in walk(data["sprints"]):
        nid = node["id"]
        if nid in seen:
            raise TaskError(f"duplicate id {nid}")
        seen.add(nid)
        uid = node.get("uid", "")
        if not uid:
            raise TaskError(f"{nid}: missing uid")
        if uid in uids:
            raise TaskError(f"duplicate uid {uid}")
        uids.add(uid)
        for link in node.get("links", []):
            if link["kind"] not in LINK_KINDS:
                raise TaskError(f"{nid}: unknown link kind {link['kind']}")
        if depth(nid) > MAX_DEPTH:
            raise TaskError(f"{nid}: nesting deeper than sprint > task > subtask is not allowed")
        if node["status"] not in STATUSES:
            raise TaskError(f"{nid}: invalid status {node['status']}")
        if not node["children"] and node["status"] == "blocked" and not node["reason"]:
            raise TaskError(f"{nid}: a blocked item needs a reason")
        for dep in node["depends_on"]:
            if dep not in ids:
                raise TaskError(f"{nid}: depends on unknown id {dep}")
            if dep == nid or dep.startswith(nid + ".") or nid.startswith(dep + "."):
                raise TaskError(f"{nid}: cannot depend on its own ancestor/descendant {dep}")
    # dependency cycles
    graph = {n["id"]: n["depends_on"] for n in walk(data["sprints"])}
    state: dict[str, int] = {}

    def visit(n: str, path: list[str]) -> None:
        if state.get(n) == 2:
            return
        if state.get(n) == 1:
            raise TaskError("dependency cycle: " + " -> ".join(path + [n]))
        state[n] = 1
        for dep in graph[n]:
            visit(dep, path + [n])
        state[n] = 2

    for n in graph:
        visit(n, [])
