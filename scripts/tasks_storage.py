"""SQLite storage layer for the task tracker.

Extracted from tasks.py to keep the monolith small and testable.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import tasks_init  # noqa: E402

# Re-use ROOT resolution from tasks_init
ROOT = tasks_init.resolve_root(Path.cwd())
DATA_FILE = ROOT / "tasks" / "tasks.db"
VIEW_DIR = ROOT / "tasks"
README_FILE = VIEW_DIR / "README.md"
VIEW_FILE = README_FILE
LOCK_FILE = ROOT / "tasks" / ".lock"
SNAPSHOT_FILE = ROOT / "tasks" / "tasks.snapshot.json"
RECOVERY_DIR = ROOT / "tasks" / "removed"
BACKUP_DIR = Path(os.environ.get("TASKS_BACKUP_DIR", str(Path.home() / ".local" / "share" / "tasks")))
BACKUP_REMOTE = os.environ.get("TASKS_BACKUP_REMOTE")  # optional

FIELDS = ("id", "uid", "title", "description", "status", "reason", "assignee", "goal", "start", "end",
          "created", "updated", "started", "finished")

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


from scripts.tasks_model import LINK_KINDS as LINK_KINDS  # noqa: E402
from scripts.tasks_model import TaskError, actor, depth, new_uid, now_iso, today, validate  # noqa: E402


def connect(create: bool = True, path: Path | None = None, project: str | None = None) -> sqlite3.Connection:
    """Open the database; create from scratch if missing."""
    db_path = path if path is not None else DATA_FILE
    if not db_path.exists():
        if not create:
            raise TaskError(f"no database at {db_path.parent.name}/{db_path.name}; run: python3 scripts/tasks_init.py")
        from scripts import tasks_init
        tasks_init.create_database(db_path, project or ROOT.name)
        print(f"note: created a new empty task database at {db_path.parent.name}/{db_path.name}", file=__import__("sys").stderr)
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current schema (idempotent, FK-safe)."""
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


def load(conn: sqlite3.Connection | None = None, *, path: Path | None = None, project: str | None = None) -> dict:
    """Build the in-memory tree from the database."""
    db_path = path if path is not None else DATA_FILE
    if conn is None and not db_path.exists():
        return {"version": 2, "project": project or ROOT.name, "sprints": []}
    own = conn is None
    conn = conn or connect(path=db_path)
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

    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else LOCK_FILE

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


def _testing_guard(target: Path) -> None:
    """When TASKS_TESTING=1, refuse to write outside a temp dir."""
    import os
    if os.environ.get("TASKS_TESTING") != "1":
        return
    import tempfile
    tmp_dir = Path(tempfile.gettempdir()).resolve()
    try:
        target.resolve().relative_to(tmp_dir)
    except ValueError:
        raise TaskError(f"refusing to write outside a temp dir while testing: {target}") from None


def _write_atomic(target: Path, text: str) -> None:
    """Write text to target atomically."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_view(data: dict, *, view_dir: Path | None = None, readme_file: Path | None = None) -> None:
    """Regenerate the tasks/ view (README + per-sprint files)."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts import tasks_render  # noqa: E402
    view_dir = view_dir if view_dir is not None else VIEW_DIR
    files = tasks_render.expected_view(data, view_dir=view_dir, readme_file=readme_file or README_FILE)
    view_dir.mkdir(parents=True, exist_ok=True)
    for path, text in files.items():
        _testing_guard(path)
        _write_atomic(path, text)
    keep = {p.name for p in files}
    for stale in view_dir.glob("sprint-*.md"):
        if stale.name not in keep:
            stale.unlink()


def snapshot_json(data: dict) -> str:
    """Deterministic JSON snapshot."""
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def write_snapshot(data: dict, path: Path | None = None) -> None:
    """Write the committed JSON snapshot file."""
    target = path if path is not None else SNAPSHOT_FILE
    _testing_guard(target)
    _write_atomic(target, snapshot_json(data))


def _log_events(conn: sqlite3.Connection, before: dict, after: dict, action: str = "change") -> None:
    """Write one audit event per added/removed node and per changed field (inside the transaction)."""
    when = now_iso()
    who = actor()
    for node_id in sorted(set(before) | set(after)):
        old_row = before.get(node_id)
        new_row = after.get(node_id)
        current = new_row if new_row is not None else old_row
        assert current is not None
        uid = current["uid"]
        if old_row is None:
            assert new_row is not None
            conn.execute("INSERT INTO events (ts,actor,action,node_uid,field,old,new) VALUES (?,?,?,?,?,?,?)",
                         (when, who, f"{action}:add", uid, "", "", new_row["title"]))
            continue
        if new_row is None:
            assert old_row is not None
            conn.execute("INSERT INTO events (ts,actor,action,node_uid,field,old,new) VALUES (?,?,?,?,?,?,?)",
                         (when, who, f"{action}:remove", uid, "", old_row["title"], ""))
            continue
        for field in ("title", "description", "status", "reason", "assignee", "goal", "start", "end"):
            if old_row[field] != new_row[field]:
                conn.execute("INSERT INTO events (ts,actor,action,node_uid,field,old,new) VALUES (?,?,?,?,?,?,?)",
                             (when, who, action, uid, field, old_row[field], new_row[field]))


def mutate(action, *, action_name="change", connect_fn=None, load_fn=None,
           write_view_fn=None, write_snapshot_fn=None, lock_path=None, after_commit=None) -> None:
    """Serialize, validate and commit a mutation before publishing derived files."""
    with Locked(lock_path):
        conn = (connect_fn or connect)()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                before = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM nodes")}
                message = action(conn)
                after = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM nodes")}
                _log_events(conn, before, after, action_name)
                data = (load_fn or load)(conn)
                validate(data)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()
        (write_view_fn or write_view)(data)
        (write_snapshot_fn or write_snapshot)(data)
        if after_commit:
            after_commit(data)
    if message:
        print(message)


# Row helpers
def row(conn: sqlite3.Connection, node_id: str) -> sqlite3.Row:
    found = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if found is None:
        raise TaskError(f"no such id: {node_id}")
    return cast(sqlite3.Row, found)


def row_by_key(conn: sqlite3.Connection, key: str) -> sqlite3.Row:
    found = conn.execute("SELECT * FROM nodes WHERE id = ? OR uid = ?", (key, key)).fetchone()
    if found is None:
        raise TaskError(f"no such id: {key}")
    return cast(sqlite3.Row, found)


def subtree_ids(conn: sqlite3.Connection, node_id: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT id FROM nodes WHERE id = ? OR id LIKE ? ORDER BY level, position", (node_id, node_id + ".%"))]


def subtree_rows(conn: sqlite3.Connection, node_id: str) -> list[sqlite3.Row]:
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
