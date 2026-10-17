#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

# ── agent guidelines drift ───────────────────────────────────────────────
echo "=== agent guidelines sync ==="
python3 scripts/agents_sync.py --check

# ── .NET tests ───────────────────────────────────────────────────────────
echo "=== .NET tests ==="
# Strip --fast from args passed to dotnet (it's a test.sh-only flag)
dotnet_args=()
for arg in "$@"; do
    [ "$arg" = "--fast" ] || dotnet_args+=("$arg")
done
dotnet test VibeEmulator.slnx "${dotnet_args[@]}"

# ── worktree.sh tests ────────────────────────────────────────────────────
echo ""
echo "=== worktree.sh tests ==="
bash scripts/test_worktree.sh

# ── spec-lint tests ──────────────────────────────────────────────────────
echo ""
echo "=== spec-lint tests ==="
bash scripts/test_spec_lint.sh

# ── runner tests ─────────────────────────────────────────────────────────
echo ""
echo "=== runner tests ==="
SPEC_LINT=off bash scripts/test_runners.sh

# ── python unit tests (tracker, MCP, watch, agent config) ────────────────
echo ""
if [ "${1:-}" = "--fast" ]; then
    echo "=== python unit tests (fast) ==="
    bash scripts/test_python_fast.sh
else
    echo "=== python unit tests ==="
    python3 -m unittest discover -s scripts -p "test_*.py"
fi
