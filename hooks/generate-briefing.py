#!/usr/bin/env python3
"""
Generate Briefing — writes a shared situational-awareness file for sessions.

Invoked backgrounded + output-discarded by hooks/auto-register.sh:
    python3 "$HOOK_DIR/generate-briefing.py" 2>/dev/null &

Design constraints (mirror lib/wire-daemon.py):
  - Stdlib only (urllib/json/os/pathlib). No third-party deps.
  - Credentials from env (SUPA_URL / SUPA_KEY, exported by lib/config.sh).
    Exit 0 silently if unset — this is a best-effort enhancement, never a
    blocker, and it runs on every session registration.
  - Self-throttled: skip if the briefing was refreshed < THROTTLE_SECONDS ago.
  - Atomic write (temp + os.replace) so a reader never sees a partial file.
  - Never raise: any failure exits 0. The hook discards our output anyway.

Output: /tmp/battlestation/briefing.md — a human-readable snapshot of active
sessions, recent shipped items, and account capacity (when populated).
lib/directives.sh appends operator directives to the same file.
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

BRIEFING = Path("/tmp/battlestation/briefing.md")
THROTTLE_SECONDS = 60

_base = os.environ.get("SUPA_URL", "").rstrip("/")
SUPABASE_URL = f"{_base}/rest/v1" if _base else ""
SUPABASE_KEY = os.environ.get("SUPA_KEY", "")


def _fetch(path):
    """GET <rest>/<path>; return parsed JSON list, or [] on any failure."""
    url = f"{SUPABASE_URL}/{path}"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read())
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _atomic_write(path, text):
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, str(path))


def _throttled():
    """True if the briefing was written recently — skip this run."""
    try:
        age = time.time() - BRIEFING.stat().st_mtime
        return age < THROTTLE_SECONDS
    except Exception:
        return False


def _render(sessions, ledger, capacity):
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    lines = [f"# Battle Station Briefing", f"_generated {now}_", ""]

    # ── Active sessions ─────────────────────────────────────────
    lines.append(f"## Active sessions ({len(sessions)})")
    if sessions:
        for s in sessions:
            sid = str(s.get("session_id", "?"))
            repo = s.get("repo", "?")
            task = (s.get("task_name") or "").replace("\n", " ").strip()[:60]
            role = s.get("role") or "worker"
            files = s.get("files_touched") or []
            nfiles = len(files) if isinstance(files, list) else 0
            lines.append(f"- `{sid}` [{role}] {repo} — {task} ({nfiles} file(s))")
    else:
        lines.append("- none")
    lines.append("")

    # ── Recently shipped ────────────────────────────────────────
    lines.append("## Recently shipped")
    if ledger:
        for item in ledger:
            title = (item.get("title") or "").replace("\n", " ").strip()[:70]
            proj = item.get("project", "?")
            itype = item.get("item_type", "?")
            lines.append(f"- [{proj}] {itype}: {title}")
    else:
        lines.append("- (no recent build_ledger items)")
    lines.append("")

    # ── Account capacity (empty until a snapshot writer populates it) ──
    if capacity:
        lines.append("## Account capacity")
        for c in capacity:
            label = c.get("account", c.get("label", "?"))
            five = c.get("five_pct", "?")
            seven = c.get("seven_pct", "?")
            active = " (active)" if c.get("is_active") else ""
            lines.append(f"- {label}{active}: 5h {five}% / 7d {seven}%")
        lines.append("")

    return "\n".join(lines) + "\n"


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        return  # no creds — best-effort, do nothing
    if _throttled():
        return

    BRIEFING.parent.mkdir(parents=True, exist_ok=True)

    sessions = _fetch(
        "session_locks?status=eq.active"
        "&select=session_id,repo,task_name,role,files_touched,heartbeat_at"
        "&order=claimed_at.desc"
    )
    ledger = _fetch(
        "build_ledger?select=title,project,item_type,created_at"
        "&order=created_at.desc&limit=8"
    )
    capacity = _fetch("account_capacity?select=*")

    try:
        _atomic_write(BRIEFING, _render(sessions, ledger, capacity))
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
