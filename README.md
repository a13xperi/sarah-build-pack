# Sarah's Build Pack — Multi-Session Build System

An isolated clone of the build system: **token window** (capacity), **battle station** (multi-session coordination), **token matrix** (model routing), and the **SAGE protocols** (operating discipline). Everything points at *your own* Supabase + *your own* accounts — nothing here talks to anyone else's fleet.

> Audience: engineer/operator. Terse on purpose. Read `docs/` for the "why" of each pillar.

---

## The four pillars

| Pillar | What it is | Where |
|---|---|---|
| **Token window** | Capacity tracking across rolling 5h + 7-day windows; A/B/C account rotation; never drain to zero | `docs/01-token-window.md` |
| **Battle station** | Multiple parallel Claude Code sessions coordinating via Supabase (session_locks + Wire) without clobbering each other | `docs/02-battlestation.md` |
| **Token matrix** | The Opus Sandwich — route each task to the cheapest capable engine; Opus only as bread (design + validate) | `docs/03-token-matrix.md` |
| **SAGE protocols** | Operating discipline — source-of-truth brain, decision logging, ask-vs-execute, spending guard | `docs/04-sage-protocols.md` |

---

## Setup (≈20 min)

```bash
cd ~/sarah-build-pack
bash bootstrap.sh          # deps check, .env, accounts.json, hook symlinks
```

Then four manual steps the bootstrap can't do for you:

### 1. Your Supabase
Create a project at supabase.com. Open SQL Editor → paste `schema/provision.sql` → Run. That creates all 8 tables.

### 2. `.env`
```bash
cp .env.example .env   # bootstrap already did this
# edit .env:
#   SUPA_URL=https://<your-ref>.supabase.co
#   SUPA_KEY=<your anon key>
```

### 3. Source the libs
Add to your `~/.zshrc` (or `~/.bashrc`):
```bash
source ~/sarah-build-pack/lib/config.sh
source ~/sarah-build-pack/lib/wire.sh
source ~/sarah-build-pack/lib/session.sh
```

### 4. Wire the hooks into `~/.claude/settings.json`
`bash hooks/install.sh` symlinks the hooks; you still register them with Claude Code. Add to the `hooks` block:

```jsonc
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "*",    "hooks": [{ "type": "command", "command": "~/.claude/hooks/auto-register.sh" }] },
      { "matcher": "*",    "hooks": [{ "type": "command", "command": "~/.claude/hooks/wire-inbox.sh" }] },
      { "matcher": "Edit|Write|NotebookEdit", "hooks": [{ "type": "command", "command": "~/.claude/hooks/file-lock-check.sh" }] },
      { "matcher": "*",    "hooks": [{ "type": "command", "command": "~/.claude/hooks/loop-guard.sh" }] },
      { "matcher": "*",    "hooks": [{ "type": "command", "command": "~/.claude/hooks/spend-guard.sh" }] }
    ],
    "PostToolUse": [
      { "matcher": "Bash", "hooks": [{ "type": "command", "command": "~/.claude/hooks/build-ledger.sh" }] },
      { "matcher": "Bash", "hooks": [{ "type": "command", "command": "~/.claude/hooks/engine-tracker.sh" }] },
      { "matcher": "*",    "hooks": [{ "type": "command", "command": "~/.claude/hooks/loop-guard.sh" }] }
    ]
  }
}
```

### Smoke test
```bash
source lib/config.sh && source lib/wire.sh
wire_broadcast status '{"message":"hello from my stack"}'
# then check your Supabase session_messages table for the row
```

---

## What's included

```
sarah-build-pack/
├── README.md              ← you are here
├── bootstrap.sh           ← one-shot setup
├── .env.example           ← copy to .env (YOUR Supabase)
├── schema/provision.sql   ← 8 tables, run in your Supabase
├── lib/                   ← config, supabase wrappers, wire, session, log, route (sourced + CLI)
├── lanes/                 ← accounts.sh (A/B/C resolve + capacity-gated switch)
├── hooks/                 ← auto-register, wire-inbox, file-lock-check, build-ledger,
│                            token-tracker, engine-tracker, loop-guard, spend-guard + install.sh
├── cron/                  ← capacity-snapshot, expire-sessions, cleanup-tmp + install-cron.sh
├── bin/                   ← bs-route (router), claude-switch (account switch)
├── templates/             ← accounts.json, token-budget.json
├── token-watch/           ← the TUI dashboard (reads your Supabase tables) + launcher
└── docs/                  ← the four pillars explained
```

## token-watch (the dashboard)

The Textual TUI that reads the tables the hooks populate — sessions, capacity, build ledger, Wire inbox, advisor. `bootstrap.sh` installs its deps (`rich`, `textual`) and symlinks the launcher to `~/.local/bin/token-watch`.

```bash
token-watch                 # launch (sources lib/config.sh for SUPA_URL/SUPA_KEY)
# or directly:
python3 token-watch/token_watch_tui.py
```

It needs `SUPA_URL` / `SUPA_KEY` in your `.env` (the launcher sources `lib/config.sh` for you). With no creds it warns and shows empty panels rather than crashing. Set `BS_GITHUB_USER` in `.env` to make the "open commit" action point at *your* GitHub. Like everything else here, it has **no baked-in creds** — it reads your stack only.

## De-personalization notes (already done)

This pack was forked from a live fleet and scrubbed:
- No baked-in Supabase URLs or keys — everything reads `SUPA_URL` / `SUPA_KEY` from your `.env`.
- Repo→project labels default to your git repo basename. To get richer `company`/`project` labels in `build_ledger`, drop a `~/.battlestation-repos` file with `register_repo "<match>" "<Company>" "<project>"` lines.
- `accounts.json` uses generic A/B/C lane names — fill in your own account emails.

## Included now
- **Crons** (`cron/`): `capacity-snapshot` (the `account_capacity` write-path), `expire-sessions` (stale-session GC), `cleanup-tmp` (dead-PID `/tmp` GC). Install all: `./cron/install-cron.sh`.
- **Account switch** (`lanes/accounts.sh` + `bin/claude-switch`): capacity-gated A/B/C rotation via the macOS Keychain.
- **Routing** (`lib/route.py` + `bin/bs-route`): the token-matrix task→engine classifier.
- **SAGE enforcers** (`hooks/loop-guard.sh`, `hooks/spend-guard.sh`): loop-prevention (3× fail → stop) + spending guard on fan-out.
- **Engine tracking** (`hooks/engine-tracker.sh`): logs external-engine usage to `ai_capacity_ledger`.

## Not included (bring your own)
- Cloud coordination dashboard. Add as you scale past one machine.
- Account credential **vaults** (`~/.claude/vaults/{A,B,C}/claudeAiOauth.json`): `claude-switch` reads them but you provision them yourself (`claude auth login` per account). macOS Keychain only.
