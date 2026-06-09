#!/bin/bash
# Expire stale Claude Code sessions whose heartbeat is >30 min old.
# Intended to run via cron every 5 minutes (see cron/README.md).
#
# Two phases:
#   - at >5 min stale : clear files_touched so zombies don't block live peers
#   - at >30 min stale: mark status=done + released_at (full expiry)
# Before full expiry, any session carrying a next_session_prompt has it
# promoted into session_tasks (status=blocked) so the work isn't lost.
#
# Credentials come from .env via lib/config.sh — no baked-in secrets.

set -euo pipefail

BATTLESTATION_HOME="$(cd "$(dirname "$0")/.." && pwd)"
source "$BATTLESTATION_HOME/lib/config.sh"
source "$BATTLESTATION_HOME/lib/supabase.sh"
source "$BATTLESTATION_HOME/lib/log.sh"

COMPONENT="expire-sessions"

# No creds → nothing to talk to. Degrade cleanly (don't let set -e abort on a
# failed curl), same philosophy as capacity-snapshot's guard.
if [ -z "${SUPA_URL:-}" ] || [ -z "${SUPA_KEY:-}" ]; then
  bs_log "WARN" "$COMPONENT" "SUPA_URL/SUPA_KEY not set — skipping"
  exit 0
fi

iso_ago() {  # $1 = minutes ago, ISO-8601 UTC; portable macOS/Linux
  if [[ "$(uname)" == "Darwin" ]]; then
    date -u -v-"$1"M +%Y-%m-%dT%H:%M:%SZ
  else
    date -u -d "$1 minutes ago" +%Y-%m-%dT%H:%M:%SZ
  fi
}

# ── Phase 0: clear file locks on sessions stale >5 min ──────────────
# Lighter than full expiry — just releases file claims so other sessions
# aren't blocked by zombies. %5B%5D is the URL-encoded empty array "[]".
LOCK_CUTOFF=$(iso_ago 5)
LOCK_CODE=$(supa_patch "session_locks" \
  "status=eq.active&heartbeat_at=lt.${LOCK_CUTOFF}&files_touched=not.eq.%5B%5D" \
  '{"files_touched":[]}')
if [ "$LOCK_CODE" = "204" ] || [ "$LOCK_CODE" = "200" ]; then
  bs_log "DEBUG" "$COMPONENT" "Cleared stale file locks (heartbeat >5m)"
fi

# ── Phase 1: full expiry of sessions stale >30 min ─────────────────
CUTOFF=$(iso_ago 30)

STALE_COUNT=$(supa_get "session_locks" \
  "select=id&status=eq.active&heartbeat_at=lt.${CUTOFF}" \
  | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")

if [ "$STALE_COUNT" -eq 0 ]; then
  bs_log "INFO" "$COMPONENT" "No stale sessions to expire"
  exit 0
fi

# Promote next-session prompts into session_tasks before expiring, so a
# parked "resume" prompt survives the session that wrote it. Uses env creds
# only (no hardcoded project URL).
STALE_WITH_PROMPTS=$(supa_get "session_locks" \
  "status=eq.active&heartbeat_at=lt.${CUTOFF}&next_session_prompt=not.is.null&select=session_id,task_name,repo,next_session_prompt" 2>/dev/null || echo "[]")

PROMOTED=$(echo "$STALE_WITH_PROMPTS" | python3 -c "
import sys, json, os, urllib.request

rows = json.load(sys.stdin)
if not rows:
    print(0); sys.exit(0)

KEY = os.environ.get('SUPA_KEY', '')
URL = os.environ.get('SUPA_URL', '').rstrip('/')
if not KEY or not URL:
    print(0); sys.exit(0)

count = 0
for row in rows:
    prompt = (row.get('next_session_prompt') or '').strip()
    if len(prompt) < 10:
        continue
    body = json.dumps({
        'session_id': row.get('session_id', 'unknown'),
        'task_name': f\"Resume: {(row.get('task_name') or 'unknown')[:70]}\",
        'project': row.get('repo', 'general'),
        'status': 'blocked',
        'notes': prompt[:4000],
    }).encode()
    req = urllib.request.Request(
        f'{URL}/rest/v1/session_tasks',
        data=body,
        headers={'apikey': KEY, 'Authorization': f'Bearer {KEY}',
                 'Content-Type': 'application/json', 'Prefer': 'return=minimal'},
        method='POST',
    )
    try:
        urllib.request.urlopen(req, timeout=5).close()
        count += 1
    except Exception:
        pass

print(count)
" 2>/dev/null || echo "0")

if [ "$PROMOTED" != "0" ]; then
  bs_log "INFO" "$COMPONENT" "Promoted ${PROMOTED} next-session prompt(s) to session_tasks"
fi

# Expire them.
HTTP_CODE=$(supa_patch "session_locks" \
  "status=eq.active&heartbeat_at=lt.${CUTOFF}" \
  "{\"status\":\"done\",\"released_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"notes\":\"auto-expired by cron\"}")

if [ "$HTTP_CODE" = "204" ] || [ "$HTTP_CODE" = "200" ]; then
  bs_log "INFO" "$COMPONENT" "Expired ${STALE_COUNT} stale session(s)"
else
  bs_log "ERROR" "$COMPONENT" "Failed to expire sessions (HTTP ${HTTP_CODE})"
  exit 1
fi
