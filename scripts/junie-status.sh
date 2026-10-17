#!/usr/bin/env bash
# Shows the progress of a delegated Junie or Kilo run: scripts/junie-status.sh <name>
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
name="${1:?usage: junie-status.sh <name>}"

[ -f "tmp/runs/$name.stdout.log" ] || [ -f "tmp/runs/$name.events.jsonl" ] || { echo "no run named '$name'" >&2; exit 1; }
echo "status:  $(cat "tmp/runs/$name.status" 2>/dev/null || echo "(no heartbeat recorded)")"
# runner/model come from the run summary once the run has finished, and from the
# last status line while it is still running; runs that predate model recording
# simply have neither and print nothing here.
runner=""
model=""
if [ -f "tmp/runs/$name.summary.json" ]; then
  runner="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("runner", ""))' "tmp/runs/$name.summary.json" 2>/dev/null || true)"
  model="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("model", ""))' "tmp/runs/$name.summary.json" 2>/dev/null || true)"
else
  last="$(cat "tmp/runs/$name.status" 2>/dev/null || true)"
  runner="$(printf '%s\n' "$last" | sed -n 's/.*runner=\([^ ]*\).*/\1/p')"
  model="$(printf '%s\n' "$last" | sed -n 's/.*model=\([^ ]*\).*/\1/p')"
fi
[ -z "$runner" ] || echo "runner:   $runner"
[ -z "$model" ] || echo "model:    $model"
if pgrep -f "(junie|kilo) .*$root/tmp/worktrees/$name" >/dev/null; then
  echo "process: running"
else
  echo "process: not running"
fi
if [ -d "tmp/worktrees/$name" ]; then
  echo "changed files:"
  git -C "tmp/worktrees/$name" status --porcelain | sed 's/^/  /'
fi
if [ -f "tmp/runs/$name.events.jsonl" ]; then
  echo "recent activity (kilo):"
  python3 - "tmp/runs/$name.events.jsonl" <<'PY' | tail -n 8
import json, sys
for line in open(sys.argv[1], errors="replace"):
    try:
        part = json.loads(line).get("part", {})
    except ValueError:
        continue
    if part.get("type") == "tool":
        print("  tool " + str(part.get("tool")))
    elif part.get("type") == "text" and part.get("text"):
        print("  " + " ".join(part["text"].split())[:130])
PY
fi
echo "recent output:"
tail -n 8 "tmp/runs/$name.stdout.log" 2>/dev/null | sed -E 's/\x1b\[[0-9;]*m//g' | cut -c1-140 | sed 's/^/  /'
[ ! -f "tmp/runs/$name.verify.log" ] || { echo "verify log tail:"; tail -n 5 "tmp/runs/$name.verify.log" | sed 's/^/  /'; }
