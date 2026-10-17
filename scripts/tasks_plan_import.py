"""Atomic plan import for the automated sprint lifecycle (Sprint 12, task 12.2).

Provides an idempotent, single-transaction import primitive owned by tasks.py.
No separate database writer or schema migration is introduced.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tasks  # noqa: E402


def canonical_fingerprint(plan: dict) -> str:
    """Deterministic SHA-256 fingerprint of a plan (sorted keys)."""
    return hashlib.sha256(
        json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _validate_spec(spec_text: str, spec_path: Path) -> None:
    """Write spec to disk and lint it (lint must be enabled)."""
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(spec_text, encoding="utf-8")
    lint = Path(__file__).resolve().parent / "spec-lint.sh"
    if not lint.exists():
        return
    env = dict(os.environ) if False else None
    import os
    env = {k: v for k, v in os.environ.items() if k != "SPEC_LINT"}
    r = subprocess.run(
        ["bash", str(lint), str(spec_path)],
        capture_output=True, text=True, env=env,
    )
    if r.returncode != 0:
        raise tasks.TaskError(f"spec lint failed for {spec_path}: {r.stderr or r.stdout}")


def _find_existing_run(conn, run_id: str):
    """Return (sprint_id, fingerprint) if run_id was already imported, else (None, None)."""
    row = conn.execute(
        "SELECT node_uid FROM links WHERE kind='agent' AND value=?",
        (f"run-id:{run_id}",),
    ).fetchone()
    if row is None:
        return None, None
    sprint_row = conn.execute(
        "SELECT id FROM nodes WHERE uid=?", (row[0],)
    ).fetchone()
    if sprint_row is None:
        return None, None
    fp_row = conn.execute(
        "SELECT value FROM links WHERE node_uid=? AND kind='agent' AND value LIKE 'fingerprint:%'",
        (row[0],),
    ).fetchone()
    fp = fp_row[0].replace("fingerprint:", "", 1) if fp_row else None
    return sprint_row[0], fp


def _reconstruct_mapping(conn, sprint_id: str) -> dict:
    """Rebuild task-key -> {id, uid} mapping from stored links."""
    result = {}
    for row in conn.execute(
        "SELECT id, uid FROM nodes WHERE parent_id=?", (sprint_id,)
    ):
        tid, tuid = row
        kr = conn.execute(
            "SELECT value FROM links WHERE node_uid=? AND kind='agent' AND value LIKE 'task-key:%'",
            (tuid,),
        ).fetchone()
        if kr:
            key = kr[0].replace("task-key:", "", 1)
            result[key] = {"id": tid, "uid": tuid}
    return result


def _repair_materialization(plan: dict, root: Path) -> None:
    """Write any missing spec files (no DB changes)."""
    spec_dir = root / "tmp" / "tasks"
    spec_dir.mkdir(parents=True, exist_ok=True)
    for t in plan.get("tasks", []):
        spec_name = t.get("spec_name", "")
        if spec_name and t.get("spec"):
            sp = spec_dir / f"{spec_name}.md"
            if not sp.exists():
                sp.write_text(t["spec"], encoding="utf-8")


def import_plan(conn, plan: dict, run_id: str, root: Path) -> dict:
    """Import *plan* under *run_id*.  Must be called inside a mutate transaction.

    Returns a receipt dict.  Raises TaskError on validation or conflict.
    """
    fingerprint = canonical_fingerprint(plan)

    # --- replay detection ---
    existing_sid, existing_fp = _find_existing_run(conn, run_id)
    if existing_sid is not None:
        if existing_fp != fingerprint:
            raise tasks.TaskError(
                f"run-id {run_id!r} reused with different content"
            )
        # repair materialization without reinserting
        _repair_materialization(plan, root)
        # re-render view + snapshot
        data = tasks.load(conn)
        tasks.write_view(data)
        tasks.write_snapshot(data)
        return {
            "sprint_id": existing_sid,
            "sprint_uid": conn.execute(
                "SELECT uid FROM nodes WHERE id=?", (existing_sid,)
            ).fetchone()[0],
            "task_map": _reconstruct_mapping(conn, existing_sid),
            "replay": True,
        }

    # --- validate plan structure ---
    sprint_info = plan.get("sprint", {})
    if not sprint_info.get("slug") or not sprint_info.get("title"):
        raise tasks.TaskError("plan missing sprint slug/title")
    tasks_list = plan.get("tasks", [])
    if not tasks_list:
        raise tasks.TaskError("plan has no tasks")

    # --- validate + write specs before mutation ---
    spec_dir = root / "tmp" / "tasks"
    spec_dir.mkdir(parents=True, exist_ok=True)
    for t in tasks_list:
        spec_name = t.get("spec_name", "")
        spec_text = t.get("spec", "")
        if spec_name and spec_text:
            _validate_spec(spec_text, spec_dir / f"{spec_name}.md")

    # --- create sprint ---
    sprint_id = tasks.insert_node(
        conn, None, sprint_info["title"],
        goal=sprint_info.get("goal", ""),
        start=sprint_info.get("start", ""),
        end=sprint_info.get("end", ""),
    )
    sprint_uid = conn.execute(
        "SELECT uid FROM nodes WHERE id=?", (sprint_id,)
    ).fetchone()[0]

    # --- store run-id and fingerprint as agent links ---
    conn.execute(
        "INSERT OR IGNORE INTO links (node_uid, kind, value, created) VALUES (?,?,?,?)",
        (sprint_uid, "agent", f"run-id:{run_id}", tasks.today()),
    )
    conn.execute(
        "INSERT OR IGNORE INTO links (node_uid, kind, value, created) VALUES (?,?,?,?)",
        (sprint_uid, "agent", f"fingerprint:{fingerprint}", tasks.today()),
    )

    # --- create tasks ---
    task_map = {}
    key_to_id: dict = {}
    for t in tasks_list:
        key = t["key"]
        task_id = tasks.insert_node(
            conn, sprint_id, t["title"],
            description=t.get("description", ""),
            assignee=t.get("assignee", ""),
        )
        task_uid = conn.execute(
            "SELECT uid FROM nodes WHERE id=?", (task_id,)
        ).fetchone()[0]
        key_to_id[key] = task_id
        conn.execute(
            "INSERT OR IGNORE INTO links (node_uid, kind, value, created) VALUES (?,?,?,?)",
            (task_uid, "agent", f"task-key:{key}", tasks.today()),
        )
        # spec link
        spec_name = t.get("spec_name", "")
        if spec_name:
            conn.execute(
                "INSERT OR IGNORE INTO links (node_uid, kind, value, created) VALUES (?,?,?,?)",
                (task_uid, "spec", f"tmp/tasks/{spec_name}.md", tasks.today()),
            )
        # scope links
        for scope in t.get("scope", []):
            conn.execute(
                "INSERT OR IGNORE INTO links (node_uid, kind, value, created) VALUES (?,?,?,?)",
                (task_uid, "scope", scope, tasks.today()),
            )
        task_map[key] = {"id": task_id, "uid": task_uid}

    # --- set dependencies ---
    for t in tasks_list:
        key = t["key"]
        deps = t.get("depends_on", [])
        dep_ids = [key_to_id[d] for d in deps if d in key_to_id]
        if dep_ids:
            tasks.set_deps(conn, key_to_id[key], dep_ids)

    return {
        "sprint_id": sprint_id,
        "sprint_uid": sprint_uid,
        "task_map": task_map,
        "replay": False,
    }


def cmd_import_plan(args) -> None:
    """CLI entry point: tasks.py import-plan PLAN --run-id NAME"""
    plan_path = Path(args.plan)
    if not plan_path.exists():
        raise tasks.TaskError(f"plan file not found: {plan_path}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    run_id = args.run_id
    root = tasks.ROOT

    # read-only replay check
    conn = tasks.connect()
    try:
        existing_sid, existing_fp = _find_existing_run(conn, run_id)
    finally:
        conn.close()

    if existing_sid is not None:
        fingerprint = canonical_fingerprint(plan)
        if existing_fp != fingerprint:
            raise tasks.TaskError(f"run-id {run_id!r} reused with different content")
        # repair materialization
        _repair_materialization(plan, root)
        conn = tasks.connect()
        try:
            data = tasks.load(conn)
            tasks.write_view(data)
            tasks.write_snapshot(data)
            mapping = _reconstruct_mapping(conn, existing_sid)
            sprint_uid = conn.execute(
                "SELECT uid FROM nodes WHERE id=?", (existing_sid,)
            ).fetchone()[0]
        finally:
            conn.close()
        receipt = {
            "sprint_id": existing_sid,
            "sprint_uid": sprint_uid,
            "task_map": mapping,
            "replay": True,
        }
        print(json.dumps(receipt, indent=2))
        return

    # validate specs before mutation (lint enabled)
    spec_dir = root / "tmp" / "tasks"
    spec_dir.mkdir(parents=True, exist_ok=True)
    for t in plan.get("tasks", []):
        sn = t.get("spec_name", "")
        st = t.get("spec", "")
        if sn and st:
            _validate_spec(st, spec_dir / f"{sn}.md")

    # mutate
    receipt_holder = {}

    def action(conn):
        r = import_plan(conn, plan, run_id, root)
        receipt_holder.update(r)
        return ""

    tasks.mutate(action)
    print(json.dumps(receipt_holder, indent=2))
