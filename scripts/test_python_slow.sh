#!/usr/bin/env bash
# Slow Python tests — subprocess-heavy integration tests.
# Run before committing for full coverage.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

echo "=== slow Python tests (subprocess-heavy) ==="
PYTHONPATH=scripts python3 -m unittest test_tasks 2>&1
PYTHONPATH=scripts python3 -m unittest test_tasks_plan_import 2>&1

echo ""
echo "=== done ==="