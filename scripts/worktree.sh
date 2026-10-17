#!/usr/bin/env bash
# Isolated git worktrees for delegated agent work, kept in the git-ignored tmp/ folder.
#
# Sprint branch workflow (branch-per-task MR flow):
#   sprint start <n> <slug> [--from <branch>]
#                                    create sprint/<n>-<slug> from main (or --from) and check it out
#   sprint status                    print sprint branch info, open task branches/worktrees
#   sprint finish [--merge]          run Rules checks; with --merge, merge sprint into main (--no-ff)
#   new <name> [--task-id <id>]      create task/<name> branch + worktree from sprint HEAD
#   path <name>                      print the worktree path
#   list                             list worktrees
#   patch <name>                     export diff since baseline to tmp/patches/<name>.patch
#   commit <name> [--runner R --model M --task-id ID --title T]
#                                    guard paths and commit the worktree on task/<name> (exit 4 forbidden, 1 no changes)
#   mr <name> [--remote]             merge sprint into task branch, run checks, write MR record
#   merge <name> [--accept-flags]    merge task branch into sprint (--no-ff) with trailers
#   accept <name> [--links-only --task-id ID]
#                                    merge, then record commit/patch/runner/model links (exit 13: links failed;
#                                    --links-only retries just the links after the merge)
#   remove <name>                    delete the worktree, its branch and its metadata
#   abandon <name>                   remove worktree + branch + meta (same as remove)
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

# ── generated files ──────────────────────────────────────────────────────
# These files are rewritten by the tracker on every change, so the main tree
# is always "dirty" with respect to them.  They are ignored by clean-tree
# checks but forbidden in task branches (agents must never commit them).
GENERATED_PATHS="tasks/tasks.snapshot.json
tasks/README.md
tasks/sprint-*.md
TASKS.md"

# Check if a path is a generated file (ignored in clean-tree checks). Entries may be glob patterns.
is_generated_path() {
    local p="$1"
    while IFS= read -r gp; do
        [[ "$p" == $gp ]] && return 0
    done <<< "$GENERATED_PATHS"
    return 1
}

# ── helpers ──────────────────────────────────────────────────────────────

name_ok() { [[ "${1:-}" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "invalid name '${1:-}' (use a-z, 0-9, -)" >&2; exit 2; }; }
wt_path() { echo "$root/tmp/worktrees/$1"; }
base_file() { echo "$root/tmp/meta/$1.base"; }
sprint_file() { echo "$root/tmp/meta/$1.sprint"; }
task_file() { echo "$root/tmp/meta/$1.task"; }
retry_file() { echo "$root/tmp/meta/$1.retries"; }
fixes_file() { echo "$root/tmp/meta/$1.fixes.md"; }

# tree_clean — check if the working tree is clean, ignoring GENERATED_PATHS.
# Returns 0 if clean, 1 if dirty.  Prints dirty files to stderr.
tree_clean() {
    local dirty=0
    # Check staged changes
    while IFS= read -r line; do
        local fpath="${line#* }"
        if ! is_generated_path "$fpath"; then
            echo "  staged: $fpath" >&2
            dirty=1
        fi
    done < <(git diff --cached --name-only 2>/dev/null || true)
    # Check unstaged changes
    while IFS= read -r line; do
        local fpath="${line#* }"
        if ! is_generated_path "$fpath"; then
            echo "  modified: $fpath" >&2
            dirty=1
        fi
    done < <(git diff --name-only 2>/dev/null || true)
    # Check untracked files (excluding gitignore'd dirs)
    while IFS= read -r fpath; do
        [ -z "$fpath" ] && continue
        if ! is_generated_path "$fpath"; then
            echo "  untracked: $fpath" >&2
            dirty=1
        fi
    done < <(git ls-files -o --exclude-standard 2>/dev/null || true)
    return "$dirty"
}

# current_branch — print the current branch name (short).
current_branch() {
    git symbolic-ref --short HEAD 2>/dev/null || true
}

# on_sprint_branch — check if current branch matches sprint/*
# Returns 0 if on a sprint branch, 1 otherwise.
on_sprint_branch() {
    local branch
    branch="$(current_branch)"
    [[ "$branch" == sprint/* ]]
}

# ── path validation ──────────────────────────────────────────────────────
# path_ok <path>
#
# Returns 0 if the path is safe for an agent patch, 1 otherwise.
# On failure prints the reason to stderr.
#
# Rules (the contract):
#   - Only characters [A-Za-z0-9._/-] are allowed; no spaces, quotes, backslashes,
#     newlines, control or non-ASCII characters.
#   - No leading '/', no '..' segment, no empty segment ('//'), no segment starting with '-'.
#   - Must not be inside .git/, .githooks/, .claude/, .junie/, .kilo/ or tmp/
#     and must not equal kilo.json or .mcp.json.
#   - Must not be tasks/tasks.db or tasks/.lock.
#   - Symlinks and executable mode changes are allowed only under scripts/.
#
# The function is pure bash (regex + case) so it can be unit-tested.
path_ok() {
    local p="$1"

    # Reject empty paths
    [[ -n "$p" ]] || { echo "empty path" >&2; return 1; }

    # Reject paths with characters outside [A-Za-z0-9._/-]
    if [[ "$p" =~ [^A-Za-z0-9._/\-] ]]; then
        echo "path '$p' contains forbidden characters (only A-Za-z0-9._/- allowed)" >&2
        return 1
    fi

    # Reject leading '/'
    [[ "${p:0:1}" != "/" ]] || { echo "path '$p' must not start with /" >&2; return 1; }

    # Split into segments and validate each
    local IFS='/'
    local -a segments
    read -ra segments <<< "$p"

    for seg in "${segments[@]}"; do
        # Reject empty segments (from '//')
        [[ -n "$seg" ]] || { echo "path '$p' contains empty segment (//)" >&2; return 1; }
        # Reject '..'
        [[ "$seg" != ".." ]] || { echo "path '$p' contains '..' segment" >&2; return 1; }
        # Reject segments starting with '-'
        [[ "${seg:0:1}" != "-" ]] || { echo "path '$p' has segment starting with '-': $seg" >&2; return 1; }
    done

    # Reject protected directories and files
    # Check if path is inside a protected directory
    for dir in .git .githooks .claude .junie .kilo tmp; do
        # Path equals the directory itself or is inside it
        if [[ "$p" == "$dir" || "$p" == "$dir/"* || "$p" == */"$dir" || "$p" == */"$dir/"* ]]; then
            echo "path '$p' is inside protected directory $dir/" >&2
            return 1
        fi
    done

    # Reject specific protected files
    case "$p" in
        kilo.json|.mcp.json|tasks/tasks.db|tasks/.lock)
            echo "path '$p' is a protected file" >&2
            return 1
            ;;
    esac

    # Reject generated paths (agents must never commit them)
    if is_generated_path "$p"; then
        echo "path '$p' is a generated file (agents must not commit it)" >&2
        return 1
    fi

    return 0
}

# Collect all paths touched by a patch (NUL-safe, handles renames and binary files).
# Sets the global array `patch_files`.
# Falls back to parsing "diff --git" lines when git apply --numstat cannot parse
# the patch (e.g. paths with // that git rejects), so validation still catches them.
collect_patch_files() {
    local patch_file="$1"
    patch_files=()

    # Use git apply --numstat -z for NUL-separated paths (handles binary files too)
    # Format: added\tdeleted\tname\0
    while IFS= read -r -d '' line; do
        # Extract the filename (after the last tab)
        local fpath="${line##*$'\t'}"
        [[ -n "$fpath" ]] && patch_files+=("$fpath")
    done < <(git apply --numstat -z "$patch_file" 2>/dev/null || true)

    # Also capture rename source paths via --summary
    # Output lines like: "rename from src/path" / "rename to dst/path"
    while IFS= read -r line; do
        if [[ "$line" == rename\ from\ * ]]; then
            local src="${line#rename from }"
            # Check if already in the list
            local found=0
            for existing in "${patch_files[@]+"${patch_files[@]}"}"; do
                [[ "$existing" == "$src" ]] && { found=1; break; }
            done
            [[ "$found" -eq 0 ]] && patch_files+=("$src")
        fi
    done < <(git apply --summary "$patch_file" 2>/dev/null || true)

    # Fallback: if git apply --numstat returned nothing (e.g. malformed paths like a//b.txt
    # that git rejects), parse "diff --git" lines directly so we can still validate them.
    if [ "${#patch_files[@]}" -eq 0 ]; then
        while IFS= read -r line; do
            if [[ "$line" == diff\ --git\ a/* ]]; then
                local fpath="${line#diff --git a/}"
                fpath="${fpath%% b/*}"
                [[ -n "$fpath" ]] && patch_files+=("$fpath")
            fi
        done < "$patch_file"
    fi
}

# Check whether the patch contains symlink additions or mode changes to symlinks.
# Returns 0 if symlinks found, 1 if none.
patch_has_symlinks() {
    local patch_file="$1"
    grep -q 'new file mode 120000\|old mode.*120000\|new mode.*120000' "$patch_file" 2>/dev/null
}

# Validate that symlink changes only affect scripts/ paths.
# Returns 0 if OK, 1 if forbidden.
validate_symlinks() {
    local patch_file="$1"

    if ! patch_has_symlinks "$patch_file"; then
        return 0
    fi

    # Symlinks exist — check they are only under scripts/
    local -a symlink_paths=()
    while IFS= read -r -d '' line; do
        local fpath="${line##*$'\t'}"
        [[ -n "$fpath" ]] && symlink_paths+=("$fpath")
    done < <(git apply --numstat -z "$patch_file" 2>/dev/null || true)

    # Also check rename summary for symlinks
    while IFS= read -r line; do
        if [[ "$line" == copy\ from\ * ]]; then
            local src="${line#copy from }"
            symlink_paths+=("$src")
        elif [[ "$line" == copy\ to\ * ]]; then
            local dst="${line#copy to }"
            symlink_paths+=("$dst")
        fi
    done < <(git apply --summary "$patch_file" 2>/dev/null || true)

    for sp in "${symlink_paths[@]+"${symlink_paths[@]}"}"; do
        if [[ "$sp" != scripts/ && "$sp" != scripts ]]; then
            echo "symlink change on non-scripts path '$sp'" >&2
            return 1
        fi
    done

    return 0
}

# ── review sheet hook ────────────────────────────────────────────────────
# Prints a markdown review sheet for the diff $sprint...$task and returns 1 when it finds red flags
# (used by `mr` to record them and by `merge` to require --accept-flags). Task 3.13.
review_sheet() {
    local sprint="$1" task="$2"
    local diff added changed forbidden n=0 flags=""
    diff="$(git -C "$wt" diff "${sprint}...${task}" 2>/dev/null || true)"
    added="$(printf '%s\n' "$diff" | grep -E '^\+' | grep -vE '^\+\+\+' || true)"
    if printf '%s\n' "$added" | grep -q '#pragma'; then
        flags+=$'\n- ⚠️ StyleCop suppression (#pragma) added'
        n=$((n + 1))
    fi
    if printf '%s\n' "$added" | grep -q 'System\.Security\.Cryptography'; then
        flags+=$'\n- ⚠️ System.Security.Cryptography referenced in the patch (forbidden in production)'
        n=$((n + 1))
    fi
    if printf '%s\n' "$added" | grep -Eq '\b(TODO|FIXME)\b'; then
        flags+=$'\n- ⚠️ TODO/FIXME left in the patch'
        n=$((n + 1))
    fi
    changed="$(git -C "$wt" diff --numstat "${sprint}...${task}" 2>/dev/null | awk '{a+=$1; d+=$2} END {print a+d+0}')"
    if [ "${changed:-0}" -gt 300 ]; then
        flags+=$'\n- ⚠️ large change: '"${changed}"' lines (spec target <= 300)'
        n=$((n + 1))
    fi
    forbidden="$(git -C "$wt" diff --name-only "${sprint}...${task}" 2>/dev/null \
        | grep -E '^(tasks/tasks\.snapshot\.json|tasks/README\.md|tasks/sprint-[^/]*\.md|TASKS\.md)$' || true)"
    if [ -n "$forbidden" ]; then
        flags+=$'\n- ⚠️ generated tracker file changed: '"$(printf '%s' "$forbidden" | tr '\n' ' ')"
        n=$((n + 1))
    fi

    echo "## Review sheet"
    echo ""
    echo "Red flags: $n"
    [ -n "$flags" ] && printf '%s\n' "$flags"
    echo ""
    echo "Checklist:"
    echo "- [ ] Diff matches the task spec (files and behaviour)"
    echo "- [ ] No #pragma suppressions; XML docs on new production code"
    echo "- [ ] Results to stdout, logs to stderr"
    echo "- [ ] No new dependency outside Directory.Packages.props"
    echo "- [ ] format, build and tests green"
    [ "$n" -gt 0 ] && return 1
    return 0
}

# ── run checks (format, build, test) ─────────────────────────────────────
# Uses ACCEPT_CHECKS_CMD env var override when set (for worktree context).
# Returns 0 on success, 1 on failure.
run_checks() {
    local context="${1:-main}" base="${2:-}" head="${3:-HEAD}"
    if [ -n "${ACCEPT_CHECKS_CMD:-}" ]; then
        eval "$ACCEPT_CHECKS_CMD"
        return $?
    fi
    if [ -n "$base" ] && ! bash "$root/scripts/needs-dotnet-checks.sh" . "$base" "$head"; then
        # Conditional verification (6.6): docs/scripts-only diff, so the .NET checks add nothing.
        echo "== conditional verification (no .NET files changed): script tests =="
        if [ -f scripts/test_spec_lint.sh ]; then
            bash scripts/test_spec_lint.sh 2>&1 || return 1
        fi
        bash scripts/test_worktree.sh 2>&1 || return 1
        bash scripts/test_runners.sh 2>&1 || return 1
        python3 -m unittest discover -s scripts -p "test_*.py" 2>&1 || return 1
        return 0
    fi
    if [ -f VibeEmulator.slnx ]; then
        echo "== format =="
        dotnet format VibeEmulator.slnx --verify-no-changes 2>&1 || return 1
        echo "== build =="
        dotnet build VibeEmulator.slnx --no-incremental -warnaserror 2>&1 || return 1
        echo "== test =="
        dotnet test VibeEmulator.slnx --no-build 2>&1 || return 1
    else
        echo "(skipping dotnet checks — no VibeEmulator.slnx)"
    fi
    return 0
}

cmd="${1:-}"
name="${2:-}"

case "$cmd" in

  # ── sprint lifecycle ───────────────────────────────────────────────────
  sprint)
    subcmd="${2:-}"
    case "$subcmd" in
      start)
        sprint_n="${3:-}"
        sprint_slug="${4:-}"
        sprint_from="main"
        [ "${5:-}" != "--from" ] || sprint_from="${6:?--from needs a branch}"
        [ -n "$sprint_n" ] && [ -n "$sprint_slug" ] \
          || { echo "usage: $0 sprint start <n> <slug> [--from <branch>]" >&2; exit 1; }
        [[ "$sprint_n" =~ ^[0-9]+$ ]] || { echo "error: sprint number must be an integer, got '$sprint_n'" >&2; exit 1; }
        git rev-parse --verify -q "$sprint_from^{commit}" >/dev/null \
          || { echo "error: base branch '$sprint_from' does not exist" >&2; exit 1; }
        sprint_branch="sprint/${sprint_n}-${sprint_slug}"
        # Must have a clean tree (ignoring generated paths)
        if ! tree_clean; then
          echo "error: working tree is not clean (ignoring generated paths)" >&2
          exit 1
        fi
        # Refuse if branch already exists
        if git rev-parse --verify "$sprint_branch" >/dev/null 2>&1; then
          echo "error: sprint branch '$sprint_branch' already exists" >&2
          exit 1
        fi
        # One sprint = one branch; a sprint starts from main, or from an explicit --from branch when
        # it builds on a previous sprint that is not merged yet (cascade). Never from whatever is checked out.
        git branch "$sprint_branch" "$sprint_from"
        git checkout -q "$sprint_branch"
        echo "sprint $sprint_branch (from $sprint_from)"
        ;;
      run)
        # Batch driver: plan-batch -> worktree.sh run per task -> list MRs awaiting review.
        # Never merges and never pushes (human merge gate).
        shift 2  # drop 'sprint' and 'run'
        sprint_max="" sprint_dry=0
        while [ $# -gt 0 ]; do
          case "$1" in
            --max)     sprint_max="${2:?--max needs a value}"; shift 2 ;;
            --dry-run) sprint_dry=1; shift ;;
            *) echo "unknown option '$1'" >&2; exit 1 ;;
          esac
        done
        batch="$(python3 scripts/tasks.py plan-batch ${sprint_max:+--max "$sprint_max"})" \
          || { echo "error: plan-batch failed" >&2; exit 1; }
        if [ "$sprint_dry" -eq 1 ]; then
          printf '%s\n' "$batch"
          exit 0
        fi
        run_results="" run_mrs="" run_failed=0
        while IFS= read -r line; do
          tid="${line%% *}"  # first token of "<id>  <title>"
          [[ "$tid" =~ ^[0-9]+(\.[0-9]+)+$ ]] || continue  # skips "no disjoint ready tasks"
          task_spec="$(python3 scripts/tasks.py show "$tid" \
            | python3 -c 'import json,sys; d=json.load(sys.stdin); s=[l["value"] for l in d.get("links", []) if l["kind"]=="spec"]; print(s[-1] if s else "")')" \
            || task_spec=""
          if [ -z "$task_spec" ] || [ ! -f "$task_spec" ]; then
            echo "skip $tid (no spec link)" >&2
            run_results+="$tid  skipped (no spec link)"$'\n'
            continue
          fi
          task_name="$(basename "$task_spec" .md)"
          echo "== running $tid as $task_name =="
          run_rc=0
          bash "$root/scripts/worktree.sh" run "$task_name" --file "$task_spec" --task-id "$tid" || run_rc=$?
          run_results+="$tid  $task_name  exit $run_rc"$'\n'
          [ "$run_rc" -eq 0 ] || run_failed=$((run_failed + 1))
          if [ "$run_rc" -eq 0 ] && [ -f "$root/tmp/mr/$task_name.md" ]; then run_mrs+="$task_name"$'\n'; fi
        done <<< "$batch"
        echo ""
        echo "Results:"
        if [ -n "$run_results" ]; then printf '%s' "$run_results" | sed 's/^/  /'; else echo "  (nothing to run)"; fi
        echo "MRs awaiting review:"
        if [ -n "$run_mrs" ]; then printf '%s' "$run_mrs" | sed 's/^/  /'; else echo "  (none)"; fi
        if [ "$run_failed" -gt 0 ]; then
          echo "error: $run_failed task(s) failed (see Results)" >&2
          exit 1
        fi
        ;;

      status)
        branch="$(current_branch)"
        echo "branch: $branch"
        if [[ "$branch" == sprint/* ]]; then
          # Compare sprint branch against main — ahead = commits on sprint not on main
          main_sha="$(git rev-parse --verify main 2>/dev/null || true)"
          if [ -n "$main_sha" ]; then
            ahead="$(git rev-list --count "main..${branch}" 2>/dev/null || echo 0)"
            behind="$(git rev-list --count "${branch}..main" 2>/dev/null || echo 0)"
            echo "ahead: $ahead  behind: $behind"
          fi
          # List open task branches
          echo "task branches:"
          git for-each-ref --format='  %(refname:short)' refs/heads/task/ 2>/dev/null || true
          # List worktrees for this sprint
          echo "worktrees:"
          if [ -d "$root/tmp/worktrees" ]; then
            for wt_dir in "$root/tmp/worktrees"/*/; do
              [ -d "$wt_dir" ] || continue
              wt_name="$(basename "$wt_dir")"
              sf="$(sprint_file "$wt_name")"
              if [ -f "$sf" ] && [ "$(cat "$sf")" = "$branch" ]; then
                echo "  $wt_name"
              fi
            done
          fi
        else
          echo "(not on a sprint branch)"
        fi
        ;;
      finish)
        branch="$(current_branch)"
        [[ "$branch" == sprint/* ]] \
          || { echo "error: not on a sprint branch (on '$branch')" >&2; exit 1; }
        merge_flag=0
        if [ "${3:-}" = "--merge" ]; then
          merge_flag=1
        fi
        # Run the three Rules checks (skip if solution file is absent, e.g. in tests)
        if [ -f VibeEmulator.slnx ]; then
          echo "== format =="
          dotnet format VibeEmulator.slnx --verify-no-changes 2>&1 || { echo "format check failed" >&2; exit 1; }
          echo "== build =="
          dotnet build VibeEmulator.slnx --no-incremental -warnaserror 2>&1 || { echo "build failed" >&2; exit 1; }
          echo "== test =="
          dotnet test VibeEmulator.slnx --no-build 2>&1 || { echo "tests failed" >&2; exit 1; }
        else
          echo "(skipping dotnet checks — no VibeEmulator.slnx)"
        fi
        echo "sprint $branch — checks passed"
        if [ "$merge_flag" -eq 1 ]; then
          # Merge into main with --no-ff
          git checkout -q main
          git merge --no-ff "$branch" -m "Merge sprint $branch"
          echo "merged $branch into main"
        else
          echo "(no merge — use --merge to merge into main)"
        fi
        ;;
      *)
        echo "usage: $0 sprint {start|status|finish}" >&2
        exit 1
        ;;
    esac
    ;;

  # ── new — create task worktree from sprint HEAD ────────────────────────
  new)
    name_ok "$name"
    wt="$(wt_path "$name")"
    [ ! -e "$wt" ] || { echo "worktree '$name' already exists: $wt" >&2; exit 1; }

    # Must be on a sprint branch
    if ! on_sprint_branch; then
      echo "error: not on a sprint branch (on '$(current_branch)')" >&2
      exit 1
    fi

    # Tree must be clean (ignoring generated paths)
    if ! tree_clean; then
      echo "error: working tree is not clean (ignoring generated paths)" >&2
      exit 1
    fi

    sprint_branch="$(current_branch)"
    sprint_head="$(git rev-parse HEAD)"

    # Parse optional --task-id first: a tracker task N.M may only run on sprint/N-*.
    task_id=""
    shift 2  # skip "new" and <name>
    while [ "${1:-}" = "--task-id" ]; do
      task_id="${2:?--task-id needs a value}"
      shift 2
    done
    if [ -n "$task_id" ]; then
      want_sprint="${task_id%%.*}"
      if [[ "$sprint_branch" != "sprint/${want_sprint}-"* ]]; then
        echo "error: task $task_id belongs to sprint $want_sprint but the checked-out branch is '$sprint_branch'." >&2
        echo "       Another agent may own this branch. Do not merge across sprints: start/switch to sprint/${want_sprint}-* first." >&2
        exit 11
      fi
    fi

    mkdir -p "$root/tmp/worktrees" "$root/tmp/meta"

    # Create branch task/<name> from sprint HEAD — no uncommitted changes carried
    git worktree add -q -b "task/$name" "$wt" "$sprint_head"

    # Write meta files
    echo "$sprint_head" > "$(base_file "$name")"
    echo "$sprint_branch" > "$(sprint_file "$name")"

    if [ -n "$task_id" ]; then
      echo "$task_id" > "$(task_file "$name")"
    fi

    echo "$wt"
    ;;

  # ── path ───────────────────────────────────────────────────────────────
  path)
    name_ok "$name"
    wt_path "$name"
    ;;

  # ── list ───────────────────────────────────────────────────────────────
  list)
    git worktree list
    ;;

  # ── patch ──────────────────────────────────────────────────────────────
  patch)
    name_ok "$name"
    wt="$(wt_path "$name")"
    [ -d "$wt" ] || { echo "no worktree '$name'" >&2; exit 1; }
    mkdir -p "$root/tmp/patches"
    git -C "$wt" add -A
    git -C "$wt" diff --cached --binary "$(cat "$(base_file "$name")")" > "$root/tmp/patches/$name.patch"
    echo "$root/tmp/patches/$name.patch"
    ;;

  # ── commit — runners commit the agent's work on task/<name> ────────────
  # commit <name> [--runner R] [--model M] [--task-id ID] [--title T]
  # Exit: 0 committed, 1 no changes / usage, 4 forbidden path (nothing committed).
  commit)
    name_ok "$name"
    wt="$(wt_path "$name")"
    [ -d "$wt" ] || { echo "no worktree '$name'" >&2; exit 1; }
    shift 2
    c_runner="agent" c_model="" c_task="" c_title="$name"
    while [ $# -gt 0 ]; do
      case "$1" in
        --runner)  c_runner="${2:?--runner needs a value}"; shift 2 ;;
        --model)   c_model="${2:?--model needs a value}"; shift 2 ;;
        --task-id) c_task="${2:?--task-id needs a value}"; shift 2 ;;
        --title)   c_title="${2:?--title needs a value}"; shift 2 ;;
        *) echo "unknown option '$1'" >&2; exit 1 ;;
      esac
    done
    # Guard every changed path (NUL-safe, relative to the worktree: modified, deleted,
    # untracked and already staged files; renames show up as delete + add).
    bad=0
    while IFS= read -r -d '' path; do
      path_ok "$path" || bad=1
    done < <({ git -C "$wt" ls-files -z --modified --deleted --others --exclude-standard
               git -C "$wt" diff --cached --name-only --relative -z; })
    if [ "$bad" -ne 0 ]; then
      echo "forbidden path(s) in the worktree - nothing committed" >&2
      exit 4
    fi
    git -C "$wt" add -A
    if git -C "$wt" diff --cached --quiet; then
      echo "no changes to commit in '$name'" >&2
      exit 1
    fi
    c_msg="${c_task:+$c_task }$c_title"$'\n\n'"runner: $c_runner"
    [ -z "$c_model" ] || c_msg+=$'\n'"model: $c_model"
    git -C "$wt" -c user.name="agent:$c_runner" -c user.email="agent@localhost" \
        commit -q -m "$c_msg"
    git -C "$wt" rev-parse HEAD
    ;;

  # ── run — the whole pipeline: new -> runner -> mr, stopping before merge ─
  run)
    name_ok "$name"
    shift 2  # drop 'run' and <name>
    run_spec="" run_task_id="" run_runner="codex" run_fallback="none"
    while [ $# -gt 0 ]; do
      case "$1" in
        --file)     run_spec="${2:?--file needs a path}"; shift 2 ;;
        --task-id)  run_task_id="${2:?--task-id needs a value}"; shift 2 ;;
        --runner)   run_runner="${2:?--runner needs a value}"; shift 2 ;;
        --fallback) run_fallback="${2:?--fallback needs a value}"; shift 2 ;;
        *) echo "unknown option '$1'" >&2; exit 1 ;;
      esac
    done
    if [ -z "$run_spec" ] || [ ! -f "$run_spec" ]; then
      echo "usage: $0 run <name> --file <spec> [--task-id <id>] [--runner kilo|junie|codex] [--fallback none]" >&2
      exit 1
    fi
    # 1) run the task (creates the worktree, runs the runner, commits and verifies)
    bash scripts/run-task.sh "$name" --file "$run_spec" ${run_task_id:+--task-id "$run_task_id"} \
      --runner "$run_runner" --fallback "$run_fallback" || exit $?
    # 2) build the MR (checks + review sheet); stop before merge (human gate)
    bash scripts/worktree.sh mr "$name"
    ;;

  # ── retry — re-run a task in the SAME worktree with a fix list (max 2) ──
  retry)
    name_ok "$name"
    shift 2  # drop 'retry' and <name>
    retry_fixes="" retry_runner="codex" retry_task_id=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --fixes)   retry_fixes="${2:?--fixes needs a path}"; shift 2 ;;
        --runner)  retry_runner="${2:?--runner needs a value}"; shift 2 ;;
        --task-id) retry_task_id="${2:?--task-id needs a value}"; shift 2 ;;
        *) echo "unknown option '$1'" >&2; exit 1 ;;
      esac
    done
    [ -n "$retry_fixes" ] && [ -f "$retry_fixes" ] \
      || { echo "usage: $0 retry <name> --fixes <file> [--runner kilo|junie|codex] [--task-id <id>]" >&2; exit 1; }
    case "$retry_runner" in junie|kilo|codex) ;; *) echo "unknown runner '$retry_runner'" >&2; exit 1 ;; esac
    [ -d "$(wt_path "$name")" ] \
      || { echo "error: no worktree for '$name'; start it with 'run' or 'new'" >&2; exit 1; }
    [ -n "$retry_task_id" ] || retry_task_id="$(cat "$(task_file "$name")" 2>/dev/null || true)"
    retry_count="$(cat "$(retry_file "$name")" 2>/dev/null || echo 0)"
    retry_count=$((retry_count + 1))
    if [ "$retry_count" -gt 2 ]; then
      echo "error: '$name' reached the retry limit (2); setting the task blocked" >&2
      if [ -n "$retry_task_id" ]; then
        python3 scripts/tasks.py block "$retry_task_id" --reason "retry limit (2) reached" >/dev/null 2>&1 || true
      fi
      exit 1
    fi
    echo "$retry_count" > "$(retry_file "$name")"
    cp "$retry_fixes" "$(fixes_file "$name")"
    echo "retry $retry_count/2 for '$name' (fixes: $retry_fixes)" >&2
    bash "scripts/$retry_runner-task.sh" "$name" --file "$retry_fixes" ${retry_task_id:+--task-id "$retry_task_id"}
    ;;

  # ── mr — merge sprint into task branch, checks, path guard, MR record ─
  mr)
    name_ok "$name"
    wt="$(wt_path "$name")"
    [ -d "$wt" ] || { echo "no worktree '$name'" >&2; exit 1; }

    # Read sprint branch from meta
    sf="$(sprint_file "$name")"
    [ -f "$sf" ] || { echo "error: sprint meta missing for '$name'" >&2; exit 1; }
    sprint_branch="$(cat "$sf")"

    # Parse optional --remote flag
    remote_flag=0
    shift 2  # skip "mr" and <name>
    while [ "${1:-}" = "--remote" ]; do
      remote_flag=1
      shift
    done

    task_branch="task/$name"

    # Merge sprint into task branch inside the worktree (no rebase)
    if ! git -C "$wt" merge "$sprint_branch" -m "Merge $sprint_branch into $task_branch" 2>/dev/null; then
      # Conflict — abort and exit 9
      git -C "$wt" merge --abort 2>/dev/null || true
      echo "error: merge conflict when merging $sprint_branch into $task_branch" >&2
      exit 9
    fi

    # Review sheet: red flags are recorded in the MR document and printed for the reviewer.
    review_out="$(review_sheet "$sprint_branch" "$task_branch" || true)"
    printf '%s\n' "$review_out" >&2

    # Run checks in the worktree (ACCEPT_CHECKS_CMD override supported; conditional on the diff)
    if ! ( cd "$wt" && run_checks worktree "$sprint_branch" "$task_branch" ); then
      echo "error: checks failed in worktree '$name'" >&2
      exit 8
    fi

    # Path guard: check all files changed between sprint and task branch
    local_violations=0
    # --no-renames lists a rename as delete + add, so both the old and the new path are checked.
    while IFS= read -r -d '' local_fpath; do
      if ! path_ok "$local_fpath"; then
        local_violations=1
      fi
    done < <(git -C "$wt" diff --no-renames --name-only -z "${sprint_branch}...${task_branch}" 2>/dev/null || true)

    if [ "$local_violations" -eq 1 ]; then
      echo "error: forbidden path(s) in diff $sprint_branch...$task_branch" >&2
      exit 4
    fi

    # Build MR record
    mkdir -p "$root/tmp/mr"
    mr_file="$root/tmp/mr/$name.md"
    task_id=""
    tf="$(task_file "$name")"
    [ -f "$tf" ] && task_id="$(cat "$tf")"

    sprint_sha="$(git -C "$wt" rev-parse "$sprint_branch")"
    task_sha="$(git -C "$wt" rev-parse "$task_branch")"
    base_sha="$(git -C "$wt" merge-base "$sprint_branch" "$task_branch")"

    diffstat="$(git -C "$wt" diff --stat "${sprint_branch}...${task_branch}" 2>/dev/null || true)"

    {
      echo "# MR: $name"
      echo ""
      echo "- **Task ID:** ${task_id:-unknown}"
      echo "- **Sprint branch:** $sprint_branch ($sprint_sha)"
      echo "- **Task branch:** $task_branch ($task_sha)"
      echo "- **Merge base:** $base_sha"
      echo "- **Checks:** passed"
      echo "- **Path guard:** passed"
      if [ "$remote_flag" -eq 1 ]; then
        echo "- **Remote hint:** Consider creating a remote MR from $task_branch to $sprint_branch"
        echo ""
        echo "  git push origin $task_branch"
      fi
      echo ""
      printf '%s\n' "$review_out"
      echo ""
      echo "## Diffstat"
      echo ""
      echo '```'
      echo "$diffstat"
      echo '```'
    } > "$mr_file"

    if [ -n "$task_id" ]; then
      # The task is ready for review (awaiting merge); `merge` moves it to done, `abandon` to none.
      python3 scripts/tasks.py status "$task_id" review >/dev/null 2>&1 || true
    fi
    echo "mr record: $mr_file"
    ;;

  # ── merge — merge task branch into sprint (--no-ff) with trailers ──────
  merge)
    name_ok "$name"
    wt="$(wt_path "$name")"
    [ -d "$wt" ] || { echo "no worktree '$name'" >&2; exit 1; }

    # Read sprint branch from meta
    sf="$(sprint_file "$name")"
    [ -f "$sf" ] || { echo "error: sprint meta missing for '$name'" >&2; exit 1; }
    sprint_branch="$(cat "$sf")"

    # The main tree may be checked out on another sprint (another agent's); never switch it silently.
    if git rev-parse --verify -q "$sprint_branch" >/dev/null && [ "$(current_branch)" != "$sprint_branch" ]; then
      echo "error: '$name' belongs to $sprint_branch but the main tree is on '$(current_branch)'; not merging across sprints." >&2
      exit 11
    fi

    # Parse optional --accept-flags / --auto
    accept_flags=""
    auto_flag=0
    shift 2  # skip "merge" and <name>
    while [ $# -gt 0 ]; do
      [ "$1" = "--auto" ] && auto_flag=1
      accept_flags="$accept_flags $1"
      shift
    done

    task_branch="task/$name"

    # Preconditions: sprint branch must exist
    if ! git rev-parse --verify "$sprint_branch" >/dev/null 2>&1; then
      echo "error: sprint branch '$sprint_branch' does not exist" >&2
      exit 6
    fi

    # The merge must be exactly what `mr` tested: the MR record has to exist and both branch heads
    # must still be the ones it was built against (otherwise run `mr` again).
    mrf="$root/tmp/mr/$name.md"
    if [ ! -f "$mrf" ]; then
      if [ "$auto_flag" -eq 1 ]; then
        # --auto: build the MR (checks + review sheet) in one step, then require green below.
        echo "merge --auto: building the MR (checks + review sheet) first" >&2
        if ! bash "$root/scripts/worktree.sh" mr "$name" >&2; then
          echo "error: --auto could not build a green MR for '$name'" >&2
          exit 8
        fi
      else
        echo "error: no MR record for '$name' - run: scripts/worktree.sh mr $name" >&2
        exit 6
      fi
    fi
    rec_sprint="$(sed -n 's/^- \*\*Sprint branch:\*\* .*(\([0-9a-f]\{40\}\))$/\1/p' "$mrf" | head -n1)"
    rec_task="$(sed -n 's/^- \*\*Task branch:\*\* .*(\([0-9a-f]\{40\}\))$/\1/p' "$mrf" | head -n1)"
    sprint_head="$(git rev-parse "$sprint_branch")"
    task_head="$(git rev-parse "$task_branch")"
    if [ -z "$rec_sprint" ] || [ -z "$rec_task" ] || [ "$rec_sprint" != "$sprint_head" ] || [ "$rec_task" != "$task_head" ]; then
      echo "error: MR for '$name' is stale (a branch moved since it was built) - run: scripts/worktree.sh mr $name" >&2
      exit 6
    fi
    if ! tree_clean; then
      echo "error: the main working tree is not clean; commit or stash first" >&2
      exit 1
    fi

    # Review gate: red flags require an explicit --accept-flags (recorded in the merge commit).
    if review_out="$(review_sheet "$sprint_branch" "$task_branch")"; then
      review_rc=0
    else
      review_rc=$?
    fi
    printf '%s\n' "$review_out" >&2
    if [ "$review_rc" -ne 0 ] && [[ "$accept_flags" != *--accept-flags* ]]; then
      echo "error: red flags found in '$name'; review the diff, then re-run merge with --accept-flags" >&2
      exit 12
    fi

    # Merge task branch into sprint (--no-ff) with trailers
    task_id=""
    tf="$(task_file "$name")"
    [ -f "$tf" ] && task_id="$(cat "$tf")"

    # Real git trailers (blank line, then Key: value): task id from the meta file, runner and model
    # from the trailers the runner put on the task branch tip.
    tip_msg="$(git log -1 --format=%B "$task_branch")"
    tip_runner="$(printf '%s\n' "$tip_msg" | sed -n 's/^runner: //p' | head -n1)"
    tip_model="$(printf '%s\n' "$tip_msg" | sed -n 's/^model: //p' | head -n1)"
    tip_title="$(git log -1 --format=%s "$task_branch")"
    tip_title="${tip_title#"$task_id" }"
    merge_msg="Merge ${task_id:+$task_id: }${tip_title:-$name}"$'\n'
    merge_msg+=$'\n'"Task: ${task_id:-none}"
    [ -z "$tip_runner" ] || merge_msg+=$'\n'"Runner: $tip_runner"
    [ -z "$tip_model" ] || merge_msg+=$'\n'"Model: $tip_model"
    merge_msg+=$'\n'"Branch: $task_branch"
    case "$accept_flags" in *--accept-flags*) merge_msg+=$'\n'"Accepted-Flags: yes" ;; esac

    if ! git merge --no-ff "$task_branch" -m "$merge_msg" 2>/dev/null; then
      # Merge failure — abort to leave clean tree, exit 10
      git merge --abort 2>/dev/null || true
      echo "error: merge failed — conflicts between $sprint_branch and $task_branch" >&2
      exit 10
    fi

    # Mark task as done if task id is known. Errors are reported, not silently ignored.
    # A merge succeeding but tracker update failing is partial success — the merge stays,
    # but cleanup is deferred so evidence is preserved for later link recording.
    tracker_ok=1
    if [ -n "$task_id" ]; then
      tracker_out="$(python3 scripts/tasks.py done "$task_id" 2>&1)" || tracker_ok=0
      if [ "$tracker_ok" -eq 0 ]; then
        echo "warning: tracker update failed for $task_id: $tracker_out" >&2
      elif [ -n "$tracker_out" ]; then
        echo "$tracker_out" >&2
      fi
    fi

    # Safe cleanup: always remove worktree, branch, and meta. Tracker errors are
    # reported above but do not block cleanup (the merge already succeeded).
    # On tracker failure, the MR record and patch are preserved for later link
    # recording (accept --links-only); meta files are still removed.
    git worktree remove --force "$wt" 2>/dev/null || true
    git branch -d "$task_branch" >/dev/null 2>&1 || echo "warning: could not delete $task_branch (not fully merged?)" >&2
    if [ "$tracker_ok" -eq 1 ]; then
      rm -f "$(base_file "$name")" "$(sprint_file "$name")" "$(task_file "$name")" "$(retry_file "$name")" "$(fixes_file "$name")" "$mrf"
    else
      # Preserve MR record and patch for retry; remove meta files
      rm -f "$(base_file "$name")" "$(sprint_file "$name")" "$(task_file "$name")" "$(retry_file "$name")" "$(fixes_file "$name")"
      echo "warning: tracker update failed — MR record and patch preserved for retry" >&2
    fi
    git worktree prune

    if [ "$tracker_ok" -eq 0 ]; then
      echo "merged $task_branch into $sprint_branch (partial: tracker update failed)" >&2
    else
      echo "merged $task_branch into $sprint_branch"
    fi
    ;;

  # ── accept — earn 'done': merge, then record the links (commit, patch, runner, model) ─
  accept)
    name_ok "$name"
    shift 2  # drop 'accept' and <name>
    accept_links_only=0 accept_given_id=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --links-only) accept_links_only=1; shift ;;
        --task-id)    accept_given_id="${2:?--task-id needs a value}"; shift 2 ;;
        *) echo "unknown option '$1'" >&2; exit 1 ;;
      esac
    done
    if [ "$accept_links_only" -eq 1 ]; then
      # Retry after exit 13: the merge already happened (and removed the task meta), so only the links are recorded.
      [ -n "$accept_given_id" ] || { echo "error: --links-only needs --task-id <id>" >&2; exit 1; }
      accept_id="$accept_given_id"
      # Recover the original merge commit SHA from git history (not current HEAD, which may have moved).
      # The merge commit message contains "Branch: task/<name>".
      accept_sha="$(git log --all --grep="Branch: task/$name" --format=%H -1 2>/dev/null || true)"
    else
      [ -f "$(task_file "$name")" ] || { echo "error: no task id for '$name'" >&2; exit 1; }
      accept_id="$(cat "$(task_file "$name")")"
      [ -f "$root/tmp/mr/$name.md" ] \
        || { echo "error: no review marker for '$name' (run: $0 mr $name first)" >&2; exit 6; }
      bash "$root/scripts/worktree.sh" merge "$name" || exit $?
    fi
    accept_failed=""
    accept_link() {  # <kind> <value>: record a tracker link; remember the kind when it fails
      [ -n "$2" ] || return 0
      python3 scripts/tasks.py link "$accept_id" "$1" "$2" >/dev/null || accept_failed="$accept_failed $1"
    }
    if [ "$accept_links_only" -eq 0 ]; then
      accept_branch="$(git branch --show-current)"
      accept_sha="$(git rev-parse "$accept_branch" 2>/dev/null || true)"
    fi
    if [ -n "$accept_sha" ]; then accept_link commit "$accept_sha"; else accept_failed="$accept_failed commit"; fi
    [ ! -f "$root/tmp/patches/$name.patch" ] || accept_link patch "tmp/patches/$name.patch"
    if [ -f "$root/tmp/runs/$name.summary.json" ]; then
      for accept_key in runner model; do
        accept_link "$accept_key" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],""))' \
          "$root/tmp/runs/$name.summary.json" "$accept_key" 2>/dev/null || true)"
      done
    fi
    if [ -n "$accept_failed" ]; then
      echo "error: merged $name (task $accept_id) but could not record links:$accept_failed" >&2
      echo "retry: $0 accept $name --links-only --task-id $accept_id" >&2
      exit 13
    fi
    echo "accepted $name (task $accept_id)"
    ;;

  # ── remove — delete worktree, branch and metadata ──────────────────────
  remove)
    name_ok "$name"
    git worktree remove --force "$(wt_path "$name")" 2>/dev/null || true
    # Try both branch naming conventions
    git branch -D "agent/$name" >/dev/null 2>&1 || true
    git branch -D "task/$name" >/dev/null 2>&1 || true
    rm -f "$(base_file "$name")" "$(sprint_file "$name")" "$(task_file "$name")" "$(retry_file "$name")" "$(fixes_file "$name")"
    git worktree prune
    echo "removed $name"
    ;;

  # ── abandon — remove worktree + branch + meta (same as remove) ─────────
  abandon)
    name_ok "$name"
    if [ -f "$(task_file "$name")" ]; then
      abandoned_id="$(cat "$(task_file "$name")")"
      [ -n "$abandoned_id" ] && python3 scripts/tasks.py status "$abandoned_id" none >/dev/null 2>&1 || true
    fi
    git worktree remove --force "$(wt_path "$name")" 2>/dev/null || true
    # Try both branch naming conventions
    git branch -D "agent/$name" >/dev/null 2>&1 || true
    git branch -D "task/$name" >/dev/null 2>&1 || true
    rm -f "$(base_file "$name")" "$(sprint_file "$name")" "$(task_file "$name")" "$(retry_file "$name")" "$(fixes_file "$name")"
    git worktree prune
    echo "abandoned $name"
    ;;

  # ── usage ──────────────────────────────────────────────────────────────
  *)
    sed -n '2,15p' "$0" >&2
    echo "" >&2
    echo "Exit codes:" >&2
    echo "  0  success" >&2
    echo "  1  precondition/usage error" >&2
    echo "  4  forbidden path in diff (nothing changed)" >&2
    echo "  6  precondition/staleness error (nothing changed)" >&2
    echo "  8  checks failed in worktree" >&2
    echo "  9  merge conflict (aborted, nothing changed)" >&2
    echo "  10 merge failure (aborted, clean tree)" >&2
    echo "  11 wrong sprint (task id or main tree belongs to another sprint branch)" >&2
    exit 2
    ;;
esac
