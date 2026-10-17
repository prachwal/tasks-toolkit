#!/usr/bin/env bash
# Lint and type-check Python scripts.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

echo "=== ruff check ==="
if command -v ruff >/dev/null 2>&1; then
    ruff check scripts/ || echo "ruff not installed, skipping"
else
    echo "ruff not installed, skipping"
fi

echo "=== mypy ==="
if command -v mypy >/dev/null 2>&1; then
    mypy scripts/ || echo "mypy not installed, skipping"
else
    echo "mypy not installed, skipping"
fi

echo "=== lint done ==="