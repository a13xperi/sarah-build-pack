#!/bin/bash
# Account management — A/B/C resolution + capacity-gated switching.
# Reads/writes ~/.claude/accounts.json; swaps credentials via the macOS Keychain.
#
# Satisfies the references in hooks/auto-register.sh:
#   bs_resolve_account_from_keychain "$PPID"  · bs_active_label
#
# Requires config.sh + supabase.sh sourced first. When sourced standalone
# (e.g. by bin/claude-switch) the preamble below loads them from the pack lib.
#
# Keychain operations are macOS-only (use `security`). On other platforms the
# read-only paths still work (accounts.json + Supabase); credential swaps
# degrade with a clear message instead of failing hard.

# ── Load deps if not already present (standalone use) ──────────────
if ! type supa_get &>/dev/null; then
  _ACC_LIB="${BATTLESTATION_HOME:+$BATTLESTATION_HOME/lib}"
  [ -z "$_ACC_LIB" ] && _ACC_LIB="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../lib" 2>/dev/null && pwd)"
  [ -f "$_ACC_LIB/config.sh" ]   && source "$_ACC_LIB/config.sh"
  [ -f "$_ACC_LIB/supabase.sh" ] && source "$_ACC_LIB/supabase.sh"
  [ -f "$_ACC_LIB/log.sh" ]      && source "$_ACC_LIB/log.sh"
fi

ACCOUNTS_FILE="$HOME/.claude/accounts.json"

# True only where the macOS Keychain CLI is available.
_bs_has_keychain() { [ "$(uname)" = "Darwin" ] && command -v security >/dev/null 2>&1; }

# bs_resolve_account_from_keychain [pid] — match keychain token prefix to a vault
# label, caching the result in /tmp/claude-account-$pid (the REAL account a
# session is running on). No-op off macOS.
bs_resolve_account_from_keychain() {
  local pid="${1:-$PPID}"
  local stamp_file="/tmp/claude-account-${pid}"
  [ -f "$stamp_file" ] && return 0  # already resolved
  _bs_has_keychain || return 1

  local keychain_raw
  keychain_raw=$(security find-generic-password -s "Claude Code-credentials" -a "$(whoami)" -w 2>/dev/null)
  [ -z "$keychain_raw" ] && return 1

  local token_prefix
  token_prefix=$(echo "$keychain_raw" | python3 -c \
    "import json,sys; d=json.load(sys.stdin); print(d.get('claudeAiOauth',d).get('accessToken','')[:30])" 2>/dev/null)
  [ -z "$token_prefix" ] && return 1

  local label
  for l in A B C; do
    local vault="$HOME/.claude/vaults/${l}/claudeAiOauth.json"
    if [ -f "$vault" ]; then
      local vpfx
      vpfx=$(jq -r '.accessToken[:30] // ""' "$vault" 2>/dev/null)
      if [ "$vpfx" = "$token_prefix" ]; then label="$l"; break; fi
    fi
  done

  # Fall back to accounts.json if no vault match.
  [ -z "$label" ] && label=$(jq -r '.active // "?"' "$ACCOUNTS_FILE" 2>/dev/null)
  echo "$label" > "$stamp_file"
}

# bs_active_label — label (A/B/C) of the active account.
# Prefers the per-PID stamp (real session token), else accounts.json.
bs_active_label() {
  local stamp="/tmp/claude-account-${PPID}"
  if [ -f "$stamp" ]; then cat "$stamp"; return; fi
  jq -r '.active // "?"' "$ACCOUNTS_FILE" 2>/dev/null
}

# bs_active_account — display name of the active account.
bs_active_account() {
  [ -f "$ACCOUNTS_FILE" ] || { echo "???"; return 1; }
  local label name
  label=$(bs_active_label)
  name=$(jq -r --arg l "$label" '.accounts[] | select(.label == $l) | .name // $l' "$ACCOUNTS_FILE" 2>/dev/null)
  echo "${name:-$label}"
}

# bs_account_capacity label — "five_pct weekly_pct" from Supabase or cache.
bs_account_capacity() {
  local label="$1"
  local cache="/tmp/claude-capacity-${label}.json"

  if type supa_get &>/dev/null && [ -n "${SUPA_KEY:-}" ]; then
    local result
    result=$(supa_get "account_capacity" "account=eq.${label}&select=five_hour_used_pct,seven_day_used_pct&limit=1")
    if [ -n "$result" ] && [ "$result" != "[]" ] && echo "$result" | jq -e '.[0]' &>/dev/null; then
      local five week
      five=$(echo "$result" | jq -r '.[0].five_hour_used_pct // 0')
      week=$(echo "$result" | jq -r '.[0].seven_day_used_pct // 0')
      echo "$result" > "$cache" 2>/dev/null
      echo "$five $week"; return 0
    fi
  fi

  if [ -f "$cache" ]; then
    local five week
    five=$(jq -r '.[0].five_hour_used_pct // 0' "$cache" 2>/dev/null)
    week=$(jq -r '.[0].seven_day_used_pct // 0' "$cache" 2>/dev/null)
    echo "$five $week"; return 0
  fi

  echo "0 0"; return 1
}

# bs_best_alt_account — label of the lowest-weekly-usage account that isn't active.
bs_best_alt_account() {
  local active; active=$(bs_active_label)
  local best_label="" best_week=999
  while IFS= read -r label; do
    [ -z "$label" ] && continue
    [ "$label" = "$active" ] && continue
    local cap week week_int
    cap=$(bs_account_capacity "$label")
    week=$(echo "$cap" | awk '{print $2}')
    week_int=$(printf '%.0f' "$week" 2>/dev/null || echo "0")
    if [ "$week_int" -lt "$best_week" ]; then best_week=$week_int; best_label=$label; fi
  done < <(jq -r '.accounts[].label' "$ACCOUNTS_FILE" 2>/dev/null)
  echo "$best_label"
}

# bs_restore_credentials label — write a vault's token into the macOS Keychain.
# Claude Code reads OAuth from the keychain (service "Claude Code-credentials").
# Vault files store the flat OAuth object; the keychain wants {"claudeAiOauth": {...}}.
bs_restore_credentials() {
  local label="$1"
  local vault_file="$HOME/.claude/vaults/${label}/claudeAiOauth.json"
  local creds_file="$HOME/.claude/.credentials.json"
  local keychain_service="Claude Code-credentials"
  local keychain_account; keychain_account=$(whoami)

  if ! _bs_has_keychain; then
    echo "ERROR: credential restore requires the macOS Keychain (security)." >&2
    echo "  On this platform, manage credentials yourself; accounts.json was still updated." >&2
    return 1
  fi
  if [ ! -f "$vault_file" ]; then
    echo "ERROR: Vault file not found: $vault_file" >&2
    echo "  Run: claude auth login  (log in as account $label), then provision the vault." >&2
    return 1
  fi
  jq empty "$vault_file" 2>/dev/null || { echo "ERROR: Vault file is not valid JSON: $vault_file" >&2; return 1; }

  local keychain_payload
  keychain_payload=$(jq -c '{"claudeAiOauth": .}' "$vault_file" 2>/dev/null)
  [ -z "$keychain_payload" ] && { echo "ERROR: Failed to build keychain payload from vault" >&2; return 1; }

  if ! security add-generic-password -U -s "$keychain_service" -a "$keychain_account" -w "$keychain_payload" 2>/dev/null; then
    echo "ERROR: Failed to write to keychain (service: $keychain_service)" >&2
    return 1
  fi

  # Keep .credentials.json in sync (some tools read it; harmless otherwise).
  if [ -f "$creds_file" ]; then
    local vault_content tmp
    vault_content=$(jq '.' "$vault_file")
    tmp="${creds_file}.tmp.$$"
    jq --argjson oauth "$vault_content" '.claudeAiOauth = $oauth' "$creds_file" > "$tmp" 2>/dev/null \
      && mv -f "$tmp" "$creds_file" 2>/dev/null || rm -f "$tmp"
  fi

  local pfx; pfx=$(jq -r '.accessToken[:30] // "unknown"' "$vault_file" 2>/dev/null)
  echo "Restored credentials for account $label (token: ${pfx}...)"
  echo "  ✓ Keychain updated — new Claude Code sessions will use account $label"
}

# bs_switch_account label [force] — set active account + swap credentials.
# Capacity-gated (5h>=95% or 7d>=90% refused) unless force=true.
bs_switch_account() {
  local new_label="$1"
  local force="${2:-false}"

  [ -f "$ACCOUNTS_FILE" ] || { echo "ERROR: $ACCOUNTS_FILE not found" >&2; return 1; }

  local found
  found=$(jq -r --arg l "$new_label" '.accounts[] | select(.label == $l) | .label' "$ACCOUNTS_FILE" 2>/dev/null)
  [ -z "$found" ] && { echo "ERROR: Account '$new_label' not found in accounts.json" >&2; return 1; }

  # Capacity gate.
  if [ "$force" != "true" ]; then
    local cap five week five_int week_int
    cap=$(bs_account_capacity "$new_label" 2>/dev/null)
    five=$(echo "$cap" | awk '{print $1}'); week=$(echo "$cap" | awk '{print $2}')
    five_int=$(printf '%.0f' "$five" 2>/dev/null || echo "0")
    week_int=$(printf '%.0f' "$week" 2>/dev/null || echo "0")
    if [ "$five_int" -ge 95 ] 2>/dev/null; then
      echo "ERROR: Account $new_label 5h capacity at ${five_int}% (>=95%). Use --force to override." >&2; return 1
    fi
    if [ "$week_int" -ge 90 ] 2>/dev/null; then
      echo "ERROR: Account $new_label weekly capacity at ${week_int}% (>=90%). Use --force to override." >&2; return 1
    fi
  fi

  local old_label; old_label=$(bs_active_label)

  local keychain_raw=""
  _bs_has_keychain && keychain_raw=$(security find-generic-password -s "Claude Code-credentials" -a "$(whoami)" -w 2>/dev/null)

  local vault_file="$HOME/.claude/vaults/${new_label}/claudeAiOauth.json"
  if _bs_has_keychain && [ ! -f "$vault_file" ]; then
    echo "ERROR: No vault found for account $new_label at $vault_file" >&2
    echo "  Run: claude auth login (as $new_label), then provision the vault." >&2
    return 1
  fi

  # Backup current keychain token.
  if [ -n "$keychain_raw" ]; then
    local backup_dir="$HOME/.claude/backups"; mkdir -p "$backup_dir"
    echo "$keychain_raw" > "${backup_dir}/keychain-${old_label}-$(date +%s).json" 2>/dev/null || true
  fi

  # Departure snapshot to history.
  if type supa_post &>/dev/null && [ -n "${SUPA_KEY:-}" ]; then
    local old_cap old_five old_seven
    old_cap=$(bs_account_capacity "$old_label" 2>/dev/null || echo "? ?")
    old_five=$(echo "$old_cap" | awk '{print $1}'); old_seven=$(echo "$old_cap" | awk '{print $2}')
    if [ -n "$old_five" ] && [ "$old_five" != "?" ]; then
      local hist_body
      hist_body=$(python3 -c "
import json
print(json.dumps({'account':'${old_label}','five_hour_used_pct':${old_five},'seven_day_used_pct':${old_seven},'is_active':False,'source':'switch_departure'}))
" 2>/dev/null)
      supa_post "account_capacity_history" "$hist_body" &>/dev/null || true
    fi
  fi

  # Update active field.
  local tmp="${ACCOUNTS_FILE}.tmp.$$"
  jq --arg l "$new_label" '.active = $l' "$ACCOUNTS_FILE" > "$tmp" 2>/dev/null \
    && mv -f "$tmp" "$ACCOUNTS_FILE" 2>/dev/null \
    || { rm -f "$tmp"; echo "ERROR: Failed to write accounts.json" >&2; return 1; }

  # Swap credentials.
  local _SWITCH_VERIFIED=false
  if _bs_has_keychain; then
    bs_restore_credentials "$new_label" || echo "WARNING: label switched but credential restore failed." >&2
    local vault_pfx keychain_pfx keychain_token
    vault_pfx=$(jq -r '.accessToken[:50] // ""' "$vault_file" 2>/dev/null)
    keychain_token=$(security find-generic-password -s "Claude Code-credentials" -a "$(whoami)" -w 2>/dev/null)
    keychain_pfx=$(echo "$keychain_token" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin); print(d.get('claudeAiOauth',d).get('accessToken','')[:50])
except Exception: pass
" 2>/dev/null)
    [ -n "$vault_pfx" ] && [ "$vault_pfx" = "$keychain_pfx" ] && _SWITCH_VERIFIED=true
  fi

  # Flip is_active in Supabase.
  if type supa_patch &>/dev/null && [ -n "${SUPA_KEY:-}" ]; then
    supa_patch "account_capacity" "account=eq.${old_label}" '{"is_active": false}' &>/dev/null
    supa_patch "account_capacity" "account=eq.${new_label}" '{"is_active": true}' &>/dev/null
  fi

  # Signal token-watch/statusline to ignore stale debug files.
  python3 -c "
import json, time
accts = json.load(open('$ACCOUNTS_FILE'))
target = next((a for a in accts['accounts'] if a['label'] == '${new_label}'), {})
json.dump({'account':'${new_label}','name':target.get('name',''),'switched_at':time.time()}, open('/tmp/token-watch-account-override.json','w'))
" 2>/dev/null || true

  local new_name; new_name=$(jq -r --arg l "$new_label" '.accounts[] | select(.label == $l) | .name' "$ACCOUNTS_FILE" 2>/dev/null)
  type bs_log &>/dev/null && bs_log "INFO" "accounts" "switch ${old_label} -> ${new_label} (verified=${_SWITCH_VERIFIED})" 2>/dev/null || true

  if ! _bs_has_keychain; then
    echo "  ✓ active set to $new_label ($new_name) in accounts.json (no Keychain on this platform)"
  elif [ "$_SWITCH_VERIFIED" = "true" ]; then
    echo "  ✓ SWITCH CONFIRMED — Account $new_label ($new_name). Keychain verified; new sessions use $new_label."
  else
    echo "  ✗ SWITCH INCOMPLETE — accounts.json updated but keychain != vault $new_label. Re-provision the vault and retry." >&2
  fi
}
