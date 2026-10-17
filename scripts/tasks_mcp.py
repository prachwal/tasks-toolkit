#!/usr/bin/env python3
"""MCP server (stdio) exposing the task tracker (scripts/tasks.py) as tools.

Speaks newline-delimited JSON-RPC 2.0 (MCP). Standard library only. Every tool is a thin wrapper
around a tasks.py command, so the CLI and the MCP interface share one implementation and one
database; each change still re-renders the tasks/ view.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import tasks  # noqa: E402

SERVER = {"name": "tasks", "version": "1.0.0"}
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")


def schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def text(description: str) -> dict:
    return {"type": "string", "description": description}


ID = text("Item id: sprint '1', task '1.2', subtask '1.2.3'")
STATUS = {"type": "string", "enum": ["none", "in_progress", "blocked", "done", "cancelled", "review"],
          "description": "none (brak), in_progress (w trakcie), blocked (bloker, needs reason), "
                         "done (gotowe), cancelled (anulowane), review (w przeglądzie)"}


def flag(name, value, out):  # optional "--name value"
    if value not in (None, ""):
        out += [name, str(value)]


# name -> (description, input schema, annotations, argv builder)
TOOLS: dict[str, tuple] = {
    "tasks_list": (
        "List sprints/tasks/subtasks as an indented tree with their (derived) status.",
        schema({"id": text("Only this item and everything below it"),
                "status": STATUS, "sprint": text("Only this sprint id"), "assignee": text("Only this assignee"),
                "text": text("Case-insensitive match in title/description"),
                "leaves": {"type": "boolean", "description": "Only items without children"},
                "desc": {"type": "boolean", "description": "Also print each item's description"},
                "notes": {"type": "boolean", "description": "Also print each item's notes"}}),
        {"readOnlyHint": True},
        lambda a: ["list"] + ([str(a["id"])] if a.get("id") else [])
        + _opts(a, status="--status", sprint="--sprint", assignee="--assignee", text="--text")
        + (["--leaves"] if a.get("leaves") else []) + (["--desc"] if a.get("desc") else [])
        + (["--notes"] if a.get("notes") else [])),
    "tasks_show": (
        "Show one or more items (description, assignee, dependencies, notes, derived status); brief=true prints one line per item.",
        schema({"ids": {"type": "array", "items": ID, "minItems": 1},
                "brief": {"type": "boolean", "description": "One line per item instead of JSON"}}, ["ids"]),
        {"readOnlyHint": True},
        lambda a: ["show", *[str(x) for x in a["ids"]]] + (["--brief"] if a.get("brief") else [])),
    "tasks_next": (
        "Next ready, unfinished leaf items (dependencies met) in the first unfinished sprint.",
        schema({"assignee": text("Only items assigned to this agent"),
                "count": {"type": "integer", "minimum": 1, "maximum": 50, "description": "How many (default 1)"}}),
        {"readOnlyHint": True},
        lambda a: ["next"] + (["--assignee", a["assignee"]] if a.get("assignee") else [])
        + (["-n", str(a["count"])] if a.get("count") else [])),
    "tasks_stats": (
        "Counts per status and open work per assignee.", schema({}), {"readOnlyHint": True}, lambda a: ["stats"]),
    "tasks_sprint": (
        "One table of a sprint's tasks (status, assignee, progress) in a single call.",
        schema({"id": text("Sprint id")}, ["id"]), {"readOnlyHint": True},
        lambda a: ["sprint-table", a["id"]]),
    "tasks_overview": (
        "All sprints in one call: derived status, counts per status and the goal.",
        schema({}), {"readOnlyHint": True}, lambda a: ["overview"]),
    "tasks_add_sprint": (
        "Create a sprint.",
        schema({"title": text("Sprint title"), "goal": text("Sprint goal"), "start": text("YYYY-MM-DD"),
                "end": text("YYYY-MM-DD")}, ["title"]),
        {"readOnlyHint": False, "destructiveHint": False},
        lambda a: ["add-sprint", a["title"]] + _opts(a, goal="--goal", start="--start", end="--end")),
    "tasks_add": (
        "Add a task under a sprint id, or a subtask under a task id (max depth: sprint > task > subtask).",
        schema({"parent": text("Sprint id (e.g. '2') or task id (e.g. '2.1')"), "title": text("Title"),
                "description": text("Description / acceptance criteria"),
                "assignee": text("Who does it: junie, kilo, claude, ..."),
                "after": text("Comma separated ids that must be done first")}, ["parent", "title"]),
        {"readOnlyHint": False, "destructiveHint": False},
        lambda a: ["add", a["parent"], a["title"]] + _opts(a, description="--desc", assignee="--assignee",
                                                           after="--after")),
    "tasks_set_status": (
        "Set a status on a leaf item. Parent statuses are derived (any blocked -> blocked, all done -> done, "
        "anything started/finished -> in progress). Blocking requires a reason. cascade_down applies the "
        "status to every leaf below a task or sprint. Pass either id or ids (exactly one).",
        schema({"id": ID, "ids": {"type": "array", "items": {"type": "string"},
                "description": "Multiple item ids (alternative to id)"},
                "status": STATUS, "reason": text("Required when status is blocked"),
                "cascade_down": {"type": "boolean", "description": "Apply to all leaves below a parent"}},
               []),
        {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
        lambda a: _build_status_argv(a)),
    "tasks_edit": (
        "Edit title, description, assignee, dependencies (--after replaces the list; empty clears) and, for "
        "sprints, goal/start/end. Pass either id or ids (exactly one). --title with multiple ids is rejected.",
        schema({"id": ID, "ids": {"type": "array", "items": {"type": "string"},
                "description": "Multiple item ids (alternative to id)"},
                "title": text("New title"), "description": text("New description"),
                "assignee": text("New assignee"), "after": text("Comma separated ids; empty string clears"),
                "goal": text("Sprint only"), "start": text("Sprint only, YYYY-MM-DD"),
                "end": text("Sprint only, YYYY-MM-DD")}, []),
        {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
        lambda a: _build_edit_argv(a)),
    "tasks_move": (
        "Move an item to another parent (renumbers the subtree). Errors: unknown ids, moving a sprint, "
        "moving into own subtree, depth > MAX_DEPTH, same parent without --pos.",
        schema({"id": ID, "new_parent": text("New parent id"),
                "pos": {"type": "integer", "minimum": 1, "description": "1-based position (default: append last)"}},
               ["id", "new_parent"]),
        {"readOnlyHint": False, "destructiveHint": False},
        lambda a: ["mv", a["id"], a["new_parent"]] + (["--pos", str(a["pos"])] if a.get("pos") else [])),
    "tasks_note": (
        "Append a dated note to an item (progress, decisions, pointers to patches).",
        schema({"id": ID, "text": text("Note text")}, ["id", "text"]),
        {"readOnlyHint": False, "destructiveHint": False}, lambda a: ["note", a["id"], a["text"]]),
    "tasks_remove": (
        "Delete an item and everything below it (cascades to notes and dependencies). Refused while other "
        "items depend on it. Two-step: call without confirm for a preview, then again with confirm=true.",
        schema({"id": ID, "confirm": {"type": "boolean",
                "description": "Omit (or false) for a preview; set true to actually delete."}}, ["id"]),
        {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
        lambda a: ["rm", a["id"], "--yes"] if a.get("confirm") else ["rm", a["id"], "--dry-run"]),
    "tasks_link": (
        "Attach a structured link (spec, patch, runner, model, agent, commit, branch, fixes, scope) to an item.",
        schema({"id": ID, "kind": {"type": "string",
                "enum": ["spec", "patch", "runner", "model", "agent", "commit", "branch", "fixes", "scope"],
                "description": "Link kind"}, "value": text("Link value (path, sha, name, ...)")}, ["id", "kind", "value"]),
        {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
        lambda a: ["link", a["id"], a["kind"], a["value"]]),
    "tasks_unlink": (
        "Remove a structured link from an item.",
        schema({"id": ID, "kind": {"type": "string",
                "enum": ["spec", "patch", "runner", "model", "agent", "commit", "branch", "fixes", "scope"],
                "description": "Link kind"}, "value": text("Link value to remove")}, ["id", "kind", "value"]),
        {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
        lambda a: ["unlink", a["id"], a["kind"], a["value"]]),
    "tasks_mv": (
        "Move an item to another parent (renumbers the subtree).",
        schema({"id": ID, "new_parent": text("New parent id"),
                "pos": {"type": "integer", "minimum": 1,
                        "description": "1-based position (default: append last)"}}, ["id", "new_parent"]),
        {"readOnlyHint": False, "destructiveHint": False},
        lambda a: ["mv", a["id"], a["new_parent"]] + (["--pos", str(a["pos"])] if a.get("pos") else [])),
    "tasks_brief": (
        "Context bundle for one item: description, dependencies with merge commits, spec link, notes, "
        "and the list of files the dependencies changed.",
        schema({"id": ID}, ["id"]), {"readOnlyHint": True},
        lambda a: ["brief", a["id"]]),
    "tasks_log": (
        "Print the audit log (optionally for one item, optionally since a timestamp).",
        schema({"id": ID, "since": text("ISO timestamp (e.g. 2026-10-17)")}), {"readOnlyHint": True},
        lambda a: (["log", a["id"]] if a.get("id") else ["log"])
        + (["--since", a["since"]] if a.get("since") else [])),
}


def _opts(args: dict, keep_empty: tuple = (), **mapping) -> list[str]:
    out: list[str] = []
    for key, option in mapping.items():
        if key == "keep_empty":
            continue
        value = args.get(key)
        if value is None or (value == "" and key not in keep_empty):
            continue
        out += [option, str(value)]
    return out


def _resolve_ids(args: dict) -> list[str]:
    """Return the list of ids from either 'id' or 'ids' (exactly one must be present)."""
    has_id = "id" in args and args["id"] not in (None, "")
    has_ids = "ids" in args and args["ids"] is not None
    if has_id and not has_ids:
        return [args["id"]]
    if has_ids and not has_id:
        return [str(value) for value in args["ids"]]
    if has_id and has_ids:
        raise ValueError("pass either id or ids, not both")
    raise ValueError("pass either id or ids")


def _build_status_argv(args: dict) -> list[str]:
    ids = _resolve_ids(args)
    result = ["status"] + ids + [args["status"]]
    result += _opts(args, reason="--reason")
    if args.get("cascade_down"):
        result += ["--all"]
    return result


def _build_edit_argv(args: dict) -> list[str]:
    ids = _resolve_ids(args)
    result = ["edit"] + ids
    result += _opts(args, title="--title", description="--desc", assignee="--assignee",
                    goal="--goal", start="--start", end="--end", after="--after",
                    keep_empty=("after",))
    return result


def run_tool(name: str, arguments: dict) -> tuple[str, bool]:
    """Run one tool; returns (text, is_error)."""
    if name == "tasks_view":
        data = tasks.load()
        return tasks.render(data), False
    if name not in TOOLS:
        return f"unknown tool: {name}", True
    schema_def = TOOLS[name][1]
    for key in schema_def["required"]:
        if key not in arguments:
            return f"missing required argument: {key}", True
    unknown = set(arguments) - set(schema_def["properties"])
    if unknown:
        return f"unknown argument(s): {', '.join(sorted(unknown))}", True
    if name == "tasks_remove":
        rm_out, rm_err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(rm_out), contextlib.redirect_stderr(rm_err):
                code = tasks.main(TOOLS[name][3](arguments))
        except Exception as exc:  # noqa: BLE001 - report instead of killing the server
            return f"internal error: {exc}", True
        text = (rm_out.getvalue() + rm_err.getvalue()).strip()
        if code != 0:
            return text or "remove failed", True
        if not arguments.get("confirm"):
            return (text + "\n\nPreview only — nothing was deleted. "
                    "Call tasks_remove again with confirm=true to delete.").strip(), False
        return text or "ok", False
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tasks.main(TOOLS[name][3](arguments))
    except SystemExit as exit_:  # argparse errors
        return (err.getvalue() or f"invalid arguments (exit {exit_.code})").strip(), True
    except Exception as exc:  # noqa: BLE001 - report instead of killing the server
        return f"internal error: {exc}", True
    if code != 0:
        return (err.getvalue() or out.getvalue()).strip(), True
    return out.getvalue().strip() or "ok", False


TOOLS_VIEW = {"tasks_view": (
    "Return the rendered task view README (overall + current sprint + links to the per-sprint files).", schema({}), {"readOnlyHint": True})}


def tool_list() -> list[dict]:
    listed = [{"name": n, "description": d, "inputSchema": s, "annotations": a} for n, (d, s, a, _) in TOOLS.items()]
    listed += [{"name": n, "description": d, "inputSchema": s, "annotations": a} for n, (d, s, a) in TOOLS_VIEW.items()]
    return listed


def handle(message: dict) -> dict | None:
    """Handle one JSON-RPC message; returns the response, or None for notifications."""
    method, msg_id, params = message.get("method"), message.get("id"), message.get("params") or {}
    if msg_id is None:  # notification (e.g. notifications/initialized)
        return None

    def ok(result) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def fail(code, text_) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": text_}}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
                   "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER,
                   "instructions": "Task tracker: sprints > tasks > subtasks with derived statuses. "
                                   "Use tasks_next to pick work, tasks_set_status to update it."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tool_list()})
    if method == "tools/call":
        content, is_error = run_tool(params.get("name", ""), params.get("arguments") or {})
        return ok({"content": [{"type": "text", "text": content}], "isError": is_error})
    return fail(-32601, f"method not found: {method}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}),
                  flush=True)
            continue
        response = handle(message)
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
