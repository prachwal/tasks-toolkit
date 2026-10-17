#!/usr/bin/env bash
# Tests for the Kilo sprint planner pipeline (plan-sprint.sh, sprint_plan.py, apply-plan.sh).
# A stub `kilo` on KILO_BIN replaces the model, so nothing touches the network or the Kilo Gateway.
# Each test runs in its own throw-away git repo with its own tracker (VIBE_TASKS_ROOT).
# Exit code: 0 if all pass, 1 if any fail.
set -uo pipefail

passed=0
failed=0
total=0
pass() { passed=$((passed + 1)); total=$((total + 1)); echo "  PASS: $1"; }
fail() { failed=$((failed + 1)); total=$((total + 1)); echo "  FAIL: $1 — $2"; }

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

make_repo() {
    TEST_REPO="$(mktemp -d)"
    cd "$TEST_REPO"
    git init -q -b main
    git config user.name test
    git config user.email test@localhost
    mkdir -p scripts bin
    for f in plan-sprint.sh apply-plan.sh sprint_plan.py spec-lint.sh tasks.py tasks_init.py worktree.sh needs-dotnet-checks.sh; do
        cp "$PROJECT_ROOT/scripts/$f" scripts/
    done
    printf 'tmp/\ntasks/\n' > .gitignore
    git add -A && git commit -q -m base
    export VIBE_TASKS_ROOT="$TEST_REPO"
    python3 scripts/tasks.py add-sprint "Old" >/dev/null
}

cleanup_repo() { cd /; rm -rf "$TEST_REPO"; }

# stub_kilo <plan-json-or-text> [exit-code]: bin/kilo prints the model answer as a Kilo JSON event stream.
stub_kilo() {
    printf '%s' "$1" > "$TEST_REPO/bin/answer.txt"
    cat > "$TEST_REPO/bin/kilo" <<FAKE
#!/usr/bin/env bash
printf '%s\n' "\$*" > "$TEST_REPO/bin/kilo.args"
[ "${2:-0}" -eq 0 ] || exit ${2:-0}
python3 - "$TEST_REPO/bin/answer.txt" <<'PY'
import json, sys
print(json.dumps({"part": {"type": "text", "text": open(sys.argv[1]).read()}}))
PY
FAKE
    chmod +x "$TEST_REPO/bin/kilo"
    export KILO_BIN="$TEST_REPO/bin/kilo"
}

SPEC='# Task: a\n\n## Goal\nDo a.\n\n## Files\n- EDIT: scripts/a.sh — a\n\n## Tests\nscripts/test_a.sh\n\n## Acceptance\n`bash scripts/test_a.sh`\n\n## Live check\n`bash scripts/a.sh`\n'

good_plan() {
    python3 - "$SPEC" <<'PY'
import json, sys
spec = sys.argv[1].encode().decode("unicode_escape")
plan = {"sprint": {"slug": "demo-plan", "title": "Demo plan", "goal": "Prove the planner"},
        "tasks": [
            {"key": "a", "title": "Add a", "description": "first", "depends_on": [], "scope": ["scripts/a.sh"],
             "spec_name": "demo-a", "spec": spec},
            {"key": "b", "title": "Add b", "description": "second", "depends_on": ["a"], "scope": ["scripts/b.sh"],
             "spec_name": "demo-b", "spec": spec.replace("a.sh", "b.sh").replace("test_a", "test_b")}]}
print("Here is the plan:\n```json\n" + json.dumps(plan) + "\n```")
PY
}

test_plan_happy_path() {
    make_repo; trap cleanup_repo RETURN
    stub_kilo "$(good_plan)"
    local rc=0
    bash scripts/plan-sprint.sh demo --goal "prove it" >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 0 ] && [ -f tmp/plans/demo.json ] && [ -f tmp/tasks/demo-a.md ] && [ -f tmp/tasks/demo-b.md ]; then
        pass "plan-sprint: valid plan is saved with its specs"
    else
        fail "plan happy path" "rc=$rc"
    fi
    if grep -q -- "--agent planner" bin/kilo.args && grep -q -- "-m kilo/openai/gpt-6.1-sol" bin/kilo.args \
        && grep -q -- "--variant high" bin/kilo.args; then
        pass "plan-sprint: runs the planner agent on Sol with the high variant"
    else
        fail "plan args" "$(cat bin/kilo.args)"
    fi
    grep -q "## Tracker overview" tmp/plans/demo.input.md && grep -q "prove it" tmp/plans/demo.input.md \
        && pass "plan-sprint: input bundle has goal and tracker overview" || fail "input bundle" "missing sections"
}

test_plan_rejects_invalid() {
    make_repo; trap cleanup_repo RETURN
    local bad
    bad="$(good_plan | sed 's/"scope": \["scripts\/a.sh"\]/"scope": []/')"
    stub_kilo "$bad"
    local rc=0 err
    err="$(bash scripts/plan-sprint.sh bad --goal g 2>&1 >/dev/null)" || rc=$?
    if [ "$rc" -eq 4 ] && [ ! -e tmp/tasks/demo-a.md ] && printf '%s' "$err" | grep -q "scope path is required"; then
        pass "plan-sprint: plan without scope exits 4 and writes no specs"
    else
        fail "invalid plan" "rc=$rc err=$err"
    fi
    stub_kilo "no json here"
    rc=0
    bash scripts/plan-sprint.sh prose --goal g >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 4 ] && pass "plan-sprint: answer without JSON exits 4" || fail "no json" "rc=$rc"
}

test_plan_rejects_forbidden_scope() {
    make_repo; trap cleanup_repo RETURN
    stub_kilo "$(good_plan | sed 's#scripts/b.sh#kilo.json#')"
    local rc=0
    bash scripts/plan-sprint.sh forb --goal g >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 4 ] && pass "plan-sprint: forbidden scope path is rejected" || fail "forbidden scope" "rc=$rc"
}

test_plan_kilo_failure() {
    make_repo; trap cleanup_repo RETURN
    stub_kilo "x" 7
    local rc=0
    bash scripts/plan-sprint.sh fail --goal g >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 3 ] && pass "plan-sprint: Kilo failure exits 3" || fail "kilo failure" "rc=$rc"
}

test_plan_usage() {
    make_repo; trap cleanup_repo RETURN
    local rc=0
    bash scripts/plan-sprint.sh >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 1 ] && pass "plan-sprint: no name exits 1" || fail "usage" "rc=$rc"
    rc=0
    bash scripts/plan-sprint.sh ok >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 1 ] && pass "plan-sprint: missing --goal exits 1" || fail "goal required" "rc=$rc"
}

test_apply_plan() {
    make_repo; trap cleanup_repo RETURN
    stub_kilo "$(good_plan)"
    bash scripts/plan-sprint.sh demo --goal g >/dev/null 2>&1
    local rc=0
    bash scripts/apply-plan.sh demo </dev/null >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 2 ] && pass "apply-plan: refuses without a TTY or --yes (exit 2)" || fail "apply needs confirm" "rc=$rc"
    [ ! -e tmp/plans/demo.applied ] || fail "apply marker" "written without confirmation"

    rc=0
    bash scripts/apply-plan.sh demo --yes >/dev/null 2>&1 || rc=$?
    local out
    out="$(python3 scripts/tasks.py show 2.2 2>&1)"
    if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q '"scripts/b.sh"' && printf '%s' "$out" | grep -q 'tmp/tasks/demo-b.md' \
        && printf '%s' "$out" | grep -q '2.1'; then
        pass "apply-plan: creates sprint, tasks, deps, spec and scope links"
    else
        fail "apply" "rc=$rc out=$out"
    fi
    rc=0
    bash scripts/apply-plan.sh demo --yes >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 5 ] && pass "apply-plan: second apply is refused (exit 5)" || fail "double apply" "rc=$rc"
}

test_apply_without_plan() {
    make_repo; trap cleanup_repo RETURN
    local rc=0
    bash scripts/apply-plan.sh nothing --yes >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 1 ] && pass "apply-plan: missing plan exits 1" || fail "no plan" "rc=$rc"
}

echo "=== plan-sprint / apply-plan tests ==="
test_plan_happy_path
test_plan_rejects_invalid
test_plan_rejects_forbidden_scope
test_plan_kilo_failure
test_plan_usage
test_apply_plan
test_apply_without_plan

echo ""
echo "=== Results: $passed/$total passed, $failed failed ==="
[ "$failed" -eq 0 ]
