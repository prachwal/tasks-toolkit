"""Provider-independent runner registry and execution configuration.

Each registered command receives a task spec on stdin and runs in the task
worktree. Providers and their CLI arguments belong in project configuration.
"""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

CONFIG_FILE = ".tasks-toolkit.json"
RUNNER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")

# Always protected: version control, the tracker's own data and the config itself.
# Project-specific paths (agent or tool config dirs) go in `protected_paths` in CONFIG_FILE.
BUILTIN_PROTECTED = (".git", ".githooks", "tmp", "tasks/tasks.db", "tasks/.lock", CONFIG_FILE)


class RunnerError(Exception):
    """Runner configuration or an execution prerequisite is invalid."""


def _normalize_protected_path(path: object) -> str:
    """Normalize harmless dot/separator spelling and reject ambiguous or escaping paths."""
    if not isinstance(path, str) or not path or path != path.strip():
        raise RunnerError("protected_paths entries must be non-empty repo-relative paths")
    if (PurePosixPath(path).is_absolute() or PureWindowsPath(path).drive or "\\" in path
            or ".." in path.split("/") or any(ord(char) < 32 for char in path)):
        raise RunnerError(f"protected_paths entry must be repo-relative without '..': {path!r}")
    normalized = str(PurePosixPath(path))
    if normalized == ".":
        raise RunnerError("protected_paths entries must name a file or directory inside the project")
    return normalized


def protected_paths(root: Path) -> tuple[str, ...]:
    """Built-in protected paths plus `protected_paths` from the project config, if present."""
    path = root / CONFIG_FILE
    extra: object = []
    if path.is_file():
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise RunnerError("runner config must be an object")
        extra = config.get("protected_paths", [])
    if not isinstance(extra, list):
        raise RunnerError("protected_paths must be a list of repo-relative paths")
    return BUILTIN_PROTECTED + tuple(_normalize_protected_path(p) for p in extra)


def is_protected(path: str, protected: tuple[str, ...]) -> bool:
    path = str(PurePosixPath(path))
    return any(path == p or path.startswith(p + "/") for p in protected)


def valid_runner_name(name: object) -> bool:
    return isinstance(name, str) and name != "none" and RUNNER_NAME.fullmatch(name) is not None


def valid_command(command: object) -> bool:
    return isinstance(command, list) and bool(command) and all(isinstance(v, str) and bool(v) for v in command)


def runner_command(config: dict, runner: str) -> list[str]:
    if not valid_runner_name(runner):
        raise RunnerError("runner must be a name containing letters, digits, underscores or hyphens; 'none' is reserved")
    command = config.get("runners", {}).get(runner)
    if not valid_command(command):
        raise RunnerError(f"configure runners.{runner} in {CONFIG_FILE}; the command receives the spec on stdin")
    return list(command)


def select_runner(config: dict, requested: str | None = None) -> str:
    name = requested if requested is not None else config.get("default_runner")
    if name is None:
        raise RunnerError(f"choose --runner or policy.runner, or set default_runner in {CONFIG_FILE}")
    runner_command(config, name)
    return str(name)


def load_config(root: Path) -> dict:
    path = root / CONFIG_FILE
    if not path.is_file():
        raise RunnerError(f"create {CONFIG_FILE} with runners and a non-empty checks list before running tasks")
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise RunnerError("runner config must be an object")
    checks = config.get("checks")
    if not isinstance(checks, list) or not checks or not all(valid_command(c) for c in checks):
        raise RunnerError("checks must be a non-empty list of command argument arrays")
    runners = config.get("runners")
    if not isinstance(runners, dict) or not runners:
        raise RunnerError("runners must be a non-empty mapping of names to command argument arrays")
    for name in runners:
        runner_command(config, name)
    if "default_runner" in config:
        if not valid_runner_name(config["default_runner"]):
            raise RunnerError("default_runner must name a configured runner")
        select_runner(config)
    timeout = config.get("timeout", 120)
    if type(timeout) is not int or timeout < 1:
        raise RunnerError("timeout must be a positive integer")
    protected_paths(root)  # validates the optional protected_paths list
    return config
