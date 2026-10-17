#!/usr/bin/env bash
# Imports a validated sprint plan (tmp/plans/<name>.json) into the tracker through tasks.py.
# Previews the plan and needs a human "y" on a TTY (otherwise --yes). A plan is applied once.
#
#   scripts/apply-plan.sh <name> [--yes] [--start]
#
# --start is accepted for compatibility but no longer creates a sprint branch (worktree.sh removed).
# Exit: 0 applied | 1 usage/missing plan | 2 not confirmed | 4 plan invalid | 5 already applied
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

name="${1:-}"
[[ "$name" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "usage: $0 <name> [--yes] [--start]" >&2; exit 1; }
shift
yes=0 start=0
while [ $# -gt 0 ]; do
  case "$1" in
    --yes)   yes=1; shift ;;
    --start) start=1; shift ;;
    *) echo "unknown option '$1'" >&2; exit 1 ;;
  esac
done

plan="tmp/plans/$name.json"
marker="tmp/plans/$name.applied"
[ -f "$plan" ] || { echo "error: no plan '$plan' (run scripts/plan-sprint.sh $name first)" >&2; exit 1; }
[ ! -f "$marker" ] || { echo "error: '$name' was already applied ($(cat "$marker"))" >&2; exit 5; }

python3 scripts/sprint_plan.py validate "$plan" --specs-written || exit 4
python3 scripts/sprint_plan.py preview "$plan"
echo ""
if [ "$yes" -ne 1 ]; then
  if [ ! -t 0 ]; then
    echo "error: not a TTY; re-run with --yes to apply without asking" >&2
    exit 2
  fi
  read -r -p "Apply this plan to the tracker? [y/N] " answer
  [[ "$answer" =~ ^[yY]([eE][sS])?$ ]] || { echo "not applied" >&2; exit 2; }
fi

mapping="$(python3 scripts/sprint_plan.py apply "$plan")"
printf '%s\n' "$mapping"
sprint_id="$(printf '%s\n' "$mapping" | head -1 | sed 's/.*-> \([0-9]*\)\..*/\1/')"
slug="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sprint"]["slug"])' "$plan")"
printf 'sprint %s on %s\n' "$sprint_id" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$marker"
echo "applied: sprint $sprint_id ($slug)"
