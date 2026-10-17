#!/usr/bin/env python3
"""Background watcher that keeps the task view (tasks/README.md + per-sprint files) and the snapshot in sync with tasks/tasks.db.

Polls the database file for changes (mtime + size) and regenerates the view and snapshot
when the DB has changed.  Uses the same functions from tasks.py (render, snapshot_json,
write_view, write_snapshot, Locked) so the output is always identical to what
`scripts/tasks.py` produces.

Usage:
    python3 scripts/tasks_watch.py [--interval SECONDS] [--once]

Options:
    --interval  Poll interval in seconds (default: 2).
    --once      Run a single check and exit (useful for tests).
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import signal
import sys
import time
from pathlib import Path

# Ensure we can import tasks from the same directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tasks  # noqa: E402


_shutting_down = False


def _handle_signal(signum, frame):
    global _shutting_down
    _shutting_down = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def _db_stat(data_file: Path) -> tuple[int, int] | None:
    """Return (mtime_ns, size) of the DB file, or None if it doesn't exist."""
    try:
        st = data_file.stat()
        return (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        return None


def _view_matches(expected: dict) -> bool:
    """True when every expected view file matches and no stale sprint file remains."""
    for path, text in expected.items():
        try:
            if path.read_text(encoding="utf-8") != text:
                return False
        except FileNotFoundError:
            return False
    keep = {p.name for p in expected}
    if tasks.VIEW_DIR.exists():
        for stale in tasks.VIEW_DIR.glob("sprint-*.md"):
            if stale.name not in keep:
                return False
    return True


def _current_snapshot_content() -> str | None:
    """Return the current snapshot content, or None if missing."""
    try:
        return tasks.SNAPSHOT_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _refresh_once() -> bool:
    """Run one poll cycle. Return True if a refresh was performed."""
    data_file = tasks.DATA_FILE

    # Check DB existence
    stat = _db_stat(data_file)
    if stat is None:
        print("watch: no database yet (tasks/tasks.db missing) — waiting...", file=sys.stderr)
        return False

    # Try to load the DB
    try:
        data = tasks.load()
    except Exception as exc:
        # Corrupted or locked DB — print once and wait for next tick
        print(f"watch: cannot read database ({exc}) — will retry", file=sys.stderr)
        return False

    # Validate
    try:
        tasks.validate(data)
    except tasks.TaskError as exc:
        print(f"watch: invalid database ({exc}) — will retry", file=sys.stderr)
        return False

    # Compute expected content
    expected_files = tasks.expected_view(data)
    expected_snapshot = tasks.snapshot_json(data)

    # Compare with current files — only write if content actually differs
    view_changed = not _view_matches(expected_files)
    snapshot_changed = False

    current_snapshot = _current_snapshot_content()
    snapshot_exists = tasks.SNAPSHOT_FILE.exists()
    if not snapshot_exists or current_snapshot != expected_snapshot:
        snapshot_changed = True

    if not view_changed and not snapshot_changed:
        return False

    # Write under the same lock tasks.py uses
    with tasks.Locked():
        if view_changed:
            tasks.write_view(data)
        if snapshot_changed:
            tasks.write_snapshot(data)

    now = dt.datetime.now().strftime("%H:%M:%S")
    parts = []
    if view_changed:
        parts.append("view")
    if snapshot_changed:
        parts.append("snapshot")
    print(f"refreshed {', '.join(parts)} at {now}", file=sys.stderr)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--interval", type=float, default=2.0, help="poll interval in seconds")
    parser.add_argument("--once", action="store_true", help="run one check and exit")
    args = parser.parse_args(argv)

    if args.once:
        _refresh_once()
        return 0

    while not _shutting_down:
        try:
            _refresh_once()
        except Exception as exc:
            print(f"watch: unexpected error ({exc}) — will retry", file=sys.stderr)
        # Sleep in small increments so we respond to signals promptly
        for _ in range(int(args.interval * 10)):
            if _shutting_down:
                break
            time.sleep(0.1)

    return 0


if __name__ == "__main__":
    sys.exit(main())
