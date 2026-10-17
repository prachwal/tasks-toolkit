#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

chmod +x .githooks/*
git config core.hooksPath .githooks
echo "Git hooks installed (core.hooksPath=.githooks)."
