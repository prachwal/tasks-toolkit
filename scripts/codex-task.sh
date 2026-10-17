#!/usr/bin/env bash
# Delegates one task to the Codex CLI agent inside its own worktree, then verifies it.
# Same contract as junie-task.sh / kilo-task.sh (worktree, heartbeat, verification, patch).
#
#   scripts/codex-task.sh <name> "<task text>" [--task-id <id>]
#   scripts/codex-task.sh <name> --file tmp/tasks/<name>.md [--task-id <id>]
#
# --task-id reports progress to the task tracker (scripts/task-report.sh): the task is
# marked in progress at the start, and after the commit and verification it is closed
# automatically (done on success, blocked on failure); failures only warn.
#
# Env: CODEX_MODEL (default: Codex's own default), CODEX_SANDBOX (default workspace-write),
#      CODEX_TIMEOUT (default 1200 s), CODEX_PING (default 30 s),
#      CODEX_VERIFY_CMD (tests only: replaces the dotnet verification).
# Output: tmp/runs/<name>.{status,events.jsonl,result.txt,verify.log,summary.json},
#         tmp/patches/<name>.patch. Every status line ends with "model=<model> runner=codex".
# Exit code: 0 verified, 1 verification failed, 3 Codex itself failed.
#
# Notes:
# - Codex runs in a workspace-write sandbox with the network off by default; `dotnet` writes to
#   ~/.nuget and ~/.dotnet, so those are added to the sandbox (--add-dir) or restore/build fails.
# - The worktree is disposable; review the patch. Never bypass the sandbox or approval flags.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

runner="codex"
name="${1:?usage: codex-task.sh <name> \"<task>\" | --file <path> [--task-id <id>]}"
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

model="${CODEX_MODEL:-codex}"
sandbox="${CODEX_SANDBOX:-workspace-write}"
limit="${CODEX_TIMEOUT:-1200}"
ping_every="${CODEX_PING:-30}"

if [ -d "tmp/worktrees/$name" ]; then
  wt="$root/tmp/worktrees/$name"
else
  wt="$(bash scripts/worktree.sh new "$name")"
fi
mkdir -p tmp/runs

prompt="$task

Rules for this run:
- Work only inside this project directory (a disposable git worktree). Read AGENTS.md first and follow it.
- Do not commit, push or touch git. Do not add NuGet packages.
- You are done only when 'bash scripts/test.sh' is green AND
  'dotnet build VibeEmulator.slnx --no-incremental -warnaserror' reports 0 warnings and 0 errors
  (StyleCop and mandatory XML documentation are enforced on production code).
- Respect the sandbox; do not bypass approval or network flags.
- Finish with a short summary of the files you created or changed."

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

# last_activity: the last non-empty output line of the agent.
last_activity() {
  grep -v '^[[:space:]]*$' "$events" 2>/dev/null | tail -n 1 | cut -c1-110 || true
}

bash scripts/task-report.sh start "$task_id"
report "starting" "worktree=$wt${task_id:+ task=$task_id}"

codex_args=(exec -C "$wt" --skip-git-repo-check --sandbox "$sandbox" --json)
[ -n "${CODEX_MODEL:-}" ] && codex_args+=(-m "$model")
[ -d "$HOME/.nuget" ] && codex_args+=(--add-dir "$HOME/.nuget")
[ -d "$HOME/.dotnet" ] && codex_args+=(--add-dir "$HOME/.dotnet")

timeout "$limit" codex "${codex_args[@]}" "$prompt" \
  >"$events" 2>"$root/tmp/runs/$name.stderr.log" &
codex_pid=$!

stalled=0
while kill -0 "$codex_pid" 2>/dev/null; do
  sleep "$ping_every" & wait $! || true
  kill -0 "$codex_pid" 2>/dev/null || break
  changed="$(git -C "$wt" status --porcelain | wc -l)"
  if [ "$changed" -eq 0 ] && [ $((SECONDS - started)) -ge "${RUNNER_STALL:-420}" ]; then
    report "stalled" "no file changed after ${RUNNER_STALL:-420}s; killing the agent"
    kill "$codex_pid" 2>/dev/null || true
    stalled=1
    break
  fi
  last="$(last_activity)"
  report "codex running" "$changed file(s) changed; last: ${last:-<no output yet>}"
done
set +e
wait "$codex_pid"
codex_rc=$?
set -e
report "codex finished" "exit $codex_rc, verifying"

# With --json the last assistant text is the final message; fall back to the last output line.
python3 - "$events" >"$root/tmp/runs/$name.result.txt" <<'PY'
import json, sys
out = []
for line in open(sys.argv[1], errors="replace"):
    line = line.strip()
    if not line:
        continue
    try:
        event = json.loads(line)
    except ValueError:
        out.append(line)
        continue
    text = event.get("text") or (event.get("message") or {}).get("content")
    if isinstance(text, str):
        out.append(text)
print(out[-1] if out else "")
PY

# Commit the agent's work on task/<name> (path guard first); agents never commit themselves.
commit_rc=0
if [ "$codex_rc" -eq 0 ]; then
  title="$(printf '%s\n' "$task" | grep -m1 -v '^[[:space:]]*$' | cut -c1-72)"
  bash scripts/worktree.sh commit "$name" --runner "$runner" --model "$model" \
    ${task_id:+--task-id "$task_id"} --title "${title:-$name}" >"tmp/runs/$name.commit.log" 2>&1 || commit_rc=$?
fi
patch="$(bash scripts/worktree.sh patch "$name")"

verify_rc=0
{
  if [ -n "${CODEX_VERIFY_CMD:-}" ]; then
    echo "== verify (CODEX_VERIFY_CMD override) =="
    (cd "$wt" && bash -c "$CODEX_VERIFY_CMD") || verify_rc=1
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
if [ "$codex_rc" -ne 0 ]; then
  verdict="agent_failed"
elif [ "$verify_rc" -ne 0 ]; then
  verdict="verify_failed"
else
  verdict="verified"
fi
if ! python3 scripts/run_summary.py "$root/tmp/runs/$name.summary.json" name="$name" runner="$runner" model="$model" \
    task_id="$task_id" started="$started_epoch" finished="$finished" \
    agent_exit="$codex_rc" verify_exit="$verify_rc" verdict="$verdict" \
    patch="$patch" files_changed="$files_changed"; then
  echo "warning: could not write run summary (tmp/runs/$name.summary.json)" >&2
fi

report "done" "codex exit $codex_rc, verification $([ "$verify_rc" -eq 0 ] && echo PASSED || echo FAILED)"
echo "runner:       $runner"
echo "model:        $model"
echo "codex exit:   $codex_rc"
echo "verification: $([ "$verify_rc" -eq 0 ] && echo PASSED || echo FAILED) (tmp/runs/$name.verify.log)"
echo "patch:        $patch"
git -C "$wt" diff --cached --stat "$base" | tail -n 15
if [ "$codex_rc" -ne 0 ]; then
  bash scripts/task-report.sh note "$task_id" "codex '$name' failed (exit 3) model=$model; see tmp/runs/$name.verify.log"
  bash scripts/task-report.sh close "$task_id" red "agent exit $codex_rc; see tmp/runs/$name.verify.log"
  exit 3
elif [ "$verify_rc" -ne 0 ]; then
  bash scripts/task-report.sh note "$task_id" "codex '$name' failed (exit 1) model=$model; see tmp/runs/$name.verify.log"
  bash scripts/task-report.sh close "$task_id" red "verification failed; see tmp/runs/$name.verify.log"
  exit 1
else
  bash scripts/task-report.sh note "$task_id" "codex '$name' verified model=$model; patch tmp/patches/$name.patch"
  bash scripts/task-report.sh close "$task_id" green
fi
