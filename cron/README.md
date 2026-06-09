# Cron — Pillar 1 capacity write-path

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

## Install (every 5 minutes)

```bash
chmod +x cron/capacity-snapshot.sh cron/install-cron.sh
./cron/install-cron.sh            # idempotent — re-running replaces, never duplicates
./cron/install-cron.sh --uninstall
```

Or add by hand with `crontab -e`:

```crontab
*/5 * * * * /ABSOLUTE/PATH/sarah-build-pack/cron/capacity-snapshot.sh >> /tmp/battlestation/cron.log 2>&1
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
