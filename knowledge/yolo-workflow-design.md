# Yolo workflow design (Sprint 12)

Status: ratified by the human policy gate (task 12.1, 2026-10-17).

## Policy (ratified)

1. **Entry point:** separate `scripts/sprint-auto.sh`.
2. **Authorization:** `--yolo-force` authorizes the named, scoped goal through a local main merge; no later confirmation for actions inside that envelope.
3. **Decisions:** the invoking planner supplies the complete goal-specific decision inventory and allowed scopes; safe defaults only for explicitly delegated decisions.
4. **Runner/Model:** retain the current Sol planner and Junie runner; serial execution; no fallback; no automatic implementation retries; bounded timeouts; disclose remote-service data exposure.
5. **State:** fresh runs only from `main` with no unrelated unfinished sprint; resume only proven owned state; reject staged generated files; allow unstaged generated files only when verified and safely preservable.
6. **Auto-accept:** scope conformity + all required checks + zero red flags; no separate human review.
7. **Cleanup:** remove only successful, clean, owned task worktrees/branches; preserve failed work and unrelated state; retain sprint branches and evidence.
8. **Hooks/Verification:** run hooks normally; verify before and after main merge; report post-merge verification failure as merged-but-unverified without automatic rollback.

**Forbidden:** push, gate override, accept-flags, reset/clean, unrelated cleanup.

## Interfaces

### Answers document (versioned)

```json
{
  "version": 1,
  "goal_reviewed": false,
  "allowed_scopes": [],
  "policy": { "runner": "junie", "serial": true, "fallback": "none", "retries": 0, "push": false },
  "decisions": [
    { "id": "runner", "prompt": "...", "answer": null, "delegated": false, "safe_default": "junie", "rationale": "" }
  ]
}
```

### Preflight result

```json
{
  "questions": [ { "id", "prompt", "answer", "delegated", "safe_default", "rationale" } ],
  "delegated_defaults": [ { "id", "value", "rationale" } ],
  "blockers": [ "..." ],
  "approval_scope": { "goal", "goal_reviewed", "allowed_scopes", "policy", ... }
}
```

### Ownership rules

- A phase label alone never authorizes replay.
- An interrupted parent must not permit a second live runner.
- Tracker `done` is insufficient evidence (runners can set it before MR/merge).
- Only `done` after merge + green checks + zero red flags counts.

## Acceptance receipt semantics

An acceptance receipt is the set of structured links recorded on a task after a successful merge. It is valid **only** when all of the following hold:

1. **Task identity** — the task UID matches the one that was delegated.
2. **Exact verified commit** — the recorded commit SHA is the merge commit that brought the task branch into the sprint.
3. **Sprint baseline** — the sprint branch HEAD at merge time matches the expected ref.
4. **Scope result** — the task touched only paths within the allowed scopes (scope conformity).
5. **Checks green** — all required Rules checks (format, build, test) passed.
6. **Zero red flags** — the review sheet reported 0 red flags (or `--accept-flags` was explicitly used).

### Insufficient evidence (never authorizes acceptance)

- Tracker `done` status alone (runners can set it before MR/merge exists).
- MR file existence alone (can be stale).
- Branch existence alone (branch may have moved).
- Worktree existence alone (worktree may be orphaned).

### Receipt links

Recorded by `worktree.sh accept`:
- `commit` — merge commit SHA (recovered from git history on `--links-only` retry)
- `patch` — path to the exported patch file
- `runner` — runner name (junie, kilo, codex)
- `model` — model used by the runner

### Partial success

If merge succeeds but link recording fails (exit 13), the merge is **not** rolled back. The MR record and patch are preserved. Retry with `accept --links-only --task-id <id>`.

### Cleanup

Cleanup (worktree removal, branch deletion, meta removal) is a **separate, guarded operation** that runs only after successful link recording. On tracker failure, MR record and patch are preserved for retry.

## Read-only constraints

- No writes, no locks, no bytecode, no artifacts before approval.
- SQLite opened in `mode=ro` (no migration, render, or check).
- Git operations are read-only (`status`, `diff`, `worktree list`, `symbolic-ref`).
- Forbidden env vars rejected: `SPEC_LINT=off`, `ACCEPT_CHECKS_CMD`, `JUNIE_VERIFY_CMD`, `KILO_VERIFY_CMD`, `CODEX_VERIFY_CMD`.
- Staged generated files are hard blockers; unstaged generated files are allowed only when verified.
- Missing answers → exit 2; safety blockers → exit 4.