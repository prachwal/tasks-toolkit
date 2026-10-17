# tasks-toolkit

A small, self-contained task tracker (sprints → tasks → subtasks) with a CLI and an MCP server.

## Contents

- `scripts/` — all tracker code (see table below).
- `pyproject.toml` — ruff/mypy/pytest configuration.

## Installation

### From PyPI (recommended)

```bash
pipx install tasks-toolkit
# or: uv tool install tasks-toolkit
tasks-init
tasks overview
```

### From source

```bash
git clone https://github.com/prachwal/tasks-toolkit.git
cd tasks-toolkit
python -m pip install -e .
tasks-init
tasks overview
```

### Docker

```bash
docker build -t tasks-toolkit .
docker run --rm -i -v tasks-data:/data tasks-toolkit
```

## Quick start (from source)

```bash
python3 scripts/tasks_init.py          # create tasks/tasks.db
python3 scripts/tasks.py overview      # show sprints
python3 scripts/tasks.py next          # next ready task
```

Data belongs to the current Git project (the main checkout when invoked from a worktree).
Outside Git, it belongs to the current directory. Installed commands never use their
installation directory for data. Override the location with `TASKS_ROOT`:

```bash
export TASKS_ROOT=/path/to/data
python3 scripts/tasks.py overview
```

## MCP clients

The `tasks-mcp` command runs the same stdio server for every client. Set `TASKS_ROOT`
to the absolute path of the project whose tasks you want to manage. It points to
the project, not to the toolkit installation. An installed server needs no copy of
this source repository. Use an absolute executable path if the client cannot find
`tasks-mcp` on its PATH; `command -v tasks-mcp` shows it on Unix.

### Claude Desktop

Open **Settings → Developer → Edit Config**, merge this entry into
`claude_desktop_config.json`, and restart the app:

```json
{
  "mcpServers": {
    "tasks": {
      "command": "/absolute/path/to/tasks-mcp",
      "args": [],
      "env": {"TASKS_ROOT": "/absolute/path/to/your-project"}
    }
  }
}
```

See the [official local-server setup guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
for configuration locations and troubleshooting. The toolkit currently uses POSIX
file locking (`fcntl`); run the server on macOS or Linux. The Windows Desktop app
needs a launcher into a Linux environment such as WSL, rather than a native Windows
Python process.

### Claude Code

From your project directory, register a project-scoped stdio server:

```bash
claude mcp add --transport stdio --scope project \
  --env TASKS_ROOT="$(pwd)" tasks -- /absolute/path/to/tasks-mcp
claude mcp list
```

Claude Code saves project-scoped servers to `.mcp.json`; see its
[official MCP documentation](https://code.claude.com/docs/en/mcp).

### VS Code / GitHub Copilot

Add a server to `.vscode/mcp.json`:

```json
{
  "servers": {
    "tasks": {
      "type": "stdio",
      "command": "/absolute/path/to/tasks-mcp",
      "args": [],
      "env": {"TASKS_ROOT": "${workspaceFolder}"}
    }
  }
}
```

See [VS Code's MCP configuration reference](https://code.visualstudio.com/docs/agents/reference/mcp-configuration).

### Cursor

Add this to `.cursor/mcp.json` in your project (or `~/.cursor/mcp.json` for all projects):

```json
{
  "mcpServers": {
    "tasks": {
      "command": "/absolute/path/to/tasks-mcp",
      "env": {"TASKS_ROOT": "/absolute/path/to/your-project"}
    }
  }
}
```

### OpenCode

Add this entry to `opencode.json`:

```json
{
  "mcp": {
    "tasks": {
      "type": "local",
      "command": ["/absolute/path/to/tasks-mcp"],
      "enabled": true,
      "environment": {"TASKS_ROOT": "/absolute/path/to/your-project"}
    }
  }
}
```

See the [OpenCode MCP documentation](https://opencode.ai/docs/mcp-servers/).

### Other stdio clients and source installs

Configure any MCP client to launch `tasks-mcp`, use stdio transport and pass
`TASKS_ROOT` in the server environment. Clients use different JSON keys; adapt one
of the examples above to the client's schema. To run from a source checkout,
replace the executable with `python3` and pass
`/absolute/path/to/tasks-toolkit/scripts/tasks_mcp.py` as its first argument
(or use that two-element array for OpenCode's `command`).

## Verified task and sprint execution

Copy `.tasks-toolkit.example.json` to `.tasks-toolkit.json`, choose the checks for
**your project**, and commit the configuration. Commands are argument arrays;
no shell interpolation is performed. Every configured check must pass. Keep
`tmp/` and generated `tasks/` files in `.gitignore`, as in this repository.

Runner names and commands are defined entirely in `.tasks-toolkit.json`:

```json
{
  "default_runner": "coder",
  "runners": {
    "coder": ["python3", "tools/coder_adapter.py"],
    "backup-coder": ["/absolute/path/to/another-adapter"]
  },
  "checks": [["python3", "-m", "pytest", "-q"]],
  "timeout": 120
}
```

Replace these sample adapter commands with your own. Adapters receive the spec on
stdin, edit files in their current working directory, leave changes uncommitted,
and return nonzero on failure. There are no built-in providers or model choices.
`--runner NAME` selects any registered adapter; otherwise `default_runner` is used.
If no default is configured, select a runner explicitly. `--fallback NAME` uses
the same registry; `none` is reserved to disable fallback. Reports and statistics
accept any runner name containing letters, digits, underscores and hyphens.

When upgrading from 0.1.4, register the command you previously relied on explicitly:
implicit provider commands and the implicit default have been removed. Existing
checkpoint and report runner names remain valid when registered in the configuration.
Changing configuration still invalidates a resume checkpoint's approval fingerprint.

Installed commands are `tasks-run` and `tasks-sprint`; from source, the corresponding
wrappers are `scripts/run-task.sh` and `scripts/sprint-auto.sh`.

Run one task in a clean working tree, with an explicit allowed scope:

```bash
tasks-run result \
  --file tmp/tasks/result.md --scope src/ --runner coder
```

Alternatively, `--task-id 1.1` reads scope links from the tracker. The runner checks
all actual tracked changes, deletions and untracked files, runs the configured
checks and commits only verified changes. Input flags such as `checks_green`
and `tracker_done` are never evidence. Failures preserve the working tree and
write a report to `tmp/runs/<name>.result.json`. Exit codes: 0 verified, 1 rejected,
3 runner/configuration failure. Optional fallback runs only after a runner failure
that left the working tree clean.

For a complete sprint, supply a plan containing `sprint` (`slug`, `title`, `goal`)
and `tasks` (`key`, `title`, `scope`, `spec_name`, `spec`, `depends_on`). Specs must
pass `scripts/spec-lint.sh`; dependencies reference earlier task keys. An answers
file binds the reviewed goal, allowed paths and execution policy. `policy.runner`
overrides the configured default; delegated decisions use the same registry:

```json
{
  "goal_reviewed": true,
  "allowed_scopes": ["src/"],
  "policy": {
    "runner": "coder", "serial": true, "fallback": "none", "retries": 0,
    "push": false, "accept_flags": false, "reset": false, "clean": false
  }
}
```

Start from a clean `main` branch:

```bash
tasks-sprint --yolo-force \
  --goal "Implement the feature" --run-name feature \
  --answers tmp/plans/feature.answers.json --plan tmp/plans/feature.json
```

The lifecycle imports the plan, creates a sprint branch and worktree, executes
tasks serially, checks the diff against task and approved scopes, and records
verified commits in the tracker. Final checks precede a fast-forward into the
unchanged approved `main`; no push is performed. Worktrees and checkpoints remain
available for inspection.

Use the same command with `--resume` to continue a clean checkpoint. Goal, answers,
plan, configuration and Git refs must still match. A dirty or unexpectedly advanced
worktree requires inspection; it is never reset, cleaned or blindly rerun.

## Development checks

```bash
python3 -m pip install -e '.[dev]'
bash scripts/test_python_lint.sh
python3 -m pytest -q
bash scripts/test_spec_lint.sh
```

CI requires lint, type checking, all Python tests, shell tests and distribution
builds to pass. VS Code tracker commands are provided in `.vscode/tasks.json`.

## Scripts

### Core tracker

| File | Purpose |
|---|---|
| `tasks.py` | Task tracker CLI: sprints → tasks → subtasks, SQLite-backed (`tasks/tasks.db`). |
| `tasks_init.py` | Create an empty task database from scratch. |
| `tasks_mcp.py` | MCP server (stdio) exposing tracker operations as tools. |
| `tasks_cli.py` | CLI parser and entry point for the tracker. |
| `tasks_commands.py` | Command handlers for the tracker. |
| `tasks_model.py` | Core data model (nodes, statuses, walk/find/effective). |
| `tasks_storage.py` | SQLite storage layer. |
| `tasks_render.py` | Markdown rendering of the task view. |
| `tasks_watch.py` | Background watcher keeping the view and snapshot in sync with the DB. |
| `tasks_plan_import.py` | Atomic plan import for the sprint planner. |

### Sprint automation

| File | Purpose |
|---|---|
| `sprint_plan.py` | Sprint plan tooling for the planner agent. |
| `sprint-auto.sh` / `sprint_auto.py` | Verified sprint execution and fast-forward integration (`--yolo-force`, `--resume`). |
| `sprint_auto_state.py` | Durable state, checkpoint and process-supervision primitives. |
| `sprint_auto_preflight.py` | Read-only approval preflight for the automated lifecycle. |
| `sprint_auto_prepare.py` | Prepare approved plans and branches. |
| `sprint_auto_execute.py` | Execute and accept guarded tasks. |
| `sprint_auto_resume.py` | Resume and finish owned sprints. |
| `apply-plan.sh` | Import a validated sprint plan (`tmp/plans/<name>.json`) into the tracker. |
| `run-task.sh` / `task_runner.py` | Run configured coders and verify actual changes before committing. |
| `runner_config.py` | Provider-independent runner registry, default selection and configuration validation. |
| `task-report.sh` | Report delegation progress to the tracker. |
| `run_summary.py` | Write a run summary JSON for finished runs. |
| `run_stats.py` | Delivery-speed statistics for delegation runners. |

### Spec / quality

| File | Purpose |
|---|---|
| `spec-lint.sh` | Quality gate for delegated task specs. |

### Tests

| File | Purpose |
|---|---|
| `test_tasks.py` | Tests for `tasks.py`. |
| `test_tasks_mcp.py` | Tests for `tasks_mcp.py`. |
| `test_tasks_storage.py` | Tests for `tasks_storage.py`. |
| `test_tasks_plan_import.py` | Tests for `tasks_plan_import.py`. |
| `test_tasks_watch.py` | Tests for `tasks_watch.py`. |
| `test_run_stats.py` | Tests for `run_stats.py`. |
| `test_sprint_auto.py` | Tests for `sprint-auto.sh`. |
| `test_sprint_auto_state.py` | Tests for `sprint_auto_state.py`. |
| `test_sprint_auto_preflight.py` | Tests for `sprint_auto_preflight.py`. |
| `test_sprint_auto_prepare.py` | Tests for `sprint_auto_prepare.py`. |
| `test_sprint_auto_execute.py` | Tests for `sprint_auto_execute.py`. |
| `test_sprint_auto_resume.py` | Tests for `sprint_auto_resume.py`. |
| `test_spec_lint.sh` | Tests for `spec-lint.sh`. |

### Test runners

| File | Purpose |
|---|---|
| `test_python_fast.sh` | Fast Python tests (in-process). |
| `test_python_slow.sh` | Slow/subprocess-heavy Python tests. |
| `test_python_lint.sh` | Ruff + mypy. |

## License

MIT — see [LICENSE](LICENSE).
