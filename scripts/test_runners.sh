#!/usr/bin/env bash
# Tests for the delegation runners' model recording (junie-task.sh / kilo-task.sh):
# every status line carries model=/runner=, the final stdout report has runner: and
# model: header lines, tmp/runs/<name>.summary.json is written with the right verdict,
# and junie-status.sh reports runner/model from the summary (or the last status line).
# Plain bash, no external dependencies beyond git, python3 and grep. Each test runs in
# its own temporary git repo with a fake `junie`/`kilo` executable first in PATH that
# edits a file and exits 0 or non-zero (see make_repo). Prints PASS/FAIL per case.
# Exit code: 0 if all pass, 1 if any fail.
set -uo pipefail

passed=0
failed=0
total=0

pass() { passed=$((passed + 1)); total=$((total + 1)); echo "  PASS: $1"; }
fail() { failed=$((failed + 1)); total=$((total + 1)); echo "  FAIL: $1 — $2"; }

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ── helpers ──────────────────────────────────────────────────────────────

# make_repo <name>: build a throw-away git repo with the runner scripts installed,
# a fake junie/kilo in bin/ (first in PATH), a fake task-report.sh that logs its
# arguments (so tracker notes are assertable) and a ready "worktree" for <name>.
make_repo() {
    local name="$1"
    TEST_REPO="$(mktemp -d)"
    cd "$TEST_REPO"
    git init -q
    git config user.name "test"
    git config user.email "test@localhost"
    mkdir -p scripts tmp/worktrees/"$name" tmp/meta tmp/runs tmp/patches tmp/xdg-empty bin
    for f in worktree.sh junie-task.sh kilo-task.sh codex-task.sh run-task.sh junie-status.sh run_summary.py spec-lint.sh; do
        cp "$PROJECT_ROOT/scripts/$f" "scripts/$f"
    done
    mkdir -p agents/junie agents/kilo scripts/coder-git-hooks
    cp "$PROJECT_ROOT/agents/junie/RUN.md" agents/junie/RUN.md
    cp "$PROJECT_ROOT/agents/kilo/RUN.md" agents/kilo/RUN.md
    cp "$PROJECT_ROOT/scripts/coder-git-hooks/prepare-commit-msg" scripts/coder-git-hooks/prepare-commit-msg
    # fake task-report.sh: record every call so the model= in tracker notes is assertable
    cat > scripts/task-report.sh <<'FAKE'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${TASK_REPORT_LOG:?}"
exit 0
FAKE
    chmod +x scripts/worktree.sh scripts/junie-task.sh scripts/kilo-task.sh scripts/run-task.sh scripts/junie-status.sh scripts/task-report.sh scripts/coder-git-hooks/prepare-commit-msg
    # Runner artifacts live under tmp/ and must not leak into the agent patch;
    # tmp/worktrees/ stays visible so the fake agent's edits are stageable.
    cat > .gitignore <<'EOF'
tmp/runs/
tmp/meta/
tmp/patches/
tmp/xdg-empty/
EOF
    cat > bin/junie <<'FAKE'
#!/usr/bin/env bash
# fake agent for tests: writes solution.txt into the agent worktree (given via
# --project=<dir> or --dir <dir>), then exits 3/5 if the prompt carries EXIT_3/EXIT_5.
set -u
wt=""
shift_wt=0
for a in "$@"; do
    case "$a" in
        --project=*) wt="${a#--project=}" ;;
        --dir=*)     wt="${a#--dir=}" ;;
        --dir)       shift_wt=1 ;;
        *)
            if [ "$shift_wt" = "1" ]; then
                wt="$a"
                shift_wt=0
            fi
            ;;
    esac
done
[ -n "$wt" ] || exit 9
if [ -n "${FAKE_KILO_ARGS_LOG:-}" ]; then
    printf '%s\n' "$*" > "$FAKE_KILO_ARGS_LOG"
fi
case "$*" in *STALL*) sleep 60; exit 0 ;; esac
if [ "${FAKE_JUNIE_LINES:-0}" -gt 0 ]; then
    : > "$wt/oversized.txt"
    for ((line = 0; line < FAKE_JUNIE_LINES; line++)); do
        printf 'line %s\n' "$line" >> "$wt/oversized.txt"
    done
else
    printf 'solution %s\n' "$*" > "$wt/solution.txt"
fi
if [ "${FAKE_JUNIE_TRY_COMMIT:-0}" = "1" ]; then
    git -C "$wt" add solution.txt
    commit_rc=0
    git -C "$wt" commit --no-verify -m "agent-owned-commit" \
        > "$wt/../../runs/junie-commit-attempt.log" 2>&1 || commit_rc=$?
    printf '%s\n' "$commit_rc" > "$wt/../../runs/junie-commit-attempt.rc"
fi
sleep 2
case "$*" in
    *EXIT_3*) exit 3 ;;
    *EXIT_5*) exit 5 ;;
esac
exit 0
FAKE
    cp bin/junie bin/kilo
    chmod +x bin/junie bin/kilo
    # fake codex: takes the worktree from `-C <dir>`, writes solution.txt, exits 0/3/5.
    cat > bin/codex <<'FAKE'
#!/usr/bin/env bash
set -u
wt=""
while [ $# -gt 0 ]; do
    case "$1" in
        -C) wt="$2"; shift 2 ;;
        *) shift ;;
    esac
done
[ -n "$wt" ] || exit 9
case "$*" in *STALL*) sleep 60; exit 0 ;; esac
printf 'solution %s\n' "$*" > "$wt/solution.txt"
sleep 1
case "$*" in
    *EXIT_3*) exit 3 ;;
    *EXIT_5*) exit 5 ;;
esac
exit 0
FAKE
    chmod +x bin/codex
    echo "initial" > solution.txt
    git add -A
    git commit -q -m "initial"
    git rev-parse HEAD > "tmp/meta/$name.base"
    export PATH="$TEST_REPO/bin:$PATH"
    export TASK_REPORT_LOG="$TEST_REPO/tmp/runs/task-report.log"
}

cleanup_repo() {
    rm -rf "$TEST_REPO"
}

# run_runner <name> <junie-task.sh|kilo-task.sh> <model> <verify-cmd> <task-text>
# Runs the runner; leaves its exit code in $RUNNER_RC.
run_runner() {
    local name="$1" script="$2" model="$3" verify="$4" text="$5"
    local rc=0
    if [ "$script" = "junie-task.sh" ]; then
        JUNIE_MODEL="$model" JUNIE_VERIFY_CMD="$verify" JUNIE_PING=1 JUNIE_TIMEOUT=120 \
            bash scripts/junie-task.sh "$name" "$text" >"tmp/runs/$name.runner.out" 2>&1 || rc=$?
    else
        KILO_MODEL="$model" KILO_VERIFY_CMD="$verify" KILO_PING=1 KILO_TIMEOUT=120 \
            bash scripts/kilo-task.sh "$name" "$text" >"tmp/runs/$name.runner.out" 2>&1 || rc=$?
    fi
    RUNNER_RC=$rc
}

# json_field <file> <key>: print one field of a JSON summary ("" when absent).
json_field() {
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$1" "$2"
}

# status_lines_ok <name> <runner>: lines of tmp/runs/<name>.status that do NOT end
# with the model=/runner= tail (empty when every line carries the tail).
status_lines_ok() {
    grep -v "model=[^ ]* runner=$2\$" "tmp/runs/$1.status" 2>/dev/null || true
}

# ── test cases ───────────────────────────────────────────────────────────

test_success_junie() {
    make_repo ok
    trap cleanup_repo RETURN

    run_runner ok junie-task.sh junie-model true "write the solution EXIT_0"

    if [ "$RUNNER_RC" -eq 0 ]; then
        pass "success (junie): runner exit 0"
    else
        fail "success (junie): runner exit" "runner exit $RUNNER_RC, want 0"
        return
    fi

    local bad
    bad="$(status_lines_ok ok junie)"
    if [ -z "$bad" ]; then
        pass "success (junie): all status lines carry model=/runner=junie"
    else
        fail "success (junie): status lines" "lines without the model/runner tail: $bad"
        return
    fi

    if python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "tmp/runs/ok.summary.json" 2>/dev/null; then
        pass "success (junie): summary.json is valid JSON"
    else
        fail "success (junie): summary.json" "not valid JSON"
        return
    fi

    local runner model verdict agent_exit verify_exit files_changed agent
    runner="$(json_field tmp/runs/ok.summary.json runner)"
    model="$(json_field tmp/runs/ok.summary.json model)"
    verdict="$(json_field tmp/runs/ok.summary.json verdict)"
    agent_exit="$(json_field tmp/runs/ok.summary.json agent_exit)"
    verify_exit="$(json_field tmp/runs/ok.summary.json verify_exit)"
    files_changed="$(json_field tmp/runs/ok.summary.json files_changed)"
    agent="$(json_field tmp/runs/ok.summary.json agent)"
    if [ "$runner" = "junie" ] && [ "$model" = "junie-model" ] && [ "$verdict" = "verified" ] \
        && [ "$agent_exit" = "0" ] && [ "$verify_exit" = "0" ] && [ "$files_changed" = "1" ] \
        && [ -z "$agent" ]; then
        pass "success (junie): summary fields"
    else
        fail "success (junie): summary fields" "runner=$runner model=$model verdict=$verdict agent_exit=$agent_exit verify_exit=$verify_exit files_changed=$files_changed agent='$agent'"
    fi

    local out
    out="$(bash scripts/junie-status.sh ok)"
    if printf '%s\n' "$out" | grep -q '^runner:   junie$' && printf '%s\n' "$out" | grep -q '^model:    junie-model$'; then
        pass "success (junie): junie-status.sh prints runner/model"
    else
        fail "success (junie): junie-status.sh" "output missing runner/model: $out"
    fi

    if grep -q 'model=junie-model' "tmp/runs/task-report.log" 2>/dev/null; then
        pass "success (junie): tracker note includes model="
    else
        fail "success (junie): tracker note" "note log lacks model=: $(cat tmp/runs/task-report.log 2>/dev/null || true)"
    fi
}

test_junie_commit_is_blocked_but_parent_runner_commits() {
    make_repo guarded
    trap cleanup_repo RETURN

    local rc=0 attempt_rc attempt_log subject body commit_count
    FAKE_JUNIE_TRY_COMMIT=1 JUNIE_MODEL=guard-model JUNIE_VERIFY_CMD=true JUNIE_PING=1 JUNIE_TIMEOUT=120 \
        bash scripts/junie-task.sh guarded "write the solution" >"tmp/runs/guarded.runner.out" 2>&1 || rc=$?
    attempt_rc="$(cat tmp/runs/junie-commit-attempt.rc 2>/dev/null || true)"
    attempt_log="$(cat tmp/runs/junie-commit-attempt.log 2>/dev/null || true)"
    subject="$(git -C tmp/worktrees/guarded log -1 --format=%s 2>/dev/null || true)"
    body="$(git -C tmp/worktrees/guarded log -1 --format=%B 2>/dev/null || true)"
    commit_count="$(git rev-list --count HEAD)"
    if [ "$rc" -eq 0 ] && [ "$attempt_rc" -ne 0 ] \
        && printf '%s\n' "$attempt_log" | grep -q 'Task coder agents may not commit' \
        && [ "$subject" != "agent-owned-commit" ] && [ "$commit_count" -eq 2 ] \
        && printf '%s\n' "$body" | grep -q '^runner: junie$' \
        && printf '%s\n' "$body" | grep -q '^model: guard-model$'; then
        pass "Junie --no-verify commit is blocked; parent runner commits with trailers"
    else
        fail "Junie commit guard" "rc=$rc attempt=$attempt_rc subject='$subject' count=$commit_count"
    fi
}

test_kilo_commit_is_blocked_but_parent_runner_commits() {
    make_repo guarded-kilo
    trap cleanup_repo RETURN

    local rc=0 attempt_rc attempt_log subject body commit_count
    FAKE_JUNIE_TRY_COMMIT=1 KILO_MODEL=guard-model KILO_VERIFY_CMD=true KILO_PING=1 KILO_TIMEOUT=120 \
        bash scripts/kilo-task.sh guarded-kilo "write the solution" >"tmp/runs/guarded-kilo.runner.out" 2>&1 || rc=$?
    attempt_rc="$(cat tmp/runs/junie-commit-attempt.rc 2>/dev/null || true)"
    attempt_log="$(cat tmp/runs/junie-commit-attempt.log 2>/dev/null || true)"
    subject="$(git -C tmp/worktrees/guarded-kilo log -1 --format=%s 2>/dev/null || true)"
    body="$(git -C tmp/worktrees/guarded-kilo log -1 --format=%B 2>/dev/null || true)"
    commit_count="$(git rev-list --count HEAD)"
    if [ "$rc" -eq 0 ] && [ "$attempt_rc" -ne 0 ] \
        && printf '%s\n' "$attempt_log" | grep -q 'Task coder agents may not commit' \
        && [ "$subject" != "agent-owned-commit" ] && [ "$commit_count" -eq 2 ] \
        && printf '%s\n' "$body" | grep -q '^runner: kilo$' \
        && printf '%s\n' "$body" | grep -q '^model: guard-model$'; then
        pass "Kilo --no-verify commit is blocked; parent runner commits with trailers"
    else
        fail "Kilo commit guard" "rc=$rc attempt=$attempt_rc subject='$subject' count=$commit_count"
    fi
}

test_kilo_uses_configured_model_when_unset() {
    make_repo configured-model
    trap cleanup_repo RETURN

    local rc=0 reported_model
    FAKE_KILO_ARGS_LOG="$TEST_REPO/tmp/runs/kilo.args" KILO_VERIFY_CMD=true KILO_PING=1 KILO_TIMEOUT=120 \
        bash scripts/kilo-task.sh configured-model "write the solution" >"tmp/runs/configured-model.runner.out" 2>&1 || rc=$?
    reported_model="$(json_field tmp/runs/configured-model.summary.json model)"
    if [ "$rc" -eq 0 ] && [ "$reported_model" = "configured-default" ] \
        && ! grep -Eq '(^| )-m( |$)|--model' tmp/runs/kilo.args; then
        pass "Kilo leaves model selection to its configuration when KILO_MODEL is unset"
    else
        fail "Kilo configured model" "rc=$rc model=$reported_model args=$(cat tmp/runs/kilo.args 2>/dev/null)"
    fi
}

test_kilo_oversized_diff_is_preserved_without_commit() {
    make_repo oversized-kilo
    trap cleanup_repo RETURN

    local rc=0 base head commit_log patch_lines
    FAKE_JUNIE_LINES=301 KILO_MODEL=large-model KILO_VERIFY_CMD=true KILO_PING=1 KILO_TIMEOUT=120 \
        bash scripts/kilo-task.sh oversized-kilo "write an oversized solution" >"tmp/runs/oversized-kilo.runner.out" 2>&1 || rc=$?
    base="$(cat tmp/meta/oversized-kilo.base)"
    head="$(git rev-parse HEAD)"
    commit_log="$(cat tmp/runs/oversized-kilo.commit.log 2>/dev/null || true)"
    patch_lines="$(wc -l < tmp/patches/oversized-kilo.patch 2>/dev/null || printf '0')"
    if [ "$rc" -eq 1 ] && [ "$head" = "$base" ] \
        && printf '%s\n' "$commit_log" | grep -q '301 added/deleted lines (limit 300)' \
        && [ -f tmp/worktrees/oversized-kilo/oversized.txt ] \
        && [ "$(wc -l < tmp/worktrees/oversized-kilo/oversized.txt)" -eq 301 ] \
        && [ "$patch_lines" -gt 0 ]; then
        pass "Kilo diff over 300 lines is not committed and worktree/patch are preserved"
    else
        fail "Kilo 300-line guard" "rc=$rc head=$head base=$base log='$commit_log' patch_lines=$patch_lines"
    fi
}

test_junie_run_policy_is_explicit() {
    if grep -qi 'Never perform Git writes' "$PROJECT_ROOT/agents/junie/RUN.md" \
        && grep -qi '300 added plus deleted' "$PROJECT_ROOT/agents/junie/RUN.md" \
        && grep -qi 'never run Git write commands' "$PROJECT_ROOT/agents/junie/INSTRUCTIONS.md" \
        && grep -qi 'Never perform Git writes' "$PROJECT_ROOT/agents/kilo/RUN.md" \
        && grep -qi '300 added plus deleted' "$PROJECT_ROOT/agents/kilo/RUN.md" \
        && grep -qi 'never run Git write commands' "$PROJECT_ROOT/agents/kilo/INSTRUCTIONS.md"; then
        pass "Junie and Kilo instructions forbid Git writes and require pre-edit scope limits"
    else
        fail "Junie run policy" "missing no-write or 300-line instruction"
    fi
}

test_success_codex() {
    make_repo "cx"
    trap cleanup_repo RETURN
    printf 'write the solution EXIT_0\n' > spec.md

    local rc=0
    CODEX_VERIFY_CMD=true CODEX_PING=1 CODEX_TIMEOUT=120 \
        bash scripts/codex-task.sh cx --file spec.md >/dev/null 2>&1 || rc=$?
    local verdict runner model
    verdict="$(json_field tmp/runs/cx.summary.json verdict)"
    runner="$(json_field tmp/runs/cx.summary.json runner)"
    model="$(json_field tmp/runs/cx.summary.json model)"
    if [ "$rc" -eq 0 ] && [ "$verdict" = "verified" ] && [ "$runner" = "codex" ] && [ "$model" = "codex" ]; then
        pass "codex runner: exit 0, verified, runner/model recorded"
    else
        fail "codex runner" "rc=$rc verdict=$verdict runner=$runner model=$model"
    fi
}

test_agent_failure_kilo() {
    make_repo agent-fail
    trap cleanup_repo RETURN

    run_runner agent-fail kilo-task.sh kilo-model true "make it fail EXIT_3"

    if [ "$RUNNER_RC" -eq 3 ]; then
        pass "agent failure (kilo): runner exit 3"
    else
        fail "agent failure (kilo): runner exit" "runner exit $RUNNER_RC, want 3"
        return
    fi

    local bad
    bad="$(status_lines_ok agent-fail kilo)"
    if [ -z "$bad" ]; then
        pass "agent failure (kilo): all status lines carry model=/runner=kilo"
    else
        fail "agent failure (kilo): status lines" "lines without the model/runner tail: $bad"
    fi

    local runner model verdict agent_exit verify_exit agent
    runner="$(json_field tmp/runs/agent-fail.summary.json runner)"
    model="$(json_field tmp/runs/agent-fail.summary.json model)"
    verdict="$(json_field tmp/runs/agent-fail.summary.json verdict)"
    agent_exit="$(json_field tmp/runs/agent-fail.summary.json agent_exit)"
    verify_exit="$(json_field tmp/runs/agent-fail.summary.json verify_exit)"
    agent="$(json_field tmp/runs/agent-fail.summary.json agent)"
    if [ "$runner" = "kilo" ] && [ "$model" = "kilo-model" ] && [ "$verdict" = "agent_failed" ] \
        && [ "$agent_exit" = "3" ] && [ "$verify_exit" = "0" ] && [ "$agent" = "code" ]; then
        pass "agent failure (kilo): summary fields (incl. agent)"
    else
        fail "agent failure (kilo): summary fields" "runner=$runner model=$model verdict=$verdict agent_exit=$agent_exit verify_exit=$verify_exit agent='$agent'"
    fi

    if grep -q 'model=kilo-model' "tmp/runs/task-report.log" 2>/dev/null; then
        pass "agent failure (kilo): tracker note includes model="
    else
        fail "agent failure (kilo): tracker note" "note log lacks model=: $(cat tmp/runs/task-report.log 2>/dev/null || true)"
    fi
}

test_verify_failure_junie() {
    make_repo verify-fail
    trap cleanup_repo RETURN

    run_runner verify-fail junie-task.sh junie-model false "write the solution EXIT_0"

    if [ "$RUNNER_RC" -eq 1 ]; then
        pass "verification failure (junie): runner exit 1"
    else
        fail "verification failure (junie): runner exit" "runner exit $RUNNER_RC, want 1"
        return
    fi

    local verdict agent_exit verify_exit
    verdict="$(json_field tmp/runs/verify-fail.summary.json verdict)"
    agent_exit="$(json_field tmp/runs/verify-fail.summary.json agent_exit)"
    verify_exit="$(json_field tmp/runs/verify-fail.summary.json verify_exit)"
    if [ "$verdict" = "verify_failed" ] && [ "$agent_exit" = "0" ] && [ "$verify_exit" = "1" ]; then
        pass "verification failure (junie): summary fields"
    else
        fail "verification failure (junie): summary fields" "verdict=$verdict agent_exit=$agent_exit verify_exit=$verify_exit"
    fi
}

test_status_fallback() {
    # No summary yet: junie-status.sh takes runner/model from the last status line.
    make_repo fb
    trap cleanup_repo RETURN

    echo "[00:00] junie running - 1 file(s) changed; last: working model=junie-lite runner=junie" > "tmp/runs/fb.status"
    touch "tmp/runs/fb.stdout.log"
    local out
    out="$(bash scripts/junie-status.sh fb)"
    if printf '%s\n' "$out" | grep -q '^runner:   junie$' && printf '%s\n' "$out" | grep -q '^model:    junie-lite$'; then
        pass "junie-status.sh: fallback to last status line"
    else
        fail "junie-status.sh: fallback" "output: $out"
    fi
}

test_prechange_run() {
    # Runs that predate model recording have no summary and no model= in the status
    # file; junie-status.sh must still work and print no runner/model lines.
    make_repo pre
    trap cleanup_repo RETURN

    echo "[00:00] starting - worktree=/x" > "tmp/runs/pre.status"
    touch "tmp/runs/pre.stdout.log"
    local rc=0 out
    out="$(bash scripts/junie-status.sh pre)" || rc=$?
    if [ "$rc" -eq 0 ] && ! printf '%s\n' "$out" | grep -q '^runner:'; then
        pass "junie-status.sh: pre-change run still works"
    else
        fail "junie-status.sh: pre-change run" "rc=$rc output: $out"
    fi
}

# The runner closes the tracker task itself after the commit and verification:
# green -> done, red -> blocked (with the failing check as reason).
test_close_status_after_commit() {
    make_repo "close"
    trap cleanup_repo RETURN
    printf 'write the solution EXIT_0\n' > spec.md

    JUNIE_MODEL=jm JUNIE_VERIFY_CMD=true JUNIE_PING=1 JUNIE_TIMEOUT=120 \
        bash scripts/junie-task.sh close --file spec.md --task-id 7.9 >/dev/null 2>&1 || true
    if grep -q '^close 7.9 green' tmp/runs/task-report.log 2>/dev/null; then
        pass "runner closes the task green after a verified commit"
    else
        fail "runner close green" "log: $(cat tmp/runs/task-report.log 2>/dev/null)"
    fi

    : > tmp/runs/task-report.log
    JUNIE_MODEL=jm JUNIE_VERIFY_CMD=false JUNIE_PING=1 JUNIE_TIMEOUT=120 \
        bash scripts/junie-task.sh close --file spec.md --task-id 7.9 >/dev/null 2>&1 || true
    if grep -q '^close 7.9 red ' tmp/runs/task-report.log 2>/dev/null; then
        pass "runner closes the task red (blocked) after a failed verification"
    else
        fail "runner close red" "log: $(cat tmp/runs/task-report.log 2>/dev/null)"
    fi
}

# Real task-report.sh: `close green` maps to done, `close red <reason>` to blocked with the reason.
test_task_report_close() {
    local root out; root="$(mktemp -d)"
    (
        export VIBE_TASKS_ROOT="$root"
        python3 "$PROJECT_ROOT/scripts/tasks_init.py" --path "$root/tasks/tasks.db" --project T >/dev/null 2>&1
        python3 "$PROJECT_ROOT/scripts/tasks.py" add-sprint S >/dev/null 2>&1
        python3 "$PROJECT_ROOT/scripts/tasks.py" add 1 T1 >/dev/null 2>&1
        bash "$PROJECT_ROOT/scripts/task-report.sh" close 1.1 green >/dev/null 2>&1
        python3 "$PROJECT_ROOT/scripts/tasks.py" show 1.1 > "$root/green.json" 2>/dev/null
        python3 "$PROJECT_ROOT/scripts/tasks.py" reset 1.1 >/dev/null 2>&1
        bash "$PROJECT_ROOT/scripts/task-report.sh" close 1.1 red "bad check" >/dev/null 2>&1
        python3 "$PROJECT_ROOT/scripts/tasks.py" show 1.1 > "$root/red.json" 2>/dev/null
    )
    out="$(python3 -c 'import json,sys; d=sys.argv[1]; g=json.load(open(d+"/green.json")); r=json.load(open(d+"/red.json")); print(g["effective_status"], r["effective_status"], r["reason"])' "$root")"
    if [ "$out" = "done blocked bad check" ]; then
        pass "task-report close: green -> done, red -> blocked with reason"
    else
        fail "task-report close mapping" "got: '$out'"
    fi
    rm -rf "$root"
}

# ── main ─────────────────────────────────────────────────────────────────

echo "=== runner model recording tests ==="

# These tests use minimal spec files that do not follow the spec template;
# disable spec-lint so the existing assertions remain valid.
export SPEC_LINT=off

# A stuck agent (no file changed after RUNNER_STALL seconds) is killed and reported.
test_stall_detector() {
    local script
    for script in junie-task.sh kilo-task.sh; do
        make_repo "stall"
        local start=$SECONDS rc=0
        if [ "$script" = "junie-task.sh" ]; then
            RUNNER_STALL=2 JUNIE_PING=1 JUNIE_TIMEOUT=120 JUNIE_VERIFY_CMD=true \
                bash scripts/junie-task.sh stall "do nothing STALL" >tmp/runs/stall.runner.out 2>&1 || rc=$?
        else
            RUNNER_STALL=2 KILO_PING=1 KILO_TIMEOUT=120 KILO_VERIFY_CMD=true \
                bash scripts/kilo-task.sh stall "do nothing STALL" >tmp/runs/stall.runner.out 2>&1 || rc=$?
        fi
        local took=$((SECONDS - start))
        if [ "$rc" -eq 3 ] && grep -q "stalled" tmp/runs/stall.status tmp/runs/stall.runner.out && [ "$took" -lt 40 ]; then
            pass "stall detector ($script): killed, exit 3, 'stalled' reported in ${took}s"
        else
            fail "stall detector ($script)" "rc=$rc took=${took}s status=$(tail -1 tmp/runs/stall.status 2>/dev/null)"
        fi
        cleanup_repo
    done
}

# run-task.sh: junie fails (exit 3) -> exactly one retry with kilo, which succeeds; a
# verification failure is NOT retried; fallback=none keeps exit 3.
test_run_task_fallback() {
    make_repo "fb"
    sed '/EXIT_3/d;/EXIT_5/d' bin/junie > bin/kilo; chmod +x bin/kilo
    printf 'make it fail EXIT_3\n' > spec.md
    local rc=0
    JUNIE_MODEL=jm KILO_MODEL=km JUNIE_VERIFY_CMD=true KILO_VERIFY_CMD=true JUNIE_PING=1 KILO_PING=1 \
        bash scripts/run-task.sh fb --file spec.md --runner junie --fallback kilo >tmp/runs/fb.rt.out 2>&1 || rc=$?
    local attempts; attempts="$(cat tmp/runs/fb.attempts 2>/dev/null)"
    if [ "$rc" -eq 0 ] && [ "$(printf '%s\n' "$attempts" | wc -l)" -eq 2 ] \
       && printf '%s' "$attempts" | grep -q "runner=junie exit=3" && printf '%s' "$attempts" | grep -q "runner=kilo exit=0"; then
        pass "run-task: junie failure falls back to kilo once, overall exit 0, both attempts recorded"
    else
        fail "run-task fallback" "rc=$rc attempts=$attempts"
    fi
    cleanup_repo

    make_repo "default-codex"
    printf 'write a successful solution\n' > spec.md; rc=0
    CODEX_VERIFY_CMD=true CODEX_PING=1 \
        bash scripts/run-task.sh default-codex --file spec.md >tmp/runs/default-codex.rt.out 2>&1 || rc=$?
    local default_attempts default_runner
    default_attempts="$(cat tmp/runs/default-codex.attempts 2>/dev/null)"
    default_runner="$(json_field tmp/runs/default-codex.summary.json runner)"
    if [ "$rc" -eq 0 ] && [ "$(printf '%s\n' "$default_attempts" | wc -l)" -eq 1 ] \
        && printf '%s' "$default_attempts" | grep -q 'runner=codex exit=0' \
        && [ "$default_runner" = "codex" ]; then
        pass "run-task: default coder is Codex, single attempt, no fallback"
    else
        fail "run-task default Codex" "rc=$rc runner=$default_runner attempts=$default_attempts"
    fi
    cleanup_repo

    make_repo "vf"
    printf 'write it EXIT_0\n' > spec.md; rc=0
    JUNIE_MODEL=jm JUNIE_VERIFY_CMD=false JUNIE_PING=1 bash scripts/run-task.sh vf --file spec.md >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ] && [ "$(wc -l < tmp/runs/vf.attempts)" -eq 1 ]; then
        pass "run-task: verification failure (exit 1) is not retried on another runner"
    else
        fail "run-task no retry on verify failure" "rc=$rc"
    fi
    cleanup_repo
}

test_success_junie
test_junie_commit_is_blocked_but_parent_runner_commits
test_kilo_commit_is_blocked_but_parent_runner_commits
test_kilo_uses_configured_model_when_unset
test_kilo_oversized_diff_is_preserved_without_commit
test_junie_run_policy_is_explicit
test_success_codex
test_agent_failure_kilo
test_verify_failure_junie
test_status_fallback
test_prechange_run
test_stall_detector
test_run_task_fallback
test_close_status_after_commit
test_task_report_close

echo ""
echo "=== Results: $passed/$total passed, $failed failed ==="

if [ "$failed" -gt 0 ]; then
    exit 1
fi
exit 0
