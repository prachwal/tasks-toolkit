#!/usr/bin/env bash
# Execute a configured runner and verify actual changes; see .tasks-toolkit.json.
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Retain the early spec gate before any runner/configuration processing.
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  if [ "${args[$i]}" = "--file" ]; then
    bash "$script_dir/spec-lint.sh" "${args[$((i+1))]:?--file requires a path}"
  fi
done
exec python3 "$script_dir/task_runner.py" "$@"
