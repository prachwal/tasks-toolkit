# Tracker v2 — design

Status: planned as Sprint 4. Motivation: a review of the task system (stale generated files, fragile hierarchical ids,
`done` depending on planner discipline, no history, local-only backup, noisy notes). See [[quality-gates]], [[junie-delegation]], [[decisions]].

## Decisions

- **Stable uid next to the display id.** Every node gets an immutable `uid` (`T-` + 4 base36 chars, never reused, assigned at creation / migration).
  Hierarchical ids (`2.1.1`) stay as the *display and address* form (they change on `mv`); `uid` is what specs, runner metadata and links store.
  Every command accepts either; `mv` prints `uid` too. `--task-id` accepts either and runners persist the uid (`tmp/meta/<name>.task`).
- **Structured links instead of notes.** Table `links(node_uid, kind, value, created)`; kinds: `spec`, `patch`, `runner`, `model`, `agent`, `commit`, `branch`.
  Runners write links (not "verified; patch …" notes). `show` prints them; `TASKS.md` shows only a compact `[spec · patch · commit]` badge row and collapses runner chatter.
- **Audit log.** Table `events(id, ts, actor, action, node_uid, field, old, new)` written inside every mutation transaction; `tasks.py log [id] [--since]`.
  `actor` = `$VIBE_ACTOR` or `$USER` (runners set `junie:<name>`, `kilo:<name>`, `haiku`, `codex:<name>`, `planner`).
- **`done` is earned, not typed.** `worktree.sh accept <name>` = apply already happened → run the three Rules checks → on green `tasks.py done <uid>` + links
  (patch, runner, model, commit when known) + remove worktree. `accept` refuses when the review marker (3.3) is missing or checks are red. Direct `done` stays possible for manual work.
- **Backup = GitLab, history included.** Private repo `https://gitlab.com/prachwal/vibe-emulator-tasks` (created). A clone outside the project
  (`$VIBE_TASKS_BACKUP_DIR`, default `~/.local/share/vibe-tasks`) holds `snapshot.json` + rendered `TASKS.md`; every mutation makes a LOCAL commit whose message is the
  event summary; `tasks.py backup --push` / `restore` are explicit. Nothing pushes on its own (AGENTS.md: never push); an opt-in hook flag can be added later.
  `TASKS.md` and `tasks.snapshot.json` leave the project repository (generated artefacts; see 3.9).
- **Cheap reads without compound shell.** `list <id> [--desc --notes --links]`, `show <id>...` (multi, `--brief`), `log`. All covered by the single allow rule `python3 scripts/tasks.py`.

## Order (dependencies)

4.1 reads (independent) → 4.2 uid + links (schema migration v2) → 4.3 events/log → 4.4 GitLab backup (uses events for commit messages) → 4.5 `accept` (uses uid + links).
Migration rule: `tasks_init.py` schema version bump; opening an old DB migrates in one transaction after an automatic `export-json` backup next to the DB; `import-json` keeps accepting v1/v2 files.
Safety rule for all of Sprint 4: tests never touch the real `tasks/`, `TASKS.md` or backup clone (VIBE_TASKS_TESTING guard from 3.9; backup tests use a local bare repo as "remote", never gitlab.com).

## Sequencing (running a whole sprint, added after the Sprint 3 retrospective)

Problem: dependencies exist (`--after`, `next`), but the planner drives every step by hand (spec -> runner -> review -> merge -> done), failures are handled ad hoc,
later tasks do not automatically see the result of earlier ones, and parallel batches are chosen by eye. Prerequisite: the branch-per-task MR flow (3.12/3.13) so every task starts from the merged sprint state.

- **Pipeline command (4.6):** `worktree.sh run <name> --file spec.md --task-id <id> [--runner junie|kilo|codex]` = `new` -> runner -> `mr`; stops BEFORE `merge` (human gate). One exit code summarises the stage reached.
- **Attempts and failure policy (4.7):** per-task attempt counter and `tmp/meta/<name>.fixes.md`; `worktree.sh retry <name> --fixes file` re-runs in the SAME worktree (max 2, then the task is set `blocked` with the last failing check as reason). The fix list is stored as a link (`fixes`).
- **Review state (4.8):** new leaf status `review` (alias `w-przeglądzie`): set when an MR is ready and not yet merged; derived-status rules: counts as in progress; `next` never offers it; `merge` moves it to `done`, `abandon` back to `none`. Shown as `🔎` in TASKS.md.
- **Context bundle (4.9):** `tasks.py brief <id>` prints what a spec author/agent needs: title+description, dependencies with their merge commit sha, diffstat, spec link and notes, plus the list of files the dependencies changed. `worktree.sh run` can prepend it to the spec (`--with-brief`).
- **Batch scheduler (4.10):** specs declare a `Files:` header (paths/globs the task may touch); stored as a `scope` link. `tasks.py plan-batch [--max N]` returns the largest set of READY tasks (deps satisfied, status none) with pairwise disjoint scopes; `sprint run` (4.11) starts that batch through `worktree.sh run`, polls `summary.json`, and prints a table of MRs waiting for review.
- **Sprint driver (4.11):** `worktree.sh sprint run [--max N] [--dry-run]` ties the above together; it never merges by itself and never pushes.
