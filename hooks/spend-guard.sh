#!/bin/bash
# spend-guard — SAGE spending guard for expensive fan-out.
#
# SAGE rule: estimate before expensive batches; warn over a soft threshold,
# block over a hard one. "Expensive" here = spawning subagents (Agent/Task) or
# dispatching to a paid external engine (codex/gemini/grok/kimi/minimax via Bash).
#
# Wire as PreToolUse (matcher *). Counts spend events in a rolling window per
# session; warns at the soft cap, blocks at the hard cap. If the session is
# already in token "caution" (token-tracker STATE), the gate tightens — it
# blocks at the soft cap instead.
#
# Config (defaults baked; override in ~/.claude/token-budget.json):
#   { "fanout_warn_at": 5, "fanout_block_at": 12, "fanout_window_sec": 300 }
#
# Requires (sourced below): config.sh, session.sh, log.sh; honors hook-gate's
# warn budget for the warn path.

HOOK_DIR="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)"
[ -f "$HOOK_DIR/../lib/config.sh" ]   && source "$HOOK_DIR/../lib/config.sh"
[ -f "$HOOK_DIR/../lib/session.sh" ]  && source "$HOOK_DIR/../lib/session.sh"
[ -f "$HOOK_DIR/../lib/log.sh" ]      && source "$HOOK_DIR/../lib/log.sh"
[ -f "$HOOK_DIR/../lib/hook-gate.sh" ] && source "$HOOK_DIR/../lib/hook-gate.sh"

INPUT=$(cat)

TOOL_NAME=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_name',''))" 2>/dev/null || echo "")
[ -z "$TOOL_NAME" ] && exit 0

# Is this a "spend" event? Subagent spawn, or a paid-engine Bash command.
KIND=""
case "$TOOL_NAME" in
  Agent|Task) KIND="subagent" ;;
  Bash)
    CMD=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" 2>/dev/null || echo "")
    CMD_LOWER=$(echo "$CMD" | tr '[:upper:]' '[:lower:]')
    if echo "$CMD_LOWER" | grep -qE 'codex exec|gemini -p|grok -p|\bkimi\b|\bminimax\b'; then
      KIND="engine"
    fi
    ;;
esac
[ -z "$KIND" ] && exit 0

# ── Config ────────────────────────────────────────────────────────
WARN_AT=5
BLOCK_AT=12
WINDOW=300
BUDGET="$HOME/.claude/token-budget.json"
if [ -f "$BUDGET" ] && command -v jq >/dev/null 2>&1; then
  _v=$(jq -r '.fanout_warn_at // empty' "$BUDGET" 2>/dev/null);    [ -n "$_v" ] && WARN_AT="$_v"
  _v=$(jq -r '.fanout_block_at // empty' "$BUDGET" 2>/dev/null);   [ -n "$_v" ] && BLOCK_AT="$_v"
  _v=$(jq -r '.fanout_window_sec // empty' "$BUDGET" 2>/dev/null); [ -n "$_v" ] && WINDOW="$_v"
fi

# Coupling: if the session is already in token caution, tighten the gate so a
# near-budget session can't fan out — block at the soft cap.
TT_STATE="/tmp/claude-token-state-${PPID}"
if [ -f "$TT_STATE" ]; then
  CAUTION=$(awk '{print $4}' "$TT_STATE" 2>/dev/null)
  [ "$CAUTION" = "1" ] && BLOCK_AT="$WARN_AT"
fi

# ── Rolling-window count of recent spend events ────────────────────
SDIR=$(bs_session_dir 2>/dev/null || echo "/tmp/battlestation/${PPID}")
mkdir -p "$SDIR" 2>/dev/null
LEDGER="$SDIR/spend-guard.state"
NOW=$(date +%s)
CUTOFF=$((NOW - WINDOW))

KEPT=""
if [ -f "$LEDGER" ]; then
  while IFS= read -r ts; do
    [ -n "$ts" ] && [ "$ts" -ge "$CUTOFF" ] 2>/dev/null && KEPT="${KEPT}${ts}"$'\n'
  done < "$LEDGER"
fi
COUNT=$(printf '%s' "$KEPT" | grep -c . 2>/dev/null); [ -z "$COUNT" ] && COUNT=0
PROSPECTIVE=$((COUNT + 1))

emit() {  # $1=decision $2=reason
  echo "{\"decision\":\"$1\",\"reason\":$(printf '%s' "$2" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}"
}

# ── Hard cap: block (do NOT record — the call is being prevented) ───
if [ "$PROSPECTIVE" -gt "$BLOCK_AT" ]; then
  printf '%s' "$KEPT" > "$LEDGER" 2>/dev/null   # persist pruned window
  bs_log "WARN" "spend-guard" "blocked ${KIND} — ${COUNT} spend events in ${WINDOW}s (cap ${BLOCK_AT})" 2>/dev/null || true
  emit "block" "SPEND GUARD: ${COUNT} expensive calls (${KIND}) already launched in the last $((WINDOW/60))m (cap ${BLOCK_AT}). Confirm this is intended or split the batch; raise fanout_block_at in ~/.claude/token-budget.json to override."
  exit 0
fi

# Record this spend event.
printf '%s%s\n' "$KEPT" "$NOW" > "$LEDGER" 2>/dev/null

# ── Soft cap: warn (respect the hook-gate warn budget) ─────────────
if [ "$PROSPECTIVE" -ge "$WARN_AT" ]; then
  if [ -z "${_HG_LOADED:-}" ] || hg_warn_allowed; then
    emit "warn" "SPEND NOTE: ${PROSPECTIVE} expensive calls (${KIND}) in the last $((WINDOW/60))m. Default to cheap/spec-clear work and batch deliberately (cap ${BLOCK_AT})."
  fi
  exit 0
fi

exit 0
