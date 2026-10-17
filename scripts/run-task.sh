#!/usr/bin/env bash
# One entry point for the orchestrator to delegate one task to a coder runner.
# Codex is the default; Junie and Kilo remain explicit opt-ins. There is no fallback by default.
#
#   scripts/run-task.sh <name> --file tmp/tasks/<name>.md [--task-id <id>] [--runner junie|kilo|codex] [--fallback kilo|codex|none]
#
# Default runner codex, default fallback none. Run it in the background (Bash run_in_background): it prints the
# runner's report and exits with the runner's code (0 verified, 1 verification failed,
# 3 agent failed or stalled on every attempt). Every attempt is appended to
# tmp/runs/<name>.attempts (runner, exit code, seconds). A verification failure (exit 1)
# is NOT retried on another runner: it needs a precise fix list from the planner.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

name="${1:?usage: run-task.sh <name> --file <spec> [--task-id <id>] [--runner kilo|junie|codex] [--fallback none]}"
shift
spec="" task_id="" runner="codex" fallback="none"
while [ $# -gt 0 ]; do
  case "$1" in
    --file)     spec="${2:?--file needs a path}"; shift 2 ;;
    --task-id)  task_id="${2:?--task-id needs a value}"; shift 2 ;;
    --runner)   runner="${2:?--runner needs a value}"; shift 2 ;;
    --fallback) fallback="${2:?--fallback needs a value}"; shift 2 ;;
    *) echo "unknown option '$1'" >&2; exit 1 ;;
  esac
done
[ -n "$spec" ] && [ -f "$spec" ] || { echo "spec file missing: '$spec'" >&2; exit 1; }
bash scripts/spec-lint.sh "$spec" || { echo "run-task: spec failed lint" >&2; exit 1; }
case "$runner" in junie|kilo|codex) ;; *) echo "unknown runner '$runner'" >&2; exit 1 ;; esac
case "$fallback" in junie|kilo|codex|none) ;; *) echo "unknown fallback '$fallback'" >&2; exit 1 ;; esac
[ "$fallback" != "$runner" ] || fallback="none"
mkdir -p tmp/runs

# Record the spec's "Files:" header as scope links, used by `tasks.py plan-batch`.
if [ -n "$task_id" ]; then
  python3 - "$spec" > "tmp/runs/$name.scopes" <<'PY'
import sys
lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
in_block, out = False, []
for line in lines:
    text = line.strip()
    if text[:6].lower() == "files:":
        in_block = True
        rest = text[6:].strip()
        if rest:
            out += [x.strip() for x in rest.split(",")]
        continue
    if in_block:
        if not text or (text[0].isalpha() and text.endswith(":")):
            in_block = False
        else:
            out += [x.strip() for x in text.lstrip("-").split(",")]
for value in out:
    value = value.strip()
    if value:
        print(value)
PY
  while IFS= read -r scope; do
    [ -n "$scope" ] && python3 scripts/tasks.py link "$task_id" scope "$scope" >/dev/null 2>&1 || true
  done < "tmp/runs/$name.scopes"
fi

attempt() {  # attempt <runner>: run one runner, record it, leave its exit code in $rc
  local r="$1" began=$SECONDS
  rc=0
  bash "scripts/$r-task.sh" "$name" --file "$spec" ${task_id:+--task-id "$task_id"} || rc=$?
  printf '%s runner=%s exit=%s seconds=%s\n' "$(date +%H:%M:%S)" "$r" "$rc" "$((SECONDS - began))" >>"tmp/runs/$name.attempts"
}

rc=0
attempt "$runner"
if [ "$rc" -eq 3 ] && [ "$fallback" != "none" ]; then
  echo "run-task: $runner failed or stalled (exit 3); retrying once with $fallback in the same worktree" >&2
  bash scripts/task-report.sh note "$task_id" "run-task: $runner failed or stalled; falling back to $fallback" || true
  attempt "$fallback"
fi
exit "$rc"
