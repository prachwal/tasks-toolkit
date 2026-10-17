#!/usr/bin/env bash
# Fast Python tests — skips subprocess-heavy integration tests.
# Use during development for quick feedback (~5s instead of ~20s).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

echo "=== fast Python tests ==="
# Fast tests (no subprocess, in-process only)
PYTHONPATH=scripts python3 -m unittest \
  test_tasks_mcp \
  test_run_stats \
  test_run_summary \
  test_runner_config \
  test_tasks_watch \
  2>&1

echo ""
echo "=== Sprint 12 fast tests ==="
PYTHONPATH=scripts python3 -m unittest \
  test_sprint_auto_preflight \
  test_sprint_auto_state \
  2>&1

echo ""
echo "=== done ==="