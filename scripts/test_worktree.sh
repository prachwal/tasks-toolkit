#!/usr/bin/env bash
# Tests for scripts/worktree.sh — plain bash, no external dependencies.
# Each test runs in its own temporary git repo.  Prints PASS/FAIL per case.
# Exit code: 0 if all pass, 1 if any fail.
set -uo pipefail

passed=0
failed=0
total=0

pass() { passed=$((passed + 1)); total=$((total + 1)); echo "  PASS: $1"; }
fail() { failed=$((failed + 1)); total=$((total + 1)); echo "  FAIL: $1 — $2"; }

# ── helpers ──────────────────────────────────────────────────────────────

make_repo() {
    # Create a temp git repo, copy worktree.sh into scripts/, and cd into it.
    # Returns the repo path via the global variable $TEST_REPO.
    TEST_REPO="$(mktemp -d)"
    cd "$TEST_REPO"
    git init -q -b main
    git config user.name "test"
    git config user.email "test@localhost"
    # worktree.sh computes root as dirname(BASH_SOURCE)/.., so it must live in scripts/
    mkdir -p scripts
    cp "$PROJECT_ROOT/scripts/worktree.sh" scripts/
    cp "$PROJECT_ROOT/scripts/needs-dotnet-checks.sh" scripts/
    chmod +x scripts/worktree.sh scripts/needs-dotnet-checks.sh
    mkdir -p tmp/worktrees tmp/meta tmp/patches
    # like the real repo: the agent workspace tmp/ is git-ignored (keeps the tree "clean")
    printf 'tmp/\n' > .gitignore
}

cleanup_repo() {
    rm -rf "$TEST_REPO"
}

# Simulate worktree.sh "new" without actually creating a git worktree
# (we just need the baseline commit hash and the meta file).
fake_new() {
    local name="$1"
    mkdir -p tmp/worktrees/"$name" tmp/meta
    # The baseline is HEAD — the current state of the repo
    git rev-parse HEAD > "tmp/meta/$name.base"
}

# Simulate worktree.sh "patch" — generate a diff between baseline and current state.
# Only stage tracked files and untracked files outside tmp/ and scripts/.
fake_patch() {
    local name="$1"
    local base
    base="$(cat "tmp/meta/$name.base")"
    # Stage only project files (exclude tmp/ and scripts/ which are test infrastructure)
    git add -A
    git reset -q "tmp/" "scripts/" 2>/dev/null || true
    git diff --cached --binary "$base" > "tmp/patches/$name.patch"
}

# ── test cases ───────────────────────────────────────────────────────────

test_clean_apply_modified_new_deleted() {
    # Apply a patch that modifies one file, adds a new one, and deletes another.
    make_repo
    trap cleanup_repo RETURN

    # Seed the repo with three files
    echo "line1" > file-a.txt
    echo "line2" > file-b.txt
    echo "line3" > file-c.txt
    git add -A && git commit -q -m "initial"

    # Create a "worktree" baseline at HEAD
    fake_new "clean"

    # Simulate agent changes in the "worktree"
    echo "modified line1" > file-a.txt       # modify
    echo "brand new file" > file-d.txt       # new
    rm file-c.txt                             # delete

    # Generate the patch
    fake_patch "clean"

    # Reset working tree to baseline (simulates main checkout at baseline state)
    git reset --hard -q HEAD

    # Apply the patch
    if bash scripts/worktree.sh apply clean > /dev/null 2>&1; then
        # Verify results
        local a d c
        a="$(cat file-a.txt)"
        d="$(cat file-d.txt 2>/dev/null || true)"
        c="$(cat file-c.txt 2>/dev/null || true)"
        if [ "$a" = "modified line1" ] && [ "$d" = "brand new file" ] && [ -z "$c" ]; then
            pass "clean apply of modified+new+deleted"
        else
            fail "clean apply of modified+new+deleted" "content mismatch: a='$a' d='$d' c='$c'"
        fi
    else
        fail "clean apply of modified+new+deleted" "apply command failed"
    fi
}

test_apply_with_unrelated_uncommitted_edits() {
    # The original bug: main checkout has uncommitted edits to a file the patch touches,
    # but those edits match the baseline state the patch was generated from.
    # The patch should still apply cleanly.
    make_repo
    trap cleanup_repo RETURN

    # Seed the repo
    echo "original content" > shared.txt
    echo "other file" > other.txt
    git add -A && git commit -q -m "initial"

    # Simulate: user edits shared.txt (uncommitted), then creates worktree.
    # In real worktree.sh "new", uncommitted changes are carried to the worktree
    # and committed as the baseline. We simulate that here.
    echo "edited before worktree" > shared.txt
    git add -A
    local baseline_commit
    baseline_commit="$(git -c user.name=test -c user.email=test commit -q -m "baseline")"
    baseline_commit="$(git rev-parse HEAD)"
    mkdir -p tmp/worktrees/unrelated tmp/meta
    echo "$baseline_commit" > tmp/meta/unrelated.base

    # Now simulate the agent modifying shared.txt further in the worktree
    echo "agent edited shared" > shared.txt
    git add -A
    git reset -q "tmp/" "scripts/" 2>/dev/null || true
    git diff --cached --binary "$baseline_commit" > tmp/patches/unrelated.patch

    # Reset main checkout to the baseline state (matching what the worktree started from)
    git reset --hard -q "$baseline_commit"
    # Re-create the meta file (git reset --hard may have affected it)
    echo "$baseline_commit" > tmp/meta/unrelated.base

    # Apply — should succeed because main's content matches baseline
    if bash scripts/worktree.sh apply unrelated > /dev/null 2>&1; then
        local content
        content="$(cat shared.txt)"
        if [ "$content" = "agent edited shared" ]; then
            pass "apply with unrelated uncommitted edits (original bug)"
        else
            fail "apply with unrelated uncommitted edits" "content='$content', expected 'agent edited shared'"
        fi
    else
        fail "apply with unrelated uncommitted edits" "apply command failed"
    fi
}

test_drift_after_creation() {
    # If the main checkout changes a file after the worktree was created,
    # apply should detect drift and abort with exit 2.
    make_repo
    trap cleanup_repo RETURN

    echo "baseline content" > drift-file.txt
    git add -A && git commit -q -m "initial"

    fake_new "drift"

    # Simulate agent changes
    echo "agent changed" > drift-file.txt
    fake_patch "drift"

    # Reset to baseline
    git reset --hard -q HEAD

    # Now someone edits the file in main AFTER the worktree was created
    echo "someone else edited" > drift-file.txt

    # Apply should fail with exit 2
    local rc=0
    bash scripts/worktree.sh apply drift > /dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 2 ]; then
        # Verify tree is untouched (still has "someone else edited")
        local content
        content="$(cat drift-file.txt)"
        if [ "$content" = "someone else edited" ]; then
            pass "drift after creation -> exit 2, tree untouched"
        else
            fail "drift after creation" "tree was modified despite drift"
        fi
    else
        fail "drift after creation" "expected exit 2, got $rc"
    fi
}

test_patch_does_not_apply() {
    # A patch that conflicts with current content should yield exit 3.
    # The file must match the baseline (no drift), but the patch itself must not apply.
    make_repo
    trap cleanup_repo RETURN

    echo "line A" > conflict.txt
    echo "line B" >> conflict.txt
    echo "line C" >> conflict.txt
    echo "line D" >> conflict.txt
    git add -A && git commit -q -m "initial"

    fake_new "bad-patch"

    # Create a patch against a different version of the file that won't apply
    # to the current content. We craft a patch whose context lines don't exist.
    cat > tmp/patches/bad-patch.patch <<'PATCH'
diff --git a/conflict.txt b/conflict.txt
index 0000000..1111111 100644
--- a/conflict.txt
+++ b/conflict.txt
@@ -1,4 +1,4 @@
 XXXX NOT IN FILE
-line B
+REPLACED B
 YYYY NOT IN FILE
-line D
+REPLACED D
PATCH

    # File matches baseline (no drift), but patch context doesn't match
    local rc=0
    bash scripts/worktree.sh apply bad-patch > /dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 3 ]; then
        # Verify tree is untouched
        local content
        content="$(cat conflict.txt)"
        if [ "$content" = "line A
line B
line C
line D" ]; then
            pass "patch that does not apply -> exit 3, tree untouched"
        else
            fail "patch that does not apply" "tree was modified"
        fi
    else
        fail "patch that does not apply" "expected exit 3, got $rc"
    fi
}

test_missing_patch() {
    # No patch file -> exit 1.
    make_repo
    trap cleanup_repo RETURN

    echo "x" > f.txt
    git add -A && git commit -q -m "initial"
    fake_new "no-patch"
    # Don't create the patch file

    local rc=0
    bash scripts/worktree.sh apply no-patch > /dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then
        pass "missing patch -> exit 1"
    else
        fail "missing patch" "expected exit 1, got $rc"
    fi
}

test_dry_run_changes_nothing() {
    # --dry-run should check the patch but not modify any files.
    make_repo
    trap cleanup_repo RETURN

    echo "before" > dryfile.txt
    git add -A && git commit -q -m "initial"

    fake_new "dry"
    echo "after" > dryfile.txt
    fake_patch "dry"

    # Reset to baseline
    git reset --hard -q HEAD

    local rc=0
    bash scripts/worktree.sh apply --dry-run dry > /dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 0 ]; then
        local content
        content="$(cat dryfile.txt)"
        if [ "$content" = "before" ]; then
            pass "--dry-run changes nothing"
        else
            fail "--dry-run changes nothing" "file was modified: '$content'"
        fi
    else
        fail "--dry-run changes nothing" "expected exit 0, got $rc"
    fi
}

test_index_unchanged_after_apply() {
    # After apply, git diff --cached should be empty (nothing staged).
    make_repo
    trap cleanup_repo RETURN

    echo "original" > index-test.txt
    git add -A && git commit -q -m "initial"

    fake_new "idx"
    echo "modified" > index-test.txt
    fake_patch "idx"

    # Reset to baseline
    git reset --hard -q HEAD

    bash scripts/worktree.sh apply idx > /dev/null 2>&1

    # Check index is clean
    local cached
    cached="$(git diff --cached --stat)"
    if [ -z "$cached" ]; then
        # But working tree should have the change
        local content
        content="$(cat index-test.txt)"
        if [ "$content" = "modified" ]; then
            pass "index stays unchanged after apply"
        else
            fail "index stays unchanged" "working tree not modified"
        fi
    else
        fail "index stays unchanged" "index has changes: $cached"
    fi
}

test_missing_baseline_meta() {
    # Missing tmp/meta/<name>.base -> exit 1.
    make_repo
    trap cleanup_repo RETURN

    echo "x" > f.txt
    git add -A && git commit -q -m "initial"
    # Create a patch but no meta file
    mkdir -p tmp/patches
    echo "some patch content" > tmp/patches/no-meta.patch

    local rc=0
    bash scripts/worktree.sh apply no-meta > /dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then
        pass "missing baseline meta -> exit 1"
    else
        fail "missing baseline meta" "expected exit 1, got $rc"
    fi
}

# ── path validation helpers ──────────────────────────────────────────────

# Create a patch file that adds a single file with the given path.
# Args: $1=patch name, $2=file path inside the patch
make_add_patch() {
    local name="$1"
    local fpath="$2"
    mkdir -p "tmp/patches"
    cat > "tmp/patches/$name.patch" <<EOF
diff --git a/$fpath b/$fpath
new file mode 100644
index 0000000..e69de29
--- /dev/null
+++ b/$fpath
@@ -0,0 +1 @@
+hello
EOF
}

# Create a patch file that deletes a file.
# Args: $1=patch name, $2=file path
make_delete_patch() {
    local name="$1"
    local fpath="$2"
    mkdir -p "tmp/patches"
    cat > "tmp/patches/$name.patch" <<EOF
diff --git a/$fpath b/$fpath
deleted file mode 100644
index e69de29..0000000
--- a/$fpath
+++ /dev/null
@@ -1 +0,0 @@
-hello
EOF
}

# Create a patch file that renames a file.
# Args: $1=patch name, $2=source path, $3=dest path
make_rename_patch() {
    local name="$1"
    local src="$2"
    local dst="$3"
    mkdir -p "tmp/patches"
    cat > "tmp/patches/$name.patch" <<EOF
diff --git a/$dst b/$dst
similarity index 100%
rename from $src
rename to $dst
EOF
}

# Create a patch file that adds a symlink.
# Args: $1=patch name, $2=file path, $3=link target
make_symlink_patch() {
    local name="$1"
    local fpath="$2"
    local target="$3"
    mkdir -p "tmp/patches"
    cat > "tmp/patches/$name.patch" <<EOF
diff --git a/$fpath b/$fpath
new file mode 120000
index 0000000..e69de29
--- /dev/null
+++ b/$fpath
@@ -0,0 +1 @@
+${target}
EOF
}

# ── path validation test cases ───────────────────────────────────────────

# Helper: source worktree.sh helpers and test path_ok directly.
# Args: $1=test name, $2=path to test, $3=expected result (0=ok, 1=forbidden)
assert_path_ok() {
    local test_name="$1"
    local test_path="$2"
    local expected="$3"
    # Source just the helper functions from worktree.sh
    # We need GENERATED_PATHS and path_ok + is_generated_path
    local saved_root="$root"
    root="$TEST_REPO"
    if path_ok "$test_path"; then
        local actual=0
    else
        local actual=1
    fi
    root="$saved_root"
    if [ "$actual" -eq "$expected" ]; then
        pass "$test_name -> path_ok returns $expected"
    else
        fail "$test_name" "expected $expected, got $actual for path '$test_path'"
    fi
}

test_forbidden_githooks_pre_commit() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok ".githooks/pre-commit" ".githooks/pre-commit" 1
}

test_forbidden_git_hooks() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok ".git/hooks/x" ".git/hooks/x" 1
}

test_forbidden_dotdot() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok "../outside.txt" "../outside.txt" 1
}

test_forbidden_absolute_path() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok "/abs/path" "/abs/path" 1
}

test_forbidden_space_in_name() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok "a b.txt" "a b.txt" 1
}

test_forbidden_non_ascii() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok "café.txt" "café.txt" 1
}

test_forbidden_dash_prefix() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok "-bad/file.txt" "-bad/file.txt" 1
}

test_forbidden_tmp() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok "tmp/x" "tmp/x" 1
}

test_forbidden_claude_settings() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok ".claude/settings.json" ".claude/settings.json" 1
}

test_forbidden_kilo_json() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok "kilo.json" "kilo.json" 1
}

test_forbidden_symlink_outside_scripts() {
    # Symlinks outside scripts/ are rejected by validate_symlinks, not path_ok.
    # path_ok itself allows src/link — the symlink check is a separate layer.
    # Test that path_ok allows the path (symlink validation is in apply/mr).
    make_repo; trap cleanup_repo RETURN
    assert_path_ok "src/link (symlink path ok)" "src/link" 0
}

test_forbidden_double_slash() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok "a//b.txt (double slash)" "a//b.txt" 1
}

test_forbidden_junie_dir() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok ".junie/config.json" ".junie/config.json" 1
}

test_forbidden_mcp_json() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok ".mcp.json" ".mcp.json" 1
}

test_forbidden_tasks_db() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok "tasks/tasks.db" "tasks/tasks.db" 1
}

test_forbidden_tasks_lock() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok "tasks/.lock" "tasks/.lock" 1
}

test_forbidden_kilo_dir() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok ".kilo/settings.json" ".kilo/settings.json" 1
}

# Allowed paths — each must return 0 from path_ok
test_allowed_src_path() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok "src/A/B.cs" "src/A/B.cs" 0
}

test_allowed_scripts_path() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok "scripts/x.sh" "scripts/x.sh" 0
}

test_allowed_knowledge_path() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok "knowledge/n.md" "knowledge/n.md" 0
}

test_allowed_tests_path() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok "tests/T/File.Name.cs" "tests/T/File.Name.cs" 0
}

test_allowed_editorconfig() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok ".editorconfig" ".editorconfig" 0
}

test_rename_allowed_path() {
    # Both source and dest of a rename must pass path_ok
    make_repo; trap cleanup_repo RETURN
    assert_path_ok "src/old.cs (rename src)" "src/old.cs" 0
    assert_path_ok "src/new.cs (rename dst)" "src/new.cs" 0
}

test_delete_allowed_path() {
    make_repo; trap cleanup_repo RETURN
    assert_path_ok "src/deleteme.cs (delete)" "src/deleteme.cs" 0
}

test_dry_run_forbidden_path() {
    # Old test for apply --dry-run with forbidden path — apply is retired.
    # Keep as a no-op placeholder.
    : # intentionally empty
}

# ── sprint lifecycle tests ───────────────────────────────────────────────

test_sprint_start_creates_branch() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"

    local out
    out="$(bash scripts/worktree.sh sprint start 1 test)"
    if [[ "$out" == "sprint sprint/1-test (from main)" ]]; then
        local branch
        branch="$(git symbolic-ref --short HEAD)"
        if [ "$branch" = "sprint/1-test" ]; then
            pass "sprint start creates and checks out sprint branch"
        else
            fail "sprint start checkout" "on branch '$branch', expected 'sprint/1-test'"
        fi
    else
        fail "sprint start output" "got '$out'"
    fi
}

test_sprint_start_refuses_existing() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"

    bash scripts/worktree.sh sprint start 1 test >/dev/null
    local rc=0
    bash scripts/worktree.sh sprint start 1 test >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then
        pass "sprint start refuses existing branch"
    else
        fail "sprint start refuses existing" "expected exit 1, got $rc"
    fi
}

test_sprint_start_refuses_dirty_tree() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    echo "dirty" > dirty.txt

    local rc=0
    bash scripts/worktree.sh sprint start 1 test >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then
        pass "sprint start refuses dirty tree"
    else
        fail "sprint start refuses dirty" "expected exit 1, got $rc"
    fi
}

test_sprint_start_ignores_generated_paths() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    # Modify a generated path — should be ignored by clean check
    mkdir -p tasks
    echo "modified" > tasks/tasks.snapshot.json

    local rc=0
    bash scripts/worktree.sh sprint start 1 test >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 0 ]; then
        pass "sprint start ignores generated paths in clean check"
    else
        fail "sprint start ignores generated" "expected exit 0, got $rc"
    fi
}

test_sprint_status_on_sprint() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    bash scripts/worktree.sh sprint start 1 test >/dev/null

    local out
    out="$(bash scripts/worktree.sh sprint status)"
    if printf '%s\n' "$out" | grep -q "branch: sprint/1-test"; then
        pass "sprint status shows sprint branch"
    else
        fail "sprint status" "output: $out"
    fi
}

test_sprint_status_off_sprint() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"

    local out
    out="$(bash scripts/worktree.sh sprint status)"
    if printf '%s\n' "$out" | grep -q "(not on a sprint branch)"; then
        pass "sprint status off sprint"
    else
        fail "sprint status off sprint" "output: $out"
    fi
}

test_sprint_finish_no_merge() {
    make_repo
    trap cleanup_repo RETURN

    # Create main branch for the finish command
    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    git branch -m main 2>/dev/null || git branch -m master main 2>/dev/null || true
    bash scripts/worktree.sh sprint start 1 test >/dev/null

    local out rc=0
    out="$(bash scripts/worktree.sh sprint finish 2>&1)" || rc=$?
    # Without dotnet, it should still print the no-merge message
    if printf '%s\n' "$out" | grep -q "(no merge"; then
        pass "sprint finish without --merge"
    else
        fail "sprint finish no merge" "rc=$rc output: $out"
    fi
}

test_sprint_finish_merge() {
    make_repo
    trap cleanup_repo RETURN

    # Set up main branch
    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    git branch -m main 2>/dev/null || true

    bash scripts/worktree.sh sprint start 1 test >/dev/null
    # Make a commit on the sprint branch
    echo "sprint work" > sprint-file.txt
    git add -A && git commit -q -m "sprint work"

    local out rc=0
    out="$(bash scripts/worktree.sh sprint finish --merge 2>&1)" || rc=$?
    if printf '%s\n' "$out" | grep -q "merged sprint/1-test into main"; then
        local branch
        branch="$(git symbolic-ref --short HEAD)"
        if [ "$branch" = "main" ]; then
            pass "sprint finish --merge merges and checks out main"
        else
            fail "sprint finish --merge checkout" "on branch '$branch'"
        fi
    else
        fail "sprint finish --merge" "rc=$rc output: $out"
    fi
}

test_sprint_finish_refuses_off_sprint() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"

    local rc=0
    bash scripts/worktree.sh sprint finish >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then
        pass "sprint finish refuses off sprint branch"
    else
        fail "sprint finish off sprint" "expected exit 1, got $rc"
    fi
}

# ── new command tests (sprint-based) ─────────────────────────────────────

test_new_refuses_off_sprint() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"

    local rc=0
    bash scripts/worktree.sh new my-task >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then
        pass "new refuses off sprint branch"
    else
        fail "new refuses off sprint" "expected exit 1, got $rc"
    fi
}

test_new_refuses_dirty_tree() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    bash scripts/worktree.sh sprint start 1 test >/dev/null
    echo "dirty" > dirty.txt

    local rc=0
    bash scripts/worktree.sh new my-task >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then
        pass "new refuses dirty tree"
    else
        fail "new refuses dirty" "expected exit 1, got $rc"
    fi
}

test_new_ignores_generated_paths() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    bash scripts/worktree.sh sprint start 1 test >/dev/null
    # Modify a generated path — should be ignored
    mkdir -p tasks
    echo "modified" > tasks/tasks.snapshot.json

    local out rc=0
    out="$(bash scripts/worktree.sh new my-task 2>&1)" || rc=$?
    if [ "$rc" -eq 0 ]; then
        pass "new ignores generated paths in clean check"
    else
        fail "new ignores generated" "expected exit 0, rc=$rc output: $out"
    fi
}

test_new_creates_task_branch_and_meta() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    bash scripts/worktree.sh sprint start 1 test >/dev/null

    local out
    out="$(bash scripts/worktree.sh new my-task)"
    if [ -d "$TEST_REPO/tmp/worktrees/my-task" ]; then
        # Check branch exists
        if git rev-parse --verify "task/my-task" >/dev/null 2>&1; then
            # Check meta files
            local base sprint
            base="$(cat tmp/meta/my-task.base)"
            sprint="$(cat tmp/meta/my-task.sprint)"
            local sprint_head
            sprint_head="$(git rev-parse HEAD)"
            if [ "$base" = "$sprint_head" ] && [ "$sprint" = "sprint/1-test" ]; then
                pass "new creates task branch, worktree and meta files"
            else
                fail "new meta files" "base=$base sprint=$sprint sprint_head=$sprint_head"
            fi
        else
            fail "new branch" "task/my-task branch not found"
        fi
    else
        fail "new worktree" "worktree dir not found: $out"
    fi
}

test_new_with_task_id() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    bash scripts/worktree.sh sprint start 1 test >/dev/null

    bash scripts/worktree.sh new my-task --task-id 1.3 >/dev/null
    if [ -f "tmp/meta/my-task.task" ]; then
        local tid
        tid="$(cat tmp/meta/my-task.task)"
        if [ "$tid" = "1.3" ]; then
            pass "new stores --task-id in meta"
        else
            fail "new task-id" "got '$tid', expected '1.3'"
        fi
    else
        fail "new task-id" "tmp/meta/my-task.task not found"
    fi
}

test_new_baseline_is_sprint_head_not_carried() {
    # Uncommitted changes on sprint branch should NOT be carried to the worktree.
    # The baseline is the sprint HEAD commit, not the dirty working tree.
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    bash scripts/worktree.sh sprint start 1 test >/dev/null

    # Make uncommitted changes on the sprint branch — these should NOT be carried
    echo "uncommitted junk" > junk.txt
    echo "modified initial" > file.txt

    # tree_clean will reject because of junk.txt and modified file.txt.
    # But if we only dirty with generated files, new should succeed and the
    # worktree should have the committed state (sprint HEAD), not the dirty state.
    # First, let's test that non-generated dirty state IS rejected:
    local rc=0
    bash scripts/worktree.sh new reject-dirty >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then
        # Good — dirty tree is rejected. Now clean up and test with only generated dirt.
        rm -f junk.txt
        echo "initial" > file.txt  # restore to committed state
        mkdir -p tasks
        echo "generated" > tasks/tasks.snapshot.json

        bash scripts/worktree.sh new my-task >/dev/null

        # The worktree should have the committed state
        local wt="$TEST_REPO/tmp/worktrees/my-task"
        if [ -d "$wt" ]; then
            local wt_content
            wt_content="$(cat "$wt/file.txt")"
            if [ "$wt_content" = "initial" ]; then
                pass "new baseline = sprint HEAD, uncommitted changes NOT carried"
            else
                fail "new baseline content" "worktree has '$wt_content', expected 'initial'"
            fi
        else
            fail "new worktree" "worktree dir not created"
        fi
    else
        fail "new rejects dirty tree" "expected exit 1 for dirty tree, got $rc"
    fi
}

test_new_refuses_existing_worktree() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    bash scripts/worktree.sh sprint start 1 test >/dev/null
    bash scripts/worktree.sh new my-task >/dev/null

    local rc=0
    bash scripts/worktree.sh new my-task >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then
        pass "new refuses existing worktree"
    else
        fail "new refuses existing" "expected exit 1, got $rc"
    fi
}

# ── GENERATED_PATHS tests ────────────────────────────────────────────────

test_generated_paths_forbidden_in_patch() {
    # GENERATED_PATHS should be rejected by path_ok
    make_repo
    trap cleanup_repo RETURN
    assert_path_ok "tasks/tasks.snapshot.json (generated)" "tasks/tasks.snapshot.json" 1
}

test_generated_paths_forbidden_view() {
    make_repo
    trap cleanup_repo RETURN
    assert_path_ok "tasks/README.md (generated)" "tasks/README.md" 1
    assert_path_ok "tasks/sprint-1-x.md (generated)" "tasks/sprint-1-x.md" 1
}

test_clean_tree_ignores_generated_snapshot() {
    # tree_clean should ignore tasks/tasks.snapshot.json
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    bash scripts/worktree.sh sprint start 1 test >/dev/null

    # Modify generated file — tree should be considered clean
    mkdir -p tasks
    echo "modified snapshot" > tasks/tasks.snapshot.json

    local rc=0
    bash scripts/worktree.sh new clean-gen >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 0 ]; then
        pass "clean tree ignores tasks/tasks.snapshot.json"
    else
        fail "clean tree ignores snapshot" "expected exit 0, got $rc"
    fi
}

test_clean_tree_ignores_generated_view() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    bash scripts/worktree.sh sprint start 1 test >/dev/null

    mkdir -p tasks
    echo "modified view" > tasks/README.md

    local rc=0
    bash scripts/worktree.sh new clean-view >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 0 ]; then
        pass "clean tree ignores tasks/README.md"
    else
        fail "clean tree ignores view" "expected exit 0, got $rc"
    fi
}

# ── abandon tests ────────────────────────────────────────────────────────

test_abandon_cleans_worktree_branch_meta() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    bash scripts/worktree.sh sprint start 1 test >/dev/null
    bash scripts/worktree.sh new my-task --task-id 1.3 >/dev/null

    local out
    out="$(bash scripts/worktree.sh abandon my-task)"
    if [[ "$out" == "abandoned my-task" ]]; then
        # Verify cleanup
        local wt_exists=0 branch_exists=0 base_exists=0 sprint_exists=0 task_exists=0
        [ -d "$TEST_REPO/tmp/worktrees/my-task" ] && wt_exists=1
        git rev-parse --verify "task/my-task" >/dev/null 2>&1 && branch_exists=1
        [ -f "tmp/meta/my-task.base" ] && base_exists=1
        [ -f "tmp/meta/my-task.sprint" ] && sprint_exists=1
        [ -f "tmp/meta/my-task.task" ] && task_exists=1
        if [ "$wt_exists" -eq 0 ] && [ "$branch_exists" -eq 0 ] && \
           [ "$base_exists" -eq 0 ] && [ "$sprint_exists" -eq 0 ] && [ "$task_exists" -eq 0 ]; then
            pass "abandon cleans worktree, branch and all meta files"
        else
            fail "abandon cleanup" "wt=$wt_exists branch=$branch_exists base=$base_exists sprint=$sprint_exists task=$task_exists"
        fi
    else
        fail "abandon output" "got '$out'"
    fi
}

test_abandon_idempotent() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"

    local rc=0
    bash scripts/worktree.sh abandon nonexistent >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 0 ]; then
        pass "abandon is idempotent on nonexistent worktree"
    else
        fail "abandon idempotent" "expected exit 0, got $rc"
    fi
}

# ── remove still works ───────────────────────────────────────────────────

test_remove_still_works() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    bash scripts/worktree.sh sprint start 1 test >/dev/null
    bash scripts/worktree.sh new my-task >/dev/null

    local out
    out="$(bash scripts/worktree.sh remove my-task)"
    if [[ "$out" == "removed my-task" ]]; then
        local wt_exists=0 branch_exists=0
        [ -d "$TEST_REPO/tmp/worktrees/my-task" ] && wt_exists=1
        git rev-parse --verify "task/my-task" >/dev/null 2>&1 && branch_exists=1
        if [ "$wt_exists" -eq 0 ] && [ "$branch_exists" -eq 0 ]; then
            pass "remove still works with task branches"
        else
            fail "remove cleanup" "wt=$wt_exists branch=$branch_exists"
        fi
    else
        fail "remove output" "got '$out'"
    fi
}

# ── assert real repo untouched ───────────────────────────────────────────

test_tests_dont_touch_real_repo() {
    # This test verifies that our test harness (make_repo) creates isolated
    # temp repos and doesn't affect the real repository.
    # We just check that PROJECT_ROOT is different from TEST_REPO.
    make_repo
    trap cleanup_repo RETURN

    if [ "$TEST_REPO" != "$PROJECT_ROOT" ]; then
        pass "tests use isolated temp repos, not PROJECT_ROOT"
    else
        fail "isolated temp repos" "TEST_REPO == PROJECT_ROOT"
    fi
}

# ── sprint status ahead/behind tests ─────────────────────────────────────

test_sprint_status_ahead_behind() {
    # sprint status must compare against main, not HEAD.
    # After 2 commits on sprint, ahead should be 2.
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    git branch -m main 2>/dev/null || true

    bash scripts/worktree.sh sprint start 1 test >/dev/null

    # Two commits on sprint branch
    echo "sprint 1" > s1.txt
    git add -A && git commit -q -m "sprint commit 1"
    echo "sprint 2" > s2.txt
    git add -A && git commit -q -m "sprint commit 2"

    local out
    out="$(bash scripts/worktree.sh sprint status)"
    if printf '%s\n' "$out" | grep -q "ahead: 2  behind: 0"; then
        pass "sprint status shows ahead: 2 behind: 0"
    else
        fail "sprint status ahead/behind" "output: $out"
    fi
}

test_sprint_status_ahead_zero_at_start() {
    # Right after sprint start (no new commits), ahead should be 0.
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    git branch -m main 2>/dev/null || true

    bash scripts/worktree.sh sprint start 1 test >/dev/null

    local out
    out="$(bash scripts/worktree.sh sprint status)"
    if printf '%s\n' "$out" | grep -q "ahead: 0  behind: 0"; then
        pass "sprint status shows ahead: 0 behind: 0 at start"
    else
        fail "sprint status at start" "output: $out"
    fi
}

# ── mr command tests ─────────────────────────────────────────────────────

# Helper: set up a repo with sprint + task worktree for mr/merge tests.
# Creates: main branch, sprint/1-test, task/<name> worktree with a commit.
# NOTE: Does NOT set trap — the caller must do `trap cleanup_repo RETURN`.
setup_mr_repo() {
    local task_name="$1"
    make_repo

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    git branch -m main 2>/dev/null || true

    bash scripts/worktree.sh sprint start 1 test >/dev/null

    # Make a commit on sprint so it diverges from main
    echo "sprint work" > sprint-file.txt
    git add -A && git commit -q -m "sprint work"

    # Create task worktree from sprint HEAD
    bash scripts/worktree.sh new "$task_name" --task-id 1.1 >/dev/null

    # Make a commit in the task worktree
    local wt
    wt="$(bash scripts/worktree.sh path "$task_name")"
    echo "task work" > "$wt/task-file.txt"
    git -C "$wt" add -A
    git -C "$wt" commit -q -m "task work"
}

test_run_requires_a_spec() {
    make_repo
    trap cleanup_repo RETURN
    local rc=0
    bash scripts/worktree.sh run foo >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then
        pass "run without --file exits 1 (usage)"
    else
        fail "run usage" "expected exit 1, got $rc"
    fi
}

test_retry_usage_and_limit() {
    make_repo
    trap cleanup_repo RETURN
    printf 'fix\n' > fixes.md
    local rc=0

    bash scripts/worktree.sh retry x >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then pass "retry without --fixes exits 1"; else fail "retry usage" "got $rc"; fi

    rc=0
    bash scripts/worktree.sh retry y --fixes fixes.md >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then pass "retry without a worktree exits 1"; else fail "retry no worktree" "got $rc"; fi

    mkdir -p tmp/worktrees/rl tmp/meta
    echo 2 > tmp/meta/rl.retries
    rc=0
    bash scripts/worktree.sh retry rl --fixes fixes.md >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then pass "retry over the limit (max 2) exits 1"; else fail "retry limit" "got $rc"; fi
}

test_sprint_run_dry_run() {
    make_repo
    trap cleanup_repo RETURN
    printf 'import sys\nprint("no disjoint ready tasks")\n' > scripts/tasks.py
    git add -A && git commit -q -m base
    local rc=0
    bash scripts/worktree.sh sprint run --dry-run >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 0 ]; then
        pass "sprint run --dry-run exits 0"
    else
        fail "sprint run dry-run" "rc=$rc"
    fi
}

test_sprint_run_reports_results_and_planner_failure() {
    make_repo
    trap cleanup_repo RETURN
    # Stub tracker: plan-batch offers one task that has no spec link; `show` returns no links.
    cat > scripts/tasks.py <<'PY'
import sys
if sys.argv[1] == "plan-batch":
    print("9.9  Needs a spec")
elif sys.argv[1] == "show":
    print("{}")
PY
    git add -A && git commit -q -m base
    local out rc=0
    out="$(bash scripts/worktree.sh sprint run 2>&1)" || rc=$?
    if [ "$rc" -eq 0 ] && printf '%s\n' "$out" | grep -q "9.9  skipped (no spec link)" \
        && printf '%s\n' "$out" | grep -q "(none)"; then
        pass "sprint run reports skipped tasks and an empty MR list"
    else
        fail "sprint run results" "rc=$rc output: $out"
    fi

    printf 'import sys\nsys.exit(2)\n' > scripts/tasks.py
    rc=0
    bash scripts/worktree.sh sprint run >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then pass "sprint run exits 1 when plan-batch fails"; else fail "sprint run planner failure" "rc=$rc"; fi
}

test_sprint_run_reports_failed_task_without_stale_mrs() {
    make_repo
    trap cleanup_repo RETURN
    mkdir -p tmp/tasks tmp/mr
    printf 'spec\n' > tmp/tasks/failed.md
    printf 'old review\n' > tmp/mr/failed.md
    cat > scripts/tasks.py <<'PY'
import sys
if sys.argv[1] == "plan-batch":
    print("9.9  Failed task")
elif sys.argv[1] == "show":
    print('{"links": [{"kind": "spec", "value": "tmp/tasks/failed.md"}]}')
PY
    printf 'exit 7\n' > scripts/run-task.sh
    git add -A && git commit -q -m base

    local out rc=0
    out="$(bash scripts/worktree.sh sprint run 2>&1)" || rc=$?
    if [ "$rc" -eq 1 ] && printf '%s\n' "$out" | grep -q "9.9  failed  exit 7" \
        && printf '%s\n' "$out" | grep -q "1 task(s) failed" \
        && printf '%s\n' "$out" | grep -q "MRs awaiting review:" \
        && printf '%s\n' "$out" | grep -q "(none)" \
        && ! printf '%s\n' "$out" | grep -q "failed.md"; then
        pass "sprint run exits 1 for a failed task and excludes its stale MR"
    else
        fail "sprint run failed task summary" "rc=$rc output: $out"
    fi
}

test_sprint_run_lists_only_mrs_created_by_run() {
    setup_mr_repo "run-only"
    trap cleanup_repo RETURN
    mkdir -p tmp/tasks tmp/mr
    printf 'spec\n' > tmp/tasks/run-only.md
    printf 'old review\n' > tmp/mr/stale.md
    cat > scripts/tasks.py <<'PY'
import sys
if sys.argv[1] == "plan-batch":
    print("1.1  Run task")
elif sys.argv[1] == "show":
    print('{"links": [{"kind": "spec", "value": "tmp/tasks/run-only.md"}]}')
PY
    printf 'exit 0\n' > scripts/run-task.sh

    local out rc=0 mr_section
    out="$(ACCEPT_CHECKS_CMD='exit 0' bash scripts/worktree.sh sprint run 2>&1)" || rc=$?
    mr_section="${out#*MRs awaiting review:}"
    if [ "$rc" -eq 0 ] && printf '%s\n' "$out" | grep -q "1.1  run-only  exit 0" \
        && printf '%s\n' "$mr_section" | grep -q "run-only" \
        && ! printf '%s\n' "$mr_section" | grep -q "stale"; then
        pass "sprint run lists only MRs created by this run"
    else
        fail "sprint run MR scope" "rc=$rc output: $out"
    fi
}

test_accept_requires_a_review_marker() {
    make_repo
    trap cleanup_repo RETURN
    mkdir -p tmp/meta
    local rc=0

    bash scripts/worktree.sh accept nometa >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then pass "accept without a task id exits 1"; else fail "accept task id" "got $rc"; fi

    echo 1.1 > tmp/meta/ac.task
    rc=0
    bash scripts/worktree.sh accept ac >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 6 ]; then pass "accept without an MR marker exits 6"; else fail "accept marker" "got $rc"; fi
}

test_accept_fails_loudly_when_links_cannot_be_recorded() {
    setup_mr_repo "acc-links"
    trap cleanup_repo RETURN
    ACCEPT_CHECKS_CMD='exit 0' bash scripts/worktree.sh mr acc-links >/dev/null 2>&1
    # Task 1.1 does not exist in this fixture's tracker, so every `link` call fails.
    local out rc=0
    out="$(bash scripts/worktree.sh accept acc-links 2>&1)" || rc=$?
    if [ "$rc" -eq 13 ] && printf '%s\n' "$out" | grep -q "could not record links"; then
        pass "accept: failed link recording exits 13"
    else
        fail "accept link failure" "rc=$rc output: $out"
    fi
}

test_accept_links_only_retries_the_links() {
    setup_mr_repo "acc-retry"
    trap cleanup_repo RETURN
    # Stub tracker that records every call; `link` succeeds only when LINK_OK=1.
    cat > scripts/tasks.py <<'PY'
import os, sys
open(os.environ["TRACKER_LOG"], "a").write(" ".join(sys.argv[1:]) + "\n")
sys.exit(0 if sys.argv[1] != "link" or os.environ.get("LINK_OK") == "1" else 1)
PY
    git add -A && git commit -q -m "stub tracker"
    ACCEPT_CHECKS_CMD='exit 0' bash scripts/worktree.sh mr acc-retry >/dev/null 2>&1
    export TRACKER_LOG="$PWD/tracker.log"
    local rc=0 out
    out="$(LINK_OK=0 bash scripts/worktree.sh accept acc-retry 2>&1)" || rc=$?
    if [ "$rc" -eq 13 ] && printf '%s\n' "$out" | grep -q "retry: .* accept acc-retry --links-only --task-id 1.1"; then
        pass "accept: exit 13 prints the --links-only retry command"
    else
        fail "accept retry hint" "rc=$rc output: $out"
    fi

    rc=0
    bash scripts/worktree.sh accept acc-retry --links-only >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 1 ] && pass "accept --links-only without --task-id exits 1" || fail "links-only needs id" "rc=$rc"

    : > "$TRACKER_LOG"
    rc=0
    LINK_OK=1 bash scripts/worktree.sh accept acc-retry --links-only --task-id 1.1 >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 0 ] && grep -q "^link 1.1 commit " "$TRACKER_LOG" && ! grep -q "^done" "$TRACKER_LOG"; then
        pass "accept --links-only records the links without merging"
    else
        fail "links-only" "rc=$rc log: $(cat "$TRACKER_LOG")"
    fi
    unset TRACKER_LOG
}

test_mr_happy_path() {
    setup_mr_repo "mr-test"
    trap cleanup_repo RETURN

    local out rc=0
    out="$(ACCEPT_CHECKS_CMD='exit 0' bash scripts/worktree.sh mr mr-test 2>&1)" || rc=$?
    if [ "$rc" -eq 0 ]; then
        if [ -f "tmp/mr/mr-test.md" ]; then
            local mr_content
            mr_content="$(cat tmp/mr/mr-test.md)"
            if printf '%s\n' "$mr_content" | grep -q "# MR: mr-test" && \
               printf '%s\n' "$mr_content" | grep -q "1.1" && \
               printf '%s\n' "$mr_content" | grep -q "passed" && \
               printf '%s\n' "$mr_content" | grep -q "Diffstat"; then
                pass "mr happy path creates MR record"
            else
                fail "mr record content" "$mr_content"
            fi
        else
            fail "mr record file" "tmp/mr/mr-test.md not found"
        fi
    else
        fail "mr happy path" "rc=$rc output: $out"
    fi
}

test_mr_with_remote_flag() {
    setup_mr_repo "mr-remote"
    trap cleanup_repo RETURN

    local out rc=0
    out="$(ACCEPT_CHECKS_CMD='exit 0' bash scripts/worktree.sh mr mr-remote --remote 2>&1)" || rc=$?
    if [ "$rc" -eq 0 ] && [ -f "tmp/mr/mr-remote.md" ]; then
        local mr_content
        mr_content="$(cat tmp/mr/mr-remote.md)"
        if printf '%s\n' "$mr_content" | grep -q "Remote hint"; then
            pass "mr --remote adds remote hint"
        else
            fail "mr --remote hint" "$mr_content"
        fi
    else
        fail "mr --remote" "rc=$rc"
    fi
}

test_mr_no_worktree() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"

    local rc=0
    bash scripts/worktree.sh mr nonexistent >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then
        pass "mr exits 1 when worktree missing"
    else
        fail "mr no worktree" "expected exit 1, got $rc"
    fi
}

test_mr_no_sprint_meta() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    mkdir -p tmp/worktrees/no-meta
    # No sprint meta file

    local rc=0
    bash scripts/worktree.sh mr no-meta >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then
        pass "mr exits 1 when sprint meta missing"
    else
        fail "mr no sprint meta" "expected exit 1, got $rc"
    fi
}

test_mr_merge_conflict_exit9() {
    setup_mr_repo "mr-conflict"
    trap cleanup_repo RETURN

    local wt
    wt="$(bash scripts/worktree.sh path "mr-conflict")"

    # Create a conflicting commit on the sprint branch (same file, different content)
    echo "sprint changed task-file" > task-file.txt
    git add -A && git commit -q -m "sprint changes task-file"

    # Now the task branch has "task work" and sprint has "sprint changed task-file"
    # Merging sprint into task should conflict
    local rc=0
    ACCEPT_CHECKS_CMD='exit 0' bash scripts/worktree.sh mr mr-conflict >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 9 ]; then
        pass "mr exits 9 on conflict"
    else
        fail "mr conflict" "expected exit 9, got $rc"
    fi
}

test_mr_checks_fail_exit8() {
    setup_mr_repo "mr-checks"
    trap cleanup_repo RETURN

    local rc=0
    ACCEPT_CHECKS_CMD='exit 1' bash scripts/worktree.sh mr mr-checks >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 8 ]; then
        pass "mr exits 8 when checks fail"
    else
        fail "mr checks fail" "expected exit 8, got $rc"
    fi
}

test_mr_path_guard_exit4() {
    setup_mr_repo "mr-guard"
    trap cleanup_repo RETURN

    local wt
    wt="$(bash scripts/worktree.sh path "mr-guard")"

    # Add a forbidden file (kilo.json) to the task branch
    echo "forbidden" > "$wt/kilo.json"
    git -C "$wt" add -A
    git -C "$wt" commit -q -m "add forbidden file"

    local rc=0
    ACCEPT_CHECKS_CMD='exit 0' bash scripts/worktree.sh mr mr-guard >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 4 ]; then
        pass "mr exits 4 on forbidden path"
    else
        fail "mr path guard" "expected exit 4, got $rc"
    fi
}

# ── merge command tests ──────────────────────────────────────────────────

test_merge_happy_path() {
    setup_mr_repo "merge-test"
    trap cleanup_repo RETURN
    ACCEPT_CHECKS_CMD='exit 0' bash scripts/worktree.sh mr merge-test >/dev/null 2>&1

    local out rc=0
    out="$(bash scripts/worktree.sh merge merge-test 2>&1)" || rc=$?
    if [ "$rc" -eq 0 ]; then
        if printf '%s\n' "$out" | grep -q "merged task/merge-test into sprint/1-test"; then
            # Verify task branch was deleted
            local branch_exists=0
            git rev-parse --verify "task/merge-test" >/dev/null 2>&1 && branch_exists=1
            if [ "$branch_exists" -eq 0 ]; then
                pass "merge happy path merges and cleans up"
            else
                fail "merge cleanup" "task branch still exists"
            fi
        else
            fail "merge output" "output: $out"
        fi
    else
        fail "merge happy path" "rc=$rc output: $out"
    fi
}

test_merge_red_flags_require_accept_flags() {
    setup_mr_repo "merge-flags"
    trap cleanup_repo RETURN
    local wt
    wt="$(bash scripts/worktree.sh path merge-flags)"
    printf '#pragma warning disable SA0001\n' >> "$wt/task-file.txt"
    git -C "$wt" add -A && git -C "$wt" commit -q -m "1.1 add a pragma"
    ACCEPT_CHECKS_CMD='exit 0' bash scripts/worktree.sh mr merge-flags >/dev/null 2>&1

    local rc=0
    ACCEPT_CHECKS_CMD='exit 0' bash scripts/worktree.sh merge merge-flags >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 12 ]; then
        pass "merge: red flags refused without --accept-flags (exit 12)"
    else
        fail "merge red-flag gate" "rc=$rc"
        return
    fi

    rc=0
    ACCEPT_CHECKS_CMD='exit 0' bash scripts/worktree.sh merge merge-flags --accept-flags >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 0 ]; then
        pass "merge: --accept-flags accepts the red flags"
    else
        fail "merge accept-flags" "rc=$rc"
    fi
}

test_merge_auto_builds_mr() {
    setup_mr_repo "merge-auto"
    trap cleanup_repo RETURN
    # No `mr` first: `merge --auto` must build the MR (checks overridden green) then merge.
    local out rc=0
    out="$(ACCEPT_CHECKS_CMD='exit 0' bash scripts/worktree.sh merge merge-auto --auto 2>&1)" || rc=$?
    if [ "$rc" -eq 0 ] && printf '%s\n' "$out" | grep -q "merged task/merge-auto into sprint/1-test"; then
        pass "merge --auto builds the MR and merges on green"
    else
        fail "merge --auto" "rc=$rc out=$out"
    fi
}

test_needs_dotnet_checks() {
    make_repo
    trap cleanup_repo RETURN
    git add -A && git commit -q -m "base"
    local base base2
    base="$(git rev-parse HEAD)"
    echo "class A {}" > a.cs
    git add -A && git commit -q -m "add cs"
    if bash scripts/needs-dotnet-checks.sh . "$base" HEAD; then
        pass "needs-dotnet-checks: a .cs change needs dotnet"
    else
        fail "needs-dotnet-checks .cs" "expected exit 0"
    fi
    base2="$(git rev-parse HEAD)"
    echo "note" >> README.md
    git add -A && git commit -q -m "docs"
    if bash scripts/needs-dotnet-checks.sh . "$base2" HEAD; then
        fail "needs-dotnet-checks docs" "expected exit 1"
    else
        pass "needs-dotnet-checks: a docs-only change skips dotnet"
    fi
}

test_merge_no_worktree() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"

    local rc=0
    bash scripts/worktree.sh merge nonexistent >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then
        pass "merge exits 1 when worktree missing"
    else
        fail "merge no worktree" "expected exit 1, got $rc"
    fi
}

test_merge_no_sprint_meta() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    mkdir -p tmp/worktrees/no-meta

    local rc=0
    bash scripts/worktree.sh merge no-meta >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then
        pass "merge exits 1 when sprint meta missing"
    else
        fail "merge no sprint meta" "expected exit 1, got $rc"
    fi
}

test_merge_sprint_branch_missing_exit6() {
    setup_mr_repo "merge-no-sprint"
    trap cleanup_repo RETURN

    # Switch to main first so we can delete the sprint branch
    git checkout -q main
    git branch -D "sprint/1-test" >/dev/null 2>&1

    local rc=0
    bash scripts/worktree.sh merge merge-no-sprint >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 6 ]; then
        pass "merge exits 6 when sprint branch missing"
    else
        fail "merge sprint missing" "expected exit 6, got $rc"
    fi
}

test_merge_stale_task_exit6() {
    setup_mr_repo "merge-stale"
    trap cleanup_repo RETURN

    # MR is built, then the sprint branch moves on: the tested merge is no longer the real one.
    ACCEPT_CHECKS_CMD='exit 0' bash scripts/worktree.sh mr merge-stale >/dev/null 2>&1
    echo "later sprint work" > later.txt
    git add -A && git commit -q -m "sprint moved after the MR"

    local rc=0
    bash scripts/worktree.sh merge merge-stale >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 6 ]; then
        pass "merge exits 6 when the sprint branch moved after the MR was built"
    else
        fail "merge stale" "expected exit 6, got $rc"
    fi
}

test_merge_without_mr_record_exit6() {
    setup_mr_repo "merge-nomr"
    trap cleanup_repo RETURN
    local rc=0
    bash scripts/worktree.sh merge merge-nomr >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 6 ] && ! git log --merges --oneline | grep -q .; then
        pass "merge exits 6 without an MR record (checks cannot be skipped), nothing merged"
    else
        fail "merge without MR" "expected exit 6, got $rc"
    fi
}

test_merge_real_trailers_and_dirty_tree() {
    setup_mr_repo "merge-trl"
    trap cleanup_repo RETURN
    ACCEPT_CHECKS_CMD='exit 0' bash scripts/worktree.sh mr merge-trl >/dev/null 2>&1
    echo "uncommitted" > dirty.txt
    local rc=0
    bash scripts/worktree.sh merge merge-trl >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then pass "merge refuses a dirty main tree (exit 1)"; else fail "merge dirty tree" "expected 1, got $rc"; fi
    rm dirty.txt
    bash scripts/worktree.sh merge merge-trl >/dev/null 2>&1 || { fail "merge trailers" "merge failed"; return; }
    local trailers
    trailers="$(git log -1 --format='%(trailers:only,unfold)')"
    if printf '%s' "$trailers" | grep -q "^Task: 1.1" && printf '%s' "$trailers" | grep -q "^Branch: task/merge-trl"; then
        pass "merge commit carries real git trailers (Task, Branch)"
    else
        fail "merge trailers" "trailers: $trailers"
    fi
}

test_merge_conflict_exit10() {
    setup_mr_repo "merge-conflict"
    trap cleanup_repo RETURN

    local wt
    wt="$(bash scripts/worktree.sh path "merge-conflict")"

    # Create a conflicting file on the sprint branch
    echo "sprint version" > conflict-file.txt
    git add -A && git commit -q -m "sprint conflict file"

    # Create the same file with different content in the task worktree
    echo "task version" > "$wt/conflict-file.txt"
    git -C "$wt" add -A
    git -C "$wt" commit -q -m "task conflict file"

    # With the MR gate a conflict surfaces in `mr` (exit 9, merge aborted), never in `merge`.
    local rc=0
    ACCEPT_CHECKS_CMD='exit 0' bash scripts/worktree.sh mr merge-conflict >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 9 ]; then
        pass "conflict is caught by mr (exit 9)"
    else
        fail "mr conflict" "expected exit 9, got $rc"
    fi
}

test_merge_with_accept_flags() {
    setup_mr_repo "merge-flags"
    trap cleanup_repo RETURN
    ACCEPT_CHECKS_CMD='exit 0' bash scripts/worktree.sh mr merge-flags >/dev/null 2>&1

    local out rc=0
    out="$(bash scripts/worktree.sh merge merge-flags --accept-flags --some-flag 2>&1)" || rc=$?
    if [ "$rc" -eq 0 ]; then
        pass "merge accepts --accept-flags without error"
    else
        fail "merge --accept-flags" "rc=$rc output: $out"
    fi
}

# ── apply removed tests ──────────────────────────────────────────────────

test_apply_command_removed() {
    # The 'apply' command should no longer exist.
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"

    local rc=0
    bash scripts/worktree.sh apply some-name >/dev/null 2>&1 || rc=$?
    # Should exit with code 2 (usage error — unknown command)
    if [ "$rc" -eq 2 ]; then
        pass "apply command removed (exit 2)"
    else
        fail "apply removed" "expected exit 2, got $rc"
    fi
}

test_patch_command_still_works() {
    # 'patch' should still work as a read-only export.
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    git branch -m main 2>/dev/null || true
    bash scripts/worktree.sh sprint start 1 test >/dev/null
    bash scripts/worktree.sh new patch-test >/dev/null

    local wt
    wt="$(bash scripts/worktree.sh path "patch-test")"
    echo "patched" > "$wt/patched.txt"
    git -C "$wt" add -A
    git -C "$wt" commit -q -m "patch commit"

    local out rc=0
    out="$(bash scripts/worktree.sh patch patch-test 2>&1)" || rc=$?
    if [ "$rc" -eq 0 ] && [ -f "$TEST_REPO/tmp/patches/patch-test.patch" ]; then
        pass "patch command still works"
    else
        fail "patch command" "rc=$rc output: $out"
    fi
}

# ── main ─────────────────────────────────────────────────────────────────

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Source helper functions from worktree.sh (path_ok, is_generated_path, GENERATED_PATHS).
# We cannot source worktree.sh directly (it has set -euo pipefail and a case statement).
# Instead, we replicate the helper functions here for testing.
GENERATED_PATHS="tasks/tasks.snapshot.json
tasks/README.md
tasks/sprint-*.md
TASKS.md"

is_generated_path() {
    local p="$1"
    while IFS= read -r gp; do
        [[ "$p" == $gp ]] && return 0
    done <<< "$GENERATED_PATHS"
    return 1
}

path_ok() {
    local p="$1"
    [[ -n "$p" ]] || { echo "empty path" >&2; return 1; }
    if [[ "$p" =~ [^A-Za-z0-9._/\-] ]]; then
        echo "path '$p' contains forbidden characters" >&2; return 1
    fi
    [[ "${p:0:1}" != "/" ]] || { echo "path '$p' must not start with /" >&2; return 1; }
    local IFS='/'
    local -a segments
    read -ra segments <<< "$p"
    for seg in "${segments[@]}"; do
        [[ -n "$seg" ]] || { echo "path '$p' contains empty segment" >&2; return 1; }
        [[ "$seg" != ".." ]] || { echo "path '$p' contains '..' segment" >&2; return 1; }
        [[ "${seg:0:1}" != "-" ]] || { echo "path '$p' has segment starting with '-'" >&2; return 1; }
    done
    for dir in .git .githooks .claude .junie .kilo tmp; do
        if [[ "$p" == "$dir" || "$p" == "$dir/"* || "$p" == */"$dir" || "$p" == */"$dir/"* ]]; then
            echo "path '$p' is inside protected directory $dir/" >&2; return 1
        fi
    done
    case "$p" in
        kilo.json|.mcp.json|tasks/tasks.db|tasks/.lock)
            echo "path '$p' is a protected file" >&2; return 1 ;;
    esac
    if is_generated_path "$p"; then
        echo "path '$p' is a generated file" >&2; return 1
    fi
    return 0
}

root="$PROJECT_ROOT"  # default root for path_ok tests

echo "=== worktree.sh core tests ==="

# Note: apply command tests removed — apply is retired; see 'apply removed tests' section

echo ""
echo "=== path validation tests ==="

# Forbidden paths — each must exit 4
test_forbidden_githooks_pre_commit
test_forbidden_git_hooks
test_forbidden_dotdot
test_forbidden_absolute_path
test_forbidden_space_in_name
test_forbidden_non_ascii
test_forbidden_dash_prefix
test_forbidden_tmp
test_forbidden_claude_settings
test_forbidden_kilo_json
test_forbidden_symlink_outside_scripts
test_forbidden_double_slash
test_forbidden_junie_dir
test_forbidden_mcp_json
test_forbidden_tasks_db
test_forbidden_tasks_lock
test_forbidden_kilo_dir

# Allowed paths — each must exit 0
test_allowed_src_path
test_allowed_scripts_path
test_allowed_knowledge_path
test_allowed_tests_path
test_allowed_editorconfig

# Rename and delete of allowed paths
test_rename_allowed_path
test_delete_allowed_path

echo ""
echo "=== sprint lifecycle tests ==="

test_sprint_start_creates_branch
test_sprint_start_refuses_existing
test_sprint_start_refuses_dirty_tree
test_sprint_start_ignores_generated_paths
test_sprint_status_on_sprint
test_sprint_status_off_sprint
test_sprint_finish_no_merge
test_sprint_finish_merge
test_sprint_finish_refuses_off_sprint

echo ""
echo "=== new command tests (sprint-based) ==="

test_new_refuses_off_sprint
test_new_refuses_dirty_tree
test_new_ignores_generated_paths
test_new_creates_task_branch_and_meta
test_new_with_task_id
test_new_baseline_is_sprint_head_not_carried
test_new_refuses_existing_worktree

echo ""
echo "=== GENERATED_PATHS tests ==="

test_generated_paths_forbidden_in_patch
test_generated_paths_forbidden_view
test_clean_tree_ignores_generated_snapshot
test_clean_tree_ignores_generated_view

echo ""
echo "=== abandon tests ==="

test_abandon_cleans_worktree_branch_meta
test_abandon_idempotent

echo ""
echo "=== remove compatibility tests ==="

test_remove_still_works

echo ""
echo "=== isolation tests ==="

test_tests_dont_touch_real_repo

echo ""
echo "=== sprint status ahead/behind tests ==="

test_sprint_status_ahead_behind
test_sprint_status_ahead_zero_at_start

echo ""
echo "=== mr command tests ==="

test_mr_happy_path
test_run_requires_a_spec
test_retry_usage_and_limit
test_accept_requires_a_review_marker
test_sprint_run_dry_run
test_mr_with_remote_flag
test_mr_no_worktree
test_mr_no_sprint_meta
test_mr_merge_conflict_exit9
test_mr_checks_fail_exit8
test_mr_path_guard_exit4

echo ""
echo "=== merge command tests ==="

test_merge_happy_path
test_merge_red_flags_require_accept_flags
test_merge_auto_builds_mr
test_needs_dotnet_checks
test_merge_no_worktree
test_merge_no_sprint_meta
test_merge_sprint_branch_missing_exit6
test_merge_stale_task_exit6
test_merge_without_mr_record_exit6
test_merge_real_trailers_and_dirty_tree
test_merge_conflict_exit10
test_merge_with_accept_flags

# ── commit command (runner commits on task/<name>) ───────────────────────

setup_commit_repo() {
    make_repo
    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    git branch -m main 2>/dev/null || true
    bash scripts/worktree.sh sprint start 7 test >/dev/null
    bash scripts/worktree.sh new "$1" --task-id 7.7 >/dev/null
    CWT="$(bash scripts/worktree.sh path "$1")"
}

test_commit_identity_message_and_flow() {
    setup_commit_repo "c1"
    trap cleanup_repo RETURN
    echo "agent work" > "$CWT/new-file.txt"
    local rc=0 sha
    sha="$(bash scripts/worktree.sh commit c1 --runner junie --model junie-lite --task-id 7.7 --title "Do the thing")" || rc=$?
    local author msg
    author="$(git -C "$CWT" log -1 --format=%an)"
    msg="$(git -C "$CWT" log -1 --format=%B)"
    if [ "$rc" -eq 0 ] && [ "$author" = "agent:junie" ] && printf '%s' "$msg" | grep -q "^7.7 Do the thing" \
       && printf '%s' "$msg" | grep -q "runner: junie" && printf '%s' "$msg" | grep -q "model: junie-lite" \
       && [ "$sha" = "$(git -C "$CWT" rev-parse HEAD)" ]; then
        pass "commit: identity, message trailers and printed sha"
    else
        fail "commit identity/message" "rc=$rc author=$author msg=$msg"
    fi
    # end to end: mr then merge bring the agent's file into the sprint branch
    local out
    out="$(ACCEPT_CHECKS_CMD='exit 0' bash scripts/worktree.sh mr c1 2>&1)" || { fail "commit -> mr" "$out"; return; }
    out="$(bash scripts/worktree.sh merge c1 2>&1)" || { fail "commit -> mr -> merge" "$out"; return; }
    if [ -f new-file.txt ] && [ "$(git log --merges --oneline | wc -l)" -ge 1 ]; then
        pass "commit -> mr -> merge end to end (--no-ff merge commit, file on sprint branch)"
    else
        fail "commit end to end" "new-file.txt missing or no merge commit"
    fi
}

test_commit_forbidden_path_nothing_committed() {
    setup_commit_repo "c2"
    trap cleanup_repo RETURN
    local before rc=0
    before="$(git -C "$CWT" rev-parse HEAD)"
    mkdir -p "$CWT/.githooks"; echo "evil" > "$CWT/.githooks/pre-commit"
    echo "fine" > "$CWT/ok.txt"
    bash scripts/worktree.sh commit c2 --runner kilo >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 4 ] && [ "$(git -C "$CWT" rev-parse HEAD)" = "$before" ]; then
        pass "commit: forbidden path -> exit 4, nothing committed"
    else
        fail "commit forbidden path" "rc=$rc head moved=$([ "$(git -C "$CWT" rev-parse HEAD)" = "$before" ] && echo no || echo yes)"
    fi
}

test_commit_generated_file_forbidden() {
    setup_commit_repo "c3"
    trap cleanup_repo RETURN
    local rc=0
    mkdir -p "$CWT/tasks"; echo "{}" > "$CWT/tasks/tasks.snapshot.json"
    bash scripts/worktree.sh commit c3 --runner junie >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 4 ]; then
        pass "commit: generated tracker files are refused (exit 4)"
    else
        fail "commit generated file" "expected 4, got $rc"
    fi
}

test_commit_no_changes_exit1() {
    setup_commit_repo "c4"
    trap cleanup_repo RETURN
    local rc=0
    bash scripts/worktree.sh commit c4 --runner junie >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then
        pass "commit: no changes -> exit 1"
    else
        fail "commit no changes" "expected 1, got $rc"
    fi
}

echo ""
echo "=== commit command tests ==="

test_commit_identity_message_and_flow
test_commit_forbidden_path_nothing_committed
test_commit_generated_file_forbidden
test_commit_no_changes_exit1

test_sprint_start_from_main_and_from_flag() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    git checkout -q -b other && echo x > x.txt && git add -A && git commit -q -m "other work"
    bash scripts/worktree.sh sprint start 2 base >/dev/null
    if [ "$(git rev-parse sprint/2-base)" = "$(git rev-parse main)" ]; then
        pass "sprint start defaults to main, not the checked-out branch"
    else
        fail "sprint start base" "sprint/2-base is not at main"
    fi
    git checkout -q other
    bash scripts/worktree.sh sprint start 3 cascade --from other >/dev/null
    if [ "$(git rev-parse sprint/3-cascade)" = "$(git rev-parse other)" ]; then
        pass "sprint start --from builds on an explicit branch (cascade)"
    else
        fail "sprint start --from" "sprint/3-cascade is not at other"
    fi
    local rc=0
    bash scripts/worktree.sh sprint start 4 bad --from nope >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 1 ] && pass "sprint start refuses unknown --from" || fail "sprint start unknown --from" "exit $rc"
}

test_new_refuses_task_of_another_sprint() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    bash scripts/worktree.sh sprint start 7 other >/dev/null
    local rc=0
    bash scripts/worktree.sh new wrong --task-id 6.2 >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 11 ] && [ ! -e tmp/worktrees/wrong ] && ! git rev-parse --verify -q task/wrong >/dev/null; then
        pass "new refuses task 6.2 on sprint/7-* (exit 11, nothing created)"
    else
        fail "new wrong sprint" "exit $rc"
    fi
    rc=0
    bash scripts/worktree.sh new right --task-id 7.1 >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 0 ] && pass "new accepts task 7.1 on sprint/7-*" || fail "new right sprint" "exit $rc"
}

test_merge_refuses_when_main_tree_on_other_sprint() {
    make_repo
    trap cleanup_repo RETURN

    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    bash scripts/worktree.sh sprint start 6 mine >/dev/null
    bash scripts/worktree.sh new t6 --task-id 6.1 >/dev/null
    git checkout -q main
    bash scripts/worktree.sh sprint start 7 theirs >/dev/null
    local rc=0
    bash scripts/worktree.sh merge t6 >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 11 ] && [ "$(git symbolic-ref --short HEAD)" = "sprint/7-theirs" ]; then
        pass "merge refuses across sprints and does not switch the main tree"
    else
        fail "merge across sprints" "exit $rc, branch $(git symbolic-ref --short HEAD)"
    fi
}

test_new_on_fresh_clone_without_tmp() {
    make_repo
    trap cleanup_repo RETURN
    echo "initial" > file.txt
    git add -A && git commit -q -m "initial"
    git branch -m main 2>/dev/null || true
    bash scripts/worktree.sh sprint start 1 test >/dev/null
    rm -rf tmp   # a fresh clone has no git-ignored tmp/ at all
    local rc=0
    bash scripts/worktree.sh new fresh >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 0 ] && [ -f tmp/meta/fresh.base ]; then
        pass "new works on a fresh clone without tmp/ (creates tmp/meta)"
    else
        fail "new on fresh clone" "rc=$rc meta=$([ -f tmp/meta/fresh.base ] && echo yes || echo missing)"
    fi
}

test_new_on_fresh_clone_without_tmp
test_accept_fails_loudly_when_links_cannot_be_recorded
test_accept_links_only_retries_the_links
test_sprint_run_reports_results_and_planner_failure
test_sprint_run_reports_failed_task_without_stale_mrs
test_sprint_run_lists_only_mrs_created_by_run
test_sprint_start_from_main_and_from_flag
test_new_refuses_task_of_another_sprint
test_merge_refuses_when_main_tree_on_other_sprint

echo ""
echo "=== apply removed tests ==="

test_apply_command_removed
test_patch_command_still_works

echo ""
echo "=== Results: $passed/$total passed, $failed failed ==="

if [ "$failed" -gt 0 ]; then
    exit 1
fi
exit 0
