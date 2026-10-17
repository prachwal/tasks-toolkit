# tasks-toolkit

A small, self-contained task tracker (sprints → tasks → subtasks) with a CLI and an MCP server,
plus helpers used by the VibeEmulator project.

## Contents

- `scripts/tasks.py` — CLI for the tracker (SQLite-backed).
- `scripts/tasks_mcp.py` — MCP (stdio) server exposing tracker operations as tools.
- `scripts/tasks_init.py` — create an empty database.
- `scripts/sprint_auto_*.py`, `scripts/sprint_plan.py`, `scripts/run_stats.py` — automation helpers.
- `scripts/worktree.sh`, `scripts/*-task.sh` — delegation runners.
- `pyproject.toml` — ruff/mypy/pytest configuration.
- `knowledge/`, `agents/` — design notes and tracker docs.

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

Override the data location with `VIBE_TASKS_ROOT`:

```bash
export VIBE_TASKS_ROOT=/path/to/data
python3 scripts/tasks.py overview
```

## MCP (OpenCode)

```jsonc
"tasks": {
  "type": "local",
  "command": ["python3", "{env:HOME}/src/tasks-toolkit/scripts/tasks_mcp.py"],
  "cwd": "{env:HOME}/src/tasks-toolkit",
  "environment": { "VIBE_TASKS_ROOT": "{env:HOME}/src/tasks-toolkit" }
}
```

## License

MIT — see [LICENSE](LICENSE).
