#!/bin/bash
# Structured logging

# bs_log "LEVEL" "component" "message"
bs_log() {
  local level="$1" component="$2" message="$3"
  local ts
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "${ts} [${level}] ${component}: ${message}" >> "$BS_LOG" 2>/dev/null

  # Rotate at 10MB
  local size
  size=$(stat -f%z "$BS_LOG" 2>/dev/null || echo "0")
  if [ "$size" -gt 10485760 ]; then
    mv "$BS_LOG" "${BS_LOG}.$(date +%Y%m%d-%H%M%S).bak" 2>/dev/null
  fi
}
