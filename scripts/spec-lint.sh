#!/usr/bin/env bash
# spec-lint.sh — quality gate for delegated task specs.
# Rejects under-specified or oversized specs before a runner starts.
#
# Usage:  scripts/spec-lint.sh <spec-file>
# Exit:   0 valid  |  1 violations  |  2 usage error
# Env:    SPEC_LINT=off  →  skip (prints "spec-lint: skipped", exit 0)
set -euo pipefail

# ── escape hatch ──────────────────────────────────────────────────────────
if [ "${SPEC_LINT:-}" = "off" ]; then
  echo "spec-lint: skipped"
  exit 0
fi

# ── usage ─────────────────────────────────────────────────────────────────
if [ $# -lt 1 ]; then
  echo "spec-lint: usage: spec-lint.sh <spec-file>" >&2
  exit 2
fi

spec="$1"
if [ ! -f "$spec" ]; then
  echo "spec-lint: file not found: $spec" >&2
  exit 2
fi

violations=0

violate() {
  echo "spec-lint: $1: $2" >&2
  violations=$((violations + 1))
}

# ── rule 1: required sections with content ───────────────────────────────
# Use an array to preserve multi-word section names like "Live check"
required_sections=("Goal" "Files" "Tests" "Acceptance" "Live check")

for section in "${required_sections[@]}"; do
  # Find the heading line number (## Section exactly)
  heading_line=$(grep -n "^## ${section}$" "$spec" 2>/dev/null | head -1 | cut -d: -f1 || true)

  if [ -z "$heading_line" ]; then
    violate "missing-section" "## ${section} not found"
    continue
  fi

  # Find the next heading after this one (or EOF)
  total_lines=$(wc -l < "$spec")
  next_heading=$(tail -n +"$((heading_line + 1))" "$spec" | grep -n "^## " | head -1 | cut -d: -f1 || true)

  if [ -z "$next_heading" ]; then
    # Section extends to EOF — count non-empty lines after the heading
    end_line=$total_lines
  else
    end_line=$((heading_line + next_heading - 1))
  fi

  # Extract content lines between heading and next heading (exclusive of both)
  content_lines=$(sed -n "$((heading_line + 1)),${end_line}p" "$spec" | grep -c '[^[:space:]]' || true)

  if [ "$content_lines" -eq 0 ]; then
    violate "empty-section" "## ${section} has no content"
  fi
done

# ── rule 2: Files section — 1..8 entries ─────────────────────────────────
files_heading=$(grep -n "^## Files$" "$spec" 2>/dev/null | head -1 | cut -d: -f1 || true)

if [ -n "$files_heading" ]; then
  # Determine the range of the Files section
  total_lines=$(wc -l < "$spec")
  next_heading=$(tail -n +"$((files_heading + 1))" "$spec" | grep -n "^## " | head -1 | cut -d: -f1 || true)

  if [ -z "$next_heading" ]; then
    end_line=$total_lines
  else
    end_line=$((files_heading + next_heading - 1))
  fi

  # Count entries: lines starting with "- " (optionally prefixed by NEW/EDIT)
  file_count=$(sed -n "$((files_heading + 1)),${end_line}p" "$spec" | grep -c "^- " || true)

  if [ "$file_count" -eq 0 ]; then
    violate "files-count" "## Files lists 0 entries (need 1-8)"
  elif [ "$file_count" -gt 8 ]; then
    violate "files-count" "## Files lists ${file_count} entries (max 8)"
  fi
fi

# ── rule 3: size budget ──────────────────────────────────────────────────
line_count=$(wc -l < "$spec")
byte_count=$(wc -c < "$spec")

if [ "$line_count" -gt 120 ]; then
  violate "size-lines" "spec is ${line_count} lines (max 120)"
fi

if [ "$byte_count" -gt 9000 ]; then
  violate "size-bytes" "spec is ${byte_count} bytes (max 9000)"
fi

# ── rule 4: Acceptance or Live check mentions a concrete command ──────────
has_command=0

for section in "Acceptance" "Live check"; do
  sec_heading=$(grep -n "^## ${section}$" "$spec" 2>/dev/null | head -1 | cut -d: -f1 || true)

  if [ -n "$sec_heading" ]; then
    total_lines=$(wc -l < "$spec")
    next_heading=$(tail -n +"$((sec_heading + 1))" "$spec" | grep -n "^## " | head -1 | cut -d: -f1 || true)

    if [ -z "$next_heading" ]; then
      end_line=$total_lines
    else
      end_line=$((sec_heading + next_heading - 1))
    fi

    # Check for backticks or lines starting with "$ "
    has_backtick=$(sed -n "$((sec_heading + 1)),${end_line}p" "$spec" | grep -c '`' || true)
    has_dollar=$(sed -n "$((sec_heading + 1)),${end_line}p" "$spec" | grep -c '^\$ ' || true)

    if [ "$has_backtick" -gt 0 ] || [ "$has_dollar" -gt 0 ]; then
      has_command=1
    fi
  fi
done

if [ "$has_command" -eq 0 ]; then
  violate "no-command" "## Acceptance and ## Live check do not mention a concrete command (backticks or '\$ ')"
fi

# ── result ────────────────────────────────────────────────────────────────
if [ "$violations" -gt 0 ]; then
  exit 1
fi

echo "spec-lint: OK"
exit 0
