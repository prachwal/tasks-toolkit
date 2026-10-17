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
import datetime as dt
import fcntl
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tasks_init  # noqa: E402

# Agents work in disposable worktrees (<main>/tmp/**); the tracker always lives in the main checkout.
ROOT = tasks_init.resolve_root(Path(__file__).resolve().parent.parent)
DATA_FILE = ROOT / "tasks" / "tasks.db"
VIEW_DIR = ROOT / "tasks"             # generated Markdown view directory (git-ignored)
README_FILE = VIEW_DIR / "README.md"  # index: overall + current sprint + links to the per-sprint files
VIEW_FILE = README_FILE               # the primary generated file (alias for the README index)
LOCK_FILE = ROOT / "tasks" / ".lock"
SNAPSHOT_FILE = ROOT / "tasks" / "tasks.snapshot.json"
RECOVERY_DIR = ROOT / "tasks" / "removed"
# Backup clone (outside the project) with history; nothing pushes on its own (explicit --push only).
BACKUP_DIR = Path(os.environ.get("VIBE_TASKS_BACKUP_DIR", str(Path.home() / ".local" / "share" / "vibe-tasks")))
BACKUP_REMOTE = os.environ.get("VIBE_TASKS_BACKUP_REMOTE", "git@gitlab.com:prachwal/vibe-emulator-tasks.git")

STATUSES = ("none", "in_progress", "blocked", "done", "cancelled", "review")
ALIASES = {
    "none": "none", "brak": "none", "todo": "none", "open": "none",
    "in_progress": "in_progress", "in-progress": "in_progress", "w-trakcie": "in_progress",
    "w_trakcie": "in_progress", "doing": "in_progress", "wip": "in_progress", "start": "in_progress",
    "blocked": "blocked", "bloker": "blocked", "block": "blocked",
    "done": "done", "gotowe": "done", "finished": "done",
    "cancelled": "cancelled", "canceled": "cancelled", "anulowane": "cancelled", "anulowany": "cancelled",
    "review": "review", "w-przeglądzie": "review", "w-przegladzie": "review", "przeglad": "review",
}
BADGE = {"none": "⬜ none", "in_progress": "🔄 in progress", "blocked": "⛔ blocked",
         "done": "✅ done", "cancelled": "🚫 cancelled", "review": "🔎 review"}
MAX_DEPTH = 3  # sprint > task > subtask

# `nodes` rebuilt with the 'cancelled' status; used by migrate() for databases created before it.
NODES_V2_DDL = """
CREATE TABLE nodes_new (
    id          TEXT PRIMARY KEY,
    uid         TEXT NOT NULL DEFAULT '',
    parent_id   TEXT REFERENCES nodes(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    level       INTEGER NOT NULL CHECK (level BETWEEN 1 AND 3),
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'none' CHECK (status IN ('none','in_progress','blocked','done','cancelled','review')),
    reason      TEXT NOT NULL DEFAULT '',
    assignee    TEXT NOT NULL DEFAULT '',
    goal        TEXT NOT NULL DEFAULT '',
    start       TEXT NOT NULL DEFAULT '',
    end         TEXT NOT NULL DEFAULT '',
    created     TEXT NOT NULL,
    updated     TEXT NOT NULL,
    started     TEXT NOT NULL DEFAULT '',
    finished    TEXT NOT NULL DEFAULT '',
    UNIQUE (parent_id, position)
);
"""


class TaskError(Exception):
    """A user-facing error (bad id, invalid transition, invalid data)."""


# --------------------------------------------------------------------------- model

def today() -> str:
    return dt.date.today().isoformat()


def now_iso() -> str:
    """Local timestamp (second precision) used for audit events."""
    return dt.datetime.now().replace(microsecond=0).isoformat()  # noqa: DTZ005 - local by design


def actor() -> str:
    """Whoever is making the change: $VIBE_ACTOR, else $USER, else 'unknown'."""
    return os.environ.get("VIBE_ACTOR") or os.environ.get("USER") or "unknown"


CURRENT_ACTION = ""  # the CLI command currently running, recorded in the audit log


def new_uid(conn: sqlite3.Connection | None = None) -> str:
    """A fresh immutable uid (`T-` + 4 base36 chars), unique among the nodes already stored."""
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    existing: set[str] = set()
    if conn is not None:
        existing = {r[0] for r in conn.execute("SELECT uid FROM nodes WHERE uid <> ''")}
    while True:
        uid = "T-" + "".join(secrets.choice(alphabet) for _ in range(4))
        if uid not in existing:
            return uid


def new_node(node_id: str, title: str, **fields) -> dict:
    node = {
        "id": node_id, "uid": "", "title": title, "description": "", "status": "none", "reason": "",
        "assignee": "", "depends_on": [], "notes": [], "links": [], "created": today(), "updated": today(),
        "started": "", "finished": "", "children": [],
    }
    node.update({k: v for k, v in fields.items() if v is not None})
    return node


def empty_data(project: str = "Project") -> dict:
    return {"version": 1, "project": project, "sprints": []}


def walk(nodes):
    """Yield every node depth-first in document order."""
    for node in nodes:
        yield node
        yield from walk(node["children"])


def find(data: dict, node_id: str) -> dict:
    for node in walk(data["sprints"]):
        if node["id"] == node_id or node.get("uid") == node_id:
            return node
    raise TaskError(f"no such id: {node_id}")


def parent_of(data: dict, node_id: str) -> dict | None:
    for node in walk(data["sprints"]):
        if any(c["id"] == node_id for c in node["children"]):
            return node
    return None


def depth(node_id: str) -> int:
    return node_id.count(".") + 1


def effective(node: dict) -> str:
    """Status of a node: stored for leaves, derived from children otherwise (cascade up).

    Cancelled children are terminal: all done or cancelled -> done, all cancelled -> cancelled, and
    cancelled leaves never block a parent from completing.
    """
    if not node["children"]:
        return node["status"]
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


def leaves(node: dict):
    if not node["children"]:
        yield node
    for child in node["children"]:
        yield from leaves(child)


def progress(node: dict) -> tuple[int, int]:
    """(done, actionable) leaves; cancelled leaves are excluded from the denominator."""
    all_leaves = [n for n in leaves(node) if n["status"] != "cancelled"]
    return sum(1 for n in all_leaves if n["status"] == "done"), len(all_leaves)


def next_child_id(parent: dict | None, data: dict) -> str:
    siblings = parent["children"] if parent else data["sprints"]
    top = 0
    for s in siblings:
        top = max(top, int(s["id"].rsplit(".", 1)[-1]))
    return f"{parent['id']}.{top + 1}" if parent else str(top + 1)


def normalize_status(value: str) -> str:
    try:
        return ALIASES[value.strip().lower()]
    except KeyError:
        raise TaskError(f"unknown status '{value}' (use: none/brak, in_progress/w-trakcie, blocked/bloker, done/gotowe)")


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


# --------------------------------------------------------------------------- storage

FIELDS = ("id", "uid", "title", "description", "status", "reason", "assignee", "goal", "start", "end",
          "created", "updated", "started", "finished")


LINK_KINDS = ("spec", "patch", "runner", "model", "agent", "commit", "branch", "fixes", "scope")


def connect(create: bool = True) -> sqlite3.Connection:
    """Open the database; when the file is missing, create it from scratch (see tasks_init.py)."""
    if not DATA_FILE.exists():
        if not create:
            raise TaskError(f"no database at {DATA_FILE.parent.name}/{DATA_FILE.name}; run: python3 scripts/tasks_init.py")
        tasks_init.create_database(DATA_FILE, ROOT.name)
        print(f"note: created a new empty task database at {DATA_FILE.parent.name}/{DATA_FILE.name}", file=sys.stderr)
    conn = sqlite3.connect(DATA_FILE, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current schema (idempotent, FK-safe).

    Adds the immutable `uid` column (backfilling it) and the `links` table, and rebuilds `nodes`
    when its CHECK predates the 'cancelled'/'review' statuses. Dependencies and notes are untouched
    and re-resolve to the rebuilt table by name.
    """
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='nodes'").fetchone()
    if row is None:
        return
    sql = row[0] or ""
    columns = [c[1] for c in conn.execute("PRAGMA table_info(nodes)")]
    have_uid = "uid" in columns
    needs_rebuild = "cancelled" not in sql or "review" not in sql

    if needs_rebuild:
        cols = ("id, uid, parent_id, position, level, title, description, status, reason, assignee, "
                "goal, start, end, created, updated, started, finished")
        select = cols if have_uid else cols.replace("uid, ", "'' AS uid, ")
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN")
            conn.execute(NODES_V2_DDL)
            conn.execute(f"INSERT INTO nodes_new ({cols}) SELECT {select} FROM nodes")
            conn.execute("DROP TABLE nodes")
            conn.execute("ALTER TABLE nodes_new RENAME TO nodes")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_assignee ON nodes(assignee)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id, position)")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    elif not have_uid:
        conn.execute("ALTER TABLE nodes ADD COLUMN uid TEXT NOT NULL DEFAULT ''")

    conn.execute("CREATE TABLE IF NOT EXISTS links ("
                 "node_uid TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL, created TEXT NOT NULL, "
                 "PRIMARY KEY (node_uid, kind, value))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_links_uid ON links(node_uid)")
    conn.execute("CREATE TABLE IF NOT EXISTS events ("
                 "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL, "
                 "action TEXT NOT NULL, node_uid TEXT NOT NULL DEFAULT '', field TEXT NOT NULL DEFAULT '', "
                 "old TEXT NOT NULL DEFAULT '', new TEXT NOT NULL DEFAULT '')")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_node ON events(node_uid)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_uid ON nodes(uid) WHERE uid <> ''")
    _backfill_uids(conn)


def _backfill_uids(conn: sqlite3.Connection) -> None:
    """Give every node that has no uid a fresh unique one."""
    rows = conn.execute("SELECT id FROM nodes WHERE uid = ''").fetchall()
    if not rows:
        return
    existing = {r[0] for r in conn.execute("SELECT uid FROM nodes WHERE uid <> ''")}
    conn.execute("BEGIN")
    try:
        for (node_id,) in rows:
            uid = new_uid(conn)
            while uid in existing:
                uid = new_uid(conn)
            existing.add(uid)
            conn.execute("UPDATE nodes SET uid = ? WHERE id = ?", (uid, node_id))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def load(conn: sqlite3.Connection | None = None) -> dict:
    """Build the in-memory tree (same shape the renderer and validator use) from the database."""
    if conn is None and not DATA_FILE.exists():
        return {"version": 2, "project": ROOT.name, "sprints": []}  # reading never creates the database
    own = conn is None
    conn = conn or connect()
    try:
        project = conn.execute("SELECT value FROM meta WHERE key = 'project'").fetchone()[0]
        deps: dict[str, list[str]] = {}
        for r in conn.execute("SELECT node_id, depends_on FROM deps ORDER BY depends_on"):
            deps.setdefault(r["node_id"], []).append(r["depends_on"])
        notes: dict[str, list[str]] = {}
        for r in conn.execute("SELECT node_id, text FROM notes ORDER BY seq"):
            notes.setdefault(r["node_id"], []).append(r["text"])
        links: dict[str, list[dict]] = {}
        for r in conn.execute("SELECT node_uid, kind, value, created FROM links ORDER BY created, kind, value"):
            links.setdefault(r["node_uid"], []).append(
                {"kind": r["kind"], "value": r["value"], "created": r["created"]})
        by_id: dict[str, dict] = {}
        sprints: list[dict] = []
        for r in conn.execute("SELECT * FROM nodes ORDER BY level, parent_id, position"):
            node = {k: r[k] for k in FIELDS}
            node.update(depends_on=deps.get(r["id"], []), notes=notes.get(r["id"], []),
                        links=links.get(r["uid"], []), children=[])
            by_id[r["id"]] = node
            (by_id[r["parent_id"]]["children"] if r["parent_id"] else sprints).append(node)
        return {"version": 2, "project": project, "sprints": sprints}
    finally:
        if own:
            conn.close()


class Locked:
    """Exclusive lock so parallel agents cannot interleave a change and its re-render."""

    def __enter__(self):
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._fh = LOCK_FILE.open("w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


# --------------------------------------------------------------------------- rendering

def counts(nodes) -> dict:
    result = {s: 0 for s in STATUSES}
    for n in nodes:
        result[n["status"]] += 1
    return result


def bar(done: int, total: int, width: int = 10) -> str:
    filled = round(width * done / total) if total else 0
    return "█" * filled + "░" * (width - filled)


def render_node(node: dict, indent: int, lines: list[str]) -> None:
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
            # Never double-prefix a line that is already a list item, and never emit a lone "-".
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


def _testing_guard(target: Path) -> None:
    """When VIBE_TASKS_TESTING=1, refuse to write a file that is not under a system temp dir.

    This is a safety net so a missed setUp redirect in a test class cannot overwrite the
    real tracker files in the main checkout.
    """
    if os.environ.get("VIBE_TASKS_TESTING") != "1":
        return
    import tempfile as _tf
    tmp_dir = Path(_tf.gettempdir()).resolve()
    try:
        target.resolve().relative_to(tmp_dir)
    except ValueError:
        raise TaskError(f"refusing to write outside a temp dir while testing: {target}")


def _write_atomic(target: Path, text: str) -> None:
    """Write text to target atomically: a temp file in the same directory, then os.replace.

    A reader (or the tasks_watch poller) never sees a truncated or half-written file, and a crash
    mid-write leaves the previous content intact. Standard library only.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_view(data: dict) -> None:
    files = expected_view(data)
    VIEW_DIR.mkdir(parents=True, exist_ok=True)
    for path, text in files.items():
        _testing_guard(path)
        _write_atomic(path, text)
    keep = {p.name for p in files}
    for stale in VIEW_DIR.glob("sprint-*.md"):
        if stale.name not in keep:
            stale.unlink()


def snapshot_json(data: dict) -> str:
    """Deterministic JSON snapshot: sorted keys, 2-space indent, trailing newline, no timestamps."""
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def write_snapshot(data: dict) -> None:
    """Write the committed JSON snapshot file (tasks/tasks.snapshot.json)."""
    _testing_guard(SNAPSHOT_FILE)
    _write_atomic(SNAPSHOT_FILE, snapshot_json(data))


# --------------------------------------------------------------------------- commands

def _log_events(conn: sqlite3.Connection, before: dict, after: dict) -> None:
    """Write one audit event per added/removed node and per changed field (inside the transaction)."""
    when = now_iso()
    who = actor()
    action = CURRENT_ACTION or "change"
    for node_id in sorted(set(before) | set(after)):
        old_row = before.get(node_id)
        new_row = after.get(node_id)
        uid = (new_row or old_row)["uid"]
        if old_row is None:
            conn.execute("INSERT INTO events (ts,actor,action,node_uid,field,old,new) VALUES (?,?,?,?,?,?,?)",
                         (when, who, f"{action}:add", uid, "", "", new_row["title"]))
            continue
        if new_row is None:
            conn.execute("INSERT INTO events (ts,actor,action,node_uid,field,old,new) VALUES (?,?,?,?,?,?,?)",
                         (when, who, f"{action}:remove", uid, "", old_row["title"], ""))
            continue
        for field in ("title", "description", "status", "reason", "assignee", "goal", "start", "end"):
            if old_row[field] != new_row[field]:
                conn.execute("INSERT INTO events (ts,actor,action,node_uid,field,old,new) VALUES (?,?,?,?,?,?,?)",
                             (when, who, action, uid, field, old_row[field], new_row[field]))


def mutate(action):
    """Run action(conn) in one transaction, validate the result, then commit and re-render.
    Both the tasks/ view and the JSON snapshot are written after a successful commit; on rollback nothing is written."""
    with Locked():
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                before = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM nodes")}
                message = action(conn)
                after = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM nodes")}
                _log_events(conn, before, after)
                data = load(conn)
                validate(data)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()
        write_view(data)
        write_snapshot(data)
        try:
            _backup_commit(data, f"{CURRENT_ACTION or 'change'}: auto")
        except Exception as exc:  # noqa: BLE001 - the backup must never break a change
            print(f"warning: backup failed: {exc}", file=sys.stderr)
    if message:
        print(message)


def row(conn: sqlite3.Connection, node_id: str) -> sqlite3.Row:
    found = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if found is None:
        raise TaskError(f"no such id: {node_id}")
    return found


def row_by_key(conn: sqlite3.Connection, key: str) -> sqlite3.Row:
    """Look a node up by its display id or its immutable uid."""
    found = conn.execute("SELECT * FROM nodes WHERE id = ? OR uid = ?", (key, key)).fetchone()
    if found is None:
        raise TaskError(f"no such id: {key}")
    return found


def subtree_ids(conn: sqlite3.Connection, node_id: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT id FROM nodes WHERE id = ? OR id LIKE ? ORDER BY level, position", (node_id, node_id + ".%"))]


def subtree_rows(conn: sqlite3.Connection, node_id: str) -> list[sqlite3.Row]:
    """Return all rows in the subtree (including the root) ordered by level, position."""
    return list(conn.execute(
        "SELECT * FROM nodes WHERE id = ? OR id LIKE ? ORDER BY level, position", (node_id, node_id + ".%")))


def leaf_ids(conn: sqlite3.Connection, node_id: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT id FROM nodes n WHERE (id = ? OR id LIKE ?) "
        "AND NOT EXISTS (SELECT 1 FROM nodes c WHERE c.parent_id = n.id) ORDER BY level, position",
        (node_id, node_id + ".%"))]


def parse_ids(text: str | None) -> list[str]:
    return [x for x in (text or "").replace(" ", "").split(",") if x]


def set_deps(conn: sqlite3.Connection, node_id: str, deps: list[str]) -> None:
    conn.execute("DELETE FROM deps WHERE node_id = ?", (node_id,))
    for dep in dict.fromkeys(deps):
        if conn.execute("SELECT 1 FROM nodes WHERE id = ?", (dep,)).fetchone() is None:
            raise TaskError(f"{node_id}: depends on unknown id {dep}")
        conn.execute("INSERT INTO deps VALUES (?, ?)", (node_id, dep))


def insert_node(conn: sqlite3.Connection, parent_id: str | None, title: str, **fields) -> str:
    where, args = ("parent_id = ?", (parent_id,)) if parent_id else ("parent_id IS NULL", ())
    position = conn.execute(f"SELECT COALESCE(MAX(position), 0) + 1 FROM nodes WHERE {where}", args).fetchone()[0]
    node_id = f"{parent_id}.{position}" if parent_id else str(position)
    uid = new_uid(conn)
    values = {"description": "", "assignee": "", "goal": "", "start": "", "end": ""}
    values.update({k: v for k, v in fields.items() if v is not None})
    conn.execute(
        "INSERT INTO nodes (id, uid, parent_id, position, level, title, description, assignee, goal, start, end, "
        "created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (node_id, uid, parent_id, position, depth(node_id), title, values["description"], values["assignee"],
         values["goal"], values["start"], values["end"], today(), today()))
    return node_id


def set_leaf_status(conn: sqlite3.Connection, node_id: str, status: str, reason: str) -> None:
    current = row(conn, node_id)
    if status == "blocked" and not reason:
        raise TaskError(f"{node_id}: use --reason to say what blocks it")
    started = current["started"] or (today() if status == "in_progress" else "")
    finished = today() if status == "done" else ("" if current["status"] == "done" else current["finished"])
    conn.execute("UPDATE nodes SET status=?, reason=?, started=?, finished=?, updated=? WHERE id=?",
                 (status, reason if status == "blocked" else "", started, finished, today(), node_id))


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
        return args.ids
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
        raise TaskError(f"could not write recovery file {path}: {exc}")
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
        raise TaskError(f"could not read recovery file {path}: {exc}")
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


def ready(data: dict, node: dict) -> bool:
    return all(effective(find(data, d)) == "done" for d in node["depends_on"])


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
        if _backup_git("remote", "add", "origin", BACKUP_REMOTE).returncode != 0:
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
    import tasks_plan_import
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
