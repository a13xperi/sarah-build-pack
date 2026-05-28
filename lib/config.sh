#!/bin/bash
# Battlestation config — sourced by every script
# Loads .env, exports all shared variables

# Find repo root — resolve this file's own path in both bash and zsh.
# (zsh does not populate ${BASH_SOURCE[0]}, which silently fell back to the
#  CWD and produced a wrong root like ~/projects when sourced from .zshrc.)
if [ -z "${BATTLESTATION_HOME:-}" ]; then
  if [ -n "${BASH_SOURCE:-}" ]; then
    _bs_src="${BASH_SOURCE[0]}"
  elif [ -n "${ZSH_VERSION:-}" ]; then
    _bs_src="$(eval 'printf %s "${(%):-%x}"')"
  else
    _bs_src="$0"
  fi
  BATTLESTATION_HOME="$(cd "$(dirname "$_bs_src")/.." && pwd)"
  unset _bs_src
fi
export BATTLESTATION_HOME

# Runtime dirs
export BS_TMP="/tmp/battlestation"
export BS_LOG="$BS_TMP/battlestation.log"
mkdir -p "$BS_TMP" 2>/dev/null

# Per-session dir
export BS_SESSION_DIR="$BS_TMP/$PPID"
mkdir -p "$BS_SESSION_DIR" 2>/dev/null

# Load .env
if [ -f "$BATTLESTATION_HOME/.env" ]; then
  set -a
  source "$BATTLESTATION_HOME/.env"
  set +a
fi

# Defaults — isolated stack: SUPA_URL/SUPA_KEY must come from YOUR .env
: "${SUPA_URL:=}"   # e.g. https://<your-project>.supabase.co
: "${SUPA_KEY:=}"   # your project's anon (or service) key
: "${PERMISSION_LOG_DIR:=$HOME/.claude-permission-feed}"
: "${BS_DEFAULT_COMPANY:=default}"
: "${BS_GITHUB_USER:=}"   # token-watch opens commits at github.com/$BS_GITHUB_USER/<repo>

if [ -z "$SUPA_URL" ] || [ -z "$SUPA_KEY" ]; then
  echo "battlestation: SUPA_URL / SUPA_KEY not set — copy .env.example to .env and fill them in." >&2
fi

export SUPA_URL SUPA_KEY PERMISSION_LOG_DIR BS_DEFAULT_COMPANY BS_GITHUB_USER
