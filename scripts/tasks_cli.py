"""CLI parser and entry point for the task tracker.

Re-exports from tasks.py for backward compatibility.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.tasks import build_parser, main  # noqa: E402, F401

if __name__ == "__main__":
    sys.exit(main())
