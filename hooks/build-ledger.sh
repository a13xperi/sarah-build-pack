#!/usr/bin/env bash
# PostToolUse hook: auto-log build items to build_ledger on git commit.
# Captures: commit message, files changed, project, company, session.

set -euo pipefail

# Write the raw hook input to a tempfile so subsequent python3 invocations can
# read the exact bytes. `echo "$INPUT" | python3 ...` mangles embedded
# backslashes and multi-line strings in tool_input.command / tool_input.content,
# and Python's default strict json.load raises on literal control characters —
# both failure modes are silent under 2>/dev/null, so build_ledger quietly
# stops logging any commit that happened to come from a multi-line Bash call.
INPUT=$(cat)
HOOK_INPUT_FILE=$(mktemp -t bldledger.XXXXXX)
trap 'rm -f "$HOOK_INPUT_FILE"' EXIT
printf '%s' "$INPUT" > "$HOOK_INPUT_FILE"

TOOL_NAME=$(python3 -c "import json; print(json.loads(open('$HOOK_INPUT_FILE').read(), strict=False).get('tool_name',''))" 2>/dev/null || echo "")
[[ "$TOOL_NAME" != "Bash" ]] && exit 0

COMMAND=$(python3 -c "import json; print(json.loads(open('$HOOK_INPUT_FILE').read(), strict=False).get('tool_input',{}).get('command',''))" 2>/dev/null || echo "")

# Only fire on git commit
echo "$COMMAND" | grep -qE 'git commit' || exit 0

# Get commit info (post-commit — HEAD is the new commit)
COMMIT_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "")
[[ -z "$COMMIT_SHA" ]] && exit 0

COMMIT_MSG=$(git log -1 --pretty=%s 2>/dev/null || echo "")
[[ -z "$COMMIT_MSG" ]] && exit 0

# Full commit body (subject + body) — used by the audit-finding close-loop below.
COMMIT_BODY=$(git log -1 --pretty=%B 2>/dev/null || echo "")

# Files changed
FILES_JSON=$(git diff --name-only HEAD~1 2>/dev/null | head -20 | python3 -c "
import sys, json
print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))
" 2>/dev/null || echo "[]")

# Project from CWD — isolated stack: defaults to git repo / dir basename.
# Override via ~/.battlestation-repos if you want richer company/project labels.
CWD="${PWD:-$(pwd)}"
PROJECT=$(basename "$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null || echo "$CWD")")
COMPANY="${BS_DEFAULT_COMPANY:-default}"

# Item type from commit prefix
ITEM_TYPE="feature"
MSG_LOWER=$(echo "$COMMIT_MSG" | tr '[:upper:]' '[:lower:]')
echo "$MSG_LOWER" | grep -qE '^fix[:(]' && ITEM_TYPE="fix"
echo "$MSG_LOWER" | grep -qE '^refactor[:(]' && ITEM_TYPE="refactor"
echo "$MSG_LOWER" | grep -qE '^docs[:(]' && ITEM_TYPE="docs"
echo "$MSG_LOWER" | grep -qE '^test[:(]' && ITEM_TYPE="test"
echo "$MSG_LOWER" | grep -qE '^chore[:(]' && ITEM_TYPE="chore"
echo "$MSG_LOWER" | grep -qE '^\[decision\]|^decision:' && ITEM_TYPE="decision"
echo "$MSG_LOWER" | grep -qE '^dispatch:' && ITEM_TYPE="feature"

# Truncate title
TITLE=$(echo "$COMMIT_MSG" | head -1 | cut -c1-120)

# Difficulty + points from file count and type
NUM_FILES=$(echo "$FILES_JSON" | python3 -c "import sys,json; print(len(json.loads(sys.stdin.read(), strict=False)))" 2>/dev/null || echo "1")
DIFFICULTY="medium"
POINTS=1
if [ "$NUM_FILES" -le 1 ]; then
  DIFFICULTY="easy"
  POINTS=1
elif [ "$NUM_FILES" -le 4 ]; then
  DIFFICULTY="medium"
  POINTS=2
elif [ "$NUM_FILES" -le 10 ]; then
  DIFFICULTY="hard"
  POINTS=3
else
  DIFFICULTY="complex"
  POINTS=5
fi
# Decisions are always easy to verify
[[ "$ITEM_TYPE" == "decision" ]] && DIFFICULTY="easy" && POINTS=1
# Fixes get a point bump (regression risk)
[[ "$ITEM_TYPE" == "fix" ]] && POINTS=$((POINTS + 1))

SESSION_ID="cc-${PPID}"

# Detect current cycle_id from statusline (resets_at is unix epoch)
CYCLE_ID=""
FIVE_RESETS_AT=$(jq -r '.rate_limits.five_hour.resets_at // empty' /tmp/statusline-debug.json 2>/dev/null || echo "")
if [ -n "$FIVE_RESETS_AT" ]; then
  CYCLE_ID=$(python3 -c "
from datetime import datetime, timedelta, timezone
reset = datetime.fromtimestamp(int('$FIVE_RESETS_AT'), tz=timezone.utc)
print((reset - timedelta(hours=5)).isoformat())
" 2>/dev/null || echo "")
fi

# Generate test hint from files + commit message + project
TEST_HINT=$(python3 -c "
import json, sys, os

files = json.loads(sys.argv[1])
msg = sys.argv[2]
project = sys.argv[3]
item_type = sys.argv[4]
msg_l = msg.lower()

hints = []

# --- Atlas: keyword → route + specific action ---
if project in ('atlas', 'atlas-backend'):
    route_hints = {
        'craft': ('/crafting', 'Draft a tweet — check AI generation + voice match'),
        'draft': ('/crafting', 'Create a draft — check generation + history sidebar'),
        'voice': ('/voice-profiles', 'Check voice sliders, blend creation, calibration'),
        'blend': ('/voice-profiles', 'Test voice blend — create/edit/preview'),
        'dashboard': ('/dashboard', 'Check stats cards, trending widget, quick-draft'),
        'feed': ('/feed', 'Check morning brief, unposted drafts, live signals'),
        'oracle': ('/onboarding', 'Run onboarding chat — check Oracle renders'),
        'onboard': ('/onboarding', 'Test onboarding — X connect + voice calibration'),
        'campaign': ('/campaigns', 'Check campaign list, create/edit, status filters'),
        'queue': ('/queue', 'Check posting queue — schedule, batch, timeline'),
        'signal': ('/alerts', 'Check alerts feed, type filter, draft-from-signal'),
        'alert': ('/alerts', 'Check alerts + MonitorBuilder subscriptions'),
        'brief': ('/briefing', 'Test briefing — topic/source selectors, AI gen'),
        'telegram': ('/telegram', 'Check Telegram setup page — connection status'),
        'arena': ('/arena', 'Check leaderboard scores, tier badges, rankings'),
        'analytic': ('/analytics', 'Check charts, confidence trend, top drafts'),
        'library': ('/team-library', 'Browse team library — drafts, filters, copy'),
        'profile': ('/profile', 'Check profile edit — name, bio, role badge'),
        'search': ('/search', 'Test search — drafts + voice dimensions'),
        'manage': ('/management', 'Check team management dashboard'),
        'admin': ('/admin', 'Check admin tools — style tile or QA runner'),
    }
    for kw, (route, action) in route_hints.items():
        if kw in msg_l:
            hints.append(f'Open delphi-atlas.vercel.app{route} — {action}')
            break

    # Fallback: detect route from file paths
    if not hints:
        file_route_map = {
            'crafting': ('/crafting', 'Check crafting station'),
            'voice': ('/voice-profiles', 'Check voice studio'),
            'dashboard': ('/dashboard', 'Check dashboard'),
            'feed': ('/feed', 'Check feed page'),
            'onboarding': ('/onboarding', 'Check onboarding flow'),
            'campaign': ('/campaigns', 'Check campaigns'),
            'queue': ('/queue', 'Check posting queue'),
            'alert': ('/alerts', 'Check alerts/signals'),
            'briefing': ('/briefing', 'Check briefing page'),
            'arena': ('/arena', 'Check arena leaderboard'),
            'analytics': ('/analytics', 'Check analytics'),
            'team-library': ('/team-library', 'Check team library'),
            'profile': ('/profile', 'Check profile page'),
            'search': ('/search', 'Check search'),
        }
        file_str = ' '.join(files).lower()
        for dir_kw, (route, action) in file_route_map.items():
            if dir_kw in file_str:
                hints.append(f'Open delphi-atlas.vercel.app{route} — {action}')
                break

# --- token-watch: keyword → tab + key + action ---
if project == 'token-watch':
    tab_hints = {
        'mission': ('M', 'Mission Control', 'check build items grouped by company'),
        'wire': ('w', 'Wire', 'check message log renders'),
        'test': ('x', 'Test Queue', 'check items, pass/fail/skip actions'),
        'usage': ('u', 'Usage', 'check metrics table + window scores'),
        'mcp': ('m', 'MCP', 'check server/action stats tables'),
        'cycle': ('s', 'Cycle', 'check task list + Pomodoro blocks'),
        'session': ('s', 'Cycle', 'check session tasks render'),
        'pomodoro': ('s', 'Cycle', 'check Pomodoro block assignment'),
        'project': ('p', 'Board', 'check project task list'),
        'board': ('p', 'Board', 'check task board renders'),
        'leaderboard': ('l', 'Leaderboard', 'check rankings table'),
        'audit': ('a', 'Audit', 'check cross-cycle summary'),
        'rule': ('g', 'Rules', 'check hook/permission list'),
        'capacity': ('c', 'Capacity', 'check A/B/C account panels'),
        'account': ('c', 'Capacity', 'check account panels'),
        'health': ('h', 'Health', 'check system health panel'),
        'burndown': ('Dashboard', 'Dashboard', 'check burndown chart'),
        'attribution': ('Dashboard', 'Dashboard', 'check token attribution'),
        'banner': ('all', 'Banner', 'check cycle banner + [ ] navigation'),
        'dashboard': ('Dashboard', 'Dashboard', 'check all panels render'),
        'nav': ('all', 'NavBar', 'check all tabs accessible'),
    }
    for kw, (key, tab, action) in tab_hints.items():
        if kw in msg_l:
            hints.append(f'Open token-watch -> {tab} ({key}) -- {action}')
            break

    # Fallback: file-based detection
    if not hints:
        for f in files:
            if 'tui.py' in f:
                hints.append('Open token-watch -- check affected tab (see commit msg)')
                break
            if 'data.py' in f:
                hints.append('Open token-watch -- verify data loads on related tab')
                break
            if '.tcss' in f:
                hints.append('Open token-watch -- check styling/layout')
                break

# --- battlestation: hook/lib specific ---
if project == 'battlestation' and not hints:
    bs_hints = {
        'wire': 'Send a wire message and check session_messages in Supabase',
        'ledger': 'Make a git commit and verify build_ledger entry in Supabase',
        'build-ledger': 'Make a git commit and verify build_ledger entry',
        'heartbeat': 'Check session heartbeat updates in session_locks',
        'hook': 'Trigger the relevant hook and check Supabase/logs',
        'cycle': 'Close/navigate a cycle in token-watch and verify',
        'park': 'Run /park and check build_ledger for item_type=idea',
        'capacity': 'Run token-watch --snapshot and check output',
        'statusline': 'Check statusline renders correctly in terminal',
    }
    for kw, hint in bs_hints.items():
        if kw in msg_l:
            hints.append(hint)
            break

# --- Generic file pattern detection (all projects) ---
exts = set(os.path.splitext(f)[1] for f in files)
dirs = set()
for f in files:
    parts = f.split('/')
    if len(parts) > 1:
        dirs.add(parts[0])
    if len(parts) > 2:
        dirs.add('/'.join(parts[:2]))

# Frontend components
if any(f.endswith(('.tsx', '.jsx')) and 'component' in f.lower() for f in files):
    hints.append('Visual: check the UI component renders correctly')
elif any(f.endswith(('.tsx', '.jsx')) for f in files):
    hints.append('Visual: open the page and verify layout')

# API / backend
if any('api/' in f or 'route' in f.lower() for f in files):
    hints.append('API: hit the endpoint and check response')

# Hooks / scripts
if any(f.endswith('.sh') for f in files):
    if 'hook' in ' '.join(files).lower():
        hints.append('Trigger the hook (make a tool call) and check logs')
    else:
        hints.append('Run the script manually and check output')

# Config / settings
if any(f.endswith('.json') and 'setting' in f.lower() for f in files):
    hints.append('Restart session — verify new config takes effect')

# Python libs
if any(f.endswith('.py') and 'lib/' in f for f in files):
    hints.append('Source the lib and call the new/changed functions')

# CSS / styles
if any(f.endswith(('.css', '.tcss', '.scss')) for f in files):
    if not hints:
        hints.append('Visual: check styling in the UI')

# Tests
if any('test' in f.lower() for f in files):
    hints.append('Run the test suite')

# --- Item type fallbacks ---
if not hints:
    if item_type == 'fix':
        hints.append('Verify the bug is fixed — reproduce the original issue')
    elif item_type == 'refactor':
        hints.append('Verify no regressions — existing behavior unchanged')
    elif item_type == 'decision':
        hints.append('Review: confirm the decision rationale makes sense')
    elif item_type == 'docs':
        hints.append('Read the docs — check accuracy and completeness')
    elif item_type == 'feature':
        hints.append('Test the new feature end-to-end')

# Combine (max 2 hints)
print(' + '.join(hints[:2]) if hints else 'Manual review needed')
" "$FILES_JSON" "$COMMIT_MSG" "$PROJECT" "$ITEM_TYPE" 2>/dev/null || echo "Manual review needed")

# Isolated stack: read from env (exported by lib/config.sh). No baked-in creds.
SUPABASE_URL="${SUPA_URL:?SUPA_URL not set — source lib/config.sh}/rest/v1"
SUPABASE_KEY="${SUPA_KEY:?SUPA_KEY not set — source lib/config.sh}"

# Audit-finding close-loop: any `closes WARN-2 / closes INFO-4 / closes CRIT-1`
# in the commit body flips the matching build_ledger audit_finding row to
# finding_status='closed'. Silent on failure (never break a commit).
if echo "$COMMIT_BODY" | grep -qiE 'clos(e|es|ed)[: ]+\s*(CRIT(ICAL)?|WARN|INFO)'; then
  LEDGER_PROJECT="$PROJECT" \
  LEDGER_SHA="$COMMIT_SHA" \
  LEDGER_BODY="$COMMIT_BODY" \
  LEDGER_URL="$SUPABASE_URL" \
  LEDGER_KEY="$SUPABASE_KEY" \
  python3 - <<'PY' 2>/dev/null || true
import os, re, json, urllib.request, datetime
body = os.environ.get("LEDGER_BODY", "")
project = os.environ.get("LEDGER_PROJECT", "")
sha = os.environ.get("LEDGER_SHA", "")
url_base = os.environ.get("LEDGER_URL", "")
key = os.environ.get("LEDGER_KEY", "")
if not (body and project and sha and url_base and key):
    raise SystemExit(0)
# Matches: closes WARN-2 | closes WARN #2 | Closes: INFO-4 | closed CRITICAL 1
pat = re.compile(r'clos(?:e|es|ed)[: ]+\s*(CRIT(?:ICAL)?|WARN|INFO)[\s#\-]*(\d+)', re.I)
now = datetime.datetime.utcnow().isoformat() + "Z"
seen = set()
for sev_raw, num in pat.findall(body):
    sev = sev_raw.upper()
    if sev == "CRITICAL":
        sev = "CRIT"
    code = f"{sev}-{num}"
    if code in seen:
        continue
    seen.add(code)
    url = (f"{url_base}/build_ledger"
           f"?project=eq.{project}"
           f"&finding_code=eq.{code}"
           f"&item_type=eq.audit_finding")
    payload = json.dumps({
        "finding_status": "closed",
        "closed_by_sha": sha,
        "closed_at": now,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="PATCH", headers={
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    })
    try:
        urllib.request.urlopen(req, timeout=2).close()
    except Exception:
        pass
PY
fi

PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
    'session_id': sys.argv[1],
    'project': sys.argv[2],
    'company': sys.argv[3],
    'item_type': sys.argv[4],
    'title': sys.argv[5],
    'files': json.loads(sys.argv[6]),
    'commit_sha': sys.argv[7],
    'test_status': 'untested',
    'source': 'commit',
    'test_hint': sys.argv[8],
    'difficulty': sys.argv[9],
    'points': int(sys.argv[10]),
    'cycle_id': sys.argv[11] if len(sys.argv) > 11 and sys.argv[11] else None
}))
" "$SESSION_ID" "$PROJECT" "$COMPANY" "$ITEM_TYPE" "$TITLE" "$FILES_JSON" "$COMMIT_SHA" "$TEST_HINT" "$DIFFICULTY" "$POINTS" "$CYCLE_ID" 2>/dev/null || exit 0)

curl -s -X POST "$SUPABASE_URL/build_ledger" \
    -H "apikey: $SUPABASE_KEY" \
    -H "Authorization: Bearer $SUPABASE_KEY" \
    -H "Content-Type: application/json" \
    -H "Prefer: return=minimal" \
    -d "$PAYLOAD" > /dev/null 2>&1 || true

exit 0
