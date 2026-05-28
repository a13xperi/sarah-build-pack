#!/bin/bash
# Atomic file operations — prevents corruption from concurrent writes

# atomic_write "target_path" "content"
# Writes to tmp file then mv (atomic on APFS/HFS+)
atomic_write() {
  local target="$1" content="$2"
  local tmp="${target}.${$}.tmp"
  echo "$content" > "$tmp" 2>/dev/null && mv -f "$tmp" "$target" 2>/dev/null || rm -f "$tmp"
}

# atomic_write_stdin "target_path"
# Reads stdin, writes atomically
atomic_write_stdin() {
  local target="$1"
  local tmp="${target}.${$}.tmp"
  cat > "$tmp" 2>/dev/null && mv -f "$tmp" "$target" 2>/dev/null || rm -f "$tmp"
}

# atomic_write_cmd "target_path" "command..."
# Runs command, captures output atomically
atomic_write_cmd() {
  local target="$1"
  shift
  local tmp="${target}.${$}.tmp"
  "$@" > "$tmp" 2>/dev/null && mv -f "$tmp" "$target" 2>/dev/null || rm -f "$tmp"
}
