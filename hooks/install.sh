#!/bin/bash
# install.sh — Symlinks battlestation hooks into ~/.claude/hooks/
# Idempotent: safe to re-run. Backs up existing non-symlink hooks.
# NOTE: symlinking puts the files in place. You still need to WIRE them in
# ~/.claude/settings.json (PreToolUse / SessionStart / etc.) — see README §4.

set -euo pipefail

PACK_HOME="$(cd "$(dirname "$0")/.." && pwd)"
HOOKS_SRC="$PACK_HOME/hooks"
HOOKS_DST="$HOME/.claude/hooks"
BACKUP_DIR="$HOME/.claude/hooks-backup/$(date +%Y%m%d-%H%M%S)"

HOOK_FILES=(
  auto-register.sh
  wire-inbox.sh
  file-lock-check.sh
  build-ledger.sh
  token-tracker.sh
  engine-tracker.sh
  loop-guard.sh
  spend-guard.sh
)

echo "Battlestation hook installer (isolated stack)"
echo "Source: $HOOKS_SRC"
echo "Target: $HOOKS_DST"
echo ""

mkdir -p "$HOOKS_DST"
chmod +x "$HOOKS_SRC"/*.sh 2>/dev/null || true

backed_up=0
for file in "${HOOK_FILES[@]}"; do
  src="$HOOKS_SRC/$file"
  dst="$HOOKS_DST/$file"
  [ -f "$src" ] || { echo "SKIP  $file (not found)"; continue; }
  if [ -L "$dst" ] && [ "$(readlink "$dst")" = "$src" ]; then
    echo "OK    $file (already linked)"; continue
  fi
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    mkdir -p "$BACKUP_DIR"; cp -a "$dst" "$BACKUP_DIR/$file" 2>/dev/null || true
    rm -f "$dst"; backed_up=$((backed_up+1)); echo "BACK  $file -> $BACKUP_DIR/$file"
  fi
  ln -sf "$src" "$dst"; echo "LINK  $dst -> $src"
done

echo ""
[ "$backed_up" -gt 0 ] && echo "Backed up $backed_up hook(s) to $BACKUP_DIR"
echo "Done. Now wire these into ~/.claude/settings.json (see README §4)."
