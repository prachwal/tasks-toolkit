#!/usr/bin/env bash
# Asks the Kilo planner agent (OpenAI Sol via the Kilo Gateway) to PROPOSE a sprint, then validates it.
# Nothing is written to the tracker: review tmp/plans/<name>.json, then run scripts/apply-plan.sh <name>.
# Design: knowledge/local-planner-design.md
#
#   scripts/plan-sprint.sh <name> --goal "<what the sprint should achieve>" [--goal-file F]
#                          [--model M] [--variant V] [--no-lint]
#
# Env: VIBE_PLANNER_MODEL (default kilo/openai/gpt-6.1-sol), VIBE_PLANNER_VARIANT (default high),
#      PLANNER_TIMEOUT (default 900 s), KILO_BIN (default kilo; tests put a stub here).
# Output: tmp/plans/<name>.{input.md,events.jsonl,raw.txt,json,stderr.log}, tmp/tasks/<spec_name>.md
# Exit: 0 valid plan | 1 usage | 3 Kilo failed or timed out | 4 plan invalid (specs are not written)
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

usage() { echo "usage: $0 <name> --goal \"<text>\" [--goal-file F] [--model M] [--variant V] [--no-lint]" >&2; exit 1; }

name="${1:-}"
[[ "$name" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "error: <name> must match ^[a-z0-9][a-z0-9-]*\$" >&2; usage; }
shift
goal="" model="${VIBE_PLANNER_MODEL:-kilo/openai/gpt-6.1-sol}" variant="${VIBE_PLANNER_VARIANT:-high}" lint_flag=""
while [ $# -gt 0 ]; do
  case "$1" in
    --goal)      goal="${2:?--goal needs text}"; shift 2 ;;
    --goal-file) [ -f "${2:?--goal-file needs a path}" ] || { echo "error: no such file '$2'" >&2; exit 1; }
                 goal="$(cat "$2")"; shift 2 ;;
    --model)     model="${2:?--model needs a value}"; shift 2 ;;
    --variant)   variant="${2:?--variant needs a value}"; shift 2 ;;
    --no-lint)   lint_flag="--no-lint"; shift ;;
    *) echo "unknown option '$1'" >&2; usage ;;
  esac
done
[ -n "$goal" ] || { echo "error: --goal is required" >&2; usage; }

kilo_bin="${KILO_BIN:-kilo}"
limit="${PLANNER_TIMEOUT:-900}"
mkdir -p tmp/plans tmp/tasks tmp/xdg-empty
input="tmp/plans/$name.input.md"
events="tmp/plans/$name.events.jsonl"
raw="tmp/plans/$name.raw.txt"
plan="tmp/plans/$name.json"
rm -f "$plan" "tmp/plans/$name.applied"

# Input bundle: only tracker state, recent history and the goal go to the model (see the privacy note in the design).
{
  echo "# Planning input: $name"
  echo
  echo "## Goal"
  printf '%s\n' "$goal"
  echo
  echo "## Tracker overview"
  python3 scripts/tasks.py overview
  echo
  echo "## Recent history"
  git log --oneline -15
  echo
  echo "## Current branch"
  git branch --show-current
} >"$input"

prompt="Plan the next sprint for this repository. Follow agents/kilo/PLANNER.md exactly and answer with the single JSON block it specifies. Input bundle: $input"

echo "planning '$name' with $model (variant $variant), timeout ${limit}s" >&2
rc=0
XDG_CONFIG_HOME="$root/tmp/xdg-empty" timeout "$limit" "$kilo_bin" run --dir "$root" --agent planner -m "$model" \
  --variant "$variant" --format json "$prompt" >"$events" 2>"tmp/plans/$name.stderr.log" || rc=$?
if [ "$rc" -ne 0 ]; then
  echo "error: planner agent failed (exit $rc); see tmp/plans/$name.stderr.log" >&2
  exit 3
fi

python3 - "$events" >"$raw" <<'PY'
import json, sys
texts = []
for line in open(sys.argv[1], errors="replace"):
    try:
        part = json.loads(line).get("part", {})
    except ValueError:
        continue
    if part.get("type") == "text" and part.get("text"):
        texts.append(part["text"])
print(texts[-1] if texts else "")
PY

python3 scripts/sprint_plan.py extract "$raw" "$plan" || { echo "error: no usable plan in $raw" >&2; exit 4; }
python3 scripts/sprint_plan.py validate "$plan" $lint_flag || { echo "error: plan rejected; raw answer kept in $raw" >&2; exit 4; }
python3 scripts/sprint_plan.py specs "$plan" >/dev/null
python3 scripts/sprint_plan.py preview "$plan"
echo ""
echo "plan: $plan  (apply with: scripts/apply-plan.sh $name)"
