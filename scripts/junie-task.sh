#!/usr/bin/env bash
# Delegates one task to the cheap Junie CLI agent inside its own worktree, then verifies it.
#
#   scripts/junie-task.sh <name> "<task text>" [--task-id <id>]
#   scripts/junie-task.sh <name> --file tmp/task.md [--task-id <id>]
#
# --task-id reports progress to the task tracker (scripts/task-report.sh): the task is
# marked in progress at the start, and after the commit and verification it is closed
# automatically (done on success, blocked on failure); failures only warn.
#
# Env: RUNNER_STALL (default 420 s: kill an agent that changed no file by then), JUNIE_MODEL (default junie-lite), JUNIE_TIMEOUT (default 1200 s), JUNIE_PING
#      (default 30 s), JUNIE_VERIFY_CMD (tests only: replaces the dotnet verification).
#
# Output (stdout): a short report. Artifacts: tmp/runs/<name>.json (Junie result),
# tmp/runs/<name>.verify.log (build + tests), tmp/runs/<name>.summary.json (machine
# readable run summary) and tmp/patches/<name>.patch.
# Prints a heartbeat line every JUNIE_PING seconds (default 30) and mirrors it to
# tmp/runs/<name>.status; every line ends with "model=<model> runner=junie" so it can
# be parsed. Check it any time with scripts/junie-status.sh <name>.
# Exit code: 0 verified, 1 verification failed, 3 Junie itself failed.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

runner="junie"
name="${1:?usage: junie-task.sh <name> \"<task>\" | --file <path> [--task-id <id>]}"
shift
if [ "${1:-}" = "--file" ]; then
  task="$(cat "${2:?missing task file}")"
  shift 2
else
  task="${1:?missing task text}"
  shift
fi
task_id=""
if [ "${1:-}" = "--task-id" ]; then
  task_id="${2:?--task-id needs a value}"
fi

model="${JUNIE_MODEL:-junie-lite}"
limit="${JUNIE_TIMEOUT:-1200}"

if [ -d "tmp/worktrees/$name" ]; then
  wt="$root/tmp/worktrees/$name"
else
  wt="$(bash scripts/worktree.sh new "$name" ${task_id:+--task-id "$task_id"})"
fi
mkdir -p tmp/runs

run_file="$root/agents/$runner/RUN.md"
[ -f "$run_file" ] || { echo "error: missing $run_file" >&2; exit 1; }
prompt="$task

$(cat "$run_file")"

status_file="$root/tmp/runs/$name.status"
log="$root/tmp/runs/$name.stdout.log"
ping_every="${JUNIE_PING:-30}"
base="$(cat "tmp/meta/$name.base")"
started=$SECONDS
started_epoch="$(date +%s)"

report() {  # report <state> [detail]: one line to stdout and the status file
  local line
  line="$(printf '[%02d:%02d] %s' $(((SECONDS - started) / 60)) $(((SECONDS - started) % 60)) "$1${2:+ - $2}")"
  echo "$line model=$model runner=$runner"
  echo "$line model=$model runner=$runner" >"$status_file"
}

bash scripts/task-report.sh start "$task_id"
report "starting" "worktree=$wt${task_id:+ task=$task_id}"
GIT_CONFIG_COUNT=1 \
GIT_CONFIG_KEY_0=core.hooksPath \
GIT_CONFIG_VALUE_0="$root/scripts/coder-git-hooks" \
  timeout "$limit" junie --skip-update-check --model="$model" --output-format=json \
  --json-output-file="$root/tmp/runs/$name.json" --project="$wt" "$prompt" >"$log" 2>&1 &
junie_pid=$!

# Heartbeat: while Junie works, report elapsed time, changed files and its latest output line.
stalled=0
while kill -0 "$junie_pid" 2>/dev/null; do
  sleep "$ping_every" & wait $! || true
  kill -0 "$junie_pid" 2>/dev/null || break
  changed="$(git -C "$wt" status --porcelain | wc -l)"
  # Stall detector: an agent that has changed nothing after RUNNER_STALL seconds is stuck
  # (seen with Kilo: 8+ minutes, 0 files). Kill it so the planner can switch runner early.
  if [ "$changed" -eq 0 ] && [ $((SECONDS - started)) -ge "${RUNNER_STALL:-420}" ]; then
    report "stalled" "no file changed after ${RUNNER_STALL:-420}s; killing the agent"
    kill "$junie_pid" 2>/dev/null || true
    stalled=1
    break
  fi
  last="$(tail -n 5 "$log" | sed -E 's/\x1b\[[0-9;]*m//g' | grep -v '^\s*$' | tail -n 1 | cut -c1-110 || true)"
  report "junie running" "$changed file(s) changed; last: ${last:-<no output yet>}"
done
set +e
wait "$junie_pid"
junie_rc=$?
set -e
report "junie finished" "exit $junie_rc, verifying"

# Commit the agent's work on task/<name> (path guard first); agents never commit themselves.
commit_rc=0
if [ "$junie_rc" -eq 0 ]; then
  task_head="$(git -C "$wt" rev-parse HEAD)"
  if [ "$task_head" != "$base" ]; then
    commit_rc=1
    printf 'Junie changed task HEAD itself; only the parent runner may commit.\n' >"tmp/runs/$name.commit.log"
  elif ! git -C "$wt" add -A; then
    commit_rc=1
    printf 'Could not stage the Junie task diff for guarded commit.\n' >"tmp/runs/$name.commit.log"
  else
    changed_lines="$(git -C "$wt" diff --cached --numstat "$base" \
      | awk '$1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ { total += $1 + $2 } END { print total + 0 }')"
    if [ "$changed_lines" -gt 300 ]; then
      commit_rc=1
      printf 'Junie task diff has %s added/deleted lines (limit 300); split the task before committing.\n' \
        "$changed_lines" >"tmp/runs/$name.commit.log"
    else
      title="$(printf '%s\n' "$task" | grep -m1 -v '^[[:space:]]*$' | cut -c1-72)"
      bash scripts/worktree.sh commit "$name" --runner "$runner" --model "$model" \
        ${task_id:+--task-id "$task_id"} --title "${title:-$name}" >"tmp/runs/$name.commit.log" 2>&1 || commit_rc=$?
    fi
  fi
fi
patch="$(bash scripts/worktree.sh patch "$name")"

verify_rc=0
{
  if [ -n "${JUNIE_VERIFY_CMD:-}" ]; then
    echo "== verify (JUNIE_VERIFY_CMD override) =="
    (cd "$wt" && bash -c "$JUNIE_VERIFY_CMD") || verify_rc=1
  elif ! bash "$root/scripts/needs-dotnet-checks.sh" "$wt" "$base" HEAD; then
    echo "== conditional verification (no .NET files changed): script tests =="
    (cd "$wt" && bash scripts/test_worktree.sh && bash scripts/test_runners.sh \
       && python3 -m unittest discover -s scripts -p "test_*.py") || verify_rc=1
  else
    echo "== format =="; (cd "$wt" && dotnet format VibeEmulator.slnx --verify-no-changes) || verify_rc=1
    echo "== build ==";  (cd "$wt" && dotnet build VibeEmulator.slnx --no-incremental -warnaserror) || verify_rc=1
    echo "== test ==";   (cd "$wt" && dotnet test VibeEmulator.slnx --no-build) || verify_rc=1
  fi
} >"tmp/runs/$name.verify.log" 2>&1

if [ "$commit_rc" -ne 0 ]; then
  verify_rc=1
  echo "== commit failed (exit $commit_rc): see tmp/runs/$name.commit.log ==" >>"tmp/runs/$name.verify.log"
fi
finished="$(date +%s)"
files_changed="$(git -C "$wt" diff --cached --name-only "$base" | wc -l)"
if [ "$junie_rc" -ne 0 ]; then
  verdict="agent_failed"
elif [ "$verify_rc" -ne 0 ]; then
  verdict="verify_failed"
else
  verdict="verified"
fi
if ! python3 scripts/run_summary.py "$root/tmp/runs/$name.summary.json" name="$name" runner="$runner" model="$model" \
    task_id="$task_id" started="$started_epoch" finished="$finished" \
    agent_exit="$junie_rc" verify_exit="$verify_rc" verdict="$verdict" \
    patch="$patch" files_changed="$files_changed"; then
  echo "warning: could not write run summary (tmp/runs/$name.summary.json)" >&2
fi

report "done" "junie exit $junie_rc, verification $([ "$verify_rc" -eq 0 ] && echo PASSED || echo FAILED)"
echo "runner:       $runner"
echo "model:        $model"
echo "junie exit:   $junie_rc"
echo "verification: $([ "$verify_rc" -eq 0 ] && echo PASSED || echo FAILED) (tmp/runs/$name.verify.log)"
echo "patch:        $patch"
git -C "$wt" diff --cached --stat "$base" | tail -n 15
if [ "$junie_rc" -ne 0 ]; then
  bash scripts/task-report.sh note "$task_id" "junie '$name' failed (exit 3) model=$model; see tmp/runs/$name.verify.log"
  bash scripts/task-report.sh close "$task_id" red "agent exit $junie_rc; see tmp/runs/$name.verify.log"
  exit 3
elif [ "$verify_rc" -ne 0 ]; then
  bash scripts/task-report.sh note "$task_id" "junie '$name' failed (exit 1) model=$model; see tmp/runs/$name.verify.log"
  bash scripts/task-report.sh close "$task_id" red "verification failed; see tmp/runs/$name.verify.log"
  exit 1
else
  bash scripts/task-report.sh note "$task_id" "junie '$name' verified model=$model; patch tmp/patches/$name.patch"
  bash scripts/task-report.sh close "$task_id" green
fi
