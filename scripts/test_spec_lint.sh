#!/usr/bin/env bash
# Tests for scripts/spec-lint.sh: valid specs, each missing section, empty
# sections, file count boundaries, size budget, command requirement, multiple
# violations, usage errors, SPEC_LINT=off, template, and run-task.sh integration.
# Plain bash, no external dependencies beyond coreutils/grep/awk.
# Prints PASS/FAIL per case. Exit code: 0 if all pass, 1 if any fail.
set -uo pipefail

passed=0
failed=0
total=0

pass() { passed=$((passed + 1)); total=$((total + 1)); echo "  PASS: $1"; }
fail() { failed=$((failed + 1)); total=$((total + 1)); echo "  FAIL: $1 — $2"; }

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LINT="$PROJECT_ROOT/scripts/spec-lint.sh"
RUN_TASK="$PROJECT_ROOT/scripts/run-task.sh"

# ── helpers ──────────────────────────────────────────────────────────────

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

write_spec() {
  # $1 = filename, $2 = content
  printf '%s\n' "$2" > "$tmpdir/$1"
}

run_lint() {
  # $1 = spec file path; captures stdout and stderr separately
  local rc=0
  LINT_OUT="$(bash "$LINT" "$1" 2>"$tmpdir/lint.err")" || rc=$?
  LINT_ERR="$(cat "$tmpdir/lint.err")"
  LINT_RC=$rc
}

# ── test cases ───────────────────────────────────────────────────────────

test_valid_spec() {
  write_spec valid.md "
## Goal
Implement a new feature.

## Files
- NEW src/NewFile.cs — the new module

## Tests
Add unit tests in tests/NewFileTests.cs.

## Acceptance
Run \`bash scripts/test.sh\` and verify green.

## Live check
Run \`bash scripts/run.sh hash text -i hello\`"
  run_lint "$tmpdir/valid.md"
  if [ "$LINT_RC" -eq 0 ] && [ "$LINT_OUT" = "spec-lint: OK" ]; then
    pass "valid spec exits 0 with OK"
  else
    fail "valid spec" "rc=$LINT_RC out='$LINT_OUT'"
  fi
}

test_missing_goal() {
  write_spec no-goal.md "
## Files
- NEW src/X.cs

## Tests
Tests for X.

## Acceptance
Run \`bash scripts/test.sh\`.

## Live check
Run \`bash scripts/run.sh\`"
  run_lint "$tmpdir/no-goal.md"
  if [ "$LINT_RC" -eq 1 ] && printf '%s' "$LINT_ERR" | grep -q "missing-section.*Goal"; then
    pass "missing ## Goal detected"
  else
    fail "missing Goal" "rc=$LINT_RC err='$LINT_ERR'"
  fi
}

test_missing_files() {
  write_spec no-files.md "
## Goal
Do something.

## Tests
Tests.

## Acceptance
Run \`bash scripts/test.sh\`.

## Live check
Run \`bash scripts/run.sh\`"
  run_lint "$tmpdir/no-files.md"
  if [ "$LINT_RC" -eq 1 ] && printf '%s' "$LINT_ERR" | grep -q "missing-section.*Files"; then
    pass "missing ## Files detected"
  else
    fail "missing Files" "rc=$LINT_RC err='$LINT_ERR'"
  fi
}

test_missing_tests() {
  write_spec no-tests.md "
## Goal
Do something.

## Files
- NEW src/X.cs

## Acceptance
Run \`bash scripts/test.sh\`.

## Live check
Run \`bash scripts/run.sh\`"
  run_lint "$tmpdir/no-tests.md"
  if [ "$LINT_RC" -eq 1 ] && printf '%s' "$LINT_ERR" | grep -q "missing-section.*Tests"; then
    pass "missing ## Tests detected"
  else
    fail "missing Tests" "rc=$LINT_RC err='$LINT_ERR'"
  fi
}

test_missing_acceptance() {
  write_spec no-acceptance.md "
## Goal
Do something.

## Files
- NEW src/X.cs

## Tests
Tests.

## Live check
Run \`bash scripts/run.sh\`"
  run_lint "$tmpdir/no-acceptance.md"
  if [ "$LINT_RC" -eq 1 ] && printf '%s' "$LINT_ERR" | grep -q "missing-section.*Acceptance"; then
    pass "missing ## Acceptance detected"
  else
    fail "missing Acceptance" "rc=$LINT_RC err='$LINT_ERR'"
  fi
}

test_missing_live_check() {
  write_spec no-live.md "
## Goal
Do something.

## Files
- NEW src/X.cs

## Tests
Tests.

## Acceptance
Run \`bash scripts/test.sh\`"
  run_lint "$tmpdir/no-live.md"
  if [ "$LINT_RC" -eq 1 ] && printf '%s' "$LINT_ERR" | grep -q "missing-section.*Live check"; then
    pass "missing ## Live check detected"
  else
    fail "missing Live check" "rc=$LINT_RC err='$LINT_ERR'"
  fi
}

test_empty_section() {
  write_spec empty-sec.md "
## Goal
Do something.

## Files

## Tests
Tests.

## Acceptance
Run \`bash scripts/test.sh\`.

## Live check
Run \`bash scripts/run.sh\`"
  run_lint "$tmpdir/empty-sec.md"
  if [ "$LINT_RC" -eq 1 ] && printf '%s' "$LINT_ERR" | grep -q "empty-section.*Files"; then
    pass "empty ## Files detected"
  else
    fail "empty section" "rc=$LINT_RC err='$LINT_ERR'"
  fi
}

test_zero_files() {
  write_spec zero-files.md "
## Goal
Do something.

## Files
No files listed here.

## Tests
Tests.

## Acceptance
Run \`bash scripts/test.sh\`.

## Live check
Run \`bash scripts/run.sh\`"
  run_lint "$tmpdir/zero-files.md"
  if [ "$LINT_RC" -eq 1 ] && printf '%s' "$LINT_ERR" | grep -q "files-count.*0"; then
    pass "zero file entries detected"
  else
    fail "zero files" "rc=$LINT_RC err='$LINT_ERR'"
  fi
}

test_nine_files() {
  # 9 entries exceeds the max of 8
  write_spec nine-files.md "
## Goal
Do something.

## Files
- NEW src/A.cs
- NEW src/B.cs
- NEW src/C.cs
- NEW src/D.cs
- NEW src/E.cs
- NEW src/F.cs
- NEW src/G.cs
- NEW src/H.cs
- NEW src/I.cs

## Tests
Tests.

## Acceptance
Run \`bash scripts/test.sh\`.

## Live check
Run \`bash scripts/run.sh\`"
  run_lint "$tmpdir/nine-files.md"
  if [ "$LINT_RC" -eq 1 ] && printf '%s' "$LINT_ERR" | grep -q "files-count.*9"; then
    pass "nine file entries detected"
  else
    fail "nine files" "rc=$LINT_RC err='$LINT_ERR'"
  fi
}

test_too_many_lines() {
  # Build a spec with 121 lines: 9 header lines + 112 padding lines
  {
    echo "## Goal"
    echo "Goal text."
    echo "## Files"
    echo "- NEW src/X.cs"
    echo "## Tests"
    echo "Tests."
    echo "## Acceptance"
    echo "Run \`bash scripts/test.sh\`."
    echo "## Live check"
    # Pad to reach 121 lines total (9 header + 112 padding = 121)
    local count=0
    while [ "$count" -lt 112 ]; do
      echo "padding line $count"
      count=$((count + 1))
    done
  } > "$tmpdir/long.md"
  run_lint "$tmpdir/long.md"
  if [ "$LINT_RC" -eq 1 ] && printf '%s' "$LINT_ERR" | grep -q "size-lines.*121"; then
    pass "121 lines detected"
  else
    fail "121 lines" "rc=$LINT_RC err='$LINT_ERR'"
  fi
}

test_no_command() {
  write_spec no-cmd.md "
## Goal
Do something.

## Files
- NEW src/X.cs

## Tests
Tests.

## Acceptance
Everything should work.

## Live check
Just run it manually."
  run_lint "$tmpdir/no-cmd.md"
  if [ "$LINT_RC" -eq 1 ] && printf '%s' "$LINT_ERR" | grep -q "no-command"; then
    pass "no concrete command detected"
  else
    fail "no command" "rc=$LINT_RC err='$LINT_ERR'"
  fi
}

test_multiple_violations() {
  # Missing Goal + empty Files + no command
  write_spec multi.md "
## Files

## Tests
Tests.

## Acceptance
It works.

## Live check
Run it."
  run_lint "$tmpdir/multi.md"
  local missing_goal=0 empty_files=0 no_cmd=0
  printf '%s' "$LINT_ERR" | grep -q "missing-section.*Goal" && missing_goal=1
  printf '%s' "$LINT_ERR" | grep -q "empty-section.*Files" && empty_files=1
  printf '%s' "$LINT_ERR" | grep -q "no-command" && no_cmd=1
  if [ "$LINT_RC" -eq 1 ] && [ "$missing_goal" -eq 1 ] && [ "$empty_files" -eq 1 ] && [ "$no_cmd" -eq 1 ]; then
    pass "multiple violations reported together"
  else
    fail "multiple violations" "rc=$LINT_RC missing_goal=$missing_goal empty_files=$empty_files no_cmd=$no_cmd err='$LINT_ERR'"
  fi
}

test_no_arg() {
  local rc=0 out
  out="$(bash "$LINT" 2>&1)" || rc=$?
  if [ "$rc" -eq 2 ]; then
    pass "no argument exits 2"
  else
    fail "no argument" "rc=$rc out='$out'"
  fi
}

test_missing_file() {
  local rc=0 out
  out="$(bash "$LINT" "/nonexistent/spec.md" 2>&1)" || rc=$?
  if [ "$rc" -eq 2 ]; then
    pass "missing file exits 2"
  else
    fail "missing file" "rc=$rc out='$out'"
  fi
}

test_spec_lint_off() {
  write_spec any.md "
## Goal
Anything."
  local rc=0 out
  out="$(SPEC_LINT=off bash "$LINT" "$tmpdir/any.md" 2>&1)" || rc=$?
  if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "skipped"; then
    pass "SPEC_LINT=off skips validation"
  else
    fail "SPEC_LINT=off" "rc=$rc out='$out'"
  fi
}

test_template_passes() {
  run_lint "$PROJECT_ROOT/knowledge/spec-template.md"
  if [ "$LINT_RC" -eq 0 ] && [ "$LINT_OUT" = "spec-lint: OK" ]; then
    pass "knowledge/spec-template.md passes spec-lint"
  else
    fail "template" "rc=$LINT_RC out='$LINT_OUT' err='$LINT_ERR'"
  fi
}

test_run_task_bad_spec() {
  # run-task.sh should reject a bad spec before creating any worktree
  write_spec bad.md "
## Goal
Incomplete spec."

  # Use a unique name to avoid collisions
  local name="spec-lint-test-$$"

  # Copy required scripts into a temp repo (minimal setup for run-task.sh)
  local repo="$tmpdir/repo"
  mkdir -p "$repo/scripts"
  cp "$PROJECT_ROOT/scripts/spec-lint.sh" "$repo/scripts/spec-lint.sh"
  cp "$PROJECT_ROOT/scripts/junie-task.sh" "$repo/scripts/junie-task.sh"
  cp "$PROJECT_ROOT/scripts/kilo-task.sh" "$repo/scripts/kilo-task.sh"
  cp "$PROJECT_ROOT/scripts/worktree.sh" "$repo/scripts/worktree.sh"
  cp "$RUN_TASK" "$repo/scripts/run-task.sh"
  mkdir -p "$repo/tmp/runs"

  # Write the bad spec in the repo
  cp "$tmpdir/bad.md" "$repo/spec.md"

  local rc=0 out
  cd "$repo"
  out="$(bash scripts/run-task.sh "$name" --file spec.md 2>&1)" || rc=$?

  # Should exit 1 (lint failure)
  if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q "spec-lint:"; then
    pass "run-task.sh rejects bad spec (exit 1)"
  else
    fail "run-task bad spec" "rc=$rc out='$out'"
  fi

  # No worktree directory should be created
  if [ ! -d "tmp/worktrees/$name" ]; then
    pass "run-task.sh creates no worktree for bad spec"
  else
    fail "run-task worktree" "tmp/worktrees/$name exists"
  fi

  cd "$PROJECT_ROOT"
}

# ── main ─────────────────────────────────────────────────────────────────

echo "=== spec-lint tests ==="

test_valid_spec
test_missing_goal
test_missing_files
test_missing_tests
test_missing_acceptance
test_missing_live_check
test_empty_section
test_zero_files
test_nine_files
test_too_many_lines
test_no_command
test_multiple_violations
test_no_arg
test_missing_file
test_spec_lint_off
test_template_passes
test_run_task_bad_spec

echo ""
echo "=== Results: $passed/$total passed, $failed failed ==="

if [ "$failed" -gt 0 ]; then
  exit 1
fi
exit 0
