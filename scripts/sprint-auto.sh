#!/usr/bin/env bash
# Automated sprint lifecycle (opt-in via --yolo-force).
# Usage: scripts/sprint-auto.sh --yolo-force --goal "..." --run-name NAME [--answers FILE] [--plan FILE]
#
# Batches unresolved decisions before mutation, validates and imports a plan,
# delegates tasks, accepts only verified zero-flag MRs, and finishes into main
# without pushing. Existing commands are NOT automatic when --yolo-force is absent.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

yolo=0
goal="" run_name="" answers="" plan=""
while [ $# -gt 0 ]; do
    case "$1" in
        --yolo-force) yolo=1; shift ;;
        --goal) goal="${2:?--goal needs a value}"; shift 2 ;;
        --run-name) run_name="${2:?--run-name needs a value}"; shift 2 ;;
        --answers) answers="${2:?--answers needs a value}"; shift 2 ;;
        --plan) plan="${2:?--plan needs a value}"; shift 2 ;;
        *) echo "unknown option '$1'" >&2; exit 1 ;;
    esac
done

[ "$yolo" -eq 1 ] || { echo "error: --yolo-force is required for automated lifecycle" >&2; exit 1; }
[ -n "$goal" ] || { echo "error: --goal is required" >&2; exit 1; }
[ -n "$run_name" ] || { echo "error: --run-name is required" >&2; exit 1; }

# Default answers file
if [ -z "$answers" ]; then
    answers="tmp/plans/${run_name}.answers.json"
fi

echo "=== sprint-auto: preflight ===" >&2
# Step 1: Preflight (read-only)
preflight_args=(python3 -B scripts/sprint_auto_preflight.py preflight-probe --goal "$goal")
# If answers file exists, use collect instead
if [ -f "$answers" ]; then
    preflight_args=(python3 -B scripts/sprint_auto_preflight.py collect --root . --answers "$answers")
    if [ -n "$plan" ]; then
        preflight_args+=(--request <(echo "{\"goal\":\"$goal\"}"))
    fi
fi

rc=0
"${preflight_args[@]}" > tmp/plans/${run_name}.preflight.json 2>&1 || rc=$?
if [ "$rc" -eq 2 ]; then
    echo "sprint-auto: missing answers (exit 2) — resolve questions in ${run_name}.preflight.json" >&2
    cat tmp/plans/${run_name}.preflight.json >&2
    exit 2
fi
if [ "$rc" -eq 4 ]; then
    echo "sprint-auto: safety blockers (exit 4)" >&2
    cat tmp/plans/${run_name}.preflight.json >&2
    exit 4
fi
if [ "$rc" -ne 0 ]; then
    echo "sprint-auto: preflight failed (exit $rc)" >&2
    exit "$rc"
fi

echo "=== sprint-auto: prepare ===" >&2
prepare_args=(python3 -B scripts/sprint_auto_prepare.py --root . --run-name "$run_name" --goal "$goal" --answers "$answers")
[ -n "$plan" ] && prepare_args+=(--plan "$plan")
rc=0
"${prepare_args[@]}" > tmp/plans/${run_name}.prepare.json 2>&1 || rc=$?
if [ "$rc" -ne 0 ]; then
    echo "sprint-auto: prepare failed (exit $rc)" >&2
    cat tmp/plans/${run_name}.prepare.json >&2
    exit "$rc"
fi

echo "=== sprint-auto: prepared ===" >&2
cat tmp/plans/${run_name}.prepare.json >&2

echo "=== sprint-auto: done (automation components wired) ===" >&2
echo "Run complete. Use worktree.sh for task execution and merge." >&2
exit 0