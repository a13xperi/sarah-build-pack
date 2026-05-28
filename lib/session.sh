#!/bin/bash
# Session identity and state management
# Requires: config.sh sourced first

# Stable session ID — one per terminal, never changes
bs_session_id() {
  echo "cc-${PPID}"
}

# Per-session directory
bs_session_dir() {
  echo "$BS_TMP/$PPID"
}

# Read directive
bs_directive() {
  cat "$BS_SESSION_DIR/directive" 2>/dev/null || echo ""
}

# Set directive
bs_set_directive() {
  echo "$1" > "$BS_SESSION_DIR/directive"
}

# Read workunit slug
bs_workunit() {
  cat "$BS_SESSION_DIR/workunit" 2>/dev/null || echo ""
}

# Set workunit from directive text
bs_set_workunit() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g;s/--*/-/g;s/^-//;s/-$//' | cut -c1-30 > "$BS_SESSION_DIR/workunit"
}

# Read assigned lane
bs_lane() {
  cat "$BS_SESSION_DIR/lane" 2>/dev/null || echo ""
}

# Set lane
bs_set_lane() {
  echo "$1" > "$BS_SESSION_DIR/lane"
}

# Check throttle — returns 0 if should proceed, 1 if throttled
# Usage: bs_throttle "directive_text" 30
bs_throttle() {
  local directive="$1" interval="${2:-30}"
  local coord_file="$BS_SESSION_DIR/coord"

  if [ -f "$coord_file" ]; then
    local prev_directive last_beat now elapsed
    prev_directive=$(head -1 "$coord_file" 2>/dev/null)
    last_beat=$(tail -1 "$coord_file" 2>/dev/null || echo "0")
    now=$(date +%s)
    elapsed=$((now - last_beat))
    if [ "$directive" = "$prev_directive" ] && [ "$elapsed" -lt "$interval" ]; then
      return 1
    fi
  fi
  return 0
}

# Update throttle state
bs_update_throttle() {
  local directive="$1"
  local coord_file="$BS_SESSION_DIR/coord"
  echo "$directive" > "$coord_file"
  date +%s >> "$coord_file"
}

# Check if a PID is still alive
bs_is_alive() {
  kill -0 "$1" 2>/dev/null
}
