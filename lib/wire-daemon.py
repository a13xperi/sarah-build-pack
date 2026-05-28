#!/usr/bin/env python3
"""
Wire Daemon — real-time inbox poller for inter-session messaging.

Polls Supabase for messages addressed to this session.
Writes results to /tmp/battlestation/$PPID/wire-live.json.
Creates /tmp/wire-trigger-$PPID when new messages arrive — the
wire-inbox.sh hook detects this file and processes immediately
on the next tool call (no 30s throttle needed).

ADAPTIVE POLLING: Reads /tmp/claude-activity-$PPID mtime to determine
session activity level and adjusts polling interval accordingly:
  active (≤30s):    3s  — user is interacting, low latency needed
  warm   (≤5min):  15s  — recent activity, moderate latency OK
  idle   (≤30min): 60s  — session idle, save resources
  dormant (>30m): 300s  — session likely abandoned, minimal polling

Config: ~/battlestation/drain-prevention.json overrides defaults.

Usage: python3 wire-daemon.py --session cc-XXXX --ppid XXXX
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

# Isolated stack: read from env (exported by lib/config.sh). No baked-in creds.
_base = os.environ.get("SUPA_URL", "").rstrip("/")
SUPABASE_URL = f"{_base}/rest/v1" if _base else ""
SUPABASE_KEY = os.environ.get("SUPA_KEY", "")
if not SUPABASE_URL or not SUPABASE_KEY:
    sys.stderr.write("wire-daemon: SUPA_URL / SUPA_KEY not set — source lib/config.sh first.\n")
    sys.exit(1)

# Default intervals per activity level (overridden by drain-prevention.json)
DEFAULT_INTERVALS = {
    "active": 3,
    "warm": 15,
    "idle": 60,
    "dormant": 300,
}

CONFIG_PATH = Path.home() / "battlestation" / "drain-prevention.json"


def _fetch(session_id):
    url = (
        f"{SUPABASE_URL}/session_messages"
        f"?to_session=eq.{session_id}"
        f"&read=eq.false"
        f"&order=created_at.asc"
        f"&limit=20"
    )
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None  # None = network error, don't update state


def _atomic_write(path, data):
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, str(path))


def _load_intervals():
    """Load polling intervals from drain-prevention.json, fall back to defaults."""
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            intervals = cfg.get("wire_daemon_intervals", {})
            return {
                "active": intervals.get("active", DEFAULT_INTERVALS["active"]),
                "warm": intervals.get("warm", DEFAULT_INTERVALS["warm"]),
                "idle": intervals.get("idle", DEFAULT_INTERVALS["idle"]),
                "dormant": intervals.get("dormant", DEFAULT_INTERVALS["dormant"]),
            }
    except Exception:
        pass
    return dict(DEFAULT_INTERVALS)


def _activity_level(activity_file: Path) -> str:
    """Determine session activity level from activity file mtime."""
    if not activity_file.exists():
        return "active"  # No file yet = new session, treat as active
    try:
        age = time.time() - activity_file.stat().st_mtime
    except Exception:
        return "warm"  # Can't stat = assume warm
    if age <= 30:
        return "active"
    elif age <= 300:
        return "warm"
    elif age <= 1800:
        return "idle"
    else:
        return "dormant"


def _poll_interval(activity_file: Path, intervals: dict) -> float:
    """Return the polling interval based on current activity level."""
    level = _activity_level(activity_file)
    return intervals.get(level, DEFAULT_INTERVALS["warm"])


def main():
    parser = argparse.ArgumentParser(description="Wire real-time inbox daemon")
    parser.add_argument("--session", required=True, help="Session ID, e.g. cc-12345")
    parser.add_argument("--ppid", required=True, help="Parent PID of the Claude session")
    args = parser.parse_args()

    session_id = args.session
    ppid = args.ppid

    session_dir = Path(f"/tmp/battlestation/{ppid}")
    session_dir.mkdir(parents=True, exist_ok=True)

    live_file = session_dir / "wire-live.json"
    trigger_file = Path(f"/tmp/wire-trigger-{ppid}")
    heartbeat_file = session_dir / "wire-daemon-heartbeat"
    pid_file = session_dir / "wire-daemon.pid"
    activity_file = Path(f"/tmp/claude-activity-{ppid}")
    stats_file = session_dir / "wire-daemon-stats.json"

    pid_file.write_text(str(os.getpid()))

    known_ids: set = set()
    intervals = _load_intervals()
    config_reload_at = time.time() + 300  # Reload config every 5 min
    polls_total = 0
    polls_saved = 0

    while True:
        try:
            now = time.time()
            heartbeat_file.write_text(str(int(now)))

            # Reload config periodically
            if now >= config_reload_at:
                intervals = _load_intervals()
                config_reload_at = now + 300

            # Adaptive interval based on activity
            interval = _poll_interval(activity_file, intervals)
            level = _activity_level(activity_file)

            # Write stats for observability
            try:
                _atomic_write(stats_file, {
                    "pid": os.getpid(),
                    "level": level,
                    "interval": interval,
                    "polls_total": polls_total,
                    "polls_saved": polls_saved,
                    "ts": int(now),
                })
            except Exception:
                pass

            messages = _fetch(session_id)
            polls_total += 1

            if messages is None:
                # Network error — keep existing state
                time.sleep(interval)
                continue

            current_ids = {m.get("id") for m in messages if m.get("id")}
            new_ids = current_ids - known_ids

            if new_ids:
                known_ids = current_ids
                _atomic_write(live_file, messages)
                # Trigger file wakes up the hook on the very next tool call
                trigger_file.write_text(str(len(new_ids)))
            elif not live_file.exists():
                _atomic_write(live_file, messages)
                known_ids = current_ids
            elif not messages and known_ids:
                # All messages cleared
                _atomic_write(live_file, [])
                known_ids = set()

        except Exception:
            pass

        time.sleep(interval)


if __name__ == "__main__":
    main()
