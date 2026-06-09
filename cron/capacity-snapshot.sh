#!/bin/bash
# Snapshot current Claude account capacity to Supabase `account_capacity`.
#
# Pillar 1 write-path: token-watch and session-guard READ account_capacity,
# but nothing populated it until this job. Reads live rate-limit data from the
# statusline debug file and the active account from accounts.json, upserts the
# active account's usage, decay-corrects idle accounts, and appends an audit
# row to account_capacity_history.
#
# Intended to run via cron every 5 minutes (see cron/README.md).
# Credentials come from .env via lib/config.sh — no baked-in secrets.

set -euo pipefail

BATTLESTATION_HOME="$(cd "$(dirname "$0")/.." && pwd)"
source "$BATTLESTATION_HOME/lib/config.sh"
source "$BATTLESTATION_HOME/lib/supabase.sh"
source "$BATTLESTATION_HOME/lib/log.sh"

COMPONENT="capacity-snapshot"

DEBUG_FILE="/tmp/statusline-debug.json"
ACCOUNTS_FILE="$HOME/.claude/accounts.json"

# --- Read active account ---
if [ ! -f "$ACCOUNTS_FILE" ]; then
  bs_log "WARN" "$COMPONENT" "No accounts.json found at ${ACCOUNTS_FILE}"
  exit 0
fi
ACTIVE_ACCOUNT=$(python3 -c "import json; print(json.load(open('${ACCOUNTS_FILE}'))['active'])" 2>/dev/null || echo "A")

# --- Read rate-limit data from statusline debug ---
if [ ! -f "$DEBUG_FILE" ]; then
  bs_log "WARN" "$COMPONENT" "No statusline debug file at ${DEBUG_FILE}"
  exit 0
fi

# Extract fields using python3 for reliable JSON parsing.
# Tolerant of BOTH statusline shapes:
#   nested  — rate_limits.five_hour.used_percentage / .resets_at  (repo convention)
#   flat    — rate_limits.five_hour_used_pct / five_hour_resets_at (legacy)
read -r HAS_DATA FIVE_PCT FIVE_RESETS SEVEN_PCT SEVEN_RESETS ACCOUNT_NAME <<< $(python3 -c "
import json
try:
    d = json.load(open('${DEBUG_FILE}'))
    rl = d.get('rate_limits') or d

    def pick(window, nested_field, flat_key):
        w = rl.get(window)
        if isinstance(w, dict) and w.get(nested_field) is not None:
            return w.get(nested_field)
        return rl.get(flat_key)

    fp = pick('five_hour', 'used_percentage', 'five_hour_used_pct')
    fr = pick('five_hour', 'resets_at',       'five_hour_resets_at')
    sp = pick('seven_day', 'used_percentage', 'seven_day_used_pct')
    sr = pick('seven_day', 'resets_at',       'seven_day_resets_at')

    # The statusline payload only carries live rate-limit data intermittently.
    # When the fields are absent or null, do NOT read that as 0% used — that
    # would zero out the active account's real capacity. Flag presence so the
    # caller can skip the write and preserve the last good snapshot.
    has = '1' if (fp is not None or sp is not None) else '0'
    print(has, fp or 0, fr or 0, sp or 0, sr or 0, d.get('account_name', '') or '')
except Exception:
    print('0 0 0 0 0 unknown')
" 2>/dev/null)

# --- Upsert to Supabase ---
# Only write the active account's capacity when the statusline actually carried
# live rate-limit data. Otherwise leave the last good snapshot in place rather
# than overwriting real usage with zeros.
if [ "${HAS_DATA:-0}" != "1" ]; then
  bs_log "WARN" "$COMPONENT" "No live rate-limit data in statusline payload — preserving last snapshot for ${ACTIVE_ACCOUNT} (not overwriting with zeros)"
else
  # First try PATCH (update existing row)
  BODY=$(python3 -c "
import json
print(json.dumps({
    'account_name': '${ACCOUNT_NAME}',
    'five_hour_used_pct': ${FIVE_PCT:-0},
    'five_hour_resets_at': ${FIVE_RESETS:-0},
    'seven_day_used_pct': ${SEVEN_PCT:-0},
    'seven_day_resets_at': ${SEVEN_RESETS:-0},
    'snapshot_at': '$(date -u +%Y-%m-%dT%H:%M:%SZ)',
    'is_active': True
}))
")

  HTTP_CODE=$(supa_patch "account_capacity" "account=eq.${ACTIVE_ACCOUNT}" "$BODY")

  if [ "$HTTP_CODE" = "204" ] || [ "$HTTP_CODE" = "200" ]; then
    bs_log "INFO" "$COMPONENT" "Snapshot updated for account ${ACTIVE_ACCOUNT}: 5h=${FIVE_PCT}% 7d=${SEVEN_PCT}%"
  else
    # Row might not exist yet — try POST
    BODY_WITH_PK=$(python3 -c "
import json
d = json.loads('${BODY}')
d['account'] = '${ACTIVE_ACCOUNT}'
print(json.dumps(d))
")
    HTTP_CODE=$(supa_post "account_capacity" "$BODY_WITH_PK")
    if [ "$HTTP_CODE" = "201" ] || [ "$HTTP_CODE" = "200" ]; then
      bs_log "INFO" "$COMPONENT" "Snapshot created for account ${ACTIVE_ACCOUNT}"
    else
      bs_log "ERROR" "$COMPONENT" "Failed to write snapshot (HTTP ${HTTP_CODE})"
      exit 1
    fi
  fi
fi

# Mark other accounts inactive AND decay-correct their stale capacity.
#
# Dormant accounts never get a fresh statusline snapshot, so their stored
# usage is frozen at the last-active value and reads as "drained" forever
# (e.g. account B shows 100% 7d-used from an old snapshot). But Claude's
# 5h / 7d limits are ROLLING windows: once an account sits idle past the
# window length, that usage ages out and the account is fully available
# again. We recompute that here so the table tells the truth for B/C, not
# just for the active account. All consumers read this table directly, so
# correcting it here fixes every reader with no per-consumer change.
NOW_EPOCH=$(date -u +%s)
SEVEN_AGO=$(date -u -v-7d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%SZ)
for ACCT in A B C; do
  if [ "$ACCT" = "$ACTIVE_ACCOUNT" ]; then
    continue
  fi

  ROW=$(supa_get "account_capacity" "account=eq.${ACCT}&select=five_hour_used_pct,five_hour_resets_at,seven_day_used_pct,seven_day_resets_at,snapshot_at&limit=1" || true)
  if [ -z "$ROW" ] || [ "$ROW" = "[]" ]; then
    # No row yet (or fetch failed) — preserve original behaviour.
    supa_patch "account_capacity" "account=eq.${ACCT}" '{"is_active":false}' >/dev/null 2>&1 || true
    continue
  fi

  # Idle = zero ledger rows in the trailing 7 days. A row is logged per
  # session, so no rows => the account has done no work inside the window.
  IDLE=1
  LEDGER=$(supa_get "ai_capacity_ledger" "account=eq.${ACCT}&created_at=gt.${SEVEN_AGO}&select=id&limit=1" || true)
  if [ -n "$LEDGER" ] && [ "$LEDGER" != "[]" ]; then
    IDLE=0
  fi

  # Compute the decayed PATCH body. Falls back to a bare is_active:false
  # (original behaviour) if anything goes wrong, so set -e never aborts here.
  BODY=$(NOW_EPOCH="$NOW_EPOCH" IDLE="$IDLE" python3 -c "
import json, os, sys, datetime
now = int(os.environ['NOW_EPOCH'])
idle = os.environ['IDLE'] == '1'
row = json.load(sys.stdin)[0]

def epoch(ts):
    try:
        return datetime.datetime.fromisoformat(ts).timestamp()
    except Exception:
        return 0.0

snap = epoch(row.get('snapshot_at') or '')
age = (now - snap) if snap else 1e12          # missing snapshot => treat as ancient
five_reset_at  = row.get('five_hour_resets_at') or 0
seven_reset_at = row.get('seven_day_resets_at') or 0

# A rolling window is 'aged out' (=> 0% used) when the account has been idle
# long enough for that window to fully elapse since its last measurement, OR
# the captured reset timestamp itself has already passed.
five_full  = (idle and age > 5 * 3600)  or (five_reset_at  > 0 and now > five_reset_at)
seven_full = (idle and age > 7 * 86400) or (seven_reset_at > 0 and now > seven_reset_at)

body = {'is_active': False}
if five_full:
    body['five_hour_used_pct'] = 0
if seven_full:
    body['seven_day_used_pct'] = 0

# Only refresh snapshot_at when BOTH windows are confidently resolved (either
# reset to 0, or the stored value is still inside its window). Otherwise leave
# the old timestamp so genuine staleness stays visible to readers.
five_ok  = five_full  or age < 5 * 3600
seven_ok = seven_full or age < 7 * 86400
if five_ok and seven_ok and ('five_hour_used_pct' in body or 'seven_day_used_pct' in body):
    body['snapshot_at'] = datetime.datetime.utcfromtimestamp(now).strftime('%Y-%m-%dT%H:%M:%SZ')

print(json.dumps(body))
" <<<"$ROW" 2>/dev/null) || BODY='{"is_active":false}'
  [ -z "$BODY" ] && BODY='{"is_active":false}'

  supa_patch "account_capacity" "account=eq.${ACCT}" "$BODY" >/dev/null 2>&1 || true
done

# --- Append to capacity history (audit log) ---
# Only write history when we have real values — skip if both are 0 (debug file had no data)
if [ "${FIVE_PCT:-0}" != "0" ] || [ "${SEVEN_PCT:-0}" != "0" ]; then
  HIST_BODY=$(python3 -c "
import json
print(json.dumps({
    'account': '${ACTIVE_ACCOUNT}',
    'five_hour_used_pct': ${FIVE_PCT:-0},
    'seven_day_used_pct': ${SEVEN_PCT:-0},
    'is_active': True,
    'source': 'cron'
}))
")
  supa_post "account_capacity_history" "$HIST_BODY" >/dev/null 2>&1 || true
fi
