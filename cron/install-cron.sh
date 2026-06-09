#!/bin/bash
# Install the battlestation maintenance cron jobs (idempotent).
#
# Jobs:
#   capacity-snapshot  every 5 min   — snapshot account capacity to Supabase
#   expire-sessions    every 5 min   — expire stale session_locks rows
#   cleanup-tmp        every 15 min  — remove /tmp state for dead session PIDs
#
# Safe to re-run: existing battlestation entries are replaced, never duplicated
# (matched by per-job marker `# battlestation: <name>` or the script path).
# Use --uninstall to remove all of them.

set -euo pipefail

CRON_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="/tmp/battlestation/cron.log"

# job-name | schedule | script
JOBS=(
  "capacity-snapshot|*/5 * * * *|${CRON_DIR}/capacity-snapshot.sh"
  "expire-sessions|*/5 * * * *|${CRON_DIR}/expire-sessions.sh"
  "cleanup-tmp|*/15 * * * *|${CRON_DIR}/cleanup-tmp.sh"
)

mkdir -p /tmp/battlestation 2>/dev/null || true

# Make scripts executable.
for spec in "${JOBS[@]}"; do
  IFS='|' read -r _name _sched script <<<"$spec"
  chmod +x "$script" 2>/dev/null || true
done

# Current crontab (empty if none).
CURRENT="$(crontab -l 2>/dev/null || true)"

# Strip any prior battlestation lines (by marker or by any of our script paths).
FILTERED="$CURRENT"
strip_line() { FILTERED="$(printf '%s\n' "$FILTERED" | grep -v -F "$1" || true)"; }
strip_line "# battlestation:"
for spec in "${JOBS[@]}"; do
  IFS='|' read -r _name _sched script <<<"$spec"
  strip_line "$script"
done

if [ "${1:-}" = "--uninstall" ]; then
  CLEAN="$(printf '%s\n' "$FILTERED" | grep -v '^$' || true)"
  if [ -z "$CLEAN" ]; then
    crontab -r 2>/dev/null || true
  else
    printf '%s\n' "$CLEAN" | crontab -
  fi
  echo "Removed battlestation cron entries."
  exit 0
fi

# Rebuild crontab: surviving user lines + a fresh entry per job.
{
  printf '%s\n' "$FILTERED" | grep -v '^$' || true
  for spec in "${JOBS[@]}"; do
    IFS='|' read -r name sched script <<<"$spec"
    printf '%s %s >> %s 2>&1  # battlestation: %s\n' "$sched" "$script" "$LOG" "$name"
  done
} | crontab -

echo "Installed battlestation maintenance cron jobs:"
for spec in "${JOBS[@]}"; do
  IFS='|' read -r name sched script <<<"$spec"
  echo "  [$name] $sched -> $script"
done
echo "Logs: $LOG  and  /tmp/battlestation/battlestation.log"
