#!/bin/bash
# Auto-register Claude Code session in Supabase.
#
# KEY DESIGN: session_id is "cc-{PPID}" — one row per terminal, always.
# PPID is stable for the lifetime of a terminal window. This prevents
# duplicate registrations no matter how many times flags get cleared.
#
# The work unit ID (human-readable slug) goes in the "notes" field.

HOOK_DIR="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)"
source "$HOOK_DIR/../lib/config.sh"
source "$HOOK_DIR/../lib/session.sh"
source "$HOOK_DIR/../lib/supabase.sh"
source "$HOOK_DIR/../lib/atomic.sh"
source "$HOOK_DIR/../lib/log.sh"
[ -f "$HOOK_DIR/../lib/hook-gate.sh" ] && source "$HOOK_DIR/../lib/hook-gate.sh"

# Touch activity on every tool call (auto-register runs on every tool call)
[ -n "${_HG_LOADED:-}" ] && hg_touch_activity

# Drain prevention: auto-register is tier=background (throttled when idle/dormant)
if [ -n "${_HG_LOADED:-}" ]; then
  hg_should_fire "auto-register" "background" || exit 0
fi

# Capacity guard — auto-switch if current account is locked
source "$HOOK_DIR/../lib/session-guard.sh" 2>/dev/null && session_guard_check || true

# ── Directive ────────────────────────────────────────────────────
# Also read from the legacy location for compat
DIRECTIVE=$(bs_directive)
[ -z "$DIRECTIVE" ] && DIRECTIVE=$(cat /tmp/claude-directive-$PPID 2>/dev/null || echo "")
[ -z "$DIRECTIVE" ] && DIRECTIVE="unnamed session"

# ── Next session prompt (if written by session) ─────────────────
NSP=""
NSP_FILE="/tmp/battlestation/$PPID/next-session-prompt"
if [ -f "$NSP_FILE" ]; then
  NSP=$(cat "$NSP_FILE" 2>/dev/null | head -c 4000)
fi
NSP_JSON=$(echo "$NSP" | jq -Rs . 2>/dev/null || echo "null")

SESSION_ID=$(bs_session_id)

# ── Throttle: skip if same directive and last update < 60s ago ──
if ! bs_throttle "$DIRECTIVE" 60; then
  exit 0
fi

# ── Detect repo + company/project ────────────────────────────────
# Isolated stack: defaults to the git repo basename. Customize the mapping
# for your own repos in ~/.battlestation-repos (sourced below if present):
#   register_repo "myapp"  "MyCo"   "myapp"      # match-substring  company  project
REPO="unknown"
COMPANY=""
PROJECT=""
GIT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")
REPO=$(basename "$GIT_ROOT")

# Optional per-operator repo→company/project map.
# Define register_repo() matches in ~/.battlestation-repos to override.
if [ -f "$HOME/.battlestation-repos" ]; then
  register_repo() {
    # $1 = substring to match in GIT_ROOT, $2 = company, $3 = project
    case "$GIT_ROOT" in
      *"$1"*) COMPANY="$2"; PROJECT="$3" ;;
    esac
  }
  # shellcheck disable=SC1091
  source "$HOME/.battlestation-repos"
fi

# Fallback: project defaults to the repo name.
: "${PROJECT:=$REPO}"
: "${COMPANY:=Default}"

# ── Human-readable work unit slug ────────────────────────────────
bs_set_workunit "$DIRECTIVE"
SLUG=$(bs_workunit)

# Also write to legacy location for backward compat
echo "$SLUG" > /tmp/claude-workunit-$PPID

# ── Changed files (uncommitted only — committed files don't need locks) ──
# IMPORTANT: advisor sessions NEVER hold file locks. Advisors are read-only
# by design — they plan + route + watch, they never write code. If an advisor
# terminal happens to CWD into a repo with uncommitted changes (from another
# session), `git diff` would return those files and the advisor would claim
# ghost locks on files it doesn't own. This blocks the actual worker sessions
# from editing their own files. Hard rule: advisor files_touched stays empty.
#
# Role authority: the marker file /tmp/claude-advisor-$PPID is the authoritative
# signal. When present, we write role=advisor into the session_locks row directly
# from this hook — no race window waiting for /advisor-on's follow-up PATCH.
# Directive-text match ("advisor" substring) is a *fallback* for files_touched
# suppression only — it does NOT set role (too loose; "advising on refactor"
# would false-positive).
IS_ADVISOR=false
ROLE_CLAUSE=""
if [ -f "/tmp/claude-advisor-$PPID" ]; then
  IS_ADVISOR=true
  # Only emit the role field when we have authoritative evidence (marker file).
  # Leaving it out of the PATCH preserves any existing role value — critical so
  # worker sessions don't have their role field nulled on every heartbeat.
  ROLE_CLAUSE="\"role\": \"advisor\","
elif echo "$DIRECTIVE" | grep -qi "advisor"; then
  IS_ADVISOR=true
fi

FILES="[]"
if [ "$IS_ADVISOR" = "false" ] && git rev-parse --is-inside-work-tree &>/dev/null; then
  # Staged + unstaged changes only — not committed diffs
  FILES=$(git diff --name-only HEAD 2>/dev/null | head -8 | jq -R . 2>/dev/null | jq -s . 2>/dev/null || echo "[]")
fi

# ── Account — resolve from keychain (real session token, not accounts.json) ──
source "$HOOK_DIR/../lanes/accounts.sh" 2>/dev/null
bs_resolve_account_from_keychain "$PPID" 2>/dev/null || true
ACCOUNT=$(bs_active_label)
NOW_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# ── Telemetry: mem, tokens, model, five_pct from statusline ──────
DEBUG="/tmp/statusline-debug.json"
MEM_MB=0
OUTPUT_TOKENS=0
MODEL_STR="null"
FIVE_PCT_VAL="null"
if [ -f "$DEBUG" ]; then
  MEM_MB=$(ps -o rss= -p "$PPID" 2>/dev/null | awk '{printf "%.0f", $1/1024}' || echo 0)
  OUTPUT_TOKENS=$(jq -r '.context_window.total_output_tokens // 0' "$DEBUG" 2>/dev/null)
  MODEL_STR=$(jq '.model.id // null' "$DEBUG" 2>/dev/null)
  FIVE_PCT_VAL=$(jq -r '.rate_limits.five_hour.used_percentage // empty' "$DEBUG" 2>/dev/null)
  [ -z "$FIVE_PCT_VAL" ] && FIVE_PCT_VAL="null"
fi

# ── Look up existing row + current role BEFORE we PATCH ──────────
# Why: explicit role PATCHes from skills (/forge-prime, /forge-audit, etc.)
# are authoritative. The hook gives a default; it must NOT clobber an
# explicit role on heartbeat. We treat null / empty / "unknown" as
# "no authoritative role yet" — only then is the hook allowed to write
# role on a heartbeat PATCH. First-time INSERTs always carry the default.
EXISTING=$(supa_get "session_locks" "session_id=eq.${SESSION_ID}&select=session_id,role")
EXISTS_ROW=false
EXISTING_ROLE=""
if [ -n "$EXISTING" ] && [ "$EXISTING" != "[]" ]; then
  EXISTS_ROW=true
  EXISTING_ROLE=$(echo "$EXISTING" | jq -r '.[0].role // ""' 2>/dev/null)
fi

# PATCH role-clause: only include role on heartbeat when existing role
# is missing/default. Otherwise leave role out of the body entirely so
# whatever a skill set stays put.
PATCH_ROLE_CLAUSE=""
if [ -n "$ROLE_CLAUSE" ]; then
  case "$EXISTING_ROLE" in
    ""|"unknown"|"null") PATCH_ROLE_CLAUSE="$ROLE_CLAUSE" ;;
  esac
fi

# ── Build PATCH payload (heartbeat path) ─────────────────────────
PAYLOAD="{
  ${PATCH_ROLE_CLAUSE}
  \"tool\": \"claude-code\",
  \"repo\": \"${REPO}\",
  \"task_name\": $(echo "$DIRECTIVE" | jq -R .),
  \"files_touched\": ${FILES},
  \"status\": \"active\",
  \"account\": \"${ACCOUNT}\",
  \"heartbeat_at\": \"${NOW_ISO}\",
  \"notes\": \"${SLUG}\",
  \"next_session_prompt\": ${NSP_JSON},
  \"mem_mb\": ${MEM_MB},
  \"output_tokens\": ${OUTPUT_TOKENS},
  \"model\": ${MODEL_STR},
  \"five_pct\": ${FIVE_PCT_VAL}
}"

# ── Upsert: PATCH existing row, INSERT only if no row exists ─────
if [ "$EXISTS_ROW" = "true" ]; then
  supa_patch "session_locks" "session_id=eq.${SESSION_ID}" "$PAYLOAD" >/dev/null
else
  INSERT_PAYLOAD="{
    ${ROLE_CLAUSE}
    \"session_id\": \"${SESSION_ID}\",
    \"tool\": \"claude-code\",
    \"repo\": \"${REPO}\",
    \"task_ref\": \"pid:${PPID}\",
    \"task_name\": $(echo "$DIRECTIVE" | jq -R .),
    \"files_touched\": ${FILES},
    \"status\": \"active\",
    \"account\": \"${ACCOUNT}\",
    \"claimed_at\": \"${NOW_ISO}\",
    \"heartbeat_at\": \"${NOW_ISO}\",
    \"notes\": \"${SLUG}\",
    \"next_session_prompt\": ${NSP_JSON},
    \"mem_mb\": ${MEM_MB},
    \"output_tokens\": ${OUTPUT_TOKENS},
    \"model\": ${MODEL_STR},
    \"five_pct\": ${FIVE_PCT_VAL}
  }"
  supa_post "session_locks" "$INSERT_PAYLOAD" >/dev/null
fi

# ── Update throttle state ────────────────────────────────────────
bs_update_throttle "$DIRECTIVE"

# ── Cache active peers for statusline (atomic write) ─────────────
atomic_write_cmd "/tmp/claude-peers.json" \
  supa_get "session_locks" "status=eq.active&select=session_id,task_name,repo,heartbeat_at,files_touched&order=claimed_at.desc"

# ── Start Wire real-time daemon (once per session) ───────────────
DAEMON_PID_FILE="/tmp/battlestation/${PPID}/wire-daemon.pid"
DAEMON_HB_FILE="/tmp/battlestation/${PPID}/wire-daemon-heartbeat"
DAEMON_ALIVE=false
if [ -f "$DAEMON_PID_FILE" ]; then
  DAEMON_PID=$(cat "$DAEMON_PID_FILE" 2>/dev/null)
  if [ -n "$DAEMON_PID" ] && kill -0 "$DAEMON_PID" 2>/dev/null; then
    DAEMON_ALIVE=true
  fi
fi
if [ "$DAEMON_ALIVE" = false ]; then
  python3 "$HOOK_DIR/../lib/wire-daemon.py" \
    --session "${SESSION_ID}" \
    --ppid "${PPID}" \
    >/dev/null 2>&1 &
fi

# ── Generate shared briefing file ────────────────────────────────
python3 "$HOOK_DIR/generate-briefing.py" 2>/dev/null &

# ── Inject strategic directives (throttled, <50ms) ──────────────
source "$HOOK_DIR/../lib/directives.sh" 2>/dev/null && inject_directives_to_briefing "$REPO" &

bs_log "INFO" "auto-register" "registered ${SESSION_ID} repo=${REPO} directive=${DIRECTIVE}"
exit 0
