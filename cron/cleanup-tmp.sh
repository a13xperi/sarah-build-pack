#!/bin/bash
# Clean up stale /tmp/claude-* and /tmp/battlestation/* files from dead sessions.
# For each session PID, if the process is gone, remove its temp state.
# Pure local — no Supabase. Intended to run via cron every 15 minutes.

set -euo pipefail

BATTLESTATION_HOME="$(cd "$(dirname "$0")/.." && pwd)"
source "$BATTLESTATION_HOME/lib/config.sh"
source "$BATTLESTATION_HOME/lib/log.sh"

COMPONENT="cleanup-tmp"
CLEANED=0

# --- /tmp/claude-directive-$PID and associated /tmp/claude-*-$PID files ---
for directive_file in /tmp/claude-directive-*; do
  [ -f "$directive_file" ] || continue
  PID="${directive_file##*-}"               # /tmp/claude-directive-12345 -> 12345
  [[ "$PID" =~ ^[0-9]+$ ]] || continue
  kill -0 "$PID" 2>/dev/null && continue    # still alive — leave it

  for f in /tmp/claude-*-"$PID" /tmp/claude-*-"$PID".*; do
    if [ -e "$f" ]; then
      rm -f "$f"
      CLEANED=$((CLEANED + 1))
    fi
  done
done

# --- /tmp/battlestation/$PID directories ---
if [ -d "/tmp/battlestation" ]; then
  for pid_dir in /tmp/battlestation/*/; do
    [ -d "$pid_dir" ] || continue
    PID="$(basename "$pid_dir")"
    [[ "$PID" =~ ^[0-9]+$ ]] || continue
    kill -0 "$PID" 2>/dev/null && continue   # still alive — leave it

    rm -rf "$pid_dir"
    CLEANED=$((CLEANED + 1))
  done
fi

if [ "$CLEANED" -gt 0 ]; then
  bs_log "INFO" "$COMPONENT" "Cleaned up ${CLEANED} stale file(s)/dir(s)"
else
  bs_log "INFO" "$COMPONENT" "No stale files to clean"
fi
