#!/bin/bash
# Pre-edit file lock check.
# Blocks Edit/Write if another active session claims the same file.
# Reads the tool input from stdin to extract the file path.

HOOK_DIR="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)"
[ -f "$HOOK_DIR/../lib/config.sh" ] && source "$HOOK_DIR/../lib/config.sh"
[ -f "$HOOK_DIR/../lib/session.sh" ] && source "$HOOK_DIR/../lib/session.sh"
[ -f "$HOOK_DIR/../lib/supabase.sh" ] && source "$HOOK_DIR/../lib/supabase.sh"
[ -f "$HOOK_DIR/../lib/log.sh" ] && source "$HOOK_DIR/../lib/log.sh"
[ -f "$HOOK_DIR/../lib/wire.sh" ] && source "$HOOK_DIR/../lib/wire.sh"

# Read hook input from stdin
INPUT=$(cat)

# Extract tool name — only check Edit and Write tools
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)
case "$TOOL_NAME" in
  Edit|Write) ;;
  *) exit 0 ;;
esac

# Extract file_path from tool input
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)
[ -z "$FILE_PATH" ] && exit 0

# Skip non-source files
case "$(basename "$FILE_PATH")" in
  *.md|*.json|*.lock|*.log|*.txt|*.env*) exit 0 ;;
esac

# Compute the relative path from the git repo root for accurate matching.
# files_touched stores relative paths like "hooks/auto-register.sh".
# Matching on basename alone causes false positives when two different files
# share the same name (e.g. hooks/auto-register.sh vs scripts/auto-register.sh).
FILE_DIR=$(dirname "$FILE_PATH")
GIT_ROOT=$(git -C "$FILE_DIR" rev-parse --show-toplevel 2>/dev/null || echo "")
if [ -n "$GIT_ROOT" ]; then
  REL_PATH="${FILE_PATH#"$GIT_ROOT"/}"
else
  # Fallback: use the full path if not in a git repo
  REL_PATH="$FILE_PATH"
fi

# Get our session ID (cc-PPID format, matches session_locks)
MY_ID="cc-${PPID}"

# Check cached peers file (updated by auto-register hook).
# PEERS_FILE env var override is supported so the regression test in
# token-watch/tests/test_auto_wire.py can point the hook at a fixture file
# without touching the live /tmp/claude-peers.json that other sessions read.
PEERS_FILE="${PEERS_FILE:-/tmp/claude-peers.json}"
[ ! -f "$PEERS_FILE" ] && exit 0

# Check if any OTHER active session has this file in files_touched
# Skip peers with stale heartbeats (>5 min) — they're zombies
CONFLICT_INFO=$(python3 -c "
import json, sys
from datetime import datetime, timezone, timedelta
try:
    with open('$PEERS_FILE') as f:
        sessions = json.load(f)
    my_id = '$MY_ID'
    rel_path = '$REL_PATH'
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(minutes=5)
    for s in sessions:
        if s['session_id'] == my_id:
            continue
        # Skip sessions with stale heartbeats
        hb = s.get('heartbeat_at', '')
        if hb:
            try:
                hb_dt = datetime.fromisoformat(hb.replace('Z', '+00:00'))
                if hb_dt < stale_cutoff:
                    continue
            except Exception:
                pass
        for f in (s.get('files_touched') or []):
            if f == rel_path:
                print(f\"{s['session_id'][:35]} ({s.get('task_name','')[:30]})\")
                print(f\"SID:{s['session_id']}\")
                sys.exit(0)
except:
    pass
" 2>/dev/null)

if [ -n "$CONFLICT_INFO" ]; then
  DISPLAY=$(echo "$CONFLICT_INFO" | grep -v '^SID:' | head -1)
  OWNER_SID=$(echo "$CONFLICT_INFO" | grep '^SID:' | head -1 | cut -d: -f2)

  # Auto-send file_release request to owning session (non-blocking)
  if [ -n "$OWNER_SID" ] && type msg_request_file_release &>/dev/null; then
    msg_request_file_release "$FILE_PATH" "$OWNER_SID" &
  fi

  DASHBOARD="${DASHBOARD_URL:-}"
  type bs_log &>/dev/null && bs_log "WARN" "file-lock-check" "blocked edit of ${REL_PATH} — conflict with ${DISPLAY}, sent release request"
  echo "BLOCKED: $REL_PATH is being edited by another session: $DISPLAY"
  echo "A file_release request has been sent automatically."
  echo "Dashboard: $DASHBOARD"
  exit 2
fi

exit 0
