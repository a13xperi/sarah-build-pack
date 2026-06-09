# Cron — battlestation maintenance jobs

| Job | Schedule | What it does |
|---|---|---|
| `capacity-snapshot.sh` | every 5 min | snapshot account capacity to Supabase (Pillar 1 write-path) |
| `expire-sessions.sh` | every 5 min | expire stale `session_locks` rows; promote parked prompts |
| `cleanup-tmp.sh` | every 15 min | remove `/tmp` state for dead session PIDs |

All read credentials from `.env` via `lib/config.sh` — no baked-in secrets.

## capacity-snapshot.sh

`token-watch` and `lib/session-guard.sh` **read** the `account_capacity` table,
but nothing populated it. `capacity-snapshot.sh` is that missing writer.

## capacity-snapshot.sh

Every run:

1. Reads the active account from `~/.claude/accounts.json` (`{"active":"A",...}`).
2. Reads live rate-limit usage from `/tmp/statusline-debug.json` (written by the
   statusline). Tolerant of both payload shapes — nested
   (`rate_limits.five_hour.used_percentage`) and flat
   (`rate_limits.five_hour_used_pct`).
3. **Upserts** the active account's row in `account_capacity`
   (`five_hour_used_pct`, `five_hour_resets_at`, `seven_day_used_pct`,
   `seven_day_resets_at`, `is_active=true`, `snapshot_at`).
4. **Decay-corrects** the other accounts: marks them `is_active=false` and, if an
   account has been idle long enough for a rolling window to fully elapse (or its
   captured reset timestamp has passed), zeroes that window's usage so dormant
   accounts don't read as permanently drained.
5. Appends an audit row to `account_capacity_history` (`source='cron'`).

Safety: if the statusline payload has no live rate-limit data, the active row is
**not** overwritten with zeros — the last good snapshot is preserved.
Credentials come from `.env` via `lib/config.sh`; no secrets are baked in.

## expire-sessions.sh

Garbage-collects the `session_locks` table:

1. At >5 min stale heartbeat → clears `files_touched` so a dead session's file
   locks don't block live peers.
2. At >30 min stale → marks `status='done'` + `released_at`.

Before full expiry, any session carrying a `next_session_prompt` has it promoted
into `session_tasks` (`status='blocked'`) so a parked "resume" prompt isn't lost.

## cleanup-tmp.sh

Removes `/tmp/claude-*-$PID` files and `/tmp/battlestation/$PID/` directories for
PIDs whose process is gone (`kill -0` check). Pure local; skips live sessions.

## Install

```bash
./cron/install-cron.sh             # installs all three jobs, idempotent
./cron/install-cron.sh --uninstall # removes them
```

The installer is idempotent — re-running replaces the battlestation entries
(matched by a `# battlestation: <name>` marker) and never duplicates them or
touches unrelated crontab lines.

Or add by hand with `crontab -e`:

```crontab
*/5  * * * * /ABS/PATH/sarah-build-pack/cron/capacity-snapshot.sh >> /tmp/battlestation/cron.log 2>&1
*/5  * * * * /ABS/PATH/sarah-build-pack/cron/expire-sessions.sh   >> /tmp/battlestation/cron.log 2>&1
*/15 * * * * /ABS/PATH/sarah-build-pack/cron/cleanup-tmp.sh       >> /tmp/battlestation/cron.log 2>&1
```

## Test

```bash
# Needs .env (SUPA_URL/SUPA_KEY), ~/.claude/accounts.json, and a statusline
# debug file. Run manually and check the log:
./cron/capacity-snapshot.sh
tail -20 /tmp/battlestation/battlestation.log

# Then confirm the row landed:
#   select * from account_capacity;
```

Prereqs: `schema/provision.sql` applied (creates `account_capacity` +
`account_capacity_history`).
