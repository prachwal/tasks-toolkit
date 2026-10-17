#!/usr/bin/env python3
"""Creates the task database from scratch. tasks/tasks.db is git-ignored; this script is what makes
a fresh clone usable. scripts/tasks.py calls create_database() automatically when the file is missing.

    python3 scripts/tasks_init.py [--path tasks/tasks.db] [--project NAME] [--force]

Refuses to touch an existing database unless --force (which deletes it first). Standard library only.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def resolve_root(start: Path, use_git: bool = True) -> Path:
    """Project root that owns the task database, even when running inside a git worktree.

    Agents work in worktrees under <main>/tmp/ (any depth); the tracker must always use the main
    checkout. Order: $TASKS_ROOT, git's common dir (works for any worktree location), the
    nearest ancestor named `tmp` that sits in a checkout with a `.git`, then `start` itself.
    """
    start = Path(start).resolve()
    override = os.environ.get("TASKS_ROOT")
    if override:
        return Path(override).resolve()
    if use_git:
        try:
            common = subprocess.run(
                ["git", "-C", str(start), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                capture_output=True, text=True, timeout=10, check=True).stdout.strip()
            if common and Path(common).name == ".git":
                return Path(common).resolve().parent
        except (OSError, subprocess.SubprocessError):
            pass
    for ancestor in start.parents:
        if ancestor.name == "tmp" and (ancestor.parent / ".git").exists():
            return ancestor.parent
    return start


ROOT = resolve_root(Path.cwd())
DEFAULT_PATH = ROOT / "tasks" / "tasks.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nodes (
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
CREATE TABLE IF NOT EXISTS deps (
    node_id    TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    depends_on TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    PRIMARY KEY (node_id, depends_on)
);
CREATE TABLE IF NOT EXISTS notes (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);
CREATE INDEX IF NOT EXISTS idx_nodes_assignee ON nodes(assignee);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id, position);
CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_uid ON nodes(uid) WHERE uid <> '';
CREATE TABLE IF NOT EXISTS links (
    node_uid TEXT NOT NULL,
    kind     TEXT NOT NULL,
    value    TEXT NOT NULL,
    created  TEXT NOT NULL,
    PRIMARY KEY (node_uid, kind, value)
);
CREATE INDEX IF NOT EXISTS idx_links_uid ON links(node_uid);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    actor    TEXT NOT NULL,
    action   TEXT NOT NULL,
    node_uid TEXT NOT NULL DEFAULT '',
    field    TEXT NOT NULL DEFAULT '',
    old      TEXT NOT NULL DEFAULT '',
    new      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_node ON events(node_uid);
"""


def create_database(path: Path, project: str) -> None:
    """Create a new, empty database file with the full schema. Raises FileExistsError if it exists."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO meta VALUES ('project', ?)", (project,))
        conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        conn.commit()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--project", default=ROOT.name)
    parser.add_argument("--force", action="store_true", help="delete an existing database first (data is lost)")
    args = parser.parse_args(argv)
    if args.path.exists():
        if not args.force:
            print(f"error: {args.path} already exists (use --force to recreate it; all tasks are lost)",
                  file=sys.stderr)
            return 1
        args.path.unlink()
    create_database(args.path, args.project)
    print(f"created empty task database: {args.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
