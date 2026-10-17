#!/usr/bin/env python3
"""Writes the machine-readable run summary for the delegation runners.

Called by scripts/junie-task.sh, scripts/kilo-task.sh and scripts/codex-task.sh when a run finishes. Writes one
JSON object to <outfile> describing the run (identity, timing, exits and verdict), so
scripts/junie-status.sh and other tools can report on finished runs without parsing logs.
Standard library only. The runners pass the values as key=value arguments, so bash never
quotes them itself and odd values (spaces, special characters) cannot break the output.

Usage: scripts/run_summary.py <outfile> <key=value> [<key=value> ...]

Keys:
    name, runner, model, task_id, patch    str (required)
    agent                                  str (required for runner=kilo only)
    started, finished                      int, epoch seconds (required)
    agent_exit, verify_exit                int (required)
    files_changed                          int (required)
    verdict                                verified|verify_failed|agent_failed

duration_s is derived as finished - started. Unknown keys, a wrong runner or verdict, or
a missing field are errors and nothing is written. Output is deterministic (sorted keys,
2-space indent, trailing newline).
"""
from __future__ import annotations

import json
import sys

STR_FIELDS = ("name", "runner", "model", "agent", "task_id", "patch", "verdict")
INT_FIELDS = ("started", "finished", "agent_exit", "verify_exit", "files_changed")
RUNNERS = ("junie", "kilo", "codex")
VERDICTS = ("verified", "verify_failed", "agent_failed")


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(f"usage: {argv[0]} <outfile> <key=value> ...", file=sys.stderr)
        return 2
    outfile = argv[1]

    data: dict[str, object] = {}
    for pair in argv[2:]:
        key, sep, value = pair.partition("=")
        if not sep:
            print(f"error: expected key=value, got {pair!r}", file=sys.stderr)
            return 2
        if key in INT_FIELDS:
            try:
                data[key] = int(value)
            except ValueError:
                print(f"error: field {key!r} must be an integer, got {value!r}", file=sys.stderr)
                return 2
        elif key in STR_FIELDS:
            data[key] = value
        else:
            print(f"error: unknown field {key!r}", file=sys.stderr)
            return 2

    required = (set(STR_FIELDS) | set(INT_FIELDS)) - {"agent"}
    missing = sorted(required - set(data))
    if missing:
        print(f"error: missing fields: {', '.join(missing)}", file=sys.stderr)
        return 2

    if data["runner"] not in RUNNERS:
        print(f"error: runner must be one of {', '.join(RUNNERS)}, got {data['runner']!r}", file=sys.stderr)
        return 2
    if data["verdict"] not in VERDICTS:
        print(f"error: verdict must be one of {', '.join(VERDICTS)}, got {data['verdict']!r}", file=sys.stderr)
        return 2
    if data["runner"] == "kilo" and "agent" not in data:
        print("error: field 'agent' is required for runner=kilo", file=sys.stderr)
        return 2
    if data["runner"] == "junie" and "agent" in data:
        print("error: field 'agent' is only allowed for runner=kilo", file=sys.stderr)
        return 2

    duration = int(data["finished"]) - int(data["started"])  # type: ignore[arg-type]
    if duration < 0:
        print("error: finished is before started", file=sys.stderr)
        return 2
    if int(data["files_changed"]) < 0:  # type: ignore[arg-type]
        print("error: files_changed must be >= 0", file=sys.stderr)
        return 2
    data["duration_s"] = duration

    with open(outfile, "w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True, indent=2)
        fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
