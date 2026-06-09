#!/usr/bin/env bash
# PostToolUse hook: track external-engine usage to Supabase ai_capacity_ledger.
#
# Fires after a Bash tool call; if the command invoked an external engine
# (codex/gemini/grok/kimi/minimax), logs one row to ai_capacity_ledger. This is
# the write-path that de-orphans that table and complements Pillar 3 routing
# (bin/bs-route classify → bs-dispatch run → engine-tracker record).
#
# Credentials come from .env via lib/config.sh — no baked-in secrets.
# Best-effort: any failure exits 0 (never breaks the tool call).

set -uo pipefail

HOOK_DIR="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)"
[ -f "$HOOK_DIR/../lib/config.sh" ] && source "$HOOK_DIR/../lib/config.sh"
[ -f "$HOOK_DIR/../lib/supabase.sh" ] && source "$HOOK_DIR/../lib/supabase.sh"
[ -f "$HOOK_DIR/../lib/log.sh" ] && source "$HOOK_DIR/../lib/log.sh"

INPUT=$(cat)

TOOL_NAME=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_name',''))" 2>/dev/null || echo "")
[ "$TOOL_NAME" != "Bash" ] && exit 0

COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" 2>/dev/null || echo "")
[ -z "$COMMAND" ] && exit 0

# Detect the engine from the command signature.
ENGINE=""
CMD_LOWER=$(echo "$COMMAND" | tr '[:upper:]' '[:lower:]')
if   echo "$CMD_LOWER" | grep -qF "codex exec"; then ENGINE="codex"
elif echo "$CMD_LOWER" | grep -qF "gemini -p";  then ENGINE="gemini"
elif echo "$CMD_LOWER" | grep -qF "grok -p";    then ENGINE="grok"
elif echo "$CMD_LOWER" | grep -qF "kimi";       then ENGINE="kimi"
elif echo "$CMD_LOWER" | grep -qF "minimax";    then ENGINE="minimax"
fi
[ -z "$ENGINE" ] && exit 0

# No creds → best-effort no-op.
{ [ -z "${SUPA_URL:-}" ] || [ -z "${SUPA_KEY:-}" ]; } && exit 0

# Platform constraint: claude | codex | gemini | grok. kimi/minimax aren't
# platform values, so log them under platform=claude with the engine prefixed
# into task_id.
if [ "$ENGINE" = "kimi" ] || [ "$ENGINE" = "minimax" ]; then
  PLATFORM="claude"
  TASK_ID="${ENGINE}|$(echo "$COMMAND" | tr '\n' ' ' | sed 's/  */ /g' | cut -c1-115)"
else
  PLATFORM="$ENGINE"
  TASK_ID=$(echo "$COMMAND" | tr '\n' ' ' | sed 's/  */ /g' | cut -c1-120)
fi

SESSION_ID="cc-${PPID}"
NOW_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Active account label (top-level "active" in accounts.json; empty → DB default).
ACCOUNT=$(python3 -c "
import json, os
try:
    d = json.load(open(os.path.expanduser('~/.claude/accounts.json')))
    print(d.get('active', '') or '')
except Exception:
    pass
" 2>/dev/null || true)

PAYLOAD=$(python3 -c "
import json, sys
obj = {
    'platform': sys.argv[1],
    'task_id': sys.argv[2],
    'session_type': 'interactive',
    'started_at': sys.argv[3],
    'ended_at': sys.argv[3],
    'duration_seconds': 0,
    'outcome': 'completed',
    'notes': 'session:' + sys.argv[4] + ' engine:' + sys.argv[5],
}
if sys.argv[6]:
    obj['account'] = sys.argv[6]
print(json.dumps(obj))
" "$PLATFORM" "$TASK_ID" "$NOW_ISO" "$SESSION_ID" "$ENGINE" "$ACCOUNT" 2>/dev/null || exit 0)

if type supa_post &>/dev/null; then
  supa_post "ai_capacity_ledger" "$PAYLOAD" >/dev/null 2>&1 || true
else
  curl -s -X POST "${SUPA_URL%/}/rest/v1/ai_capacity_ledger" \
    -H "apikey: ${SUPA_KEY}" -H "Authorization: Bearer ${SUPA_KEY}" \
    -H "Content-Type: application/json" -H "Prefer: return=minimal" \
    -d "$PAYLOAD" >/dev/null 2>&1 || true
fi

type bs_log &>/dev/null && bs_log "INFO" "engine-tracker" "logged ${ENGINE} call for ${SESSION_ID}" 2>/dev/null || true
exit 0
