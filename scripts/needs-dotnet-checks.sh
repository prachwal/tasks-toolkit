#!/usr/bin/env bash
# Exit 0 when the diff needs the .NET checks (it touches C#/MSBuild/format configuration), 1 when the
# change is docs/scripts only, so the caller can run the matching script tests instead (task 6.6).
#
#   scripts/needs-dotnet-checks.sh <dir> <base> [<head>]
#
# With an empty <base> the working tree against HEAD is inspected.
set -euo pipefail

dir="${1:-.}"
base="${2:-}"
head="${3:-HEAD}"

if [ -n "$base" ]; then
    files="$(git -C "$dir" diff --name-only "$base" "$head" 2>/dev/null || true)"
else
    files="$(git -C "$dir" diff --name-only HEAD 2>/dev/null || true)"
    files+=$'\n'"$(git -C "$dir" ls-files --others --exclude-standard 2>/dev/null || true)"
fi

if printf '%s\n' "$files" | grep -qE '\.(cs|csproj|props|targets|slnx|sln|editorconfig)$|(^|/)(Directory\.[A-Za-z]+\.props|stylecop\.json|global\.json|\.editorconfig)$'; then
    exit 0
fi
exit 1
