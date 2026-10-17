"""Tests for scripts/tasks.py. Run: python3 -m unittest scripts/test_tasks.py

RULE: Every module-level path constant in tasks.py that points to a real file must be
redirected to a temp directory in setUp and restored in tearDown.  The constants to audit
are: DATA_FILE, VIEW_DIR, README_FILE, VIEW_FILE, LOCK_FILE, SNAPSHOT_FILE, RECOVERY_DIR — and any new ones added later.
If a constant is missed, tests will silently overwrite the real tracker files in the
main checkout, because tasks.py resolves ROOT to the main checkout from inside worktrees.
The env var VIBE_TASKS_TESTING=1 (set below) provides a safety net in tasks.py that
refuses to write outside a temp dir when testing.
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Safety net: tasks.py checks this flag before any write operation.
os.environ["VIBE_TASKS_TESTING"] = "1"

import tasks
import tasks_storage
import tasks_render


class TasksTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (tasks.DATA_FILE, tasks.VIEW_DIR, tasks.README_FILE, tasks.VIEW_FILE,
                      tasks.LOCK_FILE, tasks.SNAPSHOT_FILE, tasks.RECOVERY_DIR, tasks.BACKUP_DIR)
        tasks.DATA_FILE = root / "tasks" / "tasks.db"
        tasks.VIEW_DIR = root / "tasks"
        tasks.README_FILE = tasks.VIEW_DIR / "README.md"
        tasks.VIEW_FILE = tasks.README_FILE
        tasks.LOCK_FILE = root / "tasks" / ".lock"
        tasks.SNAPSHOT_FILE = root / "tasks" / "tasks.snapshot.json"
        tasks.RECOVERY_DIR = root / "tasks" / "removed"
        tasks.BACKUP_DIR = root / "backup.d"
        tasks_storage.DATA_FILE = tasks.DATA_FILE
        tasks_storage.VIEW_DIR = tasks.VIEW_DIR
        tasks_storage.README_FILE = tasks.README_FILE
        tasks_storage.VIEW_FILE = tasks.VIEW_FILE
        tasks_storage.LOCK_FILE = tasks.LOCK_FILE
        tasks_storage.SNAPSHOT_FILE = tasks.SNAPSHOT_FILE
        tasks_storage.RECOVERY_DIR = tasks.RECOVERY_DIR
        tasks_storage.BACKUP_DIR = tasks.BACKUP_DIR
        tasks_render.VIEW_DIR = tasks.VIEW_DIR
        tasks_render.README_FILE = tasks.README_FILE
        (root / "tasks").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        (tasks.DATA_FILE, tasks.VIEW_DIR, tasks.README_FILE, tasks.VIEW_FILE,
         tasks.LOCK_FILE, tasks.SNAPSHOT_FILE, tasks.RECOVERY_DIR, tasks.BACKUP_DIR) = self.saved
        (tasks_storage.DATA_FILE, tasks_storage.VIEW_DIR, tasks_storage.README_FILE, tasks_storage.VIEW_FILE,
         tasks_storage.LOCK_FILE, tasks_storage.SNAPSHOT_FILE, tasks_storage.RECOVERY_DIR, tasks_storage.BACKUP_DIR) = self.saved
        (tasks_render.VIEW_DIR, tasks_render.README_FILE) = (tasks.VIEW_DIR, tasks.README_FILE)
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tasks.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv):
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 0, err)
        return out

    def status(self, node_id):
        return tasks.effective(tasks.find(tasks.load(), node_id))

    def build(self):
        self.ok("add-sprint", "Sprint one", "--goal", "g")
        self.ok("add", "1", "Task A")
        self.ok("add", "1.1", "Sub 1")
        self.ok("add", "1.1", "Sub 2")
        self.ok("add", "1", "Task B")


class CascadeTests(TasksTestCase):
    def test_ids_are_hierarchical(self):
        self.build()
        ids = [n["id"] for n in tasks.walk(tasks.load()["sprints"])]
        self.assertEqual(ids, ["1", "1.1", "1.1.1", "1.1.2", "1.2"])

    def test_everything_starts_as_none(self):
        self.build()
        self.assertEqual({self.status(i) for i in ("1", "1.1", "1.2")}, {"none"})

    def test_started_subtask_makes_task_and_sprint_in_progress(self):
        self.build()
        self.ok("start", "1.1.1")
        self.assertEqual(self.status("1.1"), "in_progress")
        self.assertEqual(self.status("1"), "in_progress")
        self.assertEqual(self.status("1.2"), "none")

    def test_one_done_subtask_is_in_progress_for_parents(self):
        self.build()
        self.ok("done", "1.1.1")
        self.assertEqual(self.status("1.1"), "in_progress")

    def test_all_subtasks_done_completes_task_but_not_sprint(self):
        self.build()
        self.ok("done", "1.1.1")
        self.ok("done", "1.1.2")
        self.assertEqual(self.status("1.1"), "done")
        self.assertEqual(self.status("1"), "in_progress")
        self.ok("done", "1.2")
        self.assertEqual(self.status("1"), "done")

    def test_blocked_wins_over_in_progress_and_done(self):
        self.build()
        self.ok("done", "1.1.1")
        self.ok("start", "1.2")
        self.ok("block", "1.1.2", "--reason", "waiting for API key")
        self.assertEqual(self.status("1.1"), "blocked")
        self.assertEqual(self.status("1"), "blocked")

    def test_unblocking_restores_derived_status(self):
        self.build()
        self.ok("block", "1.1.1", "--reason", "x")
        self.ok("start", "1.1.1")
        self.assertEqual(self.status("1"), "in_progress")
        self.assertEqual(tasks.find(tasks.load(), "1.1.1")["reason"], "")

    def test_reopening_a_done_subtask_reopens_parents(self):
        self.build()
        self.ok("done", "1.1", "--all")
        self.assertEqual(self.status("1.1"), "done")
        self.ok("reset", "1.1.2")
        self.assertEqual(self.status("1.1"), "in_progress")


class RuleTests(TasksTestCase):
    def test_blocking_requires_reason(self):
        self.build()
        code, _, err = self.run_cli("status", "1.2", "blocked")
        self.assertEqual(code, 1)
        self.assertIn("--reason", err)

    def test_parent_status_cannot_be_set_directly(self):
        self.build()
        code, _, err = self.run_cli("status", "1.1", "done")
        self.assertEqual(code, 1)
        self.assertIn("derived", err)

    def test_polish_aliases(self):
        self.build()
        self.ok("status", "1.2", "w-trakcie")
        self.assertEqual(self.status("1.2"), "in_progress")
        self.ok("status", "1.2", "gotowe")
        self.assertEqual(self.status("1.2"), "done")
        self.ok("status", "1.2", "brak")
        self.assertEqual(self.status("1.2"), "none")

    def test_subtasks_cannot_have_children(self):
        self.build()
        code, _, err = self.run_cli("add", "1.1.1", "too deep")
        self.assertEqual(code, 1)
        self.assertIn("subtask", err)

    def test_dependency_cycle_is_rejected(self):
        self.build()
        self.ok("edit", "1.1", "--after", "1.2")
        code, _, err = self.run_cli("edit", "1.2", "--after", "1.1")
        self.assertEqual(code, 1)
        self.assertIn("cycle", err)

    def test_unknown_dependency_is_rejected(self):
        self.build()
        code, _, _ = self.run_cli("add", "1", "x", "--after", "9.9")
        self.assertEqual(code, 1)

    def test_removed_dependency_target_is_protected(self):
        self.build()
        self.ok("edit", "1.2", "--after", "1.1")
        code, _, err = self.run_cli("rm", "1.1", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("depends on", err)

    def test_new_id_is_one_after_the_highest_sibling(self):
        self.build()
        self.ok("rm", "1.2", "--yes")
        self.ok("add", "1", "Task C")
        self.assertEqual(tasks.find(tasks.load(), "1.2")["title"], "Task C")
        self.ok("rm", "1.1", "--yes")
        self.ok("add", "1", "Task D")
        self.assertEqual(tasks.find(tasks.load(), "1.3")["title"], "Task D")


class WorkflowTests(TasksTestCase):
    def test_next_skips_unmet_dependencies_and_finished_work(self):
        self.build()
        self.ok("edit", "1.2", "--after", "1.1")
        self.assertIn("1.1.1", self.ok("next"))
        self.ok("done", "1.1", "--all")
        self.assertIn("1.2", self.ok("next"))
        self.ok("done", "1.2")
        self.assertIn("nothing ready", self.ok("next"))

    def test_next_filters_by_assignee(self):
        self.build()
        self.ok("edit", "1.2", "--assignee", "kilo")
        self.assertIn("1.2", self.ok("next", "--assignee", "kilo"))

    def test_view_is_rendered_after_every_change(self):
        self.build()
        self.ok("start", "1.1.1")
        detail = next(tasks.VIEW_DIR.glob("sprint-1-*.md")).read_text(encoding="utf-8")
        self.assertIn("**1.1.1** Sub 1 — 🔄 in progress", detail)
        self.assertIn("**1.1** Task A — 🔄 in progress (0/2)", detail)
        self.ok("block", "1.2", "--reason", "needs decision")
        detail = next(tasks.VIEW_DIR.glob("sprint-1-*.md")).read_text(encoding="utf-8")
        self.assertIn("needs decision", detail)
        readme = tasks.README_FILE.read_text(encoding="utf-8")
        self.assertIn("## ⛔ Blockers", readme)
        self.assertIn("needs decision", readme)

    def test_description_bullets_render_without_double_dash(self):
        """Description lines already starting with a list marker are not prefixed twice, and a blank
        description line does not render as a lone "-"."""
        self.build()
        self.ok("edit", "1.2", "-d", "Intro line\n\n- item one\n- item two\n  - nested")
        text = next(tasks.VIEW_DIR.glob("sprint-1-*.md")).read_text(encoding="utf-8")
        self.assertNotIn("- - ", text)
        self.assertIn("- item one", text)
        self.assertIn("- item two", text)
        self.assertIn("nested", text)
        self.assertNotIn("\n  -\n", text)
        self.assertNotIn("\n  - \n", text)

    def test_check_detects_stale_view(self):
        self.build()
        self.ok("check")
        tasks.VIEW_FILE.write_text("stale", encoding="utf-8")
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("out of date", err)
        self.ok("render")
        self.ok("check")

    def test_notes_and_timestamps(self):
        self.build()
        self.ok("start", "1.2")
        self.ok("note", "1.2", "picked up by kilo")
        self.ok("done", "1.2")
        node = tasks.find(tasks.load(), "1.2")
        self.assertTrue(node["started"] and node["finished"])
        self.assertIn("picked up by kilo", node["notes"][0])


class DatabaseTests(TasksTestCase):
    def test_failed_change_is_rolled_back(self):
        self.build()
        self.ok("edit", "1.1", "--after", "1.2")
        before = tasks.load()
        code, _, _ = self.run_cli("edit", "1.2", "--after", "1.1")  # would create a cycle
        self.assertEqual(code, 1)
        self.assertEqual(tasks.load(), before)

    def test_state_survives_reopening_the_database(self):
        self.build()
        self.ok("block", "1.2", "--reason", "x")
        self.assertEqual(self.status("1.2"), "blocked")
        self.assertTrue(tasks.DATA_FILE.exists())

    def test_removing_a_task_cascades_to_children_notes_and_deps(self):
        self.build()
        self.ok("note", "1.1.1", "n")
        self.ok("edit", "1.1.2", "--after", "1.1.1")
        self.ok("rm", "1.1", "--yes")
        conn = tasks.connect()
        try:
            for table in ("notes", "deps"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0], 2)
        finally:
            conn.close()

    def test_export_import_roundtrip(self):
        self.build()
        self.ok("edit", "1.2", "--after", "1.1", "--assignee", "kilo")
        self.ok("note", "1.2", "hello")
        self.ok("block", "1.1.1", "--reason", "r")
        before = tasks.load()
        dump = Path(self.tmp.name) / "dump.json"
        self.ok("export-json", "--file", str(dump))
        self.ok("rm", "1", "--yes")
        self.assertEqual(tasks.load()["sprints"], [])
        self.ok("import-json", str(dump))
        self.assertEqual(tasks.load(), before)

    def test_list_filters(self):
        self.build()
        self.ok("edit", "1.2", "--assignee", "kilo", "-d", "needle text")
        self.ok("start", "1.2")
        self.assertEqual(self.ok("list", "--assignee", "kilo").count("\n"), 1)
        self.assertIn("1.2", self.ok("list", "--text", "NEEDLE"))
        self.assertNotIn("1.1 ", self.ok("list", "--status", "in_progress", "--leaves"))

    def test_many_tasks(self):
        self.ok("add-sprint", "Big")
        for i in range(300):
            self.ok("add", "1", f"Task {i}")
        self.ok("done", "1", "--all")
        self.assertEqual(self.status("1"), "done")
        self.assertIn("done: 300", self.ok("stats"))
        self.assertEqual(self.ok("next", "-n", "3"), "nothing ready\n")


class InitTests(TasksTestCase):
    def test_missing_database_is_created_empty_on_first_change(self):
        self.assertFalse(tasks.DATA_FILE.exists())
        code, out, err = self.run_cli("add-sprint", "First")
        self.assertEqual(code, 0)
        self.assertIn("created a new empty task database", err)
        self.assertTrue(tasks.DATA_FILE.exists())
        self.assertEqual(tasks.load()["sprints"][0]["title"], "First")

    def test_reading_never_creates_the_database(self):
        for argv in (["list"], ["stats"], ["next"], ["check"]):
            self.ok(*argv)
        self.assertFalse(tasks.DATA_FILE.exists())

    def test_render_refuses_without_a_database(self):
        tasks.VIEW_FILE.write_text("committed view", encoding="utf-8")
        code, _, err = self.run_cli("render")
        self.assertEqual(code, 1)
        self.assertIn("tasks_init.py", err)
        self.assertEqual(tasks.VIEW_FILE.read_text(encoding="utf-8"), "committed view")

    def test_init_script_creates_schema_and_does_not_overwrite(self):
        import tasks_init
        target = Path(self.tmp.name) / "fresh" / "x.db"
        self.assertEqual(tasks_init.main(["--path", str(target), "--project", "P"]), 0)
        self.assertEqual(tasks_init.main(["--path", str(target)]), 1)  # exists, no --force
        conn = tasks.sqlite3.connect(target)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertTrue({"meta", "nodes", "deps", "notes"} <= names)
        self.assertEqual(tasks_init.main(["--path", str(target), "--force"]), 0)


class SnapshotTests(TasksTestCase):
    # SNAPSHOT_FILE is already redirected by the base TasksTestCase.setUp.
    # This class no longer needs its own setUp/tearDown for the snapshot path.

    def test_snapshot_written_on_change(self):
        """A change command writes the snapshot file alongside TASKS.md."""
        self.ok("add-sprint", "Sprint one")
        self.assertTrue(tasks.SNAPSHOT_FILE.exists())
        data = tasks.load()
        self.assertEqual(tasks.SNAPSHOT_FILE.read_text(encoding="utf-8"), tasks.snapshot_json(data))

    def test_snapshot_is_deterministic(self):
        """Two consecutive snapshots of the same state are byte-identical."""
        self.ok("add-sprint", "S")
        self.ok("add", "1", "Task A")
        self.ok("done", "1.1")
        snap1 = tasks.SNAPSHOT_FILE.read_text(encoding="utf-8")
        self.ok("snapshot")
        snap2 = tasks.SNAPSHOT_FILE.read_text(encoding="utf-8")
        self.assertEqual(snap1, snap2)

    def test_snapshot_format_has_sorted_keys(self):
        """Snapshot JSON uses sort_keys=True so output is deterministic."""
        self.ok("add-sprint", "S")
        text = tasks.SNAPSHOT_FILE.read_text(encoding="utf-8")
        # "project" should come before "sprints" alphabetically
        project_pos = text.index('"project"')
        sprints_pos = text.index('"sprints"')
        self.assertLess(project_pos, sprints_pos)

    def test_rollback_leaves_snapshot_untouched(self):
        """If a transaction rolls back, the snapshot file is not modified."""
        self.ok("add-sprint", "S")
        self.ok("add", "1", "Task A")
        self.ok("add", "1", "Task B")
        self.ok("edit", "1.1", "--after", "1.2")
        before = tasks.SNAPSHOT_FILE.read_text(encoding="utf-8")
        # This should fail (cycle) and roll back
        code, _, _ = self.run_cli("edit", "1.2", "--after", "1.1")
        self.assertEqual(code, 1)
        after = tasks.SNAPSHOT_FILE.read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_check_fails_when_snapshot_missing(self):
        """check fails if the DB exists but the snapshot file is missing."""
        self.ok("add-sprint", "S")
        tasks.SNAPSHOT_FILE.unlink()
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("snapshot", err)

    def test_check_fails_when_snapshot_stale(self):
        """check fails if the snapshot content does not match the DB."""
        self.ok("add-sprint", "S")
        tasks.SNAPSHOT_FILE.write_text('{"version": 2, "project": "old", "sprints": []}\n', encoding="utf-8")
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("out of date", err)

    def test_check_passes_after_change(self):
        """check passes after a change command that re-writes the snapshot."""
        self.ok("add-sprint", "S")
        self.ok("add", "1", "Task A")
        self.ok("start", "1.1")
        self.ok("check")

    def test_snapshot_command(self):
        """Explicit snapshot command writes the file."""
        self.ok("add-sprint", "S")
        tasks.SNAPSHOT_FILE.unlink()
        out = self.ok("snapshot")
        self.assertIn("wrote", out)
        self.assertTrue(tasks.SNAPSHOT_FILE.exists())

    def test_snapshot_command_no_database(self):
        """snapshot command fails when there is no database."""
        code, _, err = self.run_cli("snapshot")
        self.assertEqual(code, 1)
        self.assertIn("no database", err)

    def test_import_json_roundtrip_via_snapshot(self):
        """Export to snapshot, wipe DB, import from snapshot — state is restored."""
        self.ok("add-sprint", "Sprint one", "--goal", "g")
        self.ok("add", "1", "Task A")
        self.ok("add", "1.1", "Sub 1")
        self.ok("add", "1", "Task B")
        self.ok("done", "1.1.1")
        self.ok("note", "1", "a note")
        self.ok("edit", "1.2", "--after", "1.1")
        before = tasks.load()
        # Save the snapshot content before we wipe the DB (rm would overwrite it)
        backup = Path(self.tmp.name) / "backup.json"
        backup.write_text(tasks.SNAPSHOT_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        # Wipe the database
        self.ok("rm", "1", "--yes")
        self.assertEqual(tasks.load()["sprints"], [])
        # Import from the saved snapshot backup
        self.ok("import-json", str(backup))
        after = tasks.load()
        self.assertEqual(after, before)

    def test_check_skips_snapshot_when_no_db(self):
        """check skips snapshot comparison when the DB does not exist (fresh clone)."""
        # No DB created yet — read-only check should pass
        code, out, err = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertIn("no database", out)


class MoveTests(TasksTestCase):
    """Tests for the mv command: moving items between parents with subtree renumbering."""

    def _setup_two_sprints(self):
        """Create two sprints with tasks and subtasks."""
        self.ok("add-sprint", "Sprint A")
        self.ok("add", "1", "Task A1")
        self.ok("add", "1.1", "Sub A1.1")
        self.ok("add", "1.1", "Sub A1.2")
        self.ok("add", "1", "Task A2")
        self.ok("add-sprint", "Sprint B")
        self.ok("add", "2", "Task B1")

    def test_move_leaf_across_tasks(self):
        """Move a leaf subtask from one task to another."""
        self._setup_two_sprints()
        out = self.ok("mv", "1.1.1", "1.2")
        self.assertIn("moved 1.1.1 -> 1.2.1", out)
        data = tasks.load()
        # 1.2 should now have child 1.2.1 (formerly 1.1.1)
        self.assertEqual(tasks.find(data, "1.2.1")["title"], "Sub A1.1")
        # 1.1 should only have 1.1.2
        self.assertEqual(len(tasks.find(data, "1.1")["children"]), 1)
        self.assertEqual(tasks.find(data, "1.1")["children"][0]["id"], "1.1.2")

    def test_move_task_across_sprints(self):
        """Move a task with children from one sprint to another."""
        self._setup_two_sprints()
        out = self.ok("mv", "1.1", "2")
        self.assertIn("moved 1.1 -> 2.2", out)
        self.assertIn("1.1.1 -> 2.2.1", out)
        self.assertIn("1.1.2 -> 2.2.2", out)
        data = tasks.load()
        # Sprint 2 should have Task B1 (2.1) and the moved task (2.2)
        sprint_b = tasks.find(data, "2")
        self.assertEqual(len(sprint_b["children"]), 2)
        self.assertEqual(sprint_b["children"][1]["id"], "2.2")
        self.assertEqual(sprint_b["children"][1]["title"], "Task A1")
        # Children should be renumbered
        self.assertEqual(tasks.find(data, "2.2.1")["title"], "Sub A1.1")
        self.assertEqual(tasks.find(data, "2.2.2")["title"], "Sub A1.2")
        # Sprint 1 should only have Task A2
        sprint_a = tasks.find(data, "1")
        self.assertEqual(len(sprint_a["children"]), 1)
        self.assertEqual(sprint_a["children"][0]["id"], "1.2")

    def test_move_preserves_status(self):
        """Moving a leaf keeps its status."""
        self._setup_two_sprints()
        self.ok("done", "1.1.1")
        self.ok("mv", "1.1.1", "1.2")
        self.assertEqual(self.status("1.2.1"), "done")

    def test_move_preserves_notes(self):
        """Notes follow the moved item."""
        self._setup_two_sprints()
        self.ok("note", "1.1.1", "important note")
        self.ok("mv", "1.1.1", "1.2")
        node = tasks.find(tasks.load(), "1.2.1")
        self.assertIn("important note", node["notes"][0])

    def test_move_rewrites_dependencies(self):
        """Dependencies referencing moved items are rewritten."""
        self._setup_two_sprints()
        # External dep: 1.2 depends on 1.1.1
        self.ok("edit", "1.2", "--after", "1.1.1")
        # Internal dep: 1.1.2 depends on 1.1.1 (both in subtree of 1.1)
        self.ok("edit", "1.1.2", "--after", "1.1.1")
        self.ok("mv", "1.1", "2")
        data = tasks.load()
        # 1.1 -> 2.2, 1.1.1 -> 2.2.1, 1.1.2 -> 2.2.2
        # 1.2's dep on 1.1.1 -> 2.2.1 (external dep rewritten)
        self.assertEqual(tasks.find(data, "1.2")["depends_on"], ["2.2.1"])
        # 2.2.2's dep on 2.2.1 (internal dep rewritten)
        self.assertEqual(tasks.find(data, "2.2.2")["depends_on"], ["2.2.1"])

    def test_move_with_deps_on_both_sides(self):
        """Move a task with children where deps exist on both sides."""
        self._setup_two_sprints()
        # B1 depends on Sub A1.1
        self.ok("edit", "2.1", "--after", "1.1.1")
        # Sub A1.2 depends on B1
        self.ok("edit", "1.1.2", "--after", "2.1")
        self.ok("mv", "1.1", "2")
        data = tasks.load()
        # 2.1 depends on 2.2.1 (was 1.1.1)
        self.assertEqual(tasks.find(data, "2.1")["depends_on"], ["2.2.1"])
        # 2.2.2 depends on 2.1 (was 1.1.2 depends on 2.1)
        self.assertEqual(tasks.find(data, "2.2.2")["depends_on"], ["2.1"])

    def test_mv_pos_ordering(self):
        """--pos places the moved item at the specified position."""
        self._setup_two_sprints()
        self.ok("mv", "1.1.1", "1.2", "--pos", "1")
        data = tasks.load()
        # 1.2.1 should be the moved item
        self.assertEqual(tasks.find(data, "1.2.1")["title"], "Sub A1.1")

    def test_mv_cannot_move_sprint(self):
        """Moving a sprint (level 1) is rejected."""
        self._setup_two_sprints()
        code, _, err = self.run_cli("mv", "1", "2")
        self.assertEqual(code, 1)
        self.assertIn("cannot move a sprint", err)

    def test_mv_cannot_move_into_own_subtree(self):
        """Moving a node into its own descendant is rejected."""
        self._setup_two_sprints()
        code, _, err = self.run_cli("mv", "1.1", "1.1.1")
        self.assertEqual(code, 1)
        # The error could be "own subtree" or "subtask" depending on which check fires first
        self.assertTrue("own subtree" in err or "subtask" in err, err)

    def test_mv_cannot_exceed_max_depth(self):
        """Moving a task with subtasks under another task is rejected (would exceed depth 3)."""
        self._setup_two_sprints()
        code, _, err = self.run_cli("mv", "1.1", "2.1")
        self.assertEqual(code, 1)
        self.assertIn("max depth", err)

    def test_mv_same_parent_without_pos(self):
        """Moving to the same parent without --pos is rejected."""
        self._setup_two_sprints()
        code, _, err = self.run_cli("mv", "1.1.1", "1.1")
        self.assertEqual(code, 1)
        self.assertIn("already under", err)

    def test_mv_same_parent_with_pos_is_refused_and_loses_nothing(self):
        """Reordering siblings is refused (ids encode the position); the subtree, notes and db stay intact.

        Regression: it used to DELETE the node (cascading to its subtree) and re-insert only the root.
        """
        self._setup_two_sprints()
        self.ok("add", "1.1", "sibling-with-children")
        self.ok("add", "1.2", "child of 1.2")
        self.ok("note", "1.2.1", "keep me")
        before = tasks.export_data() if hasattr(tasks, "export_data") else tasks.load()
        code, _, err = self.run_cli("mv", "1.2", "1", "--pos", "1")
        self.assertEqual(code, 1)
        self.assertIn("reordering siblings is not supported", err)
        after = tasks.export_data() if hasattr(tasks, "export_data") else tasks.load()
        self.assertEqual(before, after)
        data = tasks.load()
        self.assertEqual(tasks.find(data, "1.2")["children"][0]["id"], "1.2.1")
        self.assertIn("keep me", tasks.find(data, "1.2.1")["notes"][0])

    def test_mv_error_leaves_db_unchanged(self):
        """Every error case leaves the database byte-identical."""
        self._setup_two_sprints()
        self.ok("done", "1.1.1")
        before = tasks.load()
        code, _, _ = self.run_cli("mv", "1", "2")  # can't move sprint
        self.assertEqual(code, 1)
        after = tasks.load()
        self.assertEqual(after, before)

    def test_mv_unknown_source_id(self):
        """Moving an unknown id is rejected."""
        self._setup_two_sprints()
        code, _, err = self.run_cli("mv", "9.9", "1")
        self.assertEqual(code, 1)
        self.assertIn("no such id", err)

    def test_mv_unknown_dest_id(self):
        """Moving to an unknown parent is rejected."""
        self._setup_two_sprints()
        code, _, err = self.run_cli("mv", "1.1", "9")
        self.assertEqual(code, 1)
        self.assertIn("no such id", err)

    def test_mv_task_to_sprint_root(self):
        """Move a leaf task from one sprint to another sprint."""
        self._setup_two_sprints()
        out = self.ok("mv", "1.2", "2")
        self.assertIn("moved 1.2 -> 2.2", out)
        data = tasks.load()
        self.assertEqual(tasks.find(data, "2.2")["title"], "Task A2")

    def test_mv_recomputes_parent_status(self):
        """Parent derived statuses are recomputed after move."""
        self._setup_two_sprints()
        self.ok("done", "1.1.1")
        self.ok("done", "1.1.2")
        self.assertEqual(self.status("1.1"), "done")
        self.ok("mv", "1.1", "2")
        # Sprint 1 has no children now -> none
        # Sprint 2 has 2.1 (none) and 2.2 (done) -> in_progress
        self.assertEqual(self.status("2"), "in_progress")

    def test_mv_snapshot_and_view_in_sync(self):
        """After mv, TASKS.md and snapshot are in sync."""
        self._setup_two_sprints()
        self.ok("mv", "1.1", "2")
        self.ok("check")


class BulkEditTests(TasksTestCase):
    """Tests for bulk edit and multi-id status commands."""

    def test_bulk_status_all_done(self):
        """Setting status on multiple ids in one transaction."""
        self.build()
        out = self.ok("done", "1.1.1", "1.1.2", "1.2")
        self.assertIn("1.1.1", out)
        self.assertIn("1.2", out)
        self.assertEqual(self.status("1"), "done")

    def test_bulk_status_one_bad_id(self):
        """If one id is bad, nothing changes (all-or-nothing)."""
        self.build()
        self.ok("start", "1.1.1")
        before = tasks.load()
        code, _, _ = self.run_cli("done", "1.1.2", "9.9")
        self.assertEqual(code, 1)
        after = tasks.load()
        self.assertEqual(after, before)

    def test_bulk_edit_assignee(self):
        """Edit assignee on multiple items."""
        self.build()
        out = self.ok("edit", "1.1", "1.2", "-a", "junie")
        self.assertIn("1.1", out)
        self.assertIn("1.2", out)
        data = tasks.load()
        self.assertEqual(tasks.find(data, "1.1")["assignee"], "junie")
        self.assertEqual(tasks.find(data, "1.2")["assignee"], "junie")

    def test_bulk_edit_title_rejected(self):
        """--title with multiple ids is rejected."""
        self.build()
        code, _, err = self.run_cli("edit", "1.1", "1.2", "--title", "X")
        self.assertEqual(code, 1)
        self.assertIn("--title", err)

    def test_bulk_edit_one_bad_id(self):
        """Bulk edit with one bad id leaves nothing changed."""
        self.build()
        before = tasks.load()
        code, _, _ = self.run_cli("edit", "1.1", "9.9", "-a", "junie")
        self.assertEqual(code, 1)
        after = tasks.load()
        self.assertEqual(after, before)

    def test_bulk_start(self):
        """Start multiple items at once."""
        self.build()
        self.ok("start", "1.1.1", "1.2")
        self.assertEqual(self.status("1.1.1"), "in_progress")
        self.assertEqual(self.status("1.2"), "in_progress")

    def test_bulk_reset(self):
        """Reset multiple items at once."""
        self.build()
        self.ok("done", "1.1.1", "1.2")
        self.ok("reset", "1.1.1", "1.2")
        self.assertEqual(self.status("1.1.1"), "none")
        self.assertEqual(self.status("1.2"), "none")

    def test_bulk_status_command(self):
        """The status command with multiple ids — status is the last positional."""
        self.build()
        self.ok("status", "1.1.1", "1.2", "in_progress")
        self.assertEqual(self.status("1.1.1"), "in_progress")
        self.assertEqual(self.status("1.2"), "in_progress")

    def test_single_id_behavior_unchanged(self):
        """Single-id status/edit still works as before."""
        self.build()
        self.ok("start", "1.1.1")
        self.ok("edit", "1.2", "-a", "kilo")
        self.assertEqual(self.status("1.1.1"), "in_progress")
        self.assertEqual(tasks.find(tasks.load(), "1.2")["assignee"], "kilo")


class OriginalSyntaxRegressionTests(TasksTestCase):
    """Regression tests that run the original documented one-liners through tasks.main()."""

    def test_status_positional_single_id(self):
        """status 1.1 in_progress — original positional syntax."""
        self.build()
        self.ok("status", "1.2", "in_progress")
        self.assertEqual(self.status("1.2"), "in_progress")

    def test_status_positional_with_cascade(self):
        """status 1.1 done --all — cascade down with positional syntax."""
        self.build()
        self.ok("status", "1.1", "done", "--all")
        self.assertEqual(self.status("1.1"), "done")
        self.assertEqual(self.status("1.1.1"), "done")
        self.assertEqual(self.status("1.1.2"), "done")

    def test_status_positional_blocked_with_reason(self):
        """status 1.2 blocked --reason x — blocked with reason."""
        self.build()
        self.ok("status", "1.2", "blocked", "--reason", "x")
        self.assertEqual(self.status("1.2"), "blocked")

    def test_start_shortcut(self):
        """start 1.1 — original start shortcut."""
        self.build()
        self.ok("start", "1.2")
        self.assertEqual(self.status("1.2"), "in_progress")

    def test_done_shortcut(self):
        """done 1.1 — original done shortcut."""
        self.build()
        self.ok("done", "1.2")
        self.assertEqual(self.status("1.2"), "done")

    def test_status_too_few_positionals(self):
        """status with fewer than 2 positionals is a usage error."""
        self.build()
        code, _, err = self.run_cli("status", "1.2")
        self.assertEqual(code, 1)
        self.assertIn("usage", err)

    def test_status_no_positionals(self):
        """status with no positionals is a usage error."""
        self.build()
        code, _, err = self.run_cli("status")
        self.assertEqual(code, 1)
        self.assertIn("usage", err)


class RootResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.main = Path(self.tmp.name).resolve() / "main"
        self.main.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def git(self, *args, cwd=None):
        import subprocess
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd or self.main,
                       check=True, capture_output=True)

    def test_git_common_dir_finds_main_from_any_worktree_depth(self):
        import shutil
        self.git("init", "-q")
        (self.main / "README").write_text("x")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")
        deep = self.main / "tmp" / "a" / "b" / "wt"
        self.git("worktree", "add", "-q", "-b", "agent/deep", str(deep))
        shutil.copytree(Path(tasks.__file__).parent, deep / "scripts",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.assertEqual(tasks.tasks_init.resolve_root(deep / "scripts"), self.main)
        self.assertEqual(tasks.tasks_init.resolve_root(deep), self.main)
        self.assertEqual(tasks.tasks_init.resolve_root(self.main), self.main)
        import subprocess
        import sys
        run = subprocess.run([sys.executable, str(deep / "scripts" / "tasks.py"), "add-sprint", "From worktree"],
                             capture_output=True, text=True, env={"PATH": __import__("os").environ["PATH"]})
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue((self.main / "tasks" / "tasks.db").exists())
        self.assertTrue((self.main / "tasks" / "README.md").exists())
        self.assertFalse((deep / "tasks").exists())

    def test_fallback_without_git_uses_nearest_tmp_ancestor(self):
        (self.main / ".git").mkdir()
        nested = self.main / "tmp" / "worktrees" / "x" / "sub"
        nested.mkdir(parents=True)
        self.assertEqual(tasks.tasks_init.resolve_root(nested, use_git=False), self.main)

    def test_plain_checkout_resolves_to_itself(self):
        self.assertEqual(tasks.tasks_init.resolve_root(self.main, use_git=False), self.main)

    def test_environment_override_wins(self):
        import os
        os.environ["VIBE_TASKS_ROOT"] = str(self.main)
        self.addCleanup(os.environ.pop, "VIBE_TASKS_ROOT", None)
        self.assertEqual(tasks.tasks_init.resolve_root(Path("/")), self.main)


class RealFileProtectionTests(unittest.TestCase):
    """Regression tests: the test suite must not overwrite real tracker files.

    These tests verify that setUp/tearDown redirects ALL module-level path constants
    (DATA_FILE, VIEW_FILE, LOCK_FILE, SNAPSHOT_FILE) so no test data leaks into the
    main checkout.  See the RULE comment at the top of this file.
    """

    def setUp(self):
        # Simulate the full setUp pattern used by TasksTestCase
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (tasks.DATA_FILE, tasks.VIEW_DIR, tasks.README_FILE, tasks.VIEW_FILE,
                      tasks.LOCK_FILE, tasks.SNAPSHOT_FILE, tasks.RECOVERY_DIR, tasks.BACKUP_DIR)
        tasks.DATA_FILE = root / "tasks" / "tasks.db"
        tasks.VIEW_DIR = root / "tasks"
        tasks.README_FILE = tasks.VIEW_DIR / "README.md"
        tasks.VIEW_FILE = tasks.README_FILE
        tasks.LOCK_FILE = root / "tasks" / ".lock"
        tasks.SNAPSHOT_FILE = root / "tasks" / "tasks.snapshot.json"
        tasks.RECOVERY_DIR = root / "tasks" / "removed"
        tasks.BACKUP_DIR = root / "backup.d"
        tasks_storage.DATA_FILE = tasks.DATA_FILE
        tasks_storage.VIEW_DIR = tasks.VIEW_DIR
        tasks_storage.README_FILE = tasks.README_FILE
        tasks_storage.VIEW_FILE = tasks.VIEW_FILE
        tasks_storage.LOCK_FILE = tasks.LOCK_FILE
        tasks_storage.SNAPSHOT_FILE = tasks.SNAPSHOT_FILE
        tasks_storage.RECOVERY_DIR = tasks.RECOVERY_DIR
        tasks_storage.BACKUP_DIR = tasks.BACKUP_DIR
        tasks_render.VIEW_DIR = tasks.VIEW_DIR
        tasks_render.README_FILE = tasks.README_FILE
        (root / "tasks").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        (tasks.DATA_FILE, tasks.VIEW_DIR, tasks.README_FILE, tasks.VIEW_FILE,
         tasks.LOCK_FILE, tasks.SNAPSHOT_FILE, tasks.RECOVERY_DIR, tasks.BACKUP_DIR) = self.saved
        (tasks_storage.DATA_FILE, tasks_storage.VIEW_DIR, tasks_storage.README_FILE, tasks_storage.VIEW_FILE,
         tasks_storage.LOCK_FILE, tasks_storage.SNAPSHOT_FILE, tasks_storage.RECOVERY_DIR, tasks_storage.BACKUP_DIR) = self.saved
        (tasks_render.VIEW_DIR, tasks_render.README_FILE) = (tasks.VIEW_DIR, tasks.README_FILE)
        self.tmp.cleanup()

    def _real_file_stats(self, path):
        try:
            return (path.stat().st_mtime_ns, path.stat().st_size, path.read_bytes())
        except FileNotFoundError:
            return None

    def test_real_snapshot_unchanged_after_mutating_test(self):
        """Running a full build() cycle must not change the real snapshot file."""
        real_snapshot = Path(tasks.ROOT) / "tasks" / "tasks.snapshot.json"
        before = self._real_file_stats(real_snapshot)

        # Simulate a mutating test
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            tasks.main(["add-sprint", "Test sprint"])

        after = self._real_file_stats(real_snapshot)
        if before is not None:
            self.assertIsNotNone(after, "real snapshot was deleted by test")
            self.assertEqual(before[0], after[0], "real snapshot mtime changed")
            self.assertEqual(before[1], after[1], "real snapshot size changed")
            self.assertEqual(before[2], after[2], "real snapshot content changed")

    def test_real_tasks_md_unchanged_after_mutating_test(self):
        """Running a full build() cycle must not change the real TASKS.md."""
        real_view = Path(tasks.ROOT) / "tasks" / "README.md"
        before = self._real_file_stats(real_view)

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            tasks.main(["add-sprint", "Test sprint"])

        after = self._real_file_stats(real_view)
        if before is not None:
            self.assertIsNotNone(after, "real TASKS.md was deleted by test")
            self.assertEqual(before[0], after[0], "real TASKS.md mtime changed")
            self.assertEqual(before[1], after[1], "real TASKS.md size changed")

    def test_real_db_unchanged_after_mutating_test(self):
        """Running a full build() cycle must not change the real tasks.db."""
        real_db = Path(tasks.ROOT) / "tasks" / "tasks.db"
        before = self._real_file_stats(real_db)

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            tasks.main(["add-sprint", "Test sprint"])

        after = self._real_file_stats(real_db)
        if before is not None:
            self.assertIsNotNone(after, "real tasks.db was deleted by test")
            self.assertEqual(before[0], after[0], "real tasks.db mtime changed")
            self.assertEqual(before[1], after[1], "real tasks.db size changed")

    def test_real_removed_dir_unchanged_after_rm(self):
        """Running rm --yes must not write into the real tasks/removed directory."""
        real_dir = Path(tasks.ROOT) / "tasks" / "removed"
        before = sorted((p.name, p.read_bytes()) for p in real_dir.glob("*")) if real_dir.exists() else None

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            tasks.main(["add-sprint", "S"])
            tasks.main(["add", "1", "T"])
            tasks.main(["rm", "1.1", "--yes"])

        after = sorted((p.name, p.read_bytes()) for p in real_dir.glob("*")) if real_dir.exists() else None
        if before is None:
            self.assertIsNone(after, "real tasks/removed was created by test")
        else:
            self.assertEqual(after, before, "real tasks/removed changed by test")


class CancelledStatusTests(TasksTestCase):
    """Task 3.17: 'cancelled' status, its cascade, and the migration from an older schema."""

    def test_cancelled_round_trip(self):
        self.build()
        self.ok("status", "1.1.1", "cancelled")
        self.assertEqual(self.status("1.1.1"), "cancelled")
        self.assertIn("🚫", tasks.render(tasks.load()))

    def test_cancelled_does_not_block_parent_completion(self):
        self.build()
        self.ok("done", "1.1.1")
        self.ok("status", "1.1.2", "cancelled")
        self.ok("done", "1.2")
        self.assertEqual(self.status("1.1"), "done")
        self.assertEqual(self.status("1"), "done")

    def test_all_cancelled_parent_is_cancelled(self):
        self.build()
        self.ok("status", "1.1.1", "cancelled")
        self.ok("status", "1.1.2", "cancelled")
        self.assertEqual(self.status("1.1"), "cancelled")

    def test_migration_from_old_schema(self):
        """A database whose nodes CHECK predates 'cancelled' is rebuilt without losing rows."""
        import sqlite3

        old = """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, parent_id TEXT REFERENCES nodes(id) ON DELETE CASCADE,
            position INTEGER NOT NULL, level INTEGER NOT NULL, title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'none'
              CHECK (status IN ('none','in_progress','blocked','done')),
            reason TEXT NOT NULL DEFAULT '', assignee TEXT NOT NULL DEFAULT '', goal TEXT NOT NULL DEFAULT '',
            start TEXT NOT NULL DEFAULT '', end TEXT NOT NULL DEFAULT '', created TEXT NOT NULL,
            updated TEXT NOT NULL, started TEXT NOT NULL DEFAULT '', finished TEXT NOT NULL DEFAULT '',
            UNIQUE (parent_id, position));
        CREATE TABLE deps (node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
            depends_on TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE, PRIMARY KEY (node_id, depends_on));
        CREATE TABLE notes (seq INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE, text TEXT NOT NULL);
        """
        tasks.DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(tasks.DATA_FILE)
        c.executescript(old)
        c.execute("INSERT INTO meta (key,value) VALUES ('project','Test')")
        c.execute("INSERT INTO nodes (id,parent_id,position,level,title,created,updated) "
                  "VALUES ('1',NULL,1,1,'Sprint','2026-01-01','2026-01-01')")
        c.execute("INSERT INTO nodes (id,parent_id,position,level,title,created,updated) "
                  "VALUES ('1.1','1',1,2,'Task','2026-01-01','2026-01-01')")
        c.commit()
        c.close()

        self.ok("status", "1.1", "cancelled")
        self.assertEqual(self.status("1.1"), "cancelled")
        self.assertEqual(tasks.find(tasks.load(), "1")["title"], "Sprint")
        self.assertTrue(tasks.find(tasks.load(), "1.1")["uid"].startswith("T-"))


class AtomicWriteTests(TasksTestCase):
    """Task 3.18: TASKS.md and the snapshot are written atomically (temp file + os.replace)."""

    def test_write_leaves_no_temp_files(self):
        self.ok("add-sprint", "S")
        self.assertEqual(list(tasks.VIEW_FILE.parent.glob("*.tmp")), [])
        self.assertEqual(list(tasks.SNAPSHOT_FILE.parent.glob("*.tmp")), [])

    def test_failed_replace_keeps_previous_content(self):
        self.ok("add-sprint", "S")
        before = tasks.VIEW_FILE.read_text(encoding="utf-8")
        with mock.patch.object(tasks.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                tasks.write_view(tasks.load())
        self.assertEqual(tasks.VIEW_FILE.read_text(encoding="utf-8"), before)
        self.assertEqual(list(tasks.VIEW_FILE.parent.glob("*.tmp")), [])


class ViewSplitTests(TasksTestCase):
    """Task 8.1: the view is a directory (README index + one file per sprint)."""

    def test_readme_has_links_and_no_inline_sprint(self):
        self.build()
        readme = tasks.README_FILE.read_text(encoding="utf-8")
        self.assertIn("## Sprints", readme)
        self.assertNotIn("**1.1.1**", readme)  # task detail lives in the per-sprint file, not the README
        self.assertNotIn("## Sprint 1 —", readme)
        links = re.findall(r"\]\((sprint-[^)]+\.md)\)", readme)
        self.assertTrue(links, "README has no per-sprint links")
        for link in links:
            self.assertTrue((tasks.VIEW_DIR / link).is_file(), f"dangling link {link}")

    def test_sprint_file_holds_the_detail(self):
        self.build()
        files = sorted(p.name for p in tasks.VIEW_DIR.glob("sprint-*.md"))
        self.assertEqual(len(files), 1)
        self.assertIn("sprint-1-", files[0])
        detail = (tasks.VIEW_DIR / files[0]).read_text(encoding="utf-8")
        self.assertIn("**1.1.1** Sub 1", detail)

    def test_stale_sprint_file_is_removed_on_render(self):
        self.build()
        stale = tasks.VIEW_DIR / "sprint-99-gone.md"
        stale.write_text("old\n", encoding="utf-8")
        self.ok("render")
        self.assertFalse(stale.exists())

    def test_check_detects_a_stale_sprint_file(self):
        self.build()
        victim = next(tasks.VIEW_DIR.glob("sprint-*.md"))
        victim.write_text("stale\n", encoding="utf-8")
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("out of date", err)


class ReadCommandsTests(TasksTestCase):
    """Task 4.1: cheap single-command reads (list subtree + --desc/--notes, show multi/--brief)."""

    def test_list_subtree_with_desc_and_notes(self):
        self.build()
        self.ok("edit", "1.2", "-d", "needle description")
        self.ok("note", "1.2", "a note")
        out = self.ok("list", "1.2", "--desc", "--notes")
        self.assertIn("needle description", out)
        self.assertIn("a note", out)
        self.assertNotIn("1.1.1", out)  # only the requested subtree

    def test_list_without_id_still_prints_everything(self):
        self.build()
        self.assertIn("1.1.1", self.ok("list"))

    def test_show_multi_and_brief(self):
        self.build()
        brief = self.ok("show", "1.1", "1.2", "--brief")
        self.assertIn("1.1", brief)
        self.assertIn("1.2", brief)
        self.assertNotIn("{", brief)  # brief is one line per item, not JSON
        full = self.ok("show", "1.2")
        self.assertIn('"title"', full)

    def test_sprint_table(self):
        self.build()
        out = self.ok("sprint-table", "1")
        self.assertIn("| 1.1 |", out)
        self.assertIn("Task A", out)
        code, _, err = self.run_cli("sprint-table", "1.1")
        self.assertEqual(code, 1)
        self.assertIn("not a sprint", err)

    def test_overview(self):
        self.build()
        out = self.ok("overview")
        self.assertIn("Sprint one", out)
        self.assertIn("done 0/", out)


class ReviewStatusTests(TasksTestCase):
    """Task 4.8: the 'review' status (awaiting merge)."""

    def test_review_round_trip(self):
        self.build()
        self.ok("status", "1.1.1", "review")
        self.assertEqual(self.status("1.1.1"), "review")
        self.assertIn("🔎", tasks.render(tasks.load()))

    def test_review_counts_as_in_progress_for_the_parent(self):
        self.build()
        self.ok("status", "1.1.1", "review")
        self.assertEqual(self.status("1.1"), "in_progress")

    def test_next_never_offers_review(self):
        self.build()
        self.ok("status", "1.1.1", "review")
        self.assertNotIn("1.1.1", self.ok("next", "-n", "5"))


class UidLinkTests(TasksTestCase):
    """Task 4.2: stable uid + structured links."""

    def test_every_node_gets_a_unique_uid(self):
        self.build()
        uids = [n["uid"] for n in tasks.walk(tasks.load()["sprints"])]
        self.assertTrue(all(u.startswith("T-") and len(u) == 6 for u in uids), uids)
        self.assertEqual(len(uids), len(set(uids)))

    def test_find_and_show_accept_a_uid(self):
        self.build()
        uid = tasks.find(tasks.load(), "1.1")["uid"]
        self.assertIn("Task A", self.ok("show", uid))

    def test_link_and_unlink_round_trip(self):
        self.build()
        self.ok("link", "1.2", "spec", "tmp/tasks/x.md")
        links = tasks.find(tasks.load(), "1.2")["links"]
        self.assertEqual([(link["kind"], link["value"]) for link in links], [("spec", "tmp/tasks/x.md")])
        self.ok("unlink", "1.2", "spec", "tmp/tasks/x.md")
        self.assertEqual(tasks.find(tasks.load(), "1.2")["links"], [])

    def test_unknown_link_kind_is_rejected(self):
        self.build()
        code, _, err = self.run_cli("link", "1.2", "bogus", "x")
        self.assertEqual(code, 1)
        self.assertIn("unknown link kind", err)

    def test_uid_survives_mv(self):
        self.build()
        uid = tasks.find(tasks.load(), "1.1.1")["uid"]
        self.ok("mv", "1.1.1", "1.2")
        self.assertEqual(tasks.find(tasks.load(), "1.2.1")["uid"], uid)

    def test_snapshot_round_trips_uid_and_links(self):
        self.build()
        self.ok("link", "1.2", "commit", "abc123")
        tasks.SNAPSHOT_FILE.write_text(tasks.snapshot_json(tasks.load()), encoding="utf-8")
        self.ok("import-json", str(tasks.SNAPSHOT_FILE))
        self.assertEqual(tasks.find(tasks.load(), "1.2")["links"][0]["value"], "abc123")


class BriefTests(TasksTestCase):
    """Task 4.9: tasks.py brief — the context bundle for one item."""

    def test_brief_shows_deps_and_spec(self):
        self.build()
        self.ok("edit", "1.2", "-d", "do the thing")
        self.ok("edit", "1.2", "--after", "1.1")
        self.ok("link", "1.2", "spec", "tmp/tasks/1.2.md")
        self.ok("link", "1.1", "commit", "cafe1234")
        out = self.ok("brief", "1.2")
        self.assertIn("do the thing", out)
        self.assertIn("Depends on", out)
        self.assertIn("cafe1234", out)
        self.assertIn("tmp/tasks/1.2.md", out)


class AuditLogTests(TasksTestCase):
    """Task 4.3: events table + tasks.py log + actor."""

    def test_status_change_writes_an_event(self):
        self.build()
        self.ok("start", "1.1.1")
        out = self.ok("log", "1.1.1")
        self.assertIn("status", out)
        self.assertIn("in_progress", out)

    def test_add_and_remove_are_recorded(self):
        self.build()
        self.ok("add", "1", "New task")
        self.assertIn("add", self.ok("log"))

    def test_log_since_filters(self):
        self.build()
        self.ok("start", "1.1.1")
        self.assertIn("no events", self.ok("log", "--since", "2999-01-01"))

    def test_link_is_logged(self):
        self.build()
        self.ok("link", "1.2", "commit", "abc")
        self.assertIn("commit", self.ok("log", "1.2"))

    def test_vibe_actor_is_recorded(self):
        os.environ["VIBE_ACTOR"] = "tester"
        self.addCleanup(os.environ.pop, "VIBE_ACTOR", None)
        self.build()
        self.ok("start", "1.1.1")
        self.assertIn("tester", self.ok("log", "1.1.1"))


class PlanBatchTests(TasksTestCase):
    """Task 4.10: plan-batch picks ready tasks with disjoint scope links."""

    def test_disjoint_scopes_are_batched(self):
        self.build()
        self.ok("link", "1.1.1", "scope", "src/a")
        self.ok("link", "1.1.2", "scope", "src/b")
        out = self.ok("plan-batch")
        self.assertIn("1.1.1", out)
        self.assertIn("1.1.2", out)

    def test_overlapping_scopes_pick_only_one(self):
        self.build()
        self.ok("link", "1.1.1", "scope", "src/shared")
        self.ok("link", "1.1.2", "scope", "src/shared")
        out = self.ok("plan-batch")
        self.assertNotEqual("1.1.1" in out, "1.1.2" in out)

    def test_tasks_without_scopes_are_skipped(self):
        self.build()
        self.assertIn("no disjoint ready tasks", self.ok("plan-batch"))

    def test_skipped_task_without_scope_is_warned_on_stderr(self):
        self.build()
        code, _, err = self.run_cli("plan-batch")
        self.assertEqual(code, 0)
        self.assertIn("skipped: no scope link", err)

    def test_nested_scopes_overlap(self):
        self.build()
        self.ok("link", "1.1.1", "scope", "src/a")
        self.ok("link", "1.1.2", "scope", "src/a/b")
        out = self.ok("plan-batch")
        self.assertNotEqual("1.1.1" in out, "1.1.2" in out)

    def test_similar_prefix_is_not_overlap(self):
        self.build()
        self.ok("link", "1.1.1", "scope", "src/a")
        self.ok("link", "1.1.2", "scope", "src/ab")
        out = self.ok("plan-batch")
        self.assertIn("1.1.1", out)
        self.assertIn("1.1.2", out)

    def test_batch_prefers_less_conflicted_tasks(self):
        self.build()
        self.ok("link", "1.1.1", "scope", "src/a")
        self.ok("link", "1.1.1", "scope", "src/b")
        self.ok("link", "1.1.2", "scope", "src/a")
        self.ok("link", "1.2", "scope", "src/b")

        out = self.ok("plan-batch")

        self.assertNotIn("1.1.1", out)
        self.assertIn("1.1.2", out)
        self.assertIn("1.2", out)

    def test_tasks_with_incomplete_dependencies_are_not_batched(self):
        self.build()
        self.ok("link", "1.1.1", "scope", "src/a")
        self.ok("link", "1.1.2", "scope", "src/b")
        self.ok("edit", "1.1.2", "--after", "1.1.1")

        out = self.ok("plan-batch")

        self.assertIn("1.1.1", out)
        self.assertNotIn("1.1.2", out)


class BackupTests(TasksTestCase):
    """Task 4.4: backup clone with history (local bare repo as remote), explicit push/restore."""

    def setUp(self):
        super().setUp()
        self.remote = Path(self.tmp.name) / "remote.git"
        self.backup = Path(self.tmp.name) / "backup"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        subprocess.run(["git", "init", "-q", str(self.backup)], check=True)
        subprocess.run(["git", "-C", str(self.backup), "remote", "add", "origin", str(self.remote)], check=True)
        tasks.BACKUP_DIR = self.backup

    def test_backup_writes_snapshot_and_commits(self):
        self.build()
        self.ok("backup")
        self.assertTrue((self.backup / "snapshot.json").exists())
        self.assertTrue((self.backup / "README.md").exists())
        log = subprocess.run(["git", "-C", str(self.backup), "log", "--oneline"],
                             capture_output=True, text=True).stdout
        self.assertTrue(log.strip(), "backup made no commit")

    def test_backup_push_reaches_the_remote(self):
        self.build()
        self.ok("backup", "--push")
        remote_log = subprocess.run(["git", "-C", str(self.remote), "log", "--oneline"],
                                    capture_output=True, text=True).stdout
        self.assertTrue(remote_log.strip(), "remote has no commits")

    def test_failed_push_warns_on_stderr(self):
        self.build()
        shutil.rmtree(self.remote)
        code, _, err = self.run_cli("backup", "--push")
        self.assertEqual(code, 0)
        self.assertIn("warning: backup: git push failed", err)

    def test_restore_refuses_stale_snapshot_when_pull_fails(self):
        self.build()
        self.ok("backup")
        old_snapshot = (self.backup / "snapshot.json").read_text(encoding="utf-8")
        self.ok("add-sprint", "Newer sprint")
        before = tasks.load()
        (self.backup / "snapshot.json").write_text(old_snapshot, encoding="utf-8")
        shutil.rmtree(self.remote)

        code, _, err = self.run_cli("restore", "--pull")

        self.assertEqual(code, 1)
        self.assertIn("warning: backup: git pull failed", err)
        self.assertIn("refusing to restore", err)
        self.assertEqual(tasks.load(), before)

    def test_restore_replaces_the_database(self):
        self.build()
        self.ok("backup")
        # Hand-written older snapshot (a single empty sprint) to prove restore replaces the DB.
        snapshot = {"version": 2, "project": "T", "sprints": [
            {"id": "1", "uid": "T-zzzz", "title": "Only sprint", "children": []}]}
        (self.backup / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
        self.ok("restore")
        out = self.ok("list")
        self.assertIn("Only sprint", out)
        self.assertNotIn("Task A", out)


class Task3_9Tests(TasksTestCase):
    """Tests for Task 3.9: TASKS.md is git-ignored; check regenerates it when missing."""

    def test_check_regenerates_missing_tasks_md(self):
        """When DB exists but TASKS.md is missing, check regenerates it silently."""
        self.ok("add-sprint", "S")
        tasks.VIEW_FILE.unlink()
        self.assertFalse(tasks.VIEW_FILE.exists())
        out = self.ok("check")
        self.assertIn("view regenerated", out)
        self.assertTrue(tasks.VIEW_FILE.exists())

    def test_check_still_fails_on_stale_tasks_md(self):
        """A stale TASKS.md still causes check to fail."""
        self.ok("add-sprint", "S")
        tasks.VIEW_FILE.write_text("stale content", encoding="utf-8")
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("out of date", err)

    def test_check_skips_when_no_db(self):
        """check skips all comparisons when the DB does not exist."""
        code, out, err = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertIn("no database", out)

    def test_change_command_recreates_deleted_tasks_md(self):
        """A change command (e.g., add-sprint) recreates TASKS.md if it was deleted."""
        self.ok("add-sprint", "S")
        tasks.VIEW_FILE.unlink()
        self.assertFalse(tasks.VIEW_FILE.exists())
        self.ok("add-sprint", "S2")
        self.assertTrue(tasks.VIEW_FILE.exists())
        self.assertIn("S2", tasks.VIEW_FILE.read_text(encoding="utf-8"))

    def test_git_ignores_view(self):
        """The generated view (tasks/README.md + per-sprint files) is listed in .gitignore."""
        import subprocess
        # Find the repo root via git (works in worktrees too)
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                self.skipTest("not in a git repository")
            repo_root = Path(result.stdout.strip())
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self.skipTest("git not available")

        gitignore = repo_root / ".gitignore"
        if gitignore.exists():
            content = gitignore.read_text(encoding="utf-8")
            self.assertIn("tasks/README.md", content,
                          ".gitignore should ignore tasks/README.md")
            self.assertIn("tasks/sprint-*.md", content,
                          ".gitignore should ignore the per-sprint files")


class SafeRmTests(TasksTestCase):
    """Safe deletion: preview, --dry-run, confirmation question, recovery file, restore-removed."""

    def rich_build(self):
        """Sprint 1 with Task A (subtasks, notes, an internal dep and a started leaf) and Task B."""
        self.ok("add-sprint", "Sprint one")
        self.ok("add", "1", "Task A")
        self.ok("add", "1.1", "Sub 1")
        self.ok("add", "1.1", "Sub 2")
        self.ok("add", "1", "Task B")
        self.ok("note", "1.1", "note root")
        self.ok("note", "1.1.1", "note sub")
        self.ok("edit", "1.1.2", "--after", "1.1.1")
        self.ok("start", "1.1.1")

    def exists(self, node_id):
        try:
            tasks.find(tasks.load(), node_id)
            return True
        except tasks.TaskError:
            return False

    def recovery_file(self):
        files = sorted(tasks.RECOVERY_DIR.glob("*.json"))
        self.assertEqual(len(files), 1, "expected exactly one recovery file")
        return files[0]

    def test_preview_content_and_counts(self):
        self.rich_build()
        self.ok("edit", "1.2", "--after", "1.1")  # outside depends on the subtree
        code, out, err = self.run_cli("rm", "1.1", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("1.1  «Task A»", err)
        self.assertIn("status=in_progress", err)
        self.assertIn("subtasks=2", err)
        self.assertIn("notes=1", err)
        self.assertIn("depends-on-me=1.2", err)
        self.assertIn("3 items, 2 notes will be deleted", err)
        self.assertIn("warning: some items are in progress or done, have notes, "
                      "or are depended on from outside", err)
        self.assertEqual(out, "")  # preview goes to stderr; stdout stays machine-readable

    def test_preview_plain_leaf_has_no_warning(self):
        self.rich_build()
        code, out, err = self.run_cli("rm", "1.2", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("1.2  «Task B»  status=none  subtasks=0  notes=0", err)
        self.assertIn("1 items, 0 notes will be deleted", err)
        self.assertNotIn("warning:", err)

    def test_dry_run_changes_nothing(self):
        self.rich_build()
        before = tasks.load()
        self.assertFalse(tasks.RECOVERY_DIR.exists())
        code, _, err = self.run_cli("rm", "1.1", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(tasks.load(), before)
        self.assertFalse(tasks.RECOVERY_DIR.exists())

    def test_non_tty_without_yes_refuses_with_exit_2(self):
        self.rich_build()
        before = tasks.load()
        with mock.patch.object(tasks, "_tty", return_value=False):
            code, out, err = self.run_cli("rm", "1.1")
        self.assertEqual(code, 2)
        self.assertIn("refusing to delete without confirmation: pass --yes (see --dry-run for the preview)",
                      err)
        self.assertIn("will be deleted", err)  # preview always comes first
        self.assertEqual(tasks.load(), before)
        self.assertFalse(tasks.RECOVERY_DIR.exists())

    def test_question_typed_id_confirms(self):
        self.rich_build()
        with mock.patch.object(tasks, "_tty", return_value=True), \
             mock.patch.object(tasks, "input", return_value="1.1"):
            code, out, err = self.run_cli("rm", "1.1")
        self.assertEqual(code, 0)
        self.assertIn('Type the id "1.1" to confirm, or press Enter to cancel:', err)
        self.assertNotIn("cancelled", err)
        self.assertFalse(self.exists("1.1"))
        self.assertTrue(tasks.RECOVERY_DIR.exists())

    def test_question_wrong_text_cancels(self):
        self.rich_build()
        before = tasks.load()
        with mock.patch.object(tasks, "_tty", return_value=True), \
             mock.patch.object(tasks, "input", return_value="nope"):
            code, out, err = self.run_cli("rm", "1.1")
        self.assertEqual(code, 1)
        self.assertIn("cancelled", err)
        self.assertEqual(tasks.load(), before)
        self.assertFalse(tasks.RECOVERY_DIR.exists())

    def test_question_empty_answer_cancels(self):
        self.rich_build()
        before = tasks.load()
        with mock.patch.object(tasks, "_tty", return_value=True), \
             mock.patch.object(tasks, "input", return_value=""):
            code, out, err = self.run_cli("rm", "1.1")
        self.assertEqual(code, 1)
        self.assertIn("cancelled", err)
        self.assertEqual(tasks.load(), before)

    def test_plain_leaf_accepts_y(self):
        self.rich_build()
        with mock.patch.object(tasks, "_tty", return_value=True), \
             mock.patch.object(tasks, "input", return_value="y"):
            code, out, err = self.run_cli("rm", "1.2")
        self.assertEqual(code, 0)
        self.assertFalse(self.exists("1.2"))

    def test_non_plain_item_requires_typed_id_not_y(self):
        self.rich_build()
        before = tasks.load()
        with mock.patch.object(tasks, "_tty", return_value=True), \
             mock.patch.object(tasks, "input", return_value="y"):
            code, out, err = self.run_cli("rm", "1.1")  # has children, notes, a started leaf
        self.assertEqual(code, 1)
        self.assertIn("cancelled", err)
        self.assertEqual(tasks.load(), before)

    def test_question_prompt_text(self):
        self.rich_build()
        prompt = tasks._rm_question(tasks.load(), ["1.1"])
        self.assertIn("Delete 1.1 «Task A» and everything below it (3 items, 2 notes)?", prompt)
        self.assertIn("This cannot be undone from the database (a recovery file is written).", prompt)
        self.assertIn('Type the id "1.1" to confirm, or press Enter to cancel:', prompt)

    def test_multi_id_requires_word_delete(self):
        self.rich_build()
        before = tasks.load()
        with mock.patch.object(tasks, "_tty", return_value=True), \
             mock.patch.object(tasks, "input", return_value="y"):
            code, out, err = self.run_cli("rm", "1.1", "1.2")
        self.assertEqual(code, 1)
        self.assertIn("cancelled", err)
        self.assertEqual(tasks.load(), before)
        with mock.patch.object(tasks, "_tty", return_value=True), \
             mock.patch.object(tasks, "input", return_value="delete"):
            code, out, err = self.run_cli("rm", "1.1", "1.2")
        self.assertEqual(code, 0)
        self.assertFalse(self.exists("1.1"))
        self.assertFalse(self.exists("1.2"))

    def test_yes_skips_question_but_never_preview_or_recovery(self):
        self.rich_build()
        code, out, err = self.run_cli("rm", "1.1", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("1.1  «Task A»", err)  # preview still printed with --yes
        self.assertIn("removed 1.1 and 2 item(s) below it", out)
        recovery = self.recovery_file()
        data = json.loads(recovery.read_text(encoding="utf-8"))
        self.assertEqual(data["removed"][0]["parent"], "1")
        node = data["removed"][0]["node"]
        self.assertEqual(node["id"], "1.1")
        self.assertEqual(node["children"][0]["id"], "1.1.1")
        self.assertEqual(node["children"][1]["depends_on"], ["1.1.1"])
        self.assertIn("note root", node["notes"][0])
        self.assertIn("note sub", node["children"][0]["notes"][0])
        self.assertFalse(self.exists("1.1"))
        self.ok("check")  # snapshot and TASKS.md regenerated by the normal mutate path

    def test_multi_id_recovery_filename_contains_ids(self):
        self.rich_build()
        self.ok("rm", "1.1", "1.2", "--yes")
        files = list(tasks.RECOVERY_DIR.glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertIn("1.1", files[0].name)
        self.assertIn("1.2", files[0].name)

    def test_recovery_write_failure_aborts_deletion(self):
        self.rich_build()
        tasks.RECOVERY_DIR.write_text("I am a file, not a directory", encoding="utf-8")
        before = tasks.load()
        code, out, err = self.run_cli("rm", "1.1", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("could not write recovery file", err)
        self.assertEqual(tasks.load(), before)
        self.assertTrue(self.exists("1.1"))

    def test_old_single_id_message_preserved(self):
        self.build()
        out = self.ok("rm", "1.2", "--yes")
        self.assertEqual(out, "removed 1.2 and 0 item(s) below it\n")
        out = self.ok("rm", "1.1", "--yes")
        self.assertEqual(out, "removed 1.1 and 2 item(s) below it\n")

    def test_dependency_blocked_rm_writes_no_recovery(self):
        self.rich_build()
        self.ok("edit", "1.2", "--after", "1.1")
        before = tasks.load()
        code, out, err = self.run_cli("rm", "1.1", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("depends on", err)
        self.assertEqual(tasks.load(), before)
        self.assertFalse(tasks.RECOVERY_DIR.exists())

    def test_restore_removed_round_trip(self):
        self.rich_build()
        self.ok("rm", "1.1", "--yes")
        recovery = self.recovery_file()
        out = self.ok("restore-removed", str(recovery))
        self.assertIn("restored 1.1 -> 1.3", out)
        self.assertIn("restored 1.1.1 -> 1.3.1", out)
        self.assertIn("restored 1.1.2 -> 1.3.2", out)
        data = tasks.load()
        node = tasks.find(data, "1.3")
        self.assertEqual(node["title"], "Task A")
        self.assertIn("note root", node["notes"][0])
        self.assertEqual(self.status("1.3.1"), "in_progress")  # stored status kept
        self.assertIn("note sub", tasks.find(data, "1.3.1")["notes"][0])
        self.assertEqual(tasks.find(data, "1.3.2")["depends_on"], ["1.3.1"])  # internal dep mapped
        self.assertEqual(tasks.find(data, "1.2")["depends_on"], [])
        self.ok("check")

    def test_restore_skips_missing_dependency_target(self):
        self.rich_build()
        self.ok("edit", "1.1.1", "--after", "1.2")  # internal dep on an external id
        self.ok("rm", "1.1", "--yes")
        recovery = self.recovery_file()
        self.ok("rm", "1.2", "--yes")  # the dep target no longer exists
        out = self.ok("restore-removed", str(recovery))
        self.assertIn("skipped dependency:", out)
        self.assertIn("1.2 (id does not exist)", out)
        self.assertEqual(tasks.find(tasks.load(), "1.1.1")["depends_on"], [])

    def test_restore_original_parent_missing_requires_parent(self):
        self.rich_build()
        self.ok("add-sprint", "Two")
        self.ok("rm", "1.1", "--yes")
        recovery = self.recovery_file()
        self.ok("rm", "1", "--yes")  # original parent sprint 1 is gone
        code, out, err = self.run_cli("restore-removed", str(recovery))
        self.assertEqual(code, 1)
        self.assertIn("original parent 1 no longer exists", err)
        out = self.ok("restore-removed", str(recovery), "--parent", "2")
        self.assertIn("restored 1.1 -> 2.1", out)
        self.assertIn("restored 1.1.1 -> 2.1.1", out)

    def test_restore_sprint_with_parent_refused(self):
        self.rich_build()
        self.ok("add-sprint", "Two")
        self.ok("rm", "1", "--yes")
        recovery = self.recovery_file()
        code, out, err = self.run_cli("restore-removed", str(recovery), "--parent", "2")
        self.assertEqual(code, 1)
        self.assertIn("cannot be restored under", err)

    def test_restore_too_deep_refused(self):
        self.rich_build()
        self.ok("add-sprint", "Two")
        self.ok("add", "2", "Task C")
        self.ok("rm", "1.1", "--yes")
        recovery = self.recovery_file()
        code, out, err = self.run_cli("restore-removed", str(recovery), "--parent", "2.1")
        self.assertEqual(code, 1)
        self.assertIn("too deep", err)

    def test_restore_a_sprint(self):
        self.ok("add-sprint", "S1")
        self.ok("add", "1", "T")
        self.ok("rm", "1", "--yes")
        recovery = self.recovery_file()
        out = self.ok("restore-removed", str(recovery))
        self.assertIn("restored 1 -> 1", out)
        self.assertIn("restored 1.1 -> 1.1", out)
        data = tasks.load()
        self.assertEqual(tasks.find(data, "1")["title"], "S1")
        self.assertEqual(tasks.find(data, "1.1")["title"], "T")


if __name__ == "__main__":
    unittest.main()
