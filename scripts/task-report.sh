#!/usr/bin/env bash
# Reports delegation progress to the task tracker (see AGENTS.md "Delegation flow").
# Used by junie-task.sh and kilo-task.sh through their --task-id flag.
#
#   scripts/task-report.sh start <id>            mark the task in_progress
#   scripts/task-report.sh note  <id> "<text>"   append a dated note, leaving the status unchanged
#   scripts/task-report.sh close <id> green|red [reason]
#                                                green -> done; red -> blocked with [reason]
#
# An empty <id> is a silent no-op. Any tracker failure only warns on stderr and never
# changes the caller's exit code, so reporting can never mask the runner's own result.
set -euo pipefail

action="${1:-}"
task_id="${2:-}"
text="${3:-}"

[ -n "$task_id" ] || exit 0

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tracker="$here/tasks.py"

warn() { printf 'task-report: %s\n' "$*" >&2; }

# run <args...>: call the tracker; on failure warn (never fail). Already-in-progress start
# and unknown ids are just failures here, which is exactly what the runner must tolerate.
run() {
  local output rc=0
  output="$(python3 "$tracker" "$@" 2>&1)" || rc=$?
  [ "$rc" -eq 0 ] && return 0
  warn "tasks.py $* failed (exit $rc): ${output:-no output}"
}

case "$action" in
  start) run start "$task_id" ;;
  note)  run note "$task_id" "$text" ;;
  close)
    case "${3:-}" in
      green) run done "$task_id" ;;
      red)   run block "$task_id" --reason "${4:-run failed}" ;;
      *)     warn "close needs a verdict: green|red" ;;
    esac
    ;;
  *)     warn "unknown action '${action}' (use: start|note|close)" ;;
esac
