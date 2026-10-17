#!/usr/bin/env bash
# Delegates one task to the Kilo CLI agent inside its own worktree, then verifies it.
# Same contract as junie-task.sh (worktree, heartbeat, verification, patch), different runner.
#
#   scripts/kilo-task.sh <name> "<task text>" [--task-id <id>]
#   scripts/kilo-task.sh <name> --file tmp/tasks/<name>.md [--task-id <id>]
#
# --task-id reports progress to the task tracker (scripts/task-report.sh): the task is
# marked in progress at the start, and after the commit and verification it is closed
# automatically (done on success, blocked on failure); failures only warn.
#
# Env: KILO_MODEL (default kilo/deepseek/deepseek-v4-flash), KILO_AGENT (default code),
#      KILO_TIMEOUT (default 1200 s), KILO_PING (default 30 s),
#      KILO_VERIFY_CMD (tests only: replaces the dotnet verification).
# Output: tmp/runs/<name>.{status,events.jsonl,result.txt,verify.log,summary.json},
#         tmp/patches/<name>.patch. Every status line ends with
#         "model=<model> runner=kilo" so it can be parsed.
# Exit code: 0 verified, 1 verification failed, 3 Kilo itself failed.
#
# Notes:
# - ~/.config/kilo/kilo.jsonc is written by the VS Code extension in the V2 permission format,
#   which the CLI rejects; the run therefore uses an empty XDG_CONFIG_HOME (credentials live in
#   XDG_DATA_HOME and are unaffected). The project's kilo.json still applies in the worktree.
# - --auto approves every permission that kilo.json does not explicitly deny (git push, hard reset,
#   git clean, amend and rm -rf are denied there). The worktree is disposable; review the patch.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

runner="kilo"
name="${1:?usage: kilo-task.sh <name> \"<task>\" | --file <path> [--task-id <id>]}"
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

model="${KILO_MODEL:-configured-default}"
agent="${KILO_AGENT:-code}"
limit="${KILO_TIMEOUT:-1200}"
ping_every="${KILO_PING:-30}"
model_args=()
if [ -n "${KILO_MODEL:-}" ]; then
  model_args=(-m "$KILO_MODEL")
fi

if [ -d "tmp/worktrees/$name" ]; then
  wt="$root/tmp/worktrees/$name"
else
  wt="$(bash scripts/worktree.sh new "$name" ${task_id:+--task-id "$task_id"})"
fi
mkdir -p tmp/runs tmp/xdg-empty

run_file="$root/agents/$runner/RUN.md"
[ -f "$run_file" ] || { echo "error: missing $run_file" >&2; exit 1; }
prompt="$task

$(cat "$run_file")"

status_file="$root/tmp/runs/$name.status"
events="$root/tmp/runs/$name.events.jsonl"
base="$(cat "tmp/meta/$name.base")"
started=$SECONDS
started_epoch="$(date +%s)"

report() {  # report <state> [detail]: one line to stdout and the status file
  local line
  line="$(printf '[%02d:%02d] %s' $(((SECONDS - started) / 60)) $(((SECONDS - started) % 60)) "$1${2:+ - $2}")"
  echo "$line model=$model runner=$runner"
  echo "$line model=$model runner=$runner" >"$status_file"
}

# last_activity: the latest tool call or text of the agent, from the JSON event stream.
last_activity() {
  python3 - "$events" <<'PY'
import json, sys
last = ""
try:
    for line in open(sys.argv[1], errors="replace"):
        try:
            part = json.loads(line).get("part", {})
        except ValueError:
            continue
        if part.get("type") == "tool":
            last = "tool " + str(part.get("tool"))
        elif part.get("type") == "text" and part.get("text"):
            last = "text: " + " ".join(part["text"].split())
except OSError:
    pass
print(last[:110])
PY
}

bash scripts/task-report.sh start "$task_id"
report "starting" "agent=$agent worktree=$wt${task_id:+ task=$task_id}"
GIT_CONFIG_COUNT=1 \
GIT_CONFIG_KEY_0=core.hooksPath \
GIT_CONFIG_VALUE_0="$root/scripts/coder-git-hooks" \
XDG_CONFIG_HOME="$root/tmp/xdg-empty" timeout "$limit" kilo run --dir "$wt" "${model_args[@]}" --agent "$agent" \
  --format json --auto "$prompt" >"$events" 2>"$root/tmp/runs/$name.stderr.log" &
kilo_pid=$!

stalled=0
while kill -0 "$kilo_pid" 2>/dev/null; do
  sleep "$ping_every" & wait $! || true
  kill -0 "$kilo_pid" 2>/dev/null || break
  changed="$(git -C "$wt" status --porcelain | wc -l)"
  # Stall detector: an agent that has changed nothing after RUNNER_STALL seconds is stuck
  # (seen with Kilo: 8+ minutes, 0 files). Kill it so the planner can switch runner early.
  if [ "$changed" -eq 0 ] && [ $((SECONDS - started)) -ge "${RUNNER_STALL:-420}" ]; then
    report "stalled" "no file changed after ${RUNNER_STALL:-420}s; killing the agent"
    kill "$kilo_pid" 2>/dev/null || true
    stalled=1
    break
  fi
  last="$(last_activity)"
  report "kilo running" "$changed file(s) changed; last: ${last:-<no output yet>}"
done
set +e
wait "$kilo_pid"
kilo_rc=$?
set -e
report "kilo finished" "exit $kilo_rc, verifying"

python3 - "$events" >"$root/tmp/runs/$name.result.txt" <<'PY'
import json, sys
out = []
for line in open(sys.argv[1], errors="replace"):
    try:
        part = json.loads(line).get("part", {})
    except ValueError:
        continue
    if part.get("type") == "text" and part.get("text"):
        out.append(part["text"])
print(out[-1] if out else "")
PY

# Commit the agent's work on task/<name> (path guard first); agents never commit themselves.
commit_rc=0
if [ "$kilo_rc" -eq 0 ]; then
  task_head="$(git -C "$wt" rev-parse HEAD)"
  if [ "$task_head" != "$base" ]; then
    commit_rc=1
    printf 'Kilo changed task HEAD itself; only the parent runner may commit.\n' >"tmp/runs/$name.commit.log"
  elif ! git -C "$wt" add -A; then
    commit_rc=1
    printf 'Could not stage the Kilo task diff for guarded commit.\n' >"tmp/runs/$name.commit.log"
  else
    changed_lines="$(git -C "$wt" diff --cached --numstat "$base" \
      | awk '$1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ { total += $1 + $2 } END { print total + 0 }')"
    if [ "$changed_lines" -gt 300 ]; then
      commit_rc=1
      printf 'Kilo task diff has %s added/deleted lines (limit 300); split the task before committing.\n' \
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
  if [ -n "${KILO_VERIFY_CMD:-}" ]; then
    echo "== verify (KILO_VERIFY_CMD override) =="
    (cd "$wt" && bash -c "$KILO_VERIFY_CMD") || verify_rc=1
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
if [ "$kilo_rc" -ne 0 ]; then
  verdict="agent_failed"
elif [ "$verify_rc" -ne 0 ]; then
  verdict="verify_failed"
else
  verdict="verified"
fi
if ! python3 scripts/run_summary.py "$root/tmp/runs/$name.summary.json" name="$name" runner="$runner" model="$model" \
    agent="$agent" task_id="$task_id" started="$started_epoch" finished="$finished" \
    agent_exit="$kilo_rc" verify_exit="$verify_rc" verdict="$verdict" \
    patch="$patch" files_changed="$files_changed"; then
  echo "warning: could not write run summary (tmp/runs/$name.summary.json)" >&2
fi

report "done" "kilo exit $kilo_rc, verification $([ "$verify_rc" -eq 0 ] && echo PASSED || echo FAILED)"
echo "runner:       $runner"
echo "model:        $model"
echo "kilo exit:    $kilo_rc"
echo "verification: $([ "$verify_rc" -eq 0 ] && echo PASSED || echo FAILED) (tmp/runs/$name.verify.log)"
echo "patch:        $patch"
git -C "$wt" diff --cached --stat "$base" | tail -n 15
if [ "$kilo_rc" -ne 0 ]; then
  bash scripts/task-report.sh note "$task_id" "kilo '$name' failed (exit 3) model=$model; see tmp/runs/$name.verify.log"
  bash scripts/task-report.sh close "$task_id" red "agent exit $kilo_rc; see tmp/runs/$name.verify.log"
  exit 3
elif [ "$verify_rc" -ne 0 ]; then
  bash scripts/task-report.sh note "$task_id" "kilo '$name' failed (exit 1) model=$model; see tmp/runs/$name.verify.log"
  bash scripts/task-report.sh close "$task_id" red "verification failed; see tmp/runs/$name.verify.log"
  exit 1
else
  bash scripts/task-report.sh note "$task_id" "kilo '$name' verified model=$model; patch tmp/patches/$name.patch"
  bash scripts/task-report.sh close "$task_id" green
fi
