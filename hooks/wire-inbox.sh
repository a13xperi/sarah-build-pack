#!/bin/bash
# Wire — inter-session messaging inbox hook.
# Surfaces unread Wire messages as warn decisions so Claude sees them.
# Throttled to 30s internally via wire_check_inbox.

HOOK_DIR="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)"
LIB_DIR="${HOOK_DIR}/../lib"

[ -f "$LIB_DIR/config.sh" ] && source "$LIB_DIR/config.sh"
[ -f "$LIB_DIR/session.sh" ] && source "$LIB_DIR/session.sh"
[ -f "$LIB_DIR/supabase.sh" ] && source "$LIB_DIR/supabase.sh"
[ -f "$LIB_DIR/atomic.sh" ] && source "$LIB_DIR/atomic.sh"
[ -f "$LIB_DIR/log.sh" ] && source "$LIB_DIR/log.sh"
[ -f "$LIB_DIR/wire.sh" ] && source "$LIB_DIR/wire.sh"
[ -f "$LIB_DIR/hook-gate.sh" ] && source "$LIB_DIR/hook-gate.sh"

# Consume stdin (required by hook protocol)
INPUT=$(cat)

# Drain prevention: wire-inbox is tier=active (skip when idle/dormant)
# and warn decisions are budget-gated
if [ -n "${_HG_LOADED:-}" ]; then
  hg_should_fire "wire-inbox" "active" || exit 0
fi

# Resolve session dir once — used by the Python dedupe pass below.
session_dir=$(bs_session_dir 2>/dev/null || echo "/tmp/battlestation/${PPID}")
mkdir -p "$session_dir" 2>/dev/null

# Check inbox (throttled to 30s)
MESSAGES=$(msg_check_inbox 2>/dev/null)

# Fast path: no messages
if [ -z "$MESSAGES" ] || [ "$MESSAGES" = "[]" ] || [ "$MESSAGES" = "null" ]; then
  exit 0
fi

# Count messages
COUNT=$(echo "$MESSAGES" | python3 -c "import json,sys; print(len(json.loads(sys.stdin.read(), strict=False)))" 2>/dev/null)
[ -z "$COUNT" ] || [ "$COUNT" = "0" ] && exit 0

# Format summaries and build the warn decision in one python pass.
#
# Previous versions of this script had three surfacing bugs:
# 1. Assumed `payload` was a dict. Supabase sometimes returns JSONB as a string
#    (depends on column type + client codec). `.get()` on a string raised
#    AttributeError, which was silently swallowed by `2>/dev/null`, so the
#    entire warn decision vanished and Claude never saw the inbox.
# 2. Built the decision JSON via bash string interpolation — any `"`, `\`,
#    or newline in a payload text field produced invalid JSON, which the hook
#    runtime rejects, silently dropping the surfacing.
# 3. Used `head -1` / `tail -1` to split summary from IDs, which corrupted any
#    message with an embedded newline.
#
# The fix: do EVERYTHING (parse, escape, construct the decision) inside python
# and emit a single line of valid JSON plus a newline-separated ID list.
# MESSAGES is passed via env var (NOT stdin) because combining `python3 <<PY`
# with a pipe caused the heredoc to override the pipe, leaving stdin empty —
# which was the original silent-fail mode.
#
# Additionally, this pass dedupes messages against SURFACED_FILE — a
# session-local set of (id, ts) rows. Without dedupe, a message can be
# surfaced twice in fast succession because:
#   (a) ping-path reads straight from Supabase, then daemon re-polls before
#       the mark_read PATCH has propagated and writes a new trigger file, OR
#   (b) the daemon restarts mid-session and loses its in-memory known_ids
#       set, then re-surfaces everything that's still unread in the DB.
# The file is pruned to entries newer than 300s on each write.
SURFACED_FILE="${session_dir:-/tmp/battlestation/${PPID}}/surfaced-ids.txt"
FORMATTED=$(MESSAGES_JSON="$MESSAGES" COUNT="$COUNT" SURFACED_FILE="$SURFACED_FILE" python3 -c '
import json, os, sys, time

def _payload(m):
    p = m.get("payload", {})
    if isinstance(p, str):
        try:
            # strict=False tolerates literal control chars (e.g. newlines in
            # task_complete messages that include multi-line commit bodies).
            p = json.loads(p, strict=False)
        except Exception:
            return {}
    return p if isinstance(p, dict) else {}

def _summarize(m):
    fr = m.get("from_session", "?")
    mt = m.get("msg_type", "?")
    p = _payload(m)
    if mt == "file_release":
        return "{0} requests release of {1}: {2}".format(fr, p.get("file_path","?"), p.get("reason",""))
    if mt == "patch":
        return "{0} proposes patch to {1}: {2}".format(fr, p.get("file_path","?"), p.get("description",""))
    if mt in ("info", "status"):
        return "{0}: {1}".format(fr, p.get("message",""))
    if mt == "question":
        return "{0} asks: {1}".format(fr, p.get("question", p.get("message","")))
    if mt == "ack":
        return "{0} acknowledged your message".format(fr)
    if mt == "task_handoff":
        return "{0} handoff: task #{1}".format(fr, p.get("task_id","?"))
    return "{0} [{1}]: {2}".format(fr, mt, json.dumps(p)[:80])

try:
    # strict=False: PostgREST sometimes returns literal newlines inside string
    # fields for multi-line content; Python defaults raise on control chars.
    msgs = json.loads(os.environ.get("MESSAGES_JSON", "[]"), strict=False)
except Exception:
    sys.exit(0)

if not isinstance(msgs, list) or not msgs:
    sys.exit(0)

# ── Dedupe against session-local surfaced-ids file ─────────────
# Entries are pruned to <300s on each write. If a message id is
# already in the set, silently drop it — we already told Claude.
SURFACED_FILE = os.environ.get("SURFACED_FILE", "")
now = int(time.time())
kept = {}   # id -> ts for entries still within the 300s window
if SURFACED_FILE and os.path.exists(SURFACED_FILE):
    try:
        with open(SURFACED_FILE) as f:
            for line in f:
                bits = line.strip().split()
                if len(bits) == 2 and bits[1].isdigit():
                    ts = int(bits[1])
                    if now - ts < 300:
                        kept[bits[0]] = ts
    except Exception:
        pass

fresh = [m for m in msgs if m.get("id") and m["id"] not in kept]
if not fresh:
    sys.exit(0)

ids = [m.get("id") for m in fresh if m.get("id")]
parts = [_summarize(m).replace(chr(10), " ").replace(chr(13), " ").strip() for m in fresh]
parts = [p for p in parts if p]

if not parts:
    sys.exit(0)

# Persist the merged set (old kept + new surfaced) atomically.
# Writing a tmp + rename avoids concurrent-hook corruption.
if SURFACED_FILE:
    try:
        os.makedirs(os.path.dirname(SURFACED_FILE), exist_ok=True)
        tmp = SURFACED_FILE + ".tmp"
        with open(tmp, "w") as f:
            for mid, ts in kept.items():
                f.write("{0} {1}\n".format(mid, ts))
            for mid in ids:
                f.write("{0} {1}\n".format(mid, now))
        os.replace(tmp, SURFACED_FILE)
    except Exception:
        pass

count = str(len(fresh))
reason = "INBOX ({0}): {1}".format(count, " | ".join(parts))

# Line 1: the JSON decision. Line 2: space-separated ids.
print(json.dumps({"decision": "warn", "reason": reason}))
print(" ".join(ids))
' 2>/dev/null)

# Nothing to surface — bail before any side effects.
[ -z "$FORMATTED" ] && exit 0

# Line 1 = decision JSON, line 2 = space-separated message ids.
DECISION=$(printf "%s\n" "$FORMATTED" | sed -n '1p')
MSG_IDS=$(printf "%s\n" "$FORMATTED" | sed -n '2p')

[ -z "$DECISION" ] && exit 0

# ── Deferred mark-read ───────────────────────────────────────────
# Previous bug: msg_mark_read ran in the background BEFORE Claude
# consumed the echoed decision. If the PATCH completed before the
# runtime picked up stdout, the daemon's next poll saw 0 unread,
# never re-triggered, and Claude never processed the message.
#
# Fix: write IDs to a pending-read file. On the NEXT hook call,
# mark the previous batch as read. This guarantees Claude had at
# least one full throttle cycle (30s) to consume the decision.
PENDING_READ_FILE="${session_dir}/wire-pending-read.txt"

# First: mark any PREVIOUSLY pending IDs as read (from last cycle)
if [ -f "$PENDING_READ_FILE" ]; then
  while IFS= read -r mid; do
    [ -n "$mid" ] && msg_mark_read "$mid" &
  done < "$PENDING_READ_FILE"
  rm -f "$PENDING_READ_FILE"
fi

# Then: write current IDs as pending (will be marked read next cycle)
for mid in $MSG_IDS; do
  [ -n "$mid" ] && echo "$mid" >> "$PENDING_READ_FILE"
done

# Drain prevention: check warn budget before surfacing to Claude
if [ -n "${_HG_LOADED:-}" ] && ! hg_warn_allowed; then
  hg_log_suppressed "wire-inbox" "warn-budget-exceeded (${COUNT} msgs suppressed)"
  exit 0
fi

# Surface the pre-escaped decision verbatim (it's already valid JSON)
echo "$DECISION"

exit 0
