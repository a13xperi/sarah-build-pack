#!/bin/bash
# loop-guard — SAGE loop-prevention: stop after N identical failing tool calls.
#
# SAGE rule: "same tool call fails 3x consecutively -> STOP, report the error."
# Wire BOTH events to this one script:
#   PreToolUse  (matcher *) — blocks the next attempt of a call already tripped
#   PostToolUse (matcher *) — counts consecutive failures of an identical call
#
# A call's identity = hash(tool_name + normalized tool_input). When the same
# identity fails max_repeats times in a row, the call is "tripped": the next
# PreToolUse for that identity returns decision:block with the last error and a
# nudge to change approach. Any success, or a different call, resets the streak.
#
# Config (defaults baked; override via ~/battlestation/drain-prevention.json):
#   { "loop_guard": { "max_repeats": 3, "mode": "block" } }   # mode: block|warn
#
# Requires (sourced below): config.sh, session.sh, log.sh

HOOK_DIR="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)"
[ -f "$HOOK_DIR/../lib/config.sh" ]  && source "$HOOK_DIR/../lib/config.sh"
[ -f "$HOOK_DIR/../lib/session.sh" ] && source "$HOOK_DIR/../lib/session.sh"
[ -f "$HOOK_DIR/../lib/log.sh" ]     && source "$HOOK_DIR/../lib/log.sh"

INPUT=$(cat)

SDIR=$(bs_session_dir 2>/dev/null || echo "/tmp/battlestation/${PPID}")
mkdir -p "$SDIR" 2>/dev/null
STATE="$SDIR/loop-guard.state"        # "<hash> <fail_streak>"
BLOCKED="$SDIR/loop-guard.blocked"    # "<hash>" currently tripped
ERRSNIP="$SDIR/loop-guard.err"        # last error snippet for the tripped call

# ── Config ────────────────────────────────────────────────────────
MAX_REPEATS=3
MODE="block"
CFG="${HOME}/battlestation/drain-prevention.json"
if [ -f "$CFG" ] && command -v jq >/dev/null 2>&1; then
  _v=$(jq -r '.loop_guard.max_repeats // empty' "$CFG" 2>/dev/null); [ -n "$_v" ] && MAX_REPEATS="$_v"
  _v=$(jq -r '.loop_guard.mode // empty' "$CFG" 2>/dev/null);        [ -n "$_v" ] && MODE="$_v"
fi

# ── Parse event/hash/failure from the hook input (one python pass) ──
# Emits: "<event> <hash> <fail> <errsnip...>"
PARSED=$(INPUT_JSON="$INPUT" python3 -c '
import os, json, hashlib, re

try:
    d = json.loads(os.environ.get("INPUT_JSON", "{}"))
except Exception:
    print("? ? 0"); raise SystemExit

event = d.get("hook_event_name", "")
tool  = d.get("tool_name", "")
tin   = d.get("tool_input", {})
h = hashlib.md5((tool + "|" + json.dumps(tin, sort_keys=True, default=str)).encode()).hexdigest()[:16]

# Failure detection from tool_response (best-effort, tool-agnostic).
fail = 0
snip = ""
resp = d.get("tool_response", None)
if resp is not None:
    if isinstance(resp, dict):
        if resp.get("is_error") is True or resp.get("error"):
            fail = 1
        text = json.dumps(resp)[:4000]
    else:
        text = str(resp)[:4000]
    if not fail and re.search(r"\b(error|failed|failure|exception|traceback|not found|no such file|command not found|exit code [1-9]|permission denied)\b", text, re.I):
        fail = 1
    if fail:
        m = re.search(r".{0,120}(error|failed|exception|not found|denied).{0,120}", text, re.I)
        snip = (m.group(0) if m else text[:160]).replace(chr(10), " ").replace(chr(13), " ")

print(event, h, fail, snip)
' 2>/dev/null)

EVENT=$(printf '%s' "$PARSED" | awk '{print $1}')
HASH=$(printf '%s' "$PARSED" | awk '{print $2}')
FAIL=$(printf '%s' "$PARSED" | awk '{print $3}')
SNIP=$(printf '%s' "$PARSED" | cut -d' ' -f4-)

[ -z "$HASH" ] || [ "$HASH" = "?" ] && exit 0

# ── PreToolUse: block if this exact call is already tripped ─────────
if [ "$EVENT" = "PreToolUse" ]; then
  if [ -f "$BLOCKED" ] && [ "$(cat "$BLOCKED" 2>/dev/null)" = "$HASH" ]; then
    LAST_ERR=$(cat "$ERRSNIP" 2>/dev/null | head -c 200)
    REASON="LOOP GUARD: this exact call has failed ${MAX_REPEATS}x in a row. Stop repeating it and change approach. Last error: ${LAST_ERR}"
    bs_log "WARN" "loop-guard" "blocked repeat of tripped call ${HASH}" 2>/dev/null || true
    if [ "$MODE" = "warn" ]; then
      echo "{\"decision\":\"warn\",\"reason\":$(printf '%s' "$REASON" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}"
    else
      echo "{\"decision\":\"block\",\"reason\":$(printf '%s' "$REASON" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}"
    fi
  fi
  exit 0
fi

# ── PostToolUse: update the consecutive-failure streak ─────────────
if [ "$EVENT" = "PostToolUse" ]; then
  PREV_HASH=""; STREAK=0
  if [ -f "$STATE" ]; then
    PREV_HASH=$(awk '{print $1}' "$STATE" 2>/dev/null)
    STREAK=$(awk '{print $2}' "$STATE" 2>/dev/null)
  fi
  [ -z "$STREAK" ] && STREAK=0

  if [ "$FAIL" = "1" ]; then
    if [ "$HASH" = "$PREV_HASH" ]; then STREAK=$((STREAK + 1)); else STREAK=1; fi
    echo "$HASH $STREAK" > "$STATE"
    if [ "$STREAK" -ge "$MAX_REPEATS" ]; then
      echo "$HASH" > "$BLOCKED"
      printf '%s' "$SNIP" > "$ERRSNIP"
      bs_log "WARN" "loop-guard" "tripped: ${HASH} failed ${STREAK}x" 2>/dev/null || true
    fi
  else
    # Success (or non-failing) for this call → reset its streak and clear trip.
    : > "$STATE"
    if [ -f "$BLOCKED" ] && [ "$(cat "$BLOCKED" 2>/dev/null)" = "$HASH" ]; then
      rm -f "$BLOCKED" "$ERRSNIP" 2>/dev/null
    fi
  fi
  exit 0
fi

exit 0
