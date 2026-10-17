"""Tests for scripts/tasks_mcp.py. Run: python3 -m unittest discover -s scripts -p "test_*.py" """
import json
import subprocess
import sys
import unittest
from pathlib import Path

import tasks_mcp
from test_tasks import TasksTestCase


class McpTests(TasksTestCase):
    def call(self, name, **arguments):
        reply = tasks_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments}})
        result = reply["result"]
        return result["content"][0]["text"], result["isError"]

    def test_initialize_negotiates_protocol(self):
        reply = tasks_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(reply["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", reply["result"]["capabilities"])

    def test_notifications_get_no_response(self):
        self.assertIsNone(tasks_mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_unknown_method_is_an_error(self):
        reply = tasks_mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})
        self.assertEqual(reply["error"]["code"], -32601)

    def test_every_tool_has_a_schema_and_read_only_hint(self):
        tools = tasks_mcp.tool_list()
        self.assertEqual(len(tools), 19)  # added tasks_move, tasks_sprint, tasks_overview, tasks_link, tasks_unlink, tasks_mv, tasks_brief, tasks_log
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertIn("readOnlyHint", tool["annotations"])

    def test_tool_list_contains_tasks_move(self):
        """tasks_move must be in the tool list."""
        names = {t["name"] for t in tasks_mcp.tool_list()}
        self.assertIn("tasks_move", names)

    def test_tasks_move_round_trip(self):
        """Move a leaf via MCP and verify the result."""
        self.build()
        text, error = self.call("tasks_move", id="1.1.1", new_parent="1.2")
        self.assertFalse(error, text)
        self.assertIn("moved", text)
        self.assertEqual(tasks_find(self, "1.2.1")["title"], "Sub 1")

    def test_tasks_move_same_parent_reorder_is_a_tool_error(self):
        """Reordering siblings is refused (ids encode the position) and leaves the subtree untouched."""
        self.build()
        text, error = self.call("tasks_move", id="1.1.2", new_parent="1.1", pos=1)
        self.assertTrue(error)
        self.assertIn("reordering siblings is not supported", text)
        self.assertEqual([c["id"] for c in tasks_find(self, "1.1")["children"]], ["1.1.1", "1.1.2"])

    def test_tasks_move_error_is_tool_error(self):
        """Moving a sprint via MCP returns an error, not a crash."""
        self.build()
        self.ok("add-sprint", "Sprint B")
        text, error = self.call("tasks_move", id="1", new_parent="2")
        self.assertTrue(error)
        self.assertIn("sprint", text)

    def test_tasks_list_filter_argv_uses_double_dash(self):
        """tasks_list passes each value filter as a --option (regression: bare keys were rejected)."""
        build_argv = tasks_mcp.TOOLS["tasks_list"][3]
        self.assertEqual(build_argv({"status": "in_progress"}), ["list", "--status", "in_progress"])
        self.assertEqual(build_argv({"sprint": "1"}), ["list", "--sprint", "1"])
        self.assertEqual(build_argv({"assignee": "kilo"}), ["list", "--assignee", "kilo"])
        self.assertEqual(build_argv({"text": "needle"}), ["list", "--text", "needle"])
        self.assertEqual(build_argv({"leaves": True}), ["list", "--leaves"])

    def test_tasks_list_filters_end_to_end(self):
        """Every filter reaches tasks.py and returns the expected subset without an error."""
        self.build()
        self.ok("edit", "1.2", "-a", "kilo", "-d", "needle text")
        self.ok("start", "1.2")

        text, error = self.call("tasks_list", status="in_progress")
        self.assertFalse(error, text)
        self.assertIn("1.2", text)
        self.assertNotIn("1.1.1", text)

        text, error = self.call("tasks_list", assignee="kilo")
        self.assertFalse(error, text)
        self.assertIn("1.2", text)
        self.assertNotIn("1.1.1", text)

        text, error = self.call("tasks_list", text="NEEDLE")
        self.assertFalse(error, text)
        self.assertIn("1.2", text)
        self.assertNotIn("1.1.1", text)

        text, error = self.call("tasks_list", sprint="1", leaves=True)
        self.assertFalse(error, text)
        self.assertIn("1.2", text)
        self.assertIn("1.1.1", text)

    def test_tasks_remove_is_two_step(self):
        """tasks_remove previews without confirm and only deletes with confirm=true."""
        self.build()
        text, error = self.call("tasks_remove", id="1.2")
        self.assertFalse(error, text)
        self.assertIn("confirm=true", text)
        self.assertIn("1.2", text)
        self.assertIsNotNone(tasks_find(self, "1.2"))
        text, error = self.call("tasks_remove", id="1.2", confirm=True)
        self.assertFalse(error, text)
        import tasks as tasks_module
        with self.assertRaises(tasks_module.TaskError):
            tasks_module.find(tasks_module.load(), "1.2")

    def test_set_status_with_ids_array(self):
        """tasks_set_status accepts ids array."""
        self.build()
        text, error = self.call("tasks_set_status", ids=["1.1.1", "1.1.2", "1.2"], status="done")
        self.assertFalse(error, text)
        self.assertEqual(self.status("1"), "done")

    def test_set_status_with_single_id(self):
        """tasks_set_status still works with single id."""
        self.build()
        text, error = self.call("tasks_set_status", id="1.1.1", status="done")
        self.assertFalse(error, text)
        self.assertEqual(self.status("1.1.1"), "done")

    def test_edit_with_ids_array(self):
        """tasks_edit accepts ids array."""
        self.build()
        text, error = self.call("tasks_edit", ids=["1.1", "1.2"], assignee="junie")
        self.assertFalse(error, text)
        self.assertEqual(tasks_find(self, "1.1")["assignee"], "junie")
        self.assertEqual(tasks_find(self, "1.2")["assignee"], "junie")

    def test_edit_with_ids_rejects_title(self):
        """tasks_edit with ids and title is rejected."""
        self.build()
        text, error = self.call("tasks_edit", ids=["1.1", "1.2"], title="X")
        self.assertTrue(error)
        self.assertIn("title", text)

    def test_set_status_ids_and_id_rejected(self):
        """Passing both id and ids is rejected."""
        self.build()
        text, error = self.call("tasks_set_status", id="1.1", ids=["1.2"], status="done")
        self.assertTrue(error)

    def test_plan_work_and_cascade_through_tools(self):
        self.assertFalse(self.call("tasks_add_sprint", title="S", goal="g")[1])
        self.call("tasks_add", parent="1", title="T")
        self.call("tasks_add", parent="1.1", title="Sub A")
        self.call("tasks_add", parent="1.1", title="Sub B", assignee="kilo", after="1.1.1")
        self.assertIn("1.1.1", self.call("tasks_next")[0])
        self.call("tasks_set_status", id="1.1.1", status="done")
        self.assertIn("1.1.2", self.call("tasks_next", assignee="kilo")[0])
        text, error = self.call("tasks_set_status", id="1.1.2", status="blocked")
        self.assertTrue(error)
        self.assertIn("reason", text)
        self.call("tasks_set_status", id="1.1.2", status="blocked", reason="waiting")
        self.assertEqual(self.status("1"), "blocked")
        self.assertIn("waiting", self.call("tasks_view")[0])
        self.call("tasks_set_status", id="1", status="done", cascade_down=True)
        self.assertEqual(self.status("1"), "done")
        self.assertIn("done: 2", self.call("tasks_stats")[0])

    def test_edit_can_clear_dependencies(self):
        self.build()
        self.call("tasks_edit", id="1.2", after="1.1")
        self.assertEqual(tasks_find(self, "1.2")["depends_on"], ["1.1"])
        self.call("tasks_edit", id="1.2", after="")
        self.assertEqual(tasks_find(self, "1.2")["depends_on"], [])

    def test_errors_are_tool_errors_not_crashes(self):
        self.assertTrue(self.call("tasks_show", ids=["9"])[1])
        self.assertTrue(self.call("tasks_show")[1])
        self.assertTrue(self.call("tasks_show", ids=["1"], bogus=1)[1])
        self.assertTrue(self.call("no_such_tool")[1])

    def test_tasks_overview_lists_sprints(self):
        self.build()
        text, error = self.call("tasks_overview")
        self.assertFalse(error, text)
        self.assertIn("Sprint one", text)
        self.assertIn("done 0/", text)

    def test_tasks_sprint_returns_a_table(self):
        self.build()
        text, error = self.call("tasks_sprint", id="1")
        self.assertFalse(error, text)
        self.assertIn("| 1.1 |", text)

    def test_show_multi_and_brief(self):
        self.build()
        text, error = self.call("tasks_show", ids=["1.1", "1.2"], brief=True)
        self.assertFalse(error, text)
        self.assertIn("1.1", text)
        self.assertIn("1.2", text)
        self.assertNotIn("{", text)  # brief is not JSON

    def test_list_subtree_with_desc_and_notes(self):
        self.build()
        self.ok("edit", "1.2", "-d", "needle description")
        self.ok("note", "1.2", "a note")
        text, error = self.call("tasks_list", id="1.2", desc=True, notes=True)
        self.assertFalse(error, text)
        self.assertIn("needle description", text)
        self.assertIn("a note", text)
        self.assertNotIn("1.1.1", text)  # only the requested subtree

    def test_stdio_server_end_to_end(self):
        script = Path(tasks_mcp.__file__)
        proc = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, timeout=30,
            input="\n".join(json.dumps(m) for m in (
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})) + "\n")
        replies = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2])
        self.assertEqual(len(replies[1]["result"]["tools"]), 19)


def tasks_find(case, node_id):
    import tasks
    return tasks.find(tasks.load(), node_id)


if __name__ == "__main__":
    unittest.main()
