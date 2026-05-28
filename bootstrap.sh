#!/bin/bash
# bootstrap.sh — one-shot setup for the isolated battlestation stack.
# Run from the pack root:  bash bootstrap.sh
set -euo pipefail
PACK_HOME="$(cd "$(dirname "$0")" && pwd)"
cd "$PACK_HOME"

say() { printf "\n\033[1;36m== %s\033[0m\n" "$1"; }
ok()  { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn(){ printf "  \033[33m!\033[0m %s\n" "$1"; }

say "1/6  Dependency check"
for bin in bash curl python3 git jq; do
  if command -v "$bin" >/dev/null 2>&1; then ok "$bin"; else warn "MISSING: $bin (install it before continuing)"; fi
done

say "2/6  .env"
if [ -f .env ]; then ok ".env already exists — leaving it"; else
  cp .env.example .env
  ok "created .env from template"
  warn "EDIT .env now: set SUPA_URL + SUPA_KEY to YOUR Supabase project"
fi

say "3/6  Supabase schema"
echo "  Provision your tables by pasting schema/provision.sql into your"
echo "  Supabase project's SQL Editor and running it. (Can't be done blind from here.)"
echo "  Tables: working_sessions, session_locks, session_tasks,"
echo "          account_capacity, ai_capacity_ledger, account_capacity_history,"
echo "          build_ledger, session_messages."
echo "  (build_ledger + session_messages DDL: see README §3 / your own additions.)"

say "4/6  accounts.json"
mkdir -p "$HOME/.claude"
if [ -f "$HOME/.claude/accounts.json" ]; then ok "~/.claude/accounts.json exists"; else
  cp templates/accounts.json "$HOME/.claude/accounts.json"
  ok "seeded ~/.claude/accounts.json (fill in your account emails)"
fi

say "5/6  Hooks"
bash hooks/install.sh

say "6/6  token-watch TUI"
if command -v pip3 >/dev/null 2>&1; then
  pip3 install --quiet --user "rich>=13.0" "textual>=0.50" && ok "installed rich + textual (--user)" \
    || warn "pip install failed — run:  pip3 install 'rich>=13.0' 'textual>=0.50'"
else
  warn "pip3 not found — install deps manually:  pip3 install 'rich>=13.0' 'textual>=0.50'"
fi
chmod +x token-watch/token-watch 2>/dev/null || true
mkdir -p "$HOME/.local/bin"
ln -sf "$PACK_HOME/token-watch/token-watch" "$HOME/.local/bin/token-watch"
ok "symlinked token-watch → ~/.local/bin/token-watch (ensure ~/.local/bin is on PATH)"

say "Next"
echo "  • Edit .env  (SUPA_URL / SUPA_KEY)"
echo "  • Run schema/provision.sql in your Supabase"
echo "  • Wire hooks into ~/.claude/settings.json  (README §4)"
echo "  • Source the libs in your shell rc:  source $PACK_HOME/lib/config.sh"
echo "  • Smoke test:  source lib/config.sh && source lib/wire.sh && wire_broadcast status '{\"message\":\"hello\"}'"
echo "  • Launch the dashboard:  token-watch   (reads your Supabase tables)"
