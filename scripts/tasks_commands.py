"""Command handlers for the task tracker.

Re-exports from tasks.py for backward compatibility.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tasks import *  # noqa: E402, F401, F403