#!/bin/bash
# Hook gate — drain-prevention throttle for PreToolUse/PostToolUse hooks.
#
# Hooks run on EVERY tool call. Left ungated they fire at full rate even when
# a session is idle or abandoned, and they can flood Claude with `warn`
# decisions. This gate throttles hooks by session activity level and budgets
# the number of warn surfacings per rolling window.
#
# Activity level is derived from the mtime of /tmp/claude-activity-$PPID — the
# SAME file lib/wire-daemon.py reads for its adaptive poll interval. Thresholds
# below are kept in sync with that daemon (active 30s / warm 300s / idle 1800s).
#
# Requires (already sourced by callers): config.sh, session.sh, log.sh, atomic.sh
#
# Public API:
#   hg_touch_activity                  — bump the activity marker (call on real tool use)
#   hg_should_fire "<hook>" "<tier>"   — 0 = fire, non-0 = skip. tier: active | background
#   hg_warn_allowed                    — 0 = a warn/block surfacing is within budget
#   hg_log_suppressed "<hook>" "<why>" — record a suppressed surfacing in the bs log
#
# Loaded-guard: every caller checks `[ -n "${_HG_LOADED:-}" ]` before using the
# functions, so we MUST export this once the file sources cleanly.

# Don't re-init if sourced twice in one process.
if [ -z "${_HG_LOADED:-}" ]; then

_HG_ACTIVITY_FILE="/tmp/claude-activity-${PPID}"
_HG_CONFIG="${HOME}/battlestation/drain-prevention.json"

# ── Defaults (overridable via ~/battlestation/drain-prevention.json) ──
# Activity-level boundaries (seconds) — match wire-daemon.py.
_HG_ACTIVE_MAX=30
_HG_WARM_MAX=300
_HG_IDLE_MAX=1800

# background-tier minimum seconds between fires, per activity level.
# active = effectively unthrottled; cadence relaxes as the session goes quiet.
_HG_BG_ACTIVE=0
_HG_BG_WARM=15
_HG_BG_IDLE=60
_HG_BG_DORMANT=300

# warn budget — at most N warn/block surfacings per rolling window (seconds).
_HG_WARN_MAX=5
_HG_WARN_WINDOW=300

# ── Optional JSON overrides (best-effort; defaults stand if jq/file absent) ──
if [ -f "$_HG_CONFIG" ] && command -v jq >/dev/null 2>&1; then
  _hg_cfg() { jq -r ".hook_gate.$1 // empty" "$_HG_CONFIG" 2>/dev/null; }
  _v=$(_hg_cfg "levels.active_max");  [ -n "$_v" ] && _HG_ACTIVE_MAX="$_v"
  _v=$(_hg_cfg "levels.warm_max");    [ -n "$_v" ] && _HG_WARM_MAX="$_v"
  _v=$(_hg_cfg "levels.idle_max");    [ -n "$_v" ] && _HG_IDLE_MAX="$_v"
  _v=$(_hg_cfg "background.active");  [ -n "$_v" ] && _HG_BG_ACTIVE="$_v"
  _v=$(_hg_cfg "background.warm");    [ -n "$_v" ] && _HG_BG_WARM="$_v"
  _v=$(_hg_cfg "background.idle");    [ -n "$_v" ] && _HG_BG_IDLE="$_v"
  _v=$(_hg_cfg "background.dormant"); [ -n "$_v" ] && _HG_BG_DORMANT="$_v"
  _v=$(_hg_cfg "warn.max");           [ -n "$_v" ] && _HG_WARN_MAX="$_v"
  _v=$(_hg_cfg "warn.window");        [ -n "$_v" ] && _HG_WARN_WINDOW="$_v"
  unset _v
  unset -f _hg_cfg
fi

# ── Internal: per-session state dir ──────────────────────────────
_hg_dir() {
  bs_session_dir 2>/dev/null || echo "/tmp/battlestation/${PPID}"
}

# ── Internal: current activity level from the activity-file mtime ─
# Echoes one of: active | warm | idle | dormant
_hg_activity_level() {
  [ ! -f "$_HG_ACTIVITY_FILE" ] && { echo "active"; return; }  # new session
  local mtime now age
  mtime=$(stat -f%m "$_HG_ACTIVITY_FILE" 2>/dev/null || stat -c%Y "$_HG_ACTIVITY_FILE" 2>/dev/null || echo 0)
  now=$(date +%s)
  age=$(( now - mtime ))
  if   [ "$age" -le "$_HG_ACTIVE_MAX" ]; then echo "active"
  elif [ "$age" -le "$_HG_WARM_MAX" ];   then echo "warm"
  elif [ "$age" -le "$_HG_IDLE_MAX" ];   then echo "idle"
  else echo "dormant"
  fi
}

# ── Internal: background throttle interval for a level ────────────
_hg_bg_interval() {
  case "$1" in
    active)  echo "$_HG_BG_ACTIVE" ;;
    warm)    echo "$_HG_BG_WARM" ;;
    idle)    echo "$_HG_BG_IDLE" ;;
    *)       echo "$_HG_BG_DORMANT" ;;
  esac
}

# ── Public: bump activity marker ─────────────────────────────────
# Cheap touch on every real tool call. The daemon and the level check
# both key off this file's mtime.
hg_touch_activity() {
  : > "$_HG_ACTIVITY_FILE" 2>/dev/null || touch "$_HG_ACTIVITY_FILE" 2>/dev/null || true
}

# ── Public: should this hook fire now? ───────────────────────────
# tier=active     → fire only when active/warm; skip on idle/dormant.
# tier=background → fire at all levels, but throttle by activity level.
# Returns 0 to fire, 1 to skip.
hg_should_fire() {
  local hook="$1" tier="${2:-background}"
  local level
  level=$(_hg_activity_level)

  if [ "$tier" = "active" ]; then
    case "$level" in
      active|warm) return 0 ;;        # fire — active-tier hooks self-throttle
      *) return 1 ;;                  # idle/dormant → skip entirely
    esac
  fi

  # background tier: fire at all levels, but throttle by activity level.
  local interval
  interval=$(_hg_bg_interval "$level")
  [ -z "$interval" ] && interval=0
  [ "$interval" -le 0 ] && return 0   # unthrottled at this level

  local stamp now last
  stamp="$(_hg_dir)/hg-fire-${hook}"
  now=$(date +%s)
  last=$(cat "$stamp" 2>/dev/null || echo 0)
  if [ $(( now - last )) -lt "$interval" ]; then
    return 1
  fi
  echo "$now" > "$stamp" 2>/dev/null
  return 0
}

# ── Public: is a warn/block surfacing within budget? ─────────────
# Tracks a rolling window of surfacing timestamps in a per-session file.
# Returns 0 if under the cap (and records this surfacing), 1 if exhausted.
hg_warn_allowed() {
  local ledger now cutoff count
  ledger="$(_hg_dir)/hg-warn-budget"
  now=$(date +%s)
  cutoff=$(( now - _HG_WARN_WINDOW ))

  # Keep only timestamps inside the window.
  local kept=""
  if [ -f "$ledger" ]; then
    while IFS= read -r ts; do
      [ -n "$ts" ] && [ "$ts" -ge "$cutoff" ] 2>/dev/null && kept="${kept}${ts}"$'\n'
    done < "$ledger"
  fi

  count=$(printf '%s' "$kept" | grep -c . 2>/dev/null)
  [ -z "$count" ] && count=0
  if [ "$count" -ge "$_HG_WARN_MAX" ]; then
    # Persist the pruned window so it doesn't grow unbounded.
    printf '%s' "$kept" > "$ledger" 2>/dev/null
    return 1
  fi

  printf '%s%s\n' "$kept" "$now" > "$ledger" 2>/dev/null
  return 0
}

# ── Public: log a suppressed surfacing ───────────────────────────
hg_log_suppressed() {
  local hook="$1" why="$2"
  bs_log "INFO" "hook-gate" "suppressed ${hook}: ${why}" 2>/dev/null || true
}

_HG_LOADED=1
fi
