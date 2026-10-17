#!/usr/bin/env python3
"""Task tracker: sprints > tasks > subtasks, stored in a SQLite database (tasks/tasks.db).

Every command that changes the data runs in one transaction, validates the result (rolling back
on error) and re-renders the tasks/ view, so the Markdown view is never stale. Standard library only.
`export-json` / `import-json` give a diffable backup and a way to recover from a binary merge conflict.

Statuses (leaf nodes are set by hand; parents are derived, never stored):
    none (brak) | in_progress (w trakcie) | blocked (bloker) | done (gotowe)

Cascade up (task from subtasks, sprint from tasks):
    any child blocked                 -> blocked
    all children done                 -> done
    any child in progress or done     -> in_progress
    otherwise                         -> none

Usage: scripts/tasks.py <command> --help
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os as os
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import tasks_init as tasks_init
from scripts import (
    tasks_render,  # noqa: E402
    tasks_storage,  # noqa: E402
)
from scripts.tasks_model import (  # noqa: E402
    BADGE,
    MAX_DEPTH,
    TaskError,
    actor,
    depth,
    effective,
    find,
    leaves,
    new_uid,
    normalize_status,
    now_iso,
    parent_of,
    progress,
    ready,
    today,
    walk,
)
from scripts.tasks_model import (
    LINK_KINDS as LINK_KINDS,
)
from scripts.tasks_model import (
    validate as validate,
)
from scripts.tasks_render import (
    bar as bar,
)
from scripts.tasks_render import (  # noqa: E402
    counts as counts,
)
from scripts.tasks_render import (
    render as render,
)
from scripts.tasks_render import (
    render_node as render_node,
)
from scripts.tasks_render import (
    render_readme as render_readme,
)
from scripts.tasks_render import (
    render_sprint_file as render_sprint_file,
)
from scripts.tasks_render import (
    slugify as slugify,
)
from scripts.tasks_storage import (
    BACKUP_DIR as BACKUP_DIR,
)
from scripts.tasks_storage import (
    BACKUP_REMOTE as BACKUP_REMOTE,
)
from scripts.tasks_storage import (
    DATA_FILE as DATA_FILE,
)
from scripts.tasks_storage import (
    FIELDS as FIELDS,
)
from scripts.tasks_storage import (
    LOCK_FILE as LOCK_FILE,
)
from scripts.tasks_storage import (
    NODES_V2_DDL as NODES_V2_DDL,
)
from scripts.tasks_storage import (
    README_FILE as README_FILE,
)
from scripts.tasks_storage import (
    RECOVERY_DIR as RECOVERY_DIR,
)
from scripts.tasks_storage import (  # noqa: E402
    ROOT as ROOT,
)
from scripts.tasks_storage import (
    SNAPSHOT_FILE as SNAPSHOT_FILE,
)
from scripts.tasks_storage import (
    VIEW_DIR as VIEW_DIR,
)
from scripts.tasks_storage import (
    VIEW_FILE as VIEW_FILE,
)
from scripts.tasks_storage import (
    _testing_guard as _testing_guard,
)
from scripts.tasks_storage import (
    _write_atomic as _write_atomic,
)
from scripts.tasks_storage import (
    insert_node as insert_node,
)
from scripts.tasks_storage import (
    leaf_ids as leaf_ids,
)
from scripts.tasks_storage import (
    migrate as migrate,
)
from scripts.tasks_storage import (
    parse_ids as parse_ids,
)
from scripts.tasks_storage import (
    row as row,
)
from scripts.tasks_storage import (
    row_by_key as row_by_key,
)
from scripts.tasks_storage import (
    set_deps as set_deps,
)
from scripts.tasks_storage import (
    set_leaf_status as set_leaf_status,
)
from scripts.tasks_storage import (
    snapshot_json as snapshot_json,
)
from scripts.tasks_storage import (
    subtree_ids as subtree_ids,
)
from scripts.tasks_storage import (
    subtree_rows as subtree_rows,
)

CURRENT_ACTION = ""  # the CLI command currently running, recorded in the audit log




@contextlib.contextmanager
def using_root(root: Path):
    """Temporarily bind the CLI facade to an explicit project (automation callers)."""
    names = ("ROOT", "DATA_FILE", "VIEW_DIR", "README_FILE", "VIEW_FILE", "LOCK_FILE", "SNAPSHOT_FILE", "RECOVERY_DIR")
    previous = {name: globals()[name] for name in names}
    root = root.resolve()
    view = root / "tasks"
    globals().update(ROOT=root, DATA_FILE=view / "tasks.db", VIEW_DIR=view,
                     README_FILE=view / "README.md", VIEW_FILE=view / "README.md",
                     LOCK_FILE=view / ".lock", SNAPSHOT_FILE=view / "tasks.snapshot.json",
                     RECOVERY_DIR=view / "removed")
    try:
        yield
    finally:
        globals().update(previous)


def connect(create: bool = True) -> sqlite3.Connection:
    return tasks_storage.connect(create, path=DATA_FILE, project=ROOT.name)


def load(conn: sqlite3.Connection | None = None) -> dict:
    return tasks_storage.load(conn, path=DATA_FILE, project=ROOT.name)


class Locked(tasks_storage.Locked):
    def __init__(self):
        super().__init__(LOCK_FILE)


def sprint_view_path(sprint: dict) -> Path:
    return tasks_render.sprint_view_path(sprint, view_dir=VIEW_DIR)


def expected_view(data: dict) -> dict:
    return tasks_render.expected_view(data, view_dir=VIEW_DIR, readme_file=README_FILE)


def write_view(data: dict) -> None:
    tasks_storage.write_view(data, view_dir=VIEW_DIR, readme_file=README_FILE)


def write_snapshot(data: dict) -> None:
    tasks_storage.write_snapshot(data, SNAPSHOT_FILE)


def _auto_backup(data: dict) -> None:
    try:
        _backup_commit(data, f"{CURRENT_ACTION or 'change'}: auto")
    except Exception as exc:
        print(f"warning: backup failed: {exc}", file=sys.stderr)


def mutate(action) -> None:
    tasks_storage.mutate(
        action, action_name=CURRENT_ACTION or "change", connect_fn=connect, load_fn=load,
        write_view_fn=write_view, write_snapshot_fn=write_snapshot,
        lock_path=LOCK_FILE, after_commit=_auto_backup,
    )


def cmd_add_sprint(args):
    def action(conn):
        node_id = insert_node(conn, None, args.title, goal=args.goal, start=args.start, end=args.end)
        return f"added sprint {node_id}: {args.title}"
    mutate(action)


def cmd_add(args):
    def action(conn):
        parent = row(conn, args.parent)
        if parent["level"] >= MAX_DEPTH:
            raise TaskError(f"{args.parent} is already a subtask; subtasks cannot have children")
        has_children = conn.execute("SELECT 1 FROM nodes WHERE parent_id = ?", (args.parent,)).fetchone()
        if not has_children and parent["status"] != "none":
            # a leaf becoming a parent: its status is derived from its children from now on
            conn.execute("UPDATE nodes SET status='none', reason='', started='', finished='' WHERE id=?",
                         (args.parent,))
        node_id = insert_node(conn, args.parent, args.title, description=args.desc, assignee=args.assignee)
        set_deps(conn, node_id, parse_ids(args.after))
        conn.execute("UPDATE nodes SET updated=? WHERE id=?", (today(), args.parent))
        return f"added {node_id}: {args.title}"
    mutate(action)


def apply_status(conn: sqlite3.Connection, node_id: str, status: str, reason: str, cascade_down: bool) -> str:
    row(conn, node_id)
    targets = leaf_ids(conn, node_id)
    if targets != [node_id] and not cascade_down:
        raise TaskError(f"{node_id} has subtasks; its status is derived. Change a subtask, or pass --all "
                        "to apply the status to every item below it.")
    for target in targets:
        set_leaf_status(conn, target, status, reason)
    return f"{node_id}: set {BADGE[status]} on {len(targets)} item(s)"


def cmd_status(args):
    positionals = args.positionals
    if len(positionals) < 2:
        raise TaskError("usage: status <id> <status> [--reason R] [--all]  (or: status <id1> <id2> ... <status>)")
    status = normalize_status(positionals[-1])
    ids = positionals[:-1]
    def action(conn):
        msgs = []
        for nid in ids:
            msgs.append(apply_status(conn, nid, status, args.reason or "", args.all))
        return "\n".join(msgs)
    mutate(action)


def _get_ids(args) -> list[str]:
    """Extract ids list from args, supporting both 'ids' (nargs='+') and 'id' (single)."""
    if hasattr(args, "ids") and args.ids:
        return [str(value) for value in args.ids]
    if hasattr(args, "id") and args.id:
        return [args.id]
    raise TaskError("no id provided")


def shortcut(status: str):
    def run(args):
        reason = getattr(args, "reason", "") or ""
        cascade = getattr(args, "all", False)
        ids = getattr(args, "ids", None) or [args.id]
        def action(conn):
            msgs = []
            for nid in ids:
                msgs.append(apply_status(conn, nid, status, reason, cascade))
            return "\n".join(msgs)
        mutate(action)
    return run


def cmd_edit(args):
    ids = getattr(args, "ids", None) or [args.id]
    # --title with multiple ids is rejected
    if len(ids) > 1 and args.title is not None:
        raise TaskError("--title cannot be used with multiple ids")
    def action(conn):
        for nid in ids:
            node = row(conn, nid)
            sprint_only = {"goal": args.goal, "start": args.start, "end": args.end}
            changes = {"title": args.title, "description": args.desc, "assignee": args.assignee, **sprint_only}
            for key, value in changes.items():
                if value is None:
                    continue
                if key in sprint_only and node["level"] != 1:
                    raise TaskError(f"--{key} applies to sprints only")
                conn.execute(f'UPDATE nodes SET "{key}" = ? WHERE id = ?', (value, nid))
            if args.after is not None:
                set_deps(conn, nid, parse_ids(args.after))
            conn.execute("UPDATE nodes SET updated=? WHERE id=?", (today(), nid))
        if len(ids) == 1:
            return f"edited {ids[0]}"
        return f"edited {len(ids)} item(s): {', '.join(ids)}"
    mutate(action)


def cmd_note(args):
    def action(conn):
        row(conn, args.id)
        conn.execute("INSERT INTO notes (node_id, text) VALUES (?, ?)", (args.id, f"{today()}: {args.text}"))
        conn.execute("UPDATE nodes SET updated=? WHERE id=?", (today(), args.id))
        return f"noted on {args.id}"
    mutate(action)


def cmd_link(args):
    if args.kind not in LINK_KINDS:
        raise TaskError(f"unknown link kind '{args.kind}' (use: {', '.join(LINK_KINDS)})")

    def action(conn):
        node = row_by_key(conn, args.id)
        conn.execute("INSERT OR IGNORE INTO links (node_uid, kind, value, created) VALUES (?,?,?,?)",
                     (node["uid"], args.kind, args.value, today()))
        conn.execute("INSERT INTO events (ts,actor,action,node_uid,field,old,new) VALUES (?,?,?,?,?,?,?)",
                     (now_iso(), actor(), "link", node["uid"], args.kind, "", args.value))
        conn.execute("UPDATE nodes SET updated=? WHERE id=?", (today(), node["id"]))
        return f"linked {node['id']} {args.kind}={args.value}"
    mutate(action)


def cmd_unlink(args):
    def action(conn):
        node = row_by_key(conn, args.id)
        conn.execute("DELETE FROM links WHERE node_uid=? AND kind=? AND value=?",
                     (node["uid"], args.kind, args.value))
        conn.execute("INSERT INTO events (ts,actor,action,node_uid,field,old,new) VALUES (?,?,?,?,?,?,?)",
                     (now_iso(), actor(), "unlink", node["uid"], args.kind, args.value, ""))
        conn.execute("UPDATE nodes SET updated=? WHERE id=?", (today(), node["id"]))
        return f"unlinked {node['id']} {args.kind}={args.value}"
    mutate(action)


def _dedupe(ids: list[str]) -> list[str]:
    """Keep the first occurrence of each id, preserving order."""
    seen: list[str] = []
    for nid in ids:
        if nid not in seen:
            seen.append(nid)
    return seen


def _effective_roots(ids: list[str]) -> list[str]:
    """Drop ids already covered by another requested id (an ancestor's subtree includes it)."""
    return [nid for nid in ids if not any(nid.startswith(other + ".") for other in ids if other != nid)]


def _rm_totals(data: dict, ids: list[str]) -> tuple[int, int]:
    """Total number of nodes and notes across all subtrees being deleted."""
    all_ids = {n["id"] for i in ids for n in walk([find(data, i)])}
    notes = sum(len(n["notes"]) for n in walk(data["sprints"]) if n["id"] in all_ids)
    return len(all_ids), notes


def _rm_preview(data: dict, ids: list[str]) -> str:
    """Build the preview text (printed to stderr): one block per id, then totals and a warning."""
    all_ids = {n["id"] for i in ids for n in walk([find(data, i)])}
    outside = sorted(n["id"] for n in walk(data["sprints"])
                     if n["id"] not in all_ids and any(d in all_ids for d in n["depends_on"]))
    lines = []
    for i in ids:
        node = find(data, i)
        sub_ids = {n["id"] for n in walk([node])}
        deps_on_me = sorted(o for o in outside if any(d in sub_ids for d in find(data, o)["depends_on"]))
        parts = [f"{i}  «{node['title']}»", f"status={effective(node)}"]
        if node["assignee"]:
            parts.append(f"assignee={node['assignee']}")
        parts.append(f"subtasks={len(sub_ids) - 1}")
        parts.append(f"notes={len(node['notes'])}")
        if deps_on_me:
            parts.append("depends-on-me=" + ",".join(deps_on_me))
        lines.append("  ".join(parts))
    items, notes = _rm_totals(data, ids)
    lines.append(f"{items} items, {notes} notes will be deleted")
    risky = any(effective(find(data, i)) in ("in_progress", "done") for i in ids) \
        or notes > 0 or bool(outside)
    if risky:
        lines.append("warning: some items are in progress or done, have notes, or are depended on from outside")
    return "\n".join(lines) + "\n"


def _rm_question(data: dict, ids: list[str]) -> str:
    """The confirmation question: type the id (single), or the word `delete` (several ids)."""
    items, notes = _rm_totals(data, ids)
    if len(ids) == 1:
        node = find(data, ids[0])
        return (f"Delete {ids[0]} «{node['title']}» and everything below it ({items} items, {notes} notes)? "
                f"This cannot be undone from the database (a recovery file is written). "
                f"Type the id \"{ids[0]}\" to confirm, or press Enter to cancel:\n")
    return (f"Delete {', '.join(ids)} and everything below them ({items} items, {notes} notes)? "
            f"This cannot be undone from the database (a recovery file is written). "
            f"Type \"delete\" to confirm, or press Enter to cancel:\n")


def _confirmed(ids: list[str], data: dict, answer: str) -> bool:
    """Decide whether the typed answer confirms the deletion.

    Several ids always require the word `delete`. A single id requires the id itself, except
    plain empty leaves (no children, status none, no notes) which also accept `y`/`yes`.
    """
    if len(ids) > 1:
        return answer.strip().lower() == "delete"
    node = find(data, ids[0])
    plain = not node["children"] and node["status"] == "none" and not node["notes"]
    if answer.strip() == ids[0]:
        return True
    return plain and answer.strip().lower() in ("y", "yes")


def _tty() -> bool:
    """True when stdin is interactive, so we may ask for confirmation."""
    return sys.stdin.isatty()


def _ask(prompt: str) -> str:
    """Print the prompt to stderr and read one answer line from stdin (stdout stays pipeable)."""
    sys.stderr.write(prompt)
    sys.stderr.flush()
    return input()


def _recovery_path(ids: list[str]) -> Path:
    """Unique recovery file path: tasks/removed/<YYYYMMDD-HHMMSS>-<ids>.json."""
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{'-'.join(ids)}"
    path = RECOVERY_DIR / f"{base}.json"
    n = 2
    while path.exists():
        path = RECOVERY_DIR / f"{base}-{n}.json"
        n += 1
    return path


def _write_recovery(current: dict, ids: list[str], roots: list[str]) -> Path:
    """Write the export of the deleted subtrees (same node shape as export-json, plus each
    subtree's original parent). Written inside the lock right before the delete; a failure
    aborts the deletion."""
    entries = []
    for rid in roots:
        node = find(current, rid)
        parent = parent_of(current, rid)
        entries.append({"parent": parent["id"] if parent else None,
                        "node": json.loads(json.dumps(node))})
    text = json.dumps({"version": 2, "project": current["project"], "removed": entries},
                      sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    path = _recovery_path(ids)
    _testing_guard(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise TaskError(f"could not write recovery file {path}: {exc}") from exc
    return path


def cmd_rm(args):
    ids = _dedupe(args.ids)
    data = load()
    for nid in ids:
        find(data, nid)  # every id must exist before anything is printed
    sys.stderr.write(_rm_preview(data, ids))
    if args.dry_run:
        return 0
    if not args.yes:
        if _tty():
            answer = _ask(_rm_question(data, ids))
            if not _confirmed(ids, data, answer):
                print("cancelled", file=sys.stderr)
                return 1
        else:
            print("refusing to delete without confirmation: pass --yes (see --dry-run for the preview)",
                  file=sys.stderr)
            return 2

    def action(conn):
        current = load(conn)
        roots = _effective_roots(ids)
        all_ids = {r for rid in roots for r in subtree_ids(conn, rid)}
        outside = [n for n in walk(current["sprints"])
                   if n["id"] not in all_ids and any(d in all_ids for d in n["depends_on"])]
        if outside:
            dep = next(d for d in outside[0]["depends_on"] if d in all_ids)
            raise TaskError(f"{outside[0]['id']} depends on {dep}; remove the dependency first")
        _write_recovery(current, ids, roots)
        msgs = []
        for rid in roots:
            row(conn, rid)
            count = len(subtree_ids(conn, rid)) - 1
            conn.execute("DELETE FROM nodes WHERE id = ?", (rid,))  # children, deps, notes cascade
            msgs.append(f"removed {rid} and {count} item(s) below it")
        return "\n".join(msgs)

    mutate(action)


def _subtree_depth(node: dict) -> int:
    """Deepest depth (1-based) inside a saved subtree, relative to the document."""
    return max([depth(node["id"])] + [_subtree_depth(c) for c in node.get("children", [])])


def _restore_node(conn: sqlite3.Connection, parent_id: str | None, node: dict, id_map: dict[str, str]) -> str:
    """Insert one saved node with a fresh id under parent_id, recursing into children."""
    where, args = ("parent_id = ?", (parent_id,)) if parent_id else ("parent_id IS NULL", ())
    position = conn.execute(f"SELECT COALESCE(MAX(position), 0) + 1 FROM nodes WHERE {where}", args).fetchone()[0]
    new_id = f"{parent_id}.{position}" if parent_id else str(position)
    conn.execute(
        "INSERT INTO nodes (id, uid, parent_id, position, level, title, description, status, reason, assignee, "
        "goal, start, end, created, updated, started, finished) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_id, new_uid(conn), parent_id, position, depth(new_id), node["title"], node.get("description", ""),
         node.get("status", "none"), node.get("reason", ""), node.get("assignee", ""), node.get("goal", ""),
         node.get("start", ""), node.get("end", ""), node.get("created", today()), node.get("updated", today()),
         node.get("started", ""), node.get("finished", "")))
    id_map[node["id"]] = new_id
    for child in node.get("children", []):
        _restore_node(conn, new_id, child, id_map)
    return new_id


def cmd_restore_removed(args):
    path = Path(args.file)
    try:
        recovery = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TaskError(f"could not read recovery file {path}: {exc}") from exc
    entries = recovery.get("removed")
    if not isinstance(entries, list) or not entries:
        raise TaskError(f"{path}: not a recovery file (missing 'removed' entries)")

    def action(conn):
        id_map: dict[str, str] = {}
        for entry in entries:
            node = entry["node"]
            if not isinstance(node, dict) or "id" not in node or "title" not in node:
                raise TaskError(f"{path}: malformed recovery entry (node missing id/title)")
            parent_id = args.parent if args.parent is not None else entry.get("parent")
            if depth(node["id"]) == 1 and parent_id is not None:
                raise TaskError(f"{node['id']}: a sprint cannot be restored under {parent_id}")
            if parent_id is not None:
                if conn.execute("SELECT 1 FROM nodes WHERE id = ?", (parent_id,)).fetchone() is None:
                    raise TaskError(f"original parent {parent_id} no longer exists; pass --parent to restore elsewhere")
                parent_row = row(conn, parent_id)
                if parent_row["level"] + 1 + (_subtree_depth(node) - depth(node["id"])) > MAX_DEPTH:
                    raise TaskError(f"{node['id']}: too deep to restore under {parent_id}")
            _restore_node(conn, parent_id, node, id_map)
        skipped: list[tuple[str, str]] = []
        for entry in entries:
            for node in walk([entry["node"]]):
                new_id = id_map[node["id"]]
                for dep in node.get("depends_on", []):
                    if dep in id_map:
                        conn.execute("INSERT INTO deps VALUES (?, ?)", (new_id, id_map[dep]))
                    elif conn.execute("SELECT 1 FROM nodes WHERE id = ?", (dep,)).fetchone():
                        conn.execute("INSERT INTO deps VALUES (?, ?)", (new_id, dep))
                    else:
                        skipped.append((new_id, dep))
                for text in node.get("notes", []):
                    conn.execute("INSERT INTO notes (node_id, text) VALUES (?, ?)", (new_id, text))
        for entry in entries:
            parent_id = args.parent if args.parent is not None else entry.get("parent")
            if parent_id is not None:
                conn.execute("UPDATE nodes SET updated=? WHERE id=?", (today(), parent_id))
        lines = []
        for entry in entries:
            for node in walk([entry["node"]]):
                lines.append(f"restored {node['id']} -> {id_map[node['id']]}")
        for new_id, dep in skipped:
            lines.append(f"skipped dependency: {new_id} -> {dep} (id does not exist)")
        return "\n".join(lines)

    mutate(action)


def _rewrite_id(old_id: str, new_id: str, suffix: str) -> str:
    """Replace the old_id prefix with new_id in a full hierarchical id.
    suffix is the part after old_id, e.g. '.1' for '1.1.1' when old_id='1.1'."""
    # Strip leading dot from suffix to avoid double dots
    clean = suffix.lstrip(".")
    if clean:
        return f"{new_id}.{clean}"
    return new_id


def cmd_mv(args):
    def action(conn):
        src = row(conn, args.id)
        dst = row(conn, args.parent)

        # Cannot move a sprint (level 1)
        if src["level"] == 1:
            raise TaskError(f"{args.id}: cannot move a sprint")

        # Cannot move deeper than MAX_DEPTH
        if dst["level"] >= MAX_DEPTH:
            raise TaskError(f"{args.parent} is already a subtask; cannot move under it")

        # Check depth: a task with subtasks cannot move under another task (would exceed MAX_DEPTH)
        src_has_subtasks = conn.execute(
            "SELECT 1 FROM nodes WHERE parent_id = ?", (args.id,)).fetchone()
        if src_has_subtasks and dst["level"] == 2:
            raise TaskError(f"{args.id} has subtasks; cannot move under task {args.parent} (would exceed max depth)")

        # Cannot move into own subtree
        if args.parent.startswith(args.id + "."):
            raise TaskError(f"{args.id}: cannot move into its own subtree {args.parent}")

        # Display ids encode the position (1.2 is the 2nd child), so reordering within one parent would
        # need a renumbering of the siblings and their subtrees. It used to delete the node (cascading
        # to its subtree) and re-insert only the root. Refuse instead of losing data.
        if src["parent_id"] == dst["id"]:
            raise TaskError(f"{args.id}: already under {dst['id']}; reordering siblings is not supported "
                            "(ids encode the position) - move it to another parent instead")

        # Gather the full subtree
        old_ids = subtree_ids(conn, args.id)
        old_id_set = set(old_ids)

        new_parent_id = dst["id"]

        # --- Cross-parent move: renumber IDs ---
        # Determine target position
        where_clause, where_args = ("parent_id = ?", (new_parent_id,))
        if args.pos is not None:
            pos = args.pos
        else:
            pos = conn.execute(
                f"SELECT COALESCE(MAX(position), 0) + 1 FROM nodes WHERE {where_clause}",
                where_args).fetchone()[0]

        new_root_id = f"{new_parent_id}.{pos}" if new_parent_id else str(pos)
        old_root_id = args.id

        # Build mapping: old_id -> new_id for every node in the subtree
        id_map: dict[str, str] = {}
        rows = subtree_rows(conn, args.id)
        for r in rows:
            rid = r["id"]
            suffix = rid[len(old_root_id):]
            new_id = _rewrite_id(old_root_id, new_root_id, suffix)
            if depth(new_id) > MAX_DEPTH:
                raise TaskError(f"{rid}: new id {new_id} would exceed max depth")
            id_map[rid] = new_id

        # Check new ids don't collide with existing nodes (excluding ids being moved)
        new_ids = set(id_map.values())
        for nid in new_ids:
            if nid in old_id_set:
                continue
            existing = conn.execute("SELECT 1 FROM nodes WHERE id = ?", (nid,)).fetchone()
            if existing:
                raise TaskError(f"cannot move {args.id}: id {nid} already exists")

        # --- Phase 0: Save deps and notes referencing subtree ids ---
        # CASCADE DELETE will remove these when we delete old nodes.
        saved_deps: list[tuple[str, str]] = []
        for r in conn.execute("SELECT node_id, depends_on FROM deps "
                               "WHERE node_id IN ({}) OR depends_on IN ({})".format(
                                   ",".join("?" * len(old_id_set)),
                                   ",".join("?" * len(old_id_set))),
                              old_ids + old_ids):
            saved_deps.append((r["node_id"], r["depends_on"]))

        saved_notes: list[tuple[str, str]] = []
        for r in conn.execute("SELECT node_id, text, seq FROM notes WHERE node_id IN ({})".format(
                                 ",".join("?" * len(old_id_set))), old_ids):
            saved_notes.append((r["node_id"], r["text"], r["seq"]))

        # --- Phase 1: Delete old nodes (deepest-first to avoid FK violations) ---
        delete_order = sorted(rows, key=lambda r: r["level"], reverse=True)
        for r in delete_order:
            conn.execute("DELETE FROM nodes WHERE id = ?", (r["id"],))

        # --- Phase 2: Insert new nodes (shallowest-first so parents exist first) ---
        insert_order = sorted(rows, key=lambda r: r["level"])
        for r in insert_order:
            old_id = r["id"]
            new_id = id_map[old_id]
            old_parent = r["parent_id"]

            if old_parent is None:
                new_parent = new_parent_id
            elif old_parent in id_map:
                new_parent = id_map[old_parent]
            else:
                new_parent = old_parent

            new_pos = r["position"]
            if old_id == args.id:
                new_pos = pos
                new_parent = new_parent_id

            conn.execute(
                "INSERT INTO nodes (id, uid, parent_id, position, level, title, description, status, reason, "
                "assignee, goal, start, end, created, updated, started, finished) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id, r["uid"] or new_uid(conn), new_parent, new_pos, r["level"], r["title"], r["description"],
                 r["status"], r["reason"], r["assignee"], r["goal"], r["start"], r["end"],
                 r["created"], today(), r["started"], r["finished"]))

        # --- Phase 3: Restore deps and notes with rewritten ids ---
        for node_id, depends_on in saved_deps:
            new_node = id_map.get(node_id, node_id)
            new_dep = id_map.get(depends_on, depends_on)
            conn.execute("INSERT INTO deps VALUES (?, ?)", (new_node, new_dep))

        for node_id, text, seq in saved_notes:
            new_node = id_map.get(node_id, node_id)
            conn.execute("INSERT INTO notes (seq, node_id, text) VALUES (?, ?, ?)",
                         (seq, new_node, text))

        # Update the destination parent's timestamp
        conn.execute("UPDATE nodes SET updated=? WHERE id=?", (today(), new_parent_id))

        # Build output
        lines = [f"moved {args.id} -> {new_root_id} (uid {src['uid']})"]
        if len(id_map) > 1:
            lines.append("id changes:")
            for old_id, new_id in sorted(id_map.items()):
                if old_id != args.id:
                    lines.append(f"  {old_id} -> {new_id}")
        return "\n".join(lines)

    mutate(action)


def cmd_next(args):
    data = load()
    shown = 0
    for sprint in data["sprints"]:
        if effective(sprint) == "done":
            continue
        for leaf in leaves(sprint):
            if leaf["status"] in ("none", "in_progress") and ready(data, leaf):
                if args.assignee and leaf["assignee"] != args.assignee:
                    continue
                print(f"{leaf['id']}  {leaf['title']}  [{BADGE[leaf['status']]}]"
                      f"{'  @' + leaf['assignee'] if leaf['assignee'] else ''}")
                if leaf["description"] and args.count == 1:
                    print(leaf["description"])
                shown += 1
                if shown >= args.count:
                    return
    if not shown:
        print("nothing ready")


def cmd_list(args):
    data = load()
    wanted = normalize_status(args.status) if args.status else None
    needle = (args.text or "").lower()
    if args.id:
        roots = [find(data, args.id)]
    else:
        roots = [s for s in data["sprints"] if not args.sprint or s["id"] == args.sprint]
    for root_node in roots:
        for node in walk([root_node]):
            status = effective(node)
            if wanted and status != wanted:
                continue
            if args.assignee and node["assignee"] != args.assignee:
                continue
            if needle and needle not in (node["title"] + " " + node["description"]).lower():
                continue
            if args.leaves and node["children"]:
                continue
            pad = "  " * (depth(node["id"]) - 1)
            print(f"{pad}{node['id']:<8} {BADGE[status]:<16} {node['title']}")
            if args.desc and node["description"]:
                for text in node["description"].splitlines():
                    stripped = text.strip()
                    if not stripped:
                        continue
                    body = text if stripped.startswith(("- ", "* ")) else f"- {text}"
                    print(f"{pad}  {body}")
            if args.notes:
                for note in node["notes"]:
                    print(f"{pad}  - 📝 {note}")


def cmd_overview(args):
    """All sprints in one call: derived status, counts per status and the goal."""
    data = load()
    print(f"{data['project']} — {len(data['sprints'])} sprint(s)")
    for sprint in data["sprints"]:
        leaves_s = list(leaves(sprint))
        c = counts(leaves_s)
        goal = f" — {sprint['goal']}" if sprint.get("goal") else ""
        print(f"{sprint['id']}  {BADGE[effective(sprint)]}  {sprint['title']}{goal}")
        print(f"    done {c['done']}/{len(leaves_s)} · in progress {c['in_progress']} · review {c['review']}"
              f" · blocked {c['blocked']} · none {c['none']} · cancelled {c['cancelled']}")


def cmd_sprint_table(args):
    """One compact table of a sprint's tasks (status, assignee, progress)."""
    data = load()
    sprint = find(data, args.id)
    if depth(sprint["id"]) != 1:
        raise TaskError(f"{args.id} is not a sprint")
    print(f"Sprint {sprint['id']} — {sprint['title']} {BADGE[effective(sprint)]}")
    print("")
    print("| id | status | assignee | progress | title |")
    print("| --- | --- | --- | --- | --- |")
    for task in sprint["children"]:
        d, t = progress(task)
        prog = f"{d}/{t}" if task["children"] else ""
        print(f"| {task['id']} | {BADGE[effective(task)]} | {task['assignee']} | {prog} | {task['title']} |")


def cmd_stats(args):
    data = load()
    all_leaves = [leaf for s in data["sprints"] for leaf in leaves(s)]
    c = counts(all_leaves)
    print(f"items: {sum(1 for _ in walk(data['sprints']))}  leaves: {len(all_leaves)}  "
          f"done: {c['done']}  in progress: {c['in_progress']}  blocked: {c['blocked']}  "
          f"none: {c['none']}  cancelled: {c['cancelled']}")
    by_owner: dict[str, int] = {}
    for leaf in all_leaves:
        if leaf["status"] in ("done", "cancelled"):
            continue
        by_owner[leaf["assignee"] or "(unassigned)"] = by_owner.get(leaf["assignee"] or "(unassigned)", 0) + 1
    for owner, number in sorted(by_owner.items()):
        print(f"  open for {owner}: {number}")


def cmd_show(args):
    data = load()
    for node_id in args.id:
        node = find(data, node_id)
        status = effective(node)
        if args.brief:
            d, t = progress(node)
            empty, total = d, t
            extra = f" ({empty}/{total})" if node["children"] else ""
            owner = f" @{node['assignee']}" if node["assignee"] else ""
            deps = f" after {', '.join(node['depends_on'])}" if node["depends_on"] else ""
            goal = f" goal: {node['goal']}" if node.get("goal") else ""
            kinds = " · ".join(sorted({link["kind"] for link in node.get("links", [])}))
            links = f" [{kinds}]" if kinds else ""
            print(f"{node['id']}  {BADGE[status]}{extra}  {node['title']}{owner}{deps}{goal}{links}")
        else:
            print(json.dumps({**node, "children": [c["id"] for c in node["children"]],
                              "effective_status": status}, indent=2, ensure_ascii=False))


def cmd_log(args):
    if not DATA_FILE.exists():
        print("tasks: no database; nothing to log")
        return
    data = load()
    by_uid = {n["uid"]: n["id"] for n in walk(data["sprints"])}
    uid_filter = find(data, args.id)["uid"] if args.id else None
    conn = connect(create=False)
    try:
        rows = list(conn.execute("SELECT * FROM events ORDER BY id"))
    finally:
        conn.close()
    shown = 0
    for r in rows:
        if uid_filter and r["node_uid"] != uid_filter:
            continue
        if args.since and r["ts"] < args.since:
            continue
        disp = by_uid.get(r["node_uid"], r["node_uid"])
        change = f"{r['field']}: {r['old']} -> {r['new']}" if r["field"] else (r["new"] or r["old"])
        print(f"{r['ts']}  {r['actor']}  {r['action']}  {disp}  {change}")
        shown += 1
    if not shown:
        print("no events")


def _scopes_overlap(first: str, second: str) -> bool:
    """True when two scope paths are equal or one is a directory prefix of the other."""
    one, two = first.rstrip("/") + "/", second.rstrip("/") + "/"
    return one.startswith(two) or two.startswith(one)


def cmd_plan_batch(args):
    """Greedily prefer the least-conflicted ready tasks in the first unfinished sprint.

    Ready means every dependency is done, so the chosen tasks never depend on each other. Scopes overlap
    when equal or nested (src/a vs src/a/b). Ready tasks without a scope are skipped with a stderr warning.
    Output remains in id order; selection breaks ties by id."""
    data = load()
    ready_tasks: list[dict] = []
    for sprint in data["sprints"]:
        if effective(sprint) == "done":
            continue
        ready_tasks = [leaf for leaf in leaves(sprint) if leaf["status"] == "none" and ready(data, leaf)]
        break
    scoped_tasks: list[tuple[dict, list[str]]] = []
    for task in ready_tasks:
        scopes = [link["value"] for link in task.get("links", []) if link["kind"] == "scope"]
        if not scopes:
            print(f"warning: {task['id']} skipped: no scope link", file=sys.stderr)
            continue
        scoped_tasks.append((task, scopes))

    conflicts = [set() for _ in scoped_tasks]
    for first in range(len(scoped_tasks)):
        for second in range(first + 1, len(scoped_tasks)):
            first_scopes = scoped_tasks[first][1]
            second_scopes = scoped_tasks[second][1]
            if any(_scopes_overlap(a, b) for a in first_scopes for b in second_scopes):
                conflicts[first].add(second)
                conflicts[second].add(first)

    remaining = set(range(len(scoped_tasks)))
    chosen = set()
    while remaining:
        candidate = min(remaining, key=lambda index: (len(conflicts[index] & remaining), index))
        chosen.add(candidate)
        remaining.remove(candidate)
        remaining.difference_update(conflicts[candidate])
        if args.max and len(chosen) >= args.max:
            break
    for index in sorted(chosen):
        task = scoped_tasks[index][0]
        print(f"{task['id']}  {task['title']}")
    if not chosen:
        print("no disjoint ready tasks")


def cmd_brief(args):
    """Print the context bundle a spec author/agent needs for one item."""
    data = load()
    node = find(data, args.id)
    lines = [f"# {node['id']} {node['title']} — {BADGE[effective(node)]}"]
    if node["assignee"]:
        lines.append(f"Assignee: @{node['assignee']}")
    if node["description"]:
        lines += ["", node["description"]]
    if node["depends_on"]:
        lines += ["", "## Depends on"]
        for dep_id in node["depends_on"]:
            dep = find(data, dep_id)
            commits = [link["value"] for link in dep.get("links", []) if link["kind"] == "commit"]
            suffix = f" — commit {commits[-1]}" if commits else ""
            lines.append(f"- {dep['id']} {dep['title']} ({BADGE[effective(dep)]}){suffix}")
    specs = [link["value"] for link in node.get("links", []) if link["kind"] == "spec"]
    if specs:
        lines += ["", f"Spec: {specs[-1]}"]
    scopes = sorted({link["value"] for link in node.get("links", []) if link["kind"] == "scope"})
    if scopes:
        lines += ["", "Files: " + ", ".join(scopes)]
    if node["notes"]:
        lines += ["", "## Notes"] + [f"- {note}" for note in node["notes"]]
    print("\n".join(lines))


def cmd_render(args):
    connect(create=False).close()  # refuse to render from a database that does not exist
    with Locked():
        data = load()
        validate(data)
        write_view(data)
        count = len(data["sprints"])
    print(f"rendered {VIEW_DIR.name}/README.md + {count} sprint file(s)")


def cmd_check(args):
    if not DATA_FILE.exists():
        print("tasks: no database (tasks/tasks.db is git-ignored and not created yet); nothing to check")
        return
    data = load()
    validate(data)
    expected = expected_view(data)
    if not README_FILE.exists():
        # the view is git-ignored; if missing, regenerate the whole directory silently.
        write_view(data)
        regenerated_view = True
    elif any((not path.exists()) or path.read_text(encoding="utf-8") != text
             for path, text in expected.items()):
        raise TaskError("the task view is out of date; run: python3 scripts/tasks.py render")
    else:
        regenerated_view = False
    if not SNAPSHOT_FILE.exists():
        raise TaskError("tasks/tasks.snapshot.json is missing; run: python3 scripts/tasks.py snapshot")
    if SNAPSHOT_FILE.read_text(encoding="utf-8") != snapshot_json(data):
        raise TaskError("tasks/tasks.snapshot.json is out of date; run: python3 scripts/tasks.py snapshot")
    if regenerated_view:
        print("tasks OK (view regenerated)")
    else:
        print("tasks OK (valid, view and snapshot in sync)")


def cmd_export_json(args):
    text = json.dumps(load(), indent=2, ensure_ascii=False) + "\n"
    if args.file:
        Path(args.file).write_text(text, encoding="utf-8")
        print(f"exported to {args.file}")
    else:
        sys.stdout.write(text)


def cmd_snapshot(args):
    """Write the JSON snapshot explicitly; no-op otherwise."""
    if not DATA_FILE.exists():
        raise TaskError("no database; nothing to snapshot")
    data = load()
    validate(data)
    write_snapshot(data)
    print(f"wrote {SNAPSHOT_FILE}")


def _replace_from_source(source: dict, label: str) -> None:
    """Replace the whole database with `source` (an export dict); shared by import-json and restore."""

    def insert(conn, node, parent_id):
        position = int(node["id"].rsplit(".", 1)[-1])
        uid = node.get("uid") or new_uid(conn)
        node["uid"] = uid
        conn.execute(
            "INSERT INTO nodes (id, uid, parent_id, position, level, title, description, status, reason, assignee, "
            "goal, start, end, created, updated, started, finished) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (node["id"], uid, parent_id, position, depth(node["id"]), node["title"], node.get("description", ""),
             node.get("status", "none"), node.get("reason", ""), node.get("assignee", ""), node.get("goal", ""),
             node.get("start", ""), node.get("end", ""), node.get("created", today()), node.get("updated", today()),
             node.get("started", ""), node.get("finished", "")))
        for child in node.get("children", []):
            insert(conn, child, node["id"])

    def action(conn):
        conn.execute("DELETE FROM nodes")
        conn.execute("DELETE FROM notes")
        conn.execute("DELETE FROM deps")
        conn.execute("DELETE FROM links")
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('project', ?)", (source.get("project", ROOT.name),))
        for sprint in source["sprints"]:
            insert(conn, sprint, None)
        for node in walk(source["sprints"]):
            for dep in node.get("depends_on", []):
                conn.execute("INSERT INTO deps VALUES (?, ?)", (node["id"], dep))
            for text in node.get("notes", []):
                conn.execute("INSERT INTO notes (node_id, text) VALUES (?, ?)", (node["id"], text))
            for link in node.get("links", []):
                conn.execute("INSERT OR IGNORE INTO links (node_uid, kind, value, created) VALUES (?,?,?,?)",
                             (node["uid"], link["kind"], link["value"], link.get("created", today())))
        return f"imported {sum(1 for _ in walk(source['sprints']))} item(s) from {label}"
    mutate(action)


def cmd_import_json(args):
    """Replace the whole database with the contents of a JSON export (also migrates the old tasks.json)."""
    source = json.loads(Path(args.file).read_text(encoding="utf-8"))
    _replace_from_source(source, args.file)


def _backup_files(data: dict) -> dict[str, str]:
    """The files stored in the backup clone: the JSON snapshot plus the rendered view."""
    files = {"snapshot.json": snapshot_json(data)}
    for path, text in expected_view(data).items():
        files[path.name] = text
    return files


def _backup_git(*git_args: str, quiet: bool = False) -> subprocess.CompletedProcess:
    """Run git in the backup clone; a failure is reported on stderr (never raised, never silent)."""
    result = subprocess.run(["git", "-C", str(BACKUP_DIR), *git_args], check=False, capture_output=True, text=True)
    if result.returncode != 0 and not quiet:
        print(f"warning: backup: git {git_args[0]} failed: {result.stderr.strip()}", file=sys.stderr)
    return result


def _backup_commit(data: dict, message: str, push: bool = False) -> bool:
    """Write the snapshot + view into BACKUP_DIR and make a LOCAL commit there (no push unless asked).

    No-op when BACKUP_DIR is not a git repository yet, so nothing is written or pushed on its own.
    Git failures are reported on stderr as warnings."""
    if not (BACKUP_DIR / ".git").exists():
        return False
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in _backup_files(data).items():
        (BACKUP_DIR / name).write_text(text, encoding="utf-8")
    _backup_git("add", "-A")
    if _backup_git("diff", "--cached", "--quiet", quiet=True).returncode != 0:
        _backup_git("-c", "user.name=planner", "-c", "user.email=planner@localhost", "commit", "-q", "-m", message)
    if push:
        _backup_git("push", "-q", "origin", "HEAD")
    return True


def cmd_backup(args):
    """Write the snapshot + view into the backup clone and commit locally; --push pushes it."""
    if not DATA_FILE.exists():
        raise TaskError("no database; nothing to back up")
    data = load()
    if not (BACKUP_DIR / ".git").exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        if _backup_git("init", "-q").returncode != 0:
            raise TaskError(f"cannot initialise the backup clone in {BACKUP_DIR}")
        if BACKUP_REMOTE and _backup_git("remote", "add", "origin", BACKUP_REMOTE).returncode != 0:
            raise TaskError(f"cannot add the backup remote {BACKUP_REMOTE}")
    message = args.message or f"{CURRENT_ACTION or 'backup'}: {now_iso()}"
    _backup_commit(data, message, push=args.push)
    print(f"backed up to {BACKUP_DIR}{' (pushed)' if args.push else ''}")


def cmd_restore(args):
    """Replace the database from the backup clone's snapshot (optionally pulling first)."""
    if args.pull:
        result = _backup_git("pull", "-q", "--ff-only")
        if result.returncode != 0:
            raise TaskError("backup pull failed; refusing to restore from a potentially stale snapshot")
    snapshot = BACKUP_DIR / "snapshot.json"
    if not snapshot.exists():
        raise TaskError(f"no backup snapshot at {snapshot}")
    _replace_from_source(json.loads(snapshot.read_text(encoding="utf-8")), str(snapshot))
    print(f"restored from {snapshot}")


# --------------------------------------------------------------------------- CLI

def cmd_import_plan(args):
    """Atomically import a plan by run id (delegates to tasks_plan_import)."""
    from scripts import tasks_plan_import
    tasks_plan_import.cmd_import_plan(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add-sprint", help="create a sprint")
    p.add_argument("title")
    p.add_argument("--goal")
    p.add_argument("--start", help="YYYY-MM-DD")
    p.add_argument("--end", help="YYYY-MM-DD")
    p.set_defaults(func=cmd_add_sprint)

    p = sub.add_parser("add", help="add a task under a sprint id, or a subtask under a task id")
    p.add_argument("parent", help="sprint id (1) or task id (1.2)")
    p.add_argument("title")
    p.add_argument("-d", "--desc", help="description / acceptance criteria")
    p.add_argument("-a", "--assignee", help="who does it, e.g. junie, kilo, claude")
    p.add_argument("--after", help="comma separated ids that must be done first")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("status", help="set a status (leaf items; --all cascades down)", add_help=False)
    p.add_argument("positionals", nargs="*", help="ids followed by status, e.g. 1.1 1.2 done")
    p.add_argument("--reason", help="required for blocked")
    p.add_argument("--all", action="store_true", help="apply to every leaf below a parent")
    p.add_argument("-h", "--help", action="help", default=argparse.SUPPRESS,
                   help="show this help message and exit")
    p.set_defaults(func=cmd_status)

    for name, status, extra in (("start", "in_progress", False), ("done", "done", False),
                                ("reset", "none", False), ("block", "blocked", True)):
        p = sub.add_parser(name, help=f"shortcut for: status <id> {status}")
        p.add_argument("ids", nargs="+", help="one or more item ids")
        if extra:
            p.add_argument("--reason", required=True)
        p.add_argument("--all", action="store_true")
        p.set_defaults(func=shortcut(status))

    p = sub.add_parser("edit", help="change title, description, assignee, dependencies; sprint goal/dates")
    p.add_argument("ids", nargs="+", help="one or more item ids")
    p.add_argument("--title")
    p.add_argument("-d", "--desc")
    p.add_argument("-a", "--assignee")
    p.add_argument("--after", help="comma separated ids (replaces the list; empty string clears)")
    p.add_argument("--goal")
    p.add_argument("--start")
    p.add_argument("--end")
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("mv", help="move an item to another parent (renumbers the subtree)")
    p.add_argument("id", help="item id to move")
    p.add_argument("parent", help="new parent id")
    p.add_argument("--pos", type=int, help="1-based position (default: append last)")
    p.set_defaults(func=cmd_mv)

    p = sub.add_parser("note", help="append a dated note")
    p.add_argument("id")
    p.add_argument("text")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("link", help="attach a structured link (spec/patch/commit/branch/runner/model/...)")
    p.add_argument("id", help="display id or uid")
    p.add_argument("kind", help="one of: " + ", ".join(LINK_KINDS))
    p.add_argument("value")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("unlink", help="remove a structured link")
    p.add_argument("id", help="display id or uid")
    p.add_argument("kind")
    p.add_argument("value")
    p.set_defaults(func=cmd_unlink)

    p = sub.add_parser("rm", help="remove item(s) and everything below them (asks for confirmation)")
    p.add_argument("ids", nargs="+", help="one or more item ids")
    p.add_argument("--yes", action="store_true",
                   help="skip the confirmation question (the preview and the recovery file still happen)")
    p.add_argument("--dry-run", action="store_true", help="show the preview only; change nothing")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("restore-removed", help="restore a recovery file written by rm (fresh ids)")
    p.add_argument("file", help="path to a tasks/removed/*.json recovery file")
    p.add_argument("--parent", help="restore under this id instead of the original parent")
    p.set_defaults(func=cmd_restore_removed)

    p = sub.add_parser("next", help="first ready, unfinished leaf in the first unfinished sprint")
    p.add_argument("--assignee")
    p.add_argument("-n", "--count", type=int, default=1, help="show up to N ready items")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("list", help="print the tree (or one subtree with a positional id)")
    p.add_argument("id", nargs="?", help="only this item and everything below it")
    p.add_argument("--status")
    p.add_argument("--sprint")
    p.add_argument("--assignee")
    p.add_argument("--text", help="case-insensitive match in title/description")
    p.add_argument("--leaves", action="store_true", help="only items without children")
    p.add_argument("--desc", action="store_true", help="also print each item's description")
    p.add_argument("--notes", action="store_true", help="also print each item's notes")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("stats", help="counts per status and open work per assignee")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("sprint-table", help="one table of a sprint's tasks (status, assignee, progress)")
    p.add_argument("id", help="sprint id")
    p.set_defaults(func=cmd_sprint_table)

    p = sub.add_parser("overview", help="all sprints in one call: status, counts per status, goal")
    p.set_defaults(func=cmd_overview)

    p = sub.add_parser("show", help="print one or more items as JSON (or a one-line summary with --brief)")
    p.add_argument("id", nargs="+")
    p.add_argument("--brief", action="store_true", help="one line per item instead of JSON")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("log", help="print the audit log (optionally for one item, since a timestamp)")
    p.add_argument("id", nargs="?", help="display id or uid")
    p.add_argument("--since", help="only events at or after this timestamp")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("brief", help="context bundle for one item (description, deps with commits, spec, notes)")
    p.add_argument("id", help="display id or uid")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("plan-batch", help="largest set of ready tasks with disjoint scope links")
    p.add_argument("--max", type=int, help="cap the batch size")
    p.set_defaults(func=cmd_plan_batch)

    p = sub.add_parser("render", help="regenerate the tasks/ view (README + one file per sprint)")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("export-json", help="dump the database as JSON (stdout or --file)")
    p.add_argument("--file")
    p.set_defaults(func=cmd_export_json)

    p = sub.add_parser("import-json", help="REPLACE the database with a JSON export / the old tasks.json")
    p.add_argument("file")
    p.set_defaults(func=cmd_import_json)

    p = sub.add_parser("check", help="validate the database and that the tasks/ view is in sync (used by pre-push)")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("snapshot", help="write the committed JSON snapshot (tasks/tasks.snapshot.json)")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("backup", help="write the snapshot + view into the backup clone and commit locally")
    p.add_argument("--push", action="store_true", help="also push to the backup remote")
    p.add_argument("--message", help="commit message")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore", help="replace the database from the backup clone's snapshot")
    p.add_argument("--pull", action="store_true", help="pull the backup clone first")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("import-plan", help="atomically import a validated plan by run id (automation)")
    p.add_argument("plan", help="path to the plan JSON file")
    p.add_argument("--run-id", required=True, help="unique run identity for replay detection")
    p.set_defaults(func=cmd_import_plan)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    global CURRENT_ACTION
    CURRENT_ACTION = getattr(args, "command", "") or ""
    try:
        code = args.func(args)
    except TaskError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return code or 0


if __name__ == "__main__":
    sys.exit(main())
