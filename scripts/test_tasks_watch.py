"""Tests for scripts/tasks_watch.py.

Run: python3 -m unittest scripts/test_tasks_watch.py

All tests use a temporary root so the real tasks/ directory is never touched.
"""
import contextlib
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import tasks, tasks_render, tasks_storage


def _file_hash(path: Path) -> str:
    """SHA-256 hex digest of a file, or '' if it doesn't exist."""
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RealFileGuard:
    """Assert that real tracker files are unchanged by the test run."""

    def __init__(self):
        self._real_view = Path("tasks/README.md")
        self._real_snapshot = Path("tasks/tasks.snapshot.json")
        self._real_db = Path("tasks/tasks.db")
        self._saved = {}

    def save(self):
        for name, p in [
            ("view", self._real_view),
            ("snapshot", self._real_snapshot),
            ("db", self._real_db),
        ]:
            self._saved[name] = _file_hash(p)

    def restore(self, test_case: unittest.TestCase):
        for name, p in [
            ("view", self._real_view),
            ("snapshot", self._real_snapshot),
            ("db", self._real_db),
        ]:
            actual = _file_hash(p)
            expected = self._saved.get(name)
            if expected is not None:
                test_case.assertEqual(actual, expected, f"{p.name} was modified by the test run")


_guard = RealFileGuard()


class WatchTestCase(unittest.TestCase):
    """Base class that isolates tasks_watch.py from the real database."""

    def setUp(self):
        _guard.save()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (tasks.DATA_FILE, tasks.VIEW_DIR, tasks.README_FILE, tasks.VIEW_FILE,
                      tasks.LOCK_FILE, tasks.SNAPSHOT_FILE)
        tasks.DATA_FILE = root / "tasks" / "tasks.db"
        tasks.VIEW_DIR = root / "tasks"
        tasks.README_FILE = tasks.VIEW_DIR / "README.md"
        tasks.VIEW_FILE = tasks.README_FILE
        tasks.LOCK_FILE = root / "tasks" / ".lock"
        tasks.SNAPSHOT_FILE = root / "tasks" / "tasks.snapshot.json"
        tasks_storage.DATA_FILE = tasks.DATA_FILE
        tasks_storage.VIEW_DIR = tasks.VIEW_DIR
        tasks_storage.README_FILE = tasks.README_FILE
        tasks_storage.VIEW_FILE = tasks.VIEW_FILE
        tasks_storage.LOCK_FILE = tasks.LOCK_FILE
        tasks_storage.SNAPSHOT_FILE = tasks.SNAPSHOT_FILE
        tasks_render.VIEW_DIR = tasks.VIEW_DIR
        tasks_render.README_FILE = tasks.README_FILE
        tasks.VIEW_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        (tasks.DATA_FILE, tasks.VIEW_DIR, tasks.README_FILE, tasks.VIEW_FILE,
         tasks.LOCK_FILE, tasks.SNAPSHOT_FILE) = self.saved
        (tasks_storage.DATA_FILE, tasks_storage.VIEW_DIR, tasks_storage.README_FILE, tasks_storage.VIEW_FILE,
         tasks_storage.LOCK_FILE, tasks_storage.SNAPSHOT_FILE) = self.saved
        (tasks_render.VIEW_DIR, tasks_render.README_FILE) = (tasks.VIEW_DIR, tasks.README_FILE)
        self.tmp.cleanup()
        _guard.restore(self)

    def _create_db(self):
        """Create a minimal valid database."""
        tasks.DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(tasks.DATA_FILE))
        try:
            conn.executescript(tasks.tasks_init.SCHEMA)
            conn.execute("INSERT INTO meta VALUES ('project', 'test')")
            conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
            conn.commit()
        finally:
            conn.close()

    def _run_watch(self, *extra_args):
        """Run tasks_watch.main with --once and return (stdout, stderr)."""
        from scripts import tasks_watch
        out, err = contextlib.redirect_stdout, contextlib.redirect_stderr
        import io
        stdout = io.StringIO()
        stderr = io.StringIO()
        with out(stdout), err(stderr):
            code = tasks_watch.main(["--once", *extra_args])
        return code, stdout.getvalue(), stderr.getvalue()


class WatchRefreshTests(WatchTestCase):
    """Core refresh behavior tests."""

    def test_stale_view_gets_refreshed(self):
        """When TASKS.md is stale, --once rewrites it."""
        self._create_db()
        # Write a stale view
        tasks.VIEW_FILE.write_text("stale content", encoding="utf-8")
        code, stdout, stderr = self._run_watch()
        self.assertEqual(code, 0)
        # The view should now match what render() produces
        data = tasks.load()
        self.assertEqual(tasks.VIEW_FILE.read_text(encoding="utf-8"), tasks.render(data))
        self.assertIn("refreshed", stderr)

    def test_up_to_date_view_is_left_byte_identical(self):
        """When TASKS.md is current, --once does not rewrite it (mtime untouched)."""
        self._create_db()
        data = tasks.load()
        tasks.write_view(data)
        tasks.write_snapshot(data)
        original_mtime = tasks.VIEW_FILE.stat().st_mtime_ns
        original_content = tasks.VIEW_FILE.read_bytes()
        # Small delay to ensure mtime would differ if rewritten
        import time
        time.sleep(0.05)
        code, stdout, stderr = self._run_watch()
        self.assertEqual(code, 0)
        current_mtime = tasks.VIEW_FILE.stat().st_mtime_ns
        current_content = tasks.VIEW_FILE.read_bytes()
        self.assertEqual(current_content, original_content, "content should be byte-identical")
        self.assertEqual(current_mtime, original_mtime, "mtime should be untouched")
        # No refresh message
        self.assertNotIn("refreshed", stderr)

    def test_missing_db_does_not_crash(self):
        """When tasks.db doesn't exist, --once exits 0 without crashing."""
        # Don't create the DB
        code, stdout, stderr = self._run_watch()
        self.assertEqual(code, 0)
        self.assertIn("no database", stderr)

    def test_corrupted_db_does_not_crash(self):
        """When tasks.db is corrupted, --once exits 0 without crashing."""
        tasks.DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        tasks.DATA_FILE.write_bytes(b"this is not a sqlite database")
        code, stdout, stderr = self._run_watch()
        self.assertEqual(code, 0)
        self.assertIn("cannot read database", stderr)

    def test_missing_view_gets_created(self):
        """When TASKS.md doesn't exist, --once creates it."""
        self._create_db()
        self.assertFalse(tasks.VIEW_FILE.exists())
        code, stdout, stderr = self._run_watch()
        self.assertEqual(code, 0)
        self.assertTrue(tasks.VIEW_FILE.exists())
        data = tasks.load()
        self.assertEqual(tasks.VIEW_FILE.read_text(encoding="utf-8"), tasks.render(data))

    def test_snapshot_also_refreshed_when_stale(self):
        """When the snapshot is stale, --once rewrites it too."""
        self._create_db()
        data = tasks.load()
        tasks.write_view(data)
        # Write a stale snapshot
        tasks.SNAPSHOT_FILE.write_text('{"old": true}\n', encoding="utf-8")
        code, stdout, stderr = self._run_watch()
        self.assertEqual(code, 0)
        self.assertEqual(
            tasks.SNAPSHOT_FILE.read_text(encoding="utf-8"),
            tasks.snapshot_json(data)
        )
        self.assertIn("snapshot", stderr)


if __name__ == "__main__":
    unittest.main()
