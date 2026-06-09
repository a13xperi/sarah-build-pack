#!/bin/bash
# Session guard — capacity guard that surfaces a best-alternate account.
#
# Sourced by hooks/auto-register.sh:
#   source "$HOOK_DIR/../lib/session-guard.sh" 2>/dev/null && session_guard_check || true
#
# Best-effort and non-fatal. When the active account's weekly usage is high,
# this writes the least-used alternate account label to
#   /tmp/claude-best-alt-account
# which hooks/token-tracker.sh already reads to suggest a switch in its
# hard-block message. It performs NO keychain or account-switch side effects —
# that belongs to lanes/accounts.sh (intentionally out of scope here).
#
# Reads the `account_capacity` table. That table has no snapshot writer in this
# pack yet, so in the common case it's empty/unreachable — in which case this
# returns cleanly without writing anything.
#
# Requires (already sourced by caller): config.sh, supabase.sh, log.sh

BEST_ALT_FILE="/tmp/claude-best-alt-account"
SG_SWITCH_AT_PCT="${SG_SWITCH_AT_PCT:-70}"   # weekly % over which we suggest a switch

session_guard_check() {
  # Need Supabase + jq to evaluate capacity; bail quietly otherwise.
  [ -z "${SUPA_URL:-}" ] && return 0
  command -v jq >/dev/null 2>&1 || return 0
  type supa_get >/dev/null 2>&1 || return 0

  local rows
  rows=$(supa_get "account_capacity" "select=account,seven_pct,is_active" 2>/dev/null)
  # Empty table / error / no rows → nothing to guard against.
  [ -z "$rows" ] && return 0
  [ "$rows" = "[]" ] && return 0
  echo "$rows" | grep -q '"error"' 2>/dev/null && return 0

  # Decide via jq: if the active account is over threshold, pick the
  # alternate account with the lowest seven_pct and print its label.
  local best
  best=$(echo "$rows" | jq -r --argjson thr "$SG_SWITCH_AT_PCT" '
    (map(select(.is_active == true)) | .[0].seven_pct // 0) as $active_pct
    | if $active_pct >= $thr then
        ( map(select(.is_active != true))
          | sort_by(.seven_pct // 100)
          | .[0].account // empty )
      else empty end
  ' 2>/dev/null)

  if [ -n "$best" ]; then
    echo "$best" > "$BEST_ALT_FILE" 2>/dev/null
    bs_log "WARN" "session-guard" "active account over ${SG_SWITCH_AT_PCT}% weekly — best alt: ${best}" 2>/dev/null || true
  else
    # Under threshold (or no viable alt) — clear any stale hint.
    rm -f "$BEST_ALT_FILE" 2>/dev/null
  fi

  return 0
}
