#!/usr/bin/env python3
"""Delivery-speed statistics for the delegation runners.

Reads tmp/runs/<name>.summary.json when present (exact) and falls back to the last line of
tmp/runs/<name>.status for older runs ("[mm:ss] done - junie exit 0, verification PASSED").
A run whose status never reached "done" counts as unfinished (killed, stalled or crashed).
Prints, per runner: runs, verified, failed, unfinished, median and p90 duration, plus the
share of runs that did not end verified ("rework rate"). Standard library only, read-only.

Usage: scripts/run_stats.py [--runs-dir DIR] [--json] [--since YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

LAST_STAMP = re.compile(r"^\[(\d+):(\d{2})\]\s+(.*)$")
RUNNER_TAIL = re.compile(r"\brunner=(\w+)")
DONE_RUNNER = re.compile(r"^done - (junie|kilo|codex|haiku) exit (\d+), verification (PASSED|FAILED)")


def parse_status(path: Path) -> dict | None:
    """Best-effort record for a run that has only a .status file."""
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    except OSError:
        return None
    if not lines:
        return None
    match = LAST_STAMP.match(lines[-1])
    if not match:
        return None
    seconds = int(match.group(1)) * 60 + int(match.group(2))
    body = RUNNER_TAIL.sub("", match.group(3)).strip()
    model_match = re.search(r"\bmodel=(\S+)", match.group(3))
    runner_match = RUNNER_TAIL.search(match.group(3))
    done = DONE_RUNNER.match(body.split(" model=")[0].strip())
    record = {"name": path.stem, "duration_s": seconds, "source": "status",
              "runner": runner_match.group(1) if runner_match else None,
              "model": model_match.group(1) if model_match else None}
    if done:
        record["runner"] = record["runner"] or done.group(1)
        agent_exit, verify = int(done.group(2)), done.group(3)
        record["verdict"] = ("agent_failed" if agent_exit else
                             "verified" if verify == "PASSED" else "verify_failed")
    else:
        record["verdict"] = "unfinished"
    return record


def load_runs(runs_dir: Path) -> list[dict]:
    """All runs found in runs_dir; a summary.json wins over the .status of the same name."""
    runs: dict[str, dict] = {}
    for status in sorted(runs_dir.glob("*.status")):
        record = parse_status(status)
        if record:
            record["mtime"] = status.stat().st_mtime
            runs[record["name"]] = record
    for summary in sorted(runs_dir.glob("*.summary.json")):
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = data.get("name") or summary.name.removesuffix(".summary.json")
        record = {"name": name, "duration_s": int(data.get("duration_s", 0)), "source": "summary",
                  "runner": data.get("runner"), "model": data.get("model"),
                  "verdict": data.get("verdict", "unfinished"), "mtime": summary.stat().st_mtime}
        runs[name] = record
    return list(runs.values())


def percentile(values: list[int], pct: float) -> int:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(pct * (len(ordered) - 1))))
    return ordered[index]


def summarize(runs: list[dict]) -> dict:
    """Per-runner aggregates plus an overall block."""
    groups: dict[str, list[dict]] = {}
    for run in runs:
        groups.setdefault(run.get("runner") or "unknown", []).append(run)
    groups["ALL"] = list(runs)
    result = {}
    for runner, items in sorted(groups.items()):
        finished = [r for r in items if r["verdict"] != "unfinished"]
        durations = [r["duration_s"] for r in finished]
        verified = sum(1 for r in items if r["verdict"] == "verified")
        result[runner] = {
            "runs": len(items),
            "verified": verified,
            "verify_failed": sum(1 for r in items if r["verdict"] == "verify_failed"),
            "agent_failed": sum(1 for r in items if r["verdict"] == "agent_failed"),
            "unfinished": sum(1 for r in items if r["verdict"] == "unfinished"),
            "median_s": int(statistics.median(durations)) if durations else None,
            "p90_s": percentile(durations, 0.9) if durations else None,
            "rework_rate": round(1 - verified / len(items), 2) if items else None,
        }
    return result


def fmt(seconds: int | None) -> str:
    return "-" if seconds is None else f"{seconds // 60}:{seconds % 60:02d}"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs-dir", default=str(Path(__file__).resolve().parent.parent / "tmp" / "runs"))
    parser.add_argument("--json", action="store_true", help="print the aggregates as JSON")
    parser.add_argument("--since", help="only runs whose status/summary file is newer than YYYY-MM-DD")
    args = parser.parse_args(argv)
    runs_dir = Path(args.runs_dir)
    if not runs_dir.is_dir():
        print(f"no runs directory: {runs_dir}", file=sys.stderr)
        return 1
    runs = load_runs(runs_dir)
    if args.since:
        cutoff = datetime.strptime(args.since, "%Y-%m-%d").timestamp()
        runs = [r for r in runs if r["mtime"] >= cutoff]
    summary = summarize(runs)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    print(f"{'runner':<9}{'runs':>5}{'ok':>5}{'vfail':>6}{'afail':>6}{'unfin':>6}{'median':>8}{'p90':>8}{'rework':>8}")
    for runner, s in summary.items():
        rework = "-" if s["rework_rate"] is None else f"{int(s['rework_rate'] * 100)}%"
        print(f"{runner:<9}{s['runs']:>5}{s['verified']:>5}{s['verify_failed']:>6}{s['agent_failed']:>6}"
              f"{s['unfinished']:>6}{fmt(s['median_s']):>8}{fmt(s['p90_s']):>8}{rework:>8}")
    slow = sorted((r for r in runs if r["verdict"] != "unfinished"), key=lambda r: -r["duration_s"])[:3]
    if slow:
        print("slowest: " + ", ".join(f"{r['name']} {fmt(r['duration_s'])} ({r.get('runner') or '?'})" for r in slow))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
