"""Durable state, checkpoint and process-supervision primitives (Sprint 12, task 12.3).

Runtime-generated state lives under tmp/plans/NAME.auto.json with an associated
exclusive lock.  Importing this module writes nothing.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

STATE_VERSION = 1


@dataclass
class RunState:
    """Durable lifecycle checkpoint for an automated sprint run."""
    version: int = STATE_VERSION
    repo_root: str = ""
    git_common_dir: str = ""
    run_name: str = ""
    goal: str = ""
    answers_fingerprint: str = ""
    allowed_scopes: list = field(default_factory=list)
    initial_main_sha: str = ""
    runner: str = ""
    model: str = ""
    timeout: int = 120
    plan_fingerprint: str = ""
    config_fingerprint: str = ""
    spec_fingerprints: dict = field(default_factory=dict)
    tracker_uids: dict = field(default_factory=dict)
    expected_refs: dict = field(default_factory=dict)
    owned_artifacts: list = field(default_factory=list)
    operation_intent: str = ""
    command_results: list = field(default_factory=list)
    verified_commit: str = ""
    completed_tasks: dict = field(default_factory=dict)
    child_pid: int = 0
    child_pgid: int = 0
    heartbeat_ts: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> RunState:
        if data.get("version") != STATE_VERSION:
            raise ValueError(f"unknown state version: {data.get('version')}")
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


def state_path(root: Path, run_name: str) -> Path:
    """Path to the runtime state file (under tmp/plans/)."""
    return root / "tmp" / "plans" / f"{run_name}.auto.json"


def lock_path(root: Path, run_name: str) -> Path:
    """Path to the exclusive lock file."""
    return root / "tmp" / "plans" / f"{run_name}.auto.lock"


class StateLock:
    """Exclusive file lock for a run.  Refuses symlinked paths."""

    def __init__(self, path: Path):
        if path.is_symlink():
            raise ValueError(f"symlinked lock path refused: {path}")
        self.path = path
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # refuse if path is a symlink (TOCTOU guard)
        if self.path.is_symlink():
            raise ValueError(f"symlinked lock path refused: {self.path}")
        self._fh = open(self.path, "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self._fh:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


def write_checkpoint(path: Path, state: RunState) -> None:
    """Atomically write state to path (tmp + rename + fsync).  Refuses symlinks."""
    if path.is_symlink():
        raise ValueError(f"symlinked state path refused: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"symlinked state path refused: {path}")
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state.to_dict(), fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_checkpoint(path: Path) -> RunState | None:
    """Read state from path.  Returns None if missing or unreadable."""
    if not path.exists() or path.is_symlink():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return RunState.from_dict(data)
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        return None


def acquire_lock(root: Path, run_name: str) -> StateLock:
    return StateLock(lock_path(root, run_name))


def is_child_alive(state: RunState) -> bool:
    """Check if the child process may still be running (PID-based, no stale heartbeat)."""
    if state.child_pid <= 0:
        return False
    try:
        os.kill(state.child_pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def supervised_run(cmd: list[str], cwd: Path, timeout: int,
                   on_output=None, input_text: str | None = None) -> tuple[int, str]:
    """Run with a deadline, forwarding termination to the entire child group."""
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
    )
    previous = {}

    def forward(signum, frame):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signum)

    # Signals can only be installed in the main thread.
    import threading
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, forward)
    timed_out = False
    try:
        try:
            stdout, _ = proc.communicate(input_text.encode() if input_text is not None else None, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                stdout, _ = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                stdout, _ = proc.communicate()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    output = stdout.decode("utf-8", errors="replace")
    if on_output:
        on_output(output)
    return (124 if timed_out else proc.returncode), output


def bind_state(root: Path, git_common: Path, run_name: str, goal: str,
               answers_fp: str, scopes: list, main_sha: str,
               runner: str, model: str, timeout: int) -> RunState:
    """Create a new RunState bound to identity.  Does not write to disk."""
    return RunState(
        repo_root=str(root.resolve()),
        git_common_dir=str(git_common.resolve()),
        run_name=run_name,
        goal=goal,
        answers_fingerprint=answers_fp,
        allowed_scopes=list(scopes),
        initial_main_sha=main_sha,
        runner=runner,
        model=model,
        timeout=timeout,
    )
