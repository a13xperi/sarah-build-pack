#!/bin/bash
# Install the capacity-snapshot cron job (idempotent).
#
# Adds a */5 crontab entry that snapshots account capacity to Supabase.
# Safe to re-run: if a capacity-snapshot line already exists, it is replaced
# rather than duplicated. Use --uninstall to remove it.

set -euo pipefail

CRON_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$CRON_DIR/capacity-snapshot.sh"
LOG="/tmp/battlestation/cron.log"
MARKER="# battlestation: capacity-snapshot"
ENTRY="*/5 * * * * ${SCRIPT} >> ${LOG} 2>&1  ${MARKER}"

chmod +x "$SCRIPT" 2>/dev/null || true
mkdir -p /tmp/battlestation 2>/dev/null || true

# Current crontab (empty string if none).
CURRENT="$(crontab -l 2>/dev/null || true)"

# Drop any existing capacity-snapshot lines (match our marker or the script path).
FILTERED="$(printf '%s\n' "$CURRENT" | grep -v -F "$MARKER" | grep -v -F "capacity-snapshot.sh" || true)"

if [ "${1:-}" = "--uninstall" ]; then
  printf '%s\n' "$FILTERED" | grep -v '^$' | crontab - 2>/dev/null || crontab -r 2>/dev/null || true
  echo "Removed capacity-snapshot cron entry."
  exit 0
fi

# Append the fresh entry and install.
{
  printf '%s\n' "$FILTERED" | grep -v '^$' || true
  printf '%s\n' "$ENTRY"
} | crontab -

echo "Installed capacity-snapshot cron (every 5 min):"
echo "  $ENTRY"
echo "Logs: $LOG  and  /tmp/battlestation/battlestation.log"
