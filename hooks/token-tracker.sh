#!/bin/bash
# Token tracker — records usage snapshots per tool call.
# Reads rate limit data from the last statusline render.
# Writes to per-session JSONL ledger + global persistent ledger.

BUDGET_FILE="$HOME/.claude/token-budget.json"
LEDGER="/tmp/claude-token-ledger-${PPID}.jsonl"
STATE="/tmp/claude-token-state-${PPID}"
DEBUG="/tmp/statusline-debug.json"
GLOBAL_LEDGER="$HOME/.claude/logs/token-ledger.jsonl"
DIRECTIVE_FILE="/tmp/claude-directive-${PPID}"

# Drain prevention gate
HOOK_DIR_TT="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)"
[ -f "$HOOK_DIR_TT/../lib/config.sh" ] && source "$HOOK_DIR_TT/../lib/config.sh"
[ -f "$HOOK_DIR_TT/../lib/hook-gate.sh" ] && source "$HOOK_DIR_TT/../lib/hook-gate.sh"

if [ -n "${_HG_LOADED:-}" ]; then
  # Touch activity — tool calls prove the session is alive
  hg_touch_activity
  # token-tracker is tier=background (fires at all levels but throttled when idle)
  hg_should_fire "token-tracker" "background" || exit 0
fi

# Ensure global ledger dir exists
mkdir -p "$(dirname "$GLOBAL_LEDGER")" 2>/dev/null

# Read current usage from last statusline render
[ ! -f "$DEBUG" ] && exit 0

five_pct=$(jq -r '.rate_limits.five_hour.used_percentage // empty' "$DEBUG" 2>/dev/null)
[ -z "$five_pct" ] && exit 0

now=$(date +%s)
now_iso=$(date -u +%Y-%m-%dT%H:%M:%SZ)
directive=$(cat "$DIRECTIVE_FILE" 2>/dev/null || echo "—")
seven_pct=$(jq -r '.rate_limits.seven_day.used_percentage // 0' "$DEBUG" 2>/dev/null)
model=$(jq -r '.model.id // "?" | sub("claude-";"") | sub("-4-6";"") | sub("-4-5.*";"")' "$DEBUG" 2>/dev/null)
[ -z "$model" ] && model="?"
output_tokens=$(jq -r '.context_window.total_output_tokens // 0' "$DEBUG" 2>/dev/null)

# Read tool info from stdin (hook input)
input=$(cat)
tool=$(echo "$input" | jq -r '.tool_name // "unknown"' 2>/dev/null)

# Extract tool input snippet (80 chars max)
tool_snippet=$(echo "$input" | jq -r '
  .tool_input // {} |
  if .command then .command
  elif .file_path then .file_path
  elif .pattern then .pattern
  elif .query then .query
  elif .skill then .skill
  elif .prompt then .prompt
  else (tostring)
  end' 2>/dev/null | head -c 80 | tr '\n' ' ' | tr '"' "'" | tr '\\' '/')
[ -z "$tool_snippet" ] && tool_snippet=""

# --- Session classification ---
SESSION_CLASS_FILE="/tmp/claude-session-class-${PPID}"

classify_session() {
  local ppid_of_claude="$PPID"
  local grandparent_pid grandparent_cmd
  grandparent_pid=$(ps -o ppid= -p "$ppid_of_claude" 2>/dev/null | tr -d ' ')
  grandparent_cmd=$(ps -o comm= -p "$grandparent_pid" 2>/dev/null)

  # Walk up the process tree to find the originator
  # Default to interactive — only classify as background/scheduled
  # for known non-interactive parents. Shells (-zsh, bash, etc.) are interactive.
  case "$grandparent_cmd" in
    *launchd*|*cron*|*atd*)
      echo "scheduled" ;;
    *claude*|*codex*)
      echo "background" ;;
    *)
      echo "interactive" ;;
  esac
}

# Get session start snapshot
if [ ! -f "$STATE" ]; then
  echo "${five_pct} ${now} 0 0 0" > "$STATE"

  # Classify session on first tool use and cache it
  session_class=$(classify_session)
  echo "$session_class" > "$SESSION_CLASS_FILE"

  # Session birth logging — first tool use for this session
  grandparent_pid=$(ps -o ppid= -p "$PPID" 2>/dev/null | tr -d ' ')
  if [ -n "$grandparent_pid" ] && [ "$grandparent_pid" != "0" ]; then
    grandparent_comm=$(ps -o comm= -p "$grandparent_pid" 2>/dev/null | xargs basename 2>/dev/null)
    great_grandparent_pid=$(ps -o ppid= -p "$grandparent_pid" 2>/dev/null | tr -d ' ')
    if [ -n "$great_grandparent_pid" ] && [ "$great_grandparent_pid" != "0" ] && [ "$great_grandparent_pid" != "1" ]; then
      great_grandparent_comm=$(ps -o comm= -p "$great_grandparent_pid" 2>/dev/null | xargs basename 2>/dev/null)
      parent_chain="${great_grandparent_comm} > ${grandparent_comm} > claude"
    else
      parent_chain="${grandparent_comm} > claude"
    fi
  else
    parent_chain="unknown > claude"
  fi

  working_dir="${PWD}"
  repo_name=$(basename "$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null)
  [ -z "$repo_name" ] && repo_name="none"

  birth_entry="{\"ts\":\"${now_iso}\",\"epoch\":${now},\"type\":\"session_start\",\"session\":\"cc-${PPID}\",\"parent_chain\":\"${parent_chain}\",\"initial_five_pct\":${five_pct},\"initial_seven_pct\":${seven_pct},\"working_dir\":\"${working_dir}\",\"repo\":\"${repo_name}\",\"directive\":\"${directive}\",\"model\":\"${model}\",\"session_class\":\"${session_class}\"}"
  echo "$birth_entry" >> "$GLOBAL_LEDGER"
fi

start_pct=$(awk '{print $1}' "$STATE")
start_time=$(awk '{print $2}' "$STATE")
alert_fired=$(awk '{print $3}' "$STATE")
caution_fired=$(awk '{print $4}' "$STATE")
burn_warned_at=$(awk '{print $5}' "$STATE")
# Backcompat: old state files without these fields
[ -z "$alert_fired" ] && alert_fired=0
[ -z "$caution_fired" ] && caution_fired=0
[ -z "$burn_warned_at" ] && burn_warned_at=0
delta_pct=$(echo "$five_pct - $start_pct" | bc 2>/dev/null || echo "0")
elapsed_min=$(echo "($now - $start_time) / 60" | bc 2>/dev/null || echo "1")
[ "$elapsed_min" = "0" ] && elapsed_min=1
burn_rate=$(echo "scale=2; $delta_pct / $elapsed_min" | bc 2>/dev/null || echo "0")

SESSION_ID="cc-${PPID}"

# Log to per-session ledger + global ledger
entry="{\"ts\":\"${now_iso}\",\"epoch\":${now},\"type\":\"tool_use\",\"session\":\"${SESSION_ID}\",\"tool\":\"${tool}\",\"tool_snippet\":\"${tool_snippet}\",\"five_pct\":${five_pct},\"seven_pct\":${seven_pct},\"delta_from_start\":${delta_pct},\"burn_rate_per_min\":${burn_rate},\"directive\":\"${directive}\",\"model\":\"${model}\",\"output_tokens\":${output_tokens}}"
echo "$entry" >> "$LEDGER"
echo "$entry" >> "$GLOBAL_LEDGER"

# Check budget — progressive warnings
if [ -f "$BUDGET_FILE" ]; then
  enabled=$(jq -r '.enabled' "$BUDGET_FILE" 2>/dev/null)
  [ "$enabled" != "true" ] && exit 0

  # Read session class and apply class-specific hard_stop threshold
  session_class=$(cat "$SESSION_CLASS_FILE" 2>/dev/null || echo "interactive")
  class_hard_stop=$(jq -r ".session_classes.${session_class}.hard_stop_at_pct // empty" "$BUDGET_FILE" 2>/dev/null)
  if [ -n "$class_hard_stop" ]; then
    hard_stop="$class_hard_stop"
  else
    hard_stop=$(jq -r '.hard_stop_at_pct' "$BUDGET_FILE" 2>/dev/null)
  fi
  alert_at=$(jq -r '.alert_at_pct // 10' "$BUDGET_FILE" 2>/dev/null)
  burn_alert=$(jq -r '.burn_rate_alert_pct_per_min // 2' "$BUDGET_FILE" 2>/dev/null)
  caution_at=$(echo "scale=0; $hard_stop * 70 / 100" | bc 2>/dev/null)
  remaining=$(echo "scale=1; $hard_stop - $delta_pct" | bc 2>/dev/null)
  delta_display=$(printf '%.1f' "$delta_pct" 2>/dev/null || echo "$delta_pct")
  remaining_display=$(printf '%.1f' "$remaining" 2>/dev/null || echo "$remaining")

  # --- HARD BLOCK (checked first — highest severity) ---
  over=$(echo "$delta_pct >= $hard_stop" | bc 2>/dev/null)
  if [ "$over" = "1" ]; then
    best_alt=$(cat /tmp/claude-best-alt-account 2>/dev/null)
    alt_msg=""
    [ -n "$best_alt" ] && alt_msg=" (3) switch to ${best_alt}"
    cat <<BLOCK
{"decision":"block","reason":"SESSION BUDGET HIT — used ${delta_display}% of ${hard_stop}% limit (started at ${start_pct}%, now ${five_pct}%). Options: (1) /close-session and start fresh (2) raise hard_stop_at_pct in ~/.claude/token-budget.json${alt_msg}"}
BLOCK
    exit 0
  fi

  # --- CAUTION: 70% of hard_stop, fires once ---
  past_caution=$(echo "$delta_pct >= $caution_at" | bc 2>/dev/null)
  if [ "$past_caution" = "1" ] && [ "$caution_fired" = "0" ]; then
    echo "${start_pct} ${start_time} ${alert_fired} 1 ${burn_warned_at}" > "$STATE"
    # Caution is important — check warn budget but always allow first caution
    if [ -z "${_HG_LOADED:-}" ] || hg_warn_allowed; then
      echo "{\"decision\":\"warn\",\"reason\":\"BUDGET CAUTION: ${delta_display}% of ${hard_stop}% used (${remaining_display}% remaining). Consider wrapping up or /close-session soon.\"}"
    fi
    exit 0
  fi

  # --- ALERT: early warning, fires once ---
  past_alert=$(echo "$delta_pct >= $alert_at" | bc 2>/dev/null)
  if [ "$past_alert" = "1" ] && [ "$alert_fired" = "0" ]; then
    echo "${start_pct} ${start_time} 1 ${caution_fired} ${burn_warned_at}" > "$STATE"
    if [ -z "${_HG_LOADED:-}" ] || hg_warn_allowed; then
      echo "{\"decision\":\"warn\",\"reason\":\"BUDGET NOTE: Session has used ${delta_display}% of ${hard_stop}% budget. ${remaining_display}% remaining.\"}"
    fi
    exit 0
  fi

  # --- BURN RATE: deduped with 5-min cooldown ---
  hot=$(echo "$burn_rate > $burn_alert" | bc 2>/dev/null)
  burn_cooldown=$((now - burn_warned_at))
  if [ "$hot" = "1" ] && [ "$burn_cooldown" -gt 300 ]; then
    echo "${start_pct} ${start_time} ${alert_fired} ${caution_fired} ${now}" > "$STATE"
    if [ -z "${_HG_LOADED:-}" ] || hg_warn_allowed; then
      echo "{\"decision\":\"warn\",\"reason\":\"HIGH BURN: ${burn_rate}%/min — ${delta_display}% used in ${elapsed_min}min, ${remaining_display}% of ${hard_stop}% remaining.\"}"
    fi
    exit 0
  fi
fi

exit 0
