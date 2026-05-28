#!/bin/bash
# Wire — inter-session messaging for Claude Code coordination.
# Requires: config.sh, supabase.sh, session.sh, log.sh, atomic.sh

HOOK_DIR="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)"
# Allow sourcing from any location — load deps if not already loaded
if ! type supa_post &>/dev/null; then
  _WIRE_LIB_DIR="${BATTLESTATION_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
  [ -f "$_WIRE_LIB_DIR/config.sh" ] && source "$_WIRE_LIB_DIR/config.sh"
  [ -f "$_WIRE_LIB_DIR/supabase.sh" ] && source "$_WIRE_LIB_DIR/supabase.sh"
  [ -f "$_WIRE_LIB_DIR/session.sh" ] && source "$_WIRE_LIB_DIR/session.sh"
  [ -f "$_WIRE_LIB_DIR/log.sh" ] && source "$_WIRE_LIB_DIR/log.sh"
  [ -f "$_WIRE_LIB_DIR/atomic.sh" ] && source "$_WIRE_LIB_DIR/atomic.sh"
fi

# wire_send "to_session" "msg_type" "payload_json"
# Returns: HTTP status code (201 = success)
wire_send() {
  local to="$1" msg_type="$2" payload="$3"
  local from
  from=$(bs_session_id 2>/dev/null || echo "cc-${PPID}")

  # Detect current cycle_id
  local cycle_id=""
  local five_resets_at
  five_resets_at=$(jq -r '.rate_limits.five_hour.resets_at // empty' /tmp/statusline-debug.json 2>/dev/null)
  if [ -n "$five_resets_at" ]; then
    cycle_id=$(python3 -c "
from datetime import datetime, timedelta, timezone
r = datetime.fromtimestamp(int('$five_resets_at'), tz=timezone.utc)
print((r - timedelta(hours=5)).isoformat())
" 2>/dev/null || echo "")
  fi

  local body
  body=$(cat <<EOFJ
{
  "from_session": "${from}",
  "to_session": "${to}",
  "msg_type": "${msg_type}",
  "payload": ${payload},
  "cycle_id": "${cycle_id}"
}
EOFJ
)
  local code
  if type supa_post &>/dev/null; then
    code=$(supa_post "session_messages" "$body")
  else
    # Fallback: inline curl if supa_post is missing
    code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${SUPABASE_URL}/rest/v1/session_messages" \
      -H "apikey: ${SUPABASE_KEY}" \
      -H "Authorization: Bearer ${SUPABASE_KEY}" \
      -H "Content-Type: application/json" \
      -d "$body")
  fi
  bs_log "INFO" "wire" "sent ${msg_type} to ${to} (${code})" 2>/dev/null

  # ── Direct ping: touch recipient's trigger file on same machine ──
  # session_id format is "cc-{PPID}" — extract PPID and write their trigger file.
  # This makes delivery near-instant (within one tool call) without waiting for
  # the recipient's daemon to poll Supabase.
  local to_ppid
  to_ppid="${to#cc-}"
  if [[ "$to_ppid" =~ ^[0-9]+$ ]]; then
    local to_session_dir="/tmp/battlestation/${to_ppid}"
    if [ -d "$to_session_dir" ]; then
      # Daemon is running for that session — write live file + touch trigger
      # so the fast path in wire_check_inbox sees it immediately.
      touch "/tmp/wire-trigger-${to_ppid}" 2>/dev/null || true
    fi
    # Also write ping file — caught even if daemon hasn't written live.json yet
    touch "/tmp/wire-ping-${to_ppid}" 2>/dev/null || true
  fi

  echo "$code"
}

# wire_broadcast "msg_type" "payload_json"
# Sends to all active peers except self.
wire_broadcast() {
  local msg_type="$1" payload="$2"
  local my_id
  my_id=$(bs_session_id 2>/dev/null || echo "cc-${PPID}")

  local peers_file="/tmp/claude-peers.json"
  [ ! -f "$peers_file" ] && return

  python3 -c "
import json, sys
with open('${peers_file}') as f:
    peers = json.load(f)
for p in peers:
    sid = p.get('session_id','')
    if sid and sid != '${my_id}':
        print(sid)
" 2>/dev/null | while read -r sid; do
    wire_send "$sid" "$msg_type" "$payload" >/dev/null &
  done
}

# wire_check_inbox
# Fast path: checks daemon trigger file (instant, <1ms).
# If daemon is alive but no trigger, returns [] immediately (no new msgs).
# Falls back to 30s-throttled Supabase poll only if daemon is not running.
wire_check_inbox() {
  local session_dir
  session_dir=$(bs_session_dir 2>/dev/null || echo "/tmp/battlestation/${PPID}")
  mkdir -p "$session_dir" 2>/dev/null

  local trigger_file="/tmp/wire-trigger-${PPID}"
  local ping_file="/tmp/wire-ping-${PPID}"
  local live_file="${session_dir}/wire-live.json"
  local heartbeat_file="${session_dir}/wire-daemon-heartbeat"
  local now
  now=$(date +%s)

  # ── Fastest path: another session pinged us directly ────────────
  # wire_send() writes this ping file when it sends to our session_id.
  # Bypasses ALL throttles — immediate Supabase fetch.
  if [ -f "$ping_file" ]; then
    rm -f "$ping_file"
    # Clear the daemon's trigger + live_file so a follow-up hook call
    # doesn't take the trigger-path and re-surface the same messages.
    # The daemon will rewrite live_file on its next poll if new work
    # arrives, so this is safe — it just prevents duplicate surfacing.
    rm -f "$trigger_file"
    if type atomic_write &>/dev/null; then
      atomic_write "$live_file" "[]" 2>/dev/null
    else
      echo "[]" > "$live_file" 2>/dev/null
    fi
    local my_id
    my_id=$(bs_session_id 2>/dev/null || echo "cc-${PPID}")
    local result
    result=$(supa_get "session_messages" \
      "to_session=eq.${my_id}&read=eq.false&order=created_at.asc&limit=10" 2>/dev/null)
    [ -z "$result" ] || echo "$result" | grep -q '"error"' && result="[]"
    echo "$result"
    return
  fi

  # ── Fast path: daemon is alive ──────────────────────────────────
  if [ -f "$heartbeat_file" ]; then
    local hb_age
    hb_age=$(( now - $(cat "$heartbeat_file" 2>/dev/null || echo 0) ))
    if [ "$hb_age" -lt 10 ]; then
      # Daemon alive — check trigger file
      if [ -f "$trigger_file" ]; then
        # New messages arrived — read live file and remove trigger
        rm -f "$trigger_file"
        cat "$live_file" 2>/dev/null || echo "[]"
        return
      else
        # Daemon alive, no new messages
        echo "[]"
        return
      fi
    fi
  fi

  # ── Fallback: daemon stale/missing — poll Supabase (30s throttle) ──
  local ts_file="${session_dir}/wire-ts"
  local cache_file="${session_dir}/wire-inbox.json"

  if [ -f "$ts_file" ]; then
    local last
    last=$(cat "$ts_file" 2>/dev/null || echo 0)
    if [ $((now - last)) -lt 30 ]; then
      cat "$cache_file" 2>/dev/null || echo "[]"
      return
    fi
  fi

  local my_id
  my_id=$(bs_session_id 2>/dev/null || echo "cc-${PPID}")

  local result
  result=$(supa_get "session_messages" "to_session=eq.${my_id}&read=eq.false&order=created_at.asc&limit=10")

  if [ -z "$result" ] || echo "$result" | grep -q '"error"' 2>/dev/null; then
    result="[]"
  fi

  if type atomic_write &>/dev/null; then
    atomic_write "$cache_file" "$result"
  else
    echo "$result" > "$cache_file"
  fi
  echo "$now" > "$ts_file"

  echo "$result"
}

# wire_mark_read "message_id"
# Atomic: only marks read if currently unread (read=eq.false guard).
# Prevents double-mark-read when concurrent hook invocations or the
# daemon race on the same message.
wire_mark_read() {
  local msg_id="$1"
  supa_patch "session_messages" "id=eq.${msg_id}&read=eq.false" '{"read":true}' >/dev/null
}

# wire_ack "original_message_id" "to_session" "response_text"
wire_ack() {
  local orig_id="$1" to="$2" response="$3"
  supa_patch "session_messages" "id=eq.${orig_id}" '{"acked":true}' >/dev/null
  local payload
  payload=$(python3 -c "import json; print(json.dumps({'ack_message_id':'${orig_id}','response':'${response}'}))" 2>/dev/null)
  wire_send "$to" "ack" "$payload" >/dev/null
}

# wire_request_file_release "file_path" "owning_session_id"
# Deduped: won't re-send for same file within 5 minutes.
wire_request_file_release() {
  local file_path="$1" owner="$2"
  local session_dir
  session_dir=$(bs_session_dir 2>/dev/null || echo "/tmp/battlestation/${PPID}")
  mkdir -p "$session_dir" 2>/dev/null

  local dedup_file="${session_dir}/release-requests.log"
  local filename
  filename=$(basename "$file_path")
  local now
  now=$(date +%s)

  # Check dedup: skip if same file requested within 300s
  if [ -f "$dedup_file" ]; then
    local last_req
    last_req=$(grep "^${filename} " "$dedup_file" 2>/dev/null | tail -1 | awk '{print $2}')
    if [ -n "$last_req" ] && [ $((now - last_req)) -lt 300 ]; then
      return 0
    fi
  fi

  echo "${filename} ${now}" >> "$dedup_file"

  local payload
  payload=$(python3 -c "import json; print(json.dumps({'file_path':'${file_path}','reason':'edit blocked by file lock'}))" 2>/dev/null)
  wire_send "$owner" "file_release" "$payload" >/dev/null
  bs_log "INFO" "wire" "requested file release of ${filename} from ${owner}" 2>/dev/null
}

# wire_cleanup_expired
# Deletes messages past their expires_at.
wire_cleanup_expired() {
  local now_iso
  now_iso=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  supa_delete "session_messages" "expires_at=lt.${now_iso}" >/dev/null
}

# wire_claim_task "task_id" "task_name"
# Broadcasts to all active peers that you've claimed a task.
# Prevents double-claiming when multiple sessions scan Dispatch.
wire_claim_task() {
  local task_id="$1" task_name="$2"
  local payload
  payload=$(python3 -c "import json; print(json.dumps({'task_id':'${task_id}','task_name':'${task_name}'}))" 2>/dev/null \
    || echo "{\"task_id\":\"${task_id}\",\"task_name\":\"${task_name}\"}")
  wire_broadcast "task_claim" "$payload"
  bs_log "INFO" "wire" "claimed task ${task_id}: ${task_name}" 2>/dev/null
}

# wire_handoff "to_session" "task_id" "context_text"
# Pass incomplete work to another session with context.
wire_handoff() {
  local to="$1" task_id="$2" context="$3"
  local payload
  payload=$(python3 -c "import json; print(json.dumps({'task_id':'${task_id}','context':$(echo "$context" | jq -Rs . 2>/dev/null || echo '\"\"')}))" 2>/dev/null \
    || echo "{\"task_id\":\"${task_id}\",\"context\":\"${context}\"}")
  wire_send "$to" "task_handoff" "$payload"
  bs_log "INFO" "wire" "handed off task ${task_id} to ${to}" 2>/dev/null
}

# Backward compat aliases (remove after all hooks updated)
msg_send() { wire_send "$@"; }
msg_send_broadcast() { wire_broadcast "$@"; }
msg_check_inbox() { wire_check_inbox "$@"; }
msg_mark_read() { wire_mark_read "$@"; }
msg_send_ack() { wire_ack "$@"; }
msg_request_file_release() { wire_request_file_release "$@"; }
msg_cleanup_expired() { wire_cleanup_expired "$@"; }

# ── OpenClaw Bridge ──────────────────────────────────────────────────
# wire_send_openclaw "msg_type" "payload_json"
# Shortcut to send a message to the OpenClaw Wire Bridge.
# Falls back to /tmp/wire-bridge/ file if Supabase is unreachable.
wire_send_openclaw() {
  local msg_type="$1" payload="$2"
  local code
  code=$(wire_send "openclaw-main" "$msg_type" "$payload")
  if [ "$code" != "201" ]; then
    # Fallback: write to file-based channel
    mkdir -p /tmp/wire-bridge
    local ts
    ts=$(date +%s)
    local from
    from=$(bs_session_id 2>/dev/null || echo "cc-${PPID}")
    cat > "/tmp/wire-bridge/to-openclaw-${ts}.json" <<EOFJ
{"from_session":"${from}","msg_type":"${msg_type}","payload":${payload}}
EOFJ
    bs_log "WARN" "wire" "supabase failed (${code}), wrote fallback /tmp/wire-bridge/to-openclaw-${ts}.json" 2>/dev/null
  fi
}
