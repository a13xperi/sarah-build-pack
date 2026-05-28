#!/usr/bin/env python3
"""
Token Window TUI — Textual-based interactive dashboard for Claude Code token monitoring.
Scrollable panels, keyboard navigation, no dead space.
"""

import json
import math
import os
import sys
import time
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.text import Text
from rich.markup import escape as _rich_escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import Screen
from textual.widgets import Button, ContentSwitcher, DataTable, RichLog, Static

from rich.panel import Panel
from rich.table import Table as RichTable

from token_watch_data import (
    make_urgent_panel,
    _abbrev_model,
    _safe_float,
    _active_pids,
    _active_sessions,
    _build_or_update_index,
    _build_pid_map,
    _countdown,
    _current_pct,
    _etime_to_secs,
    _estimate_cost,
    _extract_accomplishments,
    _format_cost,
    _get_active_account,
    _get_agent_stats,
    _get_burndown_data,
    _get_token_attribution,
    _get_call_data_map,
    _get_call_history,
    _get_daily_usage,
    _get_mcp_stats,
    _get_peer_sessions,
    _get_wire_reliability,
    _get_pid_cpu,
    _get_session_history,
    _estimate_turn_cost,
    _get_session_turns,
    _get_system_health,
    _get_engine_status,
    _get_external_engine_usage,
    _get_usage_metrics,
    _gravity_center,
    _index_building,
    _index_cache,
    _index_lock,
    _load_index,
    _load_ledger,
    _reset_day,
    _shorten_tool,
    _token_pacing,
    check_and_notify,
    _check_auto_gate,
    _get_test_queue,
    _add_test_item,
    _update_test_item,
    _delete_test_item,
    _import_atlas_qa_tests,
    _scrape_cycle_sessions,
    _populate_cycle_from_sessions,
    export_session_history_csv,
    focus_session_terminal,
    get_account_capacity_display,
    lookup_by_ccid,
    make_drain_panel,
    make_header,
    make_skills_panel,
    make_tool_stats,
    _build_full_audit,
    export_audit_markdown,
    _get_utilization_analytics,
    _stars_display,
    _score_dimension,
)

try:
    from token_watch_data import _get_claude_plans
except ImportError:
    def _get_claude_plans(limit=100, status=None, session_id=None, force=False):
        return []

class LazyView(ScrollableContainer):
    """Content view that lazy-loads data on first display."""
    _loaded: bool = False

    def load_content(self) -> None:
        """Override to populate widgets. Called once on first show."""
        pass

    def refresh_content(self) -> None:
        """Override for timer-driven refresh when visible."""
        pass


class BaseMetricsView(LazyView):
    """LazyView with a standard 30s refresh throttle and table-clear logic.

    Subclasses set _table_ids to the list of DataTable widget IDs that should
    be cleared before each reload. refresh_content is fully handled here;
    subclasses only need to implement load_content().
    """
    _refresh_interval: float = 30.0
    _table_ids: list = []

    def refresh_content(self) -> None:
        now = time.time()
        last = getattr(self, "_last_refresh", 0.0)
        if (now - last) >= self._refresh_interval:
            self._last_refresh = now
            try:
                for tid in self._table_ids:
                    self.query_one(tid, DataTable).clear(columns=True)
                self.load_content()
            except Exception:
                pass


def _start_hot_reload_watcher(app):
    # type: (Any) -> None
    """Watch source files for changes. Signal the app instead of auto-restarting."""
    watch_dir = Path(__file__).resolve().parent

    def _snapshot():
        # type: () -> Dict[Path, float]
        result = {}
        for p in watch_dir.glob("*.py"):
            try:
                result[p] = p.stat().st_mtime
            except Exception:
                pass
        tcss = watch_dir / "token_watch_tui.tcss"
        try:
            result[tcss] = tcss.stat().st_mtime
        except Exception:
            pass
        return result

    mtimes = _snapshot()
    while True:
        time.sleep(2)
        current = _snapshot()
        if current != mtimes:
            mtimes = current
            app.call_from_thread(app._signal_files_changed)


_BACKUP_DIR = Path(f"/tmp/Token Window-backup-{os.getpid()}")
_SOURCE_DIR = Path(__file__).resolve().parent
_BACKUP_FILES = ["token_watch_tui.py", "token_watch_data.py", "token_watch.py", "token_watch_tui.tcss", "token_watch_advisor.py"]


def _backup_working_files():
    """Snapshot current source files as last-known-good backup."""
    try:
        _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        import shutil
        for fname in _BACKUP_FILES:
            src = _SOURCE_DIR / fname
            if src.exists():
                shutil.copy2(str(src), str(_BACKUP_DIR / fname))
    except Exception:
        pass


def _restore_backup_files():
    """Restore backed-up files over current files. Returns True if restored."""
    import shutil
    if not _BACKUP_DIR.exists():
        return False
    restored = False
    for fname in _BACKUP_FILES:
        bak = _BACKUP_DIR / fname
        dst = _SOURCE_DIR / fname
        if bak.exists():
            shutil.copy2(str(bak), str(dst))
            restored = True
    return restored


def _get_backup_age():
    """Return seconds since backup was written, or float('inf') if no backup."""
    try:
        for fname in _BACKUP_FILES:
            bak = _BACKUP_DIR / fname
            if bak.exists():
                return time.time() - bak.stat().st_mtime
    except Exception:
        pass
    return float('inf')


def _project_to_company(project: str, company: str = "") -> tuple[str, str]:
    """Return (company_name, style) from company field or project string."""
    if company:
        c = company.lower().strip()
        if "delphi" in c: return "Delphi", "blue"
        if "kaa" in c: return "KAA", "green"
        if "frank" in c: return "Frank", "magenta"
        if "sage" in c: return "SAGE", "yellow"
        if "adinkra" in c: return "Adinkra", "purple"
        if "personal" in c: return "Personal", "bright_cyan"
        return company[:12], "dim"
    p = (project or "").lower().strip()
    if p in ("atlas", "atlas-be", "atlas-fe", "atlas-portal", "atlas-backend"):
        return "Delphi", "blue"
    if p == "kaa" or p.startswith("kaa-"):
        return "KAA", "green"
    if p in ("frank", "frank-pilot"):
        return "Frank", "magenta"
    if p == "sage" or p.startswith("sage-"):
        return "SAGE", "yellow"
    if p in ("life", "health", "finance", "home", "growth", "social", "travel"):
        return "Personal", "bright_cyan"
    if p in ("openclaw", "paperclip", "token watch", "token-watch", "battlestation"):
        return "Personal", "bright_cyan"
    return "—", "dim"


# ── Static widgets (wrap existing Rich renderables) ──────────────────────────


class UrgentAlerts(Static):
    def update_content(self):
        panel = make_urgent_panel()
        if panel:
            self.update(panel)
            self.display = True
        else:
            self.update("")
            self.display = False


class TokenHeader(Static):
    def update_content(self, five, seven, fr, sr):
        self.update(make_header(five, seven, fr, sr))


class CompactStatusBar(Static):
    """Single-line compact status bar — docked top above the NavBar."""

    def update_content(self):
        try:
            from token_watch_data import (
                _current_pct, _countdown, _get_active_account,
                _active_pids, _get_peer_sessions, _get_dispatch_queue,
                _burn_rate_sparkline,
            )
        except ImportError:
            from token_watch_data import (
                _current_pct, _countdown, _get_active_account,
                _active_pids, _get_peer_sessions, _burn_rate_sparkline,
            )
            _get_dispatch_queue = None

        def mini_bar(pct, width=6):
            try:
                f = float(pct)
                filled = int(f * width / 100)
                color = "green" if f < 50 else ("yellow" if f < 75 else "red")
                return f"[{color}]{'█' * filled}{'░' * (width - filled)}[/{color}]"
            except Exception:
                return "[dim]" + "░" * width + "[/dim]"

        try:
            five, seven, five_reset, _ = _current_pct()
            countdown = _countdown(five_reset)
            # Strip "(at HH:MM AM)" for compactness — keep just "4h36m"
            countdown_short = countdown.split(" (")[0] if countdown else "?"
        except Exception:
            five, seven, countdown_short = "?", "?", "?"

        # Burn-rate sparkline: 10 unicode blocks, one per minute of the last
        # 10 minutes. Shows TREND instead of just totals. Non-blocking: the
        # helper reuses the ledger cache and caps work at O(entries_in_10min).
        try:
            spark = _burn_rate_sparkline(window_mins=10, slots=10)
        except Exception:
            spark = ""
        if not spark:
            spark_cell = "[dim]" + "·" * 10 + "[/dim]"
        else:
            # Colour the whole strip by the peak intensity class so the
            # header glances red when the session is accelerating. We have
            # no per-char colour here (the spark is a single string) —
            # colour selection mirrors the 5h bar palette.
            try:
                _f = float(five) if five != "?" else 0.0
            except (TypeError, ValueError):
                _f = 0.0
            spark_color = "green" if _f < 50 else ("yellow" if _f < 75 else "red")
            spark_cell = f"[{spark_color}]{spark}[/{spark_color}]"

        # Task counts from dispatch queue
        p1_str = "—"
        try:
            if _get_dispatch_queue is not None:
                q = _get_dispatch_queue()
                total = q.get("stats", {}).get("total_ready", 0)
                p1 = sum(1 for t in q.get("queue", []) if t.get("priority") == 1)
                p1_str = f"P{p1}/{total}"
        except Exception:
            pass

        # Active sessions
        try:
            local_pids = _active_pids()
            peers = _get_peer_sessions()
            n_sessions = len(local_pids) + len(peers)
            peer_ids = [p.get("session_id", "?")[:12] for p in peers[:3]]
            local_ids = [f"cc-{pid}"[:12] for pid in local_pids[:2]]
            all_ids = local_ids + peer_ids
            session_str = f"{n_sessions} " + ", ".join(all_ids[:3])
            if n_sessions > 3:
                session_str += f" +{n_sessions - 3}"
        except Exception:
            session_str = "?"

        five_str = str(int(float(five))) + "%" if five != "?" else "?"
        seven_str = str(int(float(seven))) + "%" if seven != "?" else "?"

        bar5 = mini_bar(five if five != "?" else 0)
        bar7 = mini_bar(seven if seven != "?" else 0)

        label, _, _ = _get_active_account()
        acct_color = "cyan" if label == "A" else ("magenta" if label == "B" else "yellow")

        line = (
            f"[dim]T[/dim]  "
            f"[cyan]{countdown_short}[/cyan]  "
            f"[dim]{p1_str}[/dim]  "
            f"5h {bar5} {five_str}  "
            f"[dim]burn[/dim] {spark_cell}  "
            f"7d {bar7} {seven_str}  "
            f"[{acct_color}]Acct {label}[/{acct_color}]  "
            f"[dim]{session_str}[/dim]"
        )
        self.update(line)


class AccountCapacityPanel(Static):
    """Compact side-by-side view of all Claude accounts."""

    def update_content(self):
        from token_watch_data import _get_all_account_capacities
        accounts = _get_all_account_capacities()
        if not accounts:
            self.update("")
            self.display = False
            return

        def mini_bar(pct_str, width=6):
            try:
                pct = float(pct_str)
                filled = int(pct * width / 100)
                color = "green" if pct < 50 else ("yellow" if pct < 75 else "red")
                return f"[{color}]{'█' * filled}{'░' * (width - filled)}[/{color}] {pct:.0f}%"
            except Exception:
                return f"[dim]{'░' * width}[/dim] —"

        t = RichTable(show_header=False, box=None, padding=(0, 2), expand=True)
        for _ in accounts:
            t.add_column(justify="left")

        # Row 1: Account labels
        labels = []
        for a in accounts:
            color = "cyan" if a["label"] == "A" else ("magenta" if a["label"] == "B" else "yellow")
            active = " ← ACTIVE" if a["active"] else ""
            lock = " [bold red]LOCKED[/bold red]" if a.get("locked") else ""
            labels.append(f"[{color} bold]Account {a['label']}[/{color} bold] [dim]({a['name']})[/dim]{active}{lock}")
        t.add_row(*labels)

        # Row 2: 5h bars
        t.add_row(*[f"5h: {mini_bar(a['five_pct'])}" for a in accounts])

        # Row 3: 7d bars
        t.add_row(*[f"7d: {mini_bar(a['seven_pct'])}" for a in accounts])

        self.update(Panel(t, title="[bold]Account Capacity[/bold]", border_style="dim"))



# ── Shared helper functions (extracted from SystemHealthPanel for reuse) ──────

def _mem_mini_gauge(mb):
    """Mini memory gauge bar with color coding."""
    pct = min(mb / 10, 100)  # scale: 1000MB = 100%
    filled = min(int(pct * 3 / 100), 3)
    color = "green" if mb < 300 else ("yellow" if mb < 500 else "red")
    _F, _E = "\u2588", "\u2591"
    bar = f"[{color}]{_F * filled}{_E * (3 - filled)}[/{color}]"
    if mb >= 1024:
        return f"{bar} {mb / 1024:.1f}GB"
    return f"{bar} {mb}MB"


def _gauge_bar(pct, width=10):
    """Percentage gauge bar with color zones."""
    filled = int(pct * width / 100)
    color = "green" if pct < 40 else ("yellow" if pct < 70 else "red")
    fill_chars = "█" * filled + "░" * (width - filled)
    return f"[{color}]{fill_chars}[/{color}]"


def _zone_label(pct):
    """Return (label, color) for a percentage zone."""
    if pct < 40:
        return ("COOL", "green")
    if pct < 70:
        return ("WARM", "yellow")
    if pct < 85:
        return ("HOT", "red")
    return ("REDLINE", "bold red")



class SystemStatusPanel(Static):
    """System pressure, memory, and CPU status — sits above Engine Management."""

    def update_content(self):
        from token_watch_data import _get_engine_status
        engine = _get_engine_status()
        pressure = engine["pressure"]
        totals = engine["totals"]

        mem_pct = totals.get("mem_pct", 0)
        total_cpu = totals.get("cpu", 0)
        total_mem = totals.get("mem_mb", 0)
        sys_mem = totals.get("system_mem_mb", 16384)
        n_sessions = len(engine["sessions"]) + len(engine["peers"])

        mem_zone, mem_zc = _zone_label(mem_pct)
        cpu_capped = min(total_cpu, 100)
        cpu_zone, cpu_zc = _zone_label(cpu_capped)
        mem_gb = total_mem / 1024
        sys_gb = sys_mem / 1024

        parts = []
        # Pressure alert with trim recommendations
        if pressure["active"]:
            parts.append(f"[bold red]\u26a0 {pressure['reason']}[/bold red]")
            for tr in pressure["trim_order"][:3]:
                directive = tr.get("directive", "\u2014")[:35] or "\u2014"
                age = tr.get("age", "?")
                delta = tr.get("delta", "?")
                mem = tr.get("mem_freed_mb", 0)
                parts.append(
                    f"  [red]\u2192[/red] [bold]{tr['sid']}[/bold]  "
                    f"[dim]{directive}[/dim]  "
                    f"age: {age}  used: {delta}  mem: {mem}MB  "
                    f"[dim italic]({tr.get('reason', '')})[/dim italic]"
                )

        # Gauges
        parts.append(
            f"MEM {_gauge_bar(mem_pct)} {mem_gb:.1f}/{sys_gb:.0f}GB [{mem_zc}]{mem_zone}[/{mem_zc}]"
            f"    CPU {_gauge_bar(cpu_capped)} {total_cpu:.0f}% [{cpu_zc}]{cpu_zone}[/{cpu_zc}]"
            f"    Sessions: {n_sessions}"
        )

        self.update(Panel(
            "\n".join(parts),
            title="[bold]System Status[/bold]",
            border_style="cyan" if not pressure["active"] else "red",
        ))


class EngineTable(DataTable):
    """Engine management — unified session health + system pressure."""

    BORDER_TITLE = "Engine Management (live)"
    BORDER_SUBTITLE = "f=focus  k=force quit  K=release files"

    BINDINGS = [
        Binding("f", "focus_selected", "Focus terminal", show=True),
        Binding("k", "kill_selected", "Force quit", show=True),
        Binding("K", "release_files", "Release files", show=True),
    ]

    def on_mount(self):
        self.cursor_type = "row"
        self.zebra_stripes = False
        self.add_column("When", width=11, key="when")
        self.add_column("Session", width=10, key="session")
        self.add_column("Acct", width=4, key="acct")
        self.add_column("Src", width=10, key="src")
        self.add_column("Co", width=8, key="co")
        self.add_column("Project", width=12, key="project")
        self.add_column("Mdl", width=10, key="mdl")
        self.add_column("Mem", width=10, key="mem")
        self.add_column("Dur", width=12, key="dur")
        self.add_column("Used", width=11, key="used")
        self.add_column("Directive", key="directive")


    def on_mouse_scroll_down(self, event) -> None:
        if self.scroll_y >= self.max_scroll_y:
            return  # Let parent ScrollableContainer handle it
        self.scroll_down()
        event.stop()

    def on_mouse_scroll_up(self, event) -> None:
        if self.scroll_y <= 0:
            return  # Let parent ScrollableContainer handle it
        self.scroll_up()
        event.stop()

    def refresh_rows(self):
        """Rebuild the table from unified engine status with health scoring."""
        engine = _get_engine_status()
        engine_sessions = engine["sessions"]
        remote_peers = engine["peers"]
        pressure = engine["pressure"]
        totals = engine["totals"]

        entries = _load_ledger(last_n=500)
        now_utc = datetime.now(timezone.utc)
        now_local = datetime.now()

        # Per-session attribution (scaled to current window)
        attr_data = _get_token_attribution()
        attr_map = {}  # sid -> attributed pct
        if attr_data:
            for _as in attr_data.get("sessions", []):
                attr_map[_as["session_id"]] = _as["pct_used"]

        n_local = len(engine_sessions)
        n_peers = len(remote_peers)
        n_total = n_local + n_peers
        if n_total:
            if n_peers:
                self.border_title = "Engine Management (live) — {} ({} local, {} peers)".format(
                    n_total, n_local, n_peers
                )
            else:
                self.border_title = "Engine Management (live) — {}".format(n_total)
        else:
            self.border_title = "Engine Management (live)"

        try:
            cur_row = self.cursor_row
            saved_y = self.scroll_y
        except Exception:
            cur_row = 0
            saved_y = 0

        self.clear()

        if not engine_sessions and not remote_peers:
            self.add_row(
                "", Text("--", style="dim"), "", "", "", "", "", "", "", "",
                Text("no active sessions", style="dim"),
                key="empty",
            )
            return

        # Single-pass ledger scan: build model, last call, first output per session
        model_map = {}    # type: dict
        last_call = {}    # type: dict
        first_out = {}    # type: dict
        for e in entries:
            sid = e.get("session", "")
            if not sid:
                continue
            mdl = e.get("model")
            if mdl and mdl != "?":
                model_map[sid] = mdl
            if e.get("type") == "tool_use":
                if sid not in first_out:
                    first_out[sid] = e.get("output_tokens", 0)
                try:
                    ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))
                    tool = _shorten_tool(e.get("tool", "?"))
                    out = e.get("output_tokens", 0)
                    last_call[sid] = (ts, tool, out)
                except Exception:
                    pass

        # Detect active account for local sessions
        from token_watch_data import _get_active_account
        active_label, _, _ = _get_active_account()
        acct_color_map = {"A": "cyan", "B": "magenta", "C": "yellow"}
        acct_color_local = acct_color_map.get(active_label, "dim")

        # Pressure/totals moved to SystemStatusPanel

        for session in engine_sessions:
            pid = session["pid"]
            sid = session["sid"]
            age = session["age"]
            directive = session["directive"]
            delta = session["delta"]
            source = session["source"]
            mem_mb = session["mem_mb"]
            health = session["health"]

            # Header row
            elapsed_s = _etime_to_secs(age)
            start_str = (
                (now_local - timedelta(seconds=elapsed_s)).strftime("%m/%d %H:%M")
                if elapsed_s else "?"
            )

            # Use per-session attribution instead of raw window delta
            attr_pct = attr_map.get(sid)
            if attr_pct is not None and attr_pct > 0:
                delta = f"+{attr_pct:.1f}%"
            color = "green"
            if delta == "new":
                color = "dim"
            else:
                try:
                    val = float(delta.strip("+%↻"))
                    color = "red" if val > 10 else ("yellow" if val > 5 else "green")
                except Exception:
                    pass

            mdl = _abbrev_model(model_map.get(sid, "?"))
            mdl_style = "magenta" if "opus" in mdl else ("cyan" if "sonnet" in mdl else "dim")
            src_color = (
                "yellow" if ("/" in source or source == "paperclip")
                else ("green" if source == "cli"
                       else ("cyan" if "atlas" in source else "dim"))
            )

            # Derive project — check repo from session_locks first, then source, then directive
            project = "—"
            repo = session.get("repo", "")
            if repo in ("atlas-portal", "atlas-backend"):
                project = "atlas"
            elif repo == "token-watch":
                project = "Token Window"
            elif repo in ("paperclip", "openclaw", "frank-pilot", "battlestation"):
                project = repo
            elif repo and repo not in ("a13xperi", "unknown", "—", ""):
                project = repo
            elif source in ("atlas-be", "atlas-fe"):
                project = "atlas"
            elif source == "openclaw":
                project = "openclaw"
            elif source == "frank":
                project = "frank"
            elif "/" in source:
                parts = source.split("/", 1)
                project = parts[1] if len(parts) > 1 else parts[0]

            # Fallback: check directive text
            if project == "—":
                d_lower = directive.lower() if directive else ""
                for kw, pname in [("token watch", "Token Window"), ("tw ", "Token Window"),
                                   ("atlas", "atlas"), ("paperclip", "paperclip"),
                                   ("openclaw", "openclaw"), ("frank", "frank"),
                                   ("kaa", "kaa"), ("sage", "sage")]:
                    if kw in d_lower:
                        project = pname
                        break

            if "/" in source:
                co_name = source.split("/", 1)[0]
                co_style = "yellow"
            else:
                co_name, co_style = _project_to_company(project)

            # Compute state BEFORE main row so dot color reflects live activity
            cpu = _get_pid_cpu(pid)
            lc = last_call.get(sid)
            if lc:
                secs_since = int((now_utc - lc[0]).total_seconds())
                tool_name = lc[1]
                token_delta = lc[2] - first_out.get(sid, 0)
            else:
                secs_since = None
                tool_name = "?"
                token_delta = 0

            # State detection — use health score for dot color
            dot_color = {"green": "bold green", "yellow": "bold yellow", "red": "bold red"}[health]
            if secs_since is not None and secs_since < 15:
                state_txt = Text(f">> {tool_name[:12]}", style="bold green")
            elif cpu > 20:
                state_txt = Text("thinking...", style="bold yellow")
            elif secs_since is not None and secs_since < 120:
                state_txt = Text(f"~ {tool_name[:12]}", style="dim")
            elif secs_since is not None:
                idle_m = secs_since // 60
                state_txt = Text(f"· {idle_m}m", style="dim")
            else:
                state_txt = Text("· ?", style="dim")

            # Memory gauge
            mem_text = Text.from_markup(_mem_mini_gauge(mem_mb)) if mem_mb > 0 else Text("—", style="dim")

            # Estimate cost from output tokens
            try:
                out_tok = token_delta  # already computed above
                mdl_str = model_map.get(sid, "")
                from token_watch_data import _estimate_cost, _format_cost
                session_cost = _estimate_cost(out_tok, mdl_str)
                if session_cost >= 0.01:
                    cost_style = "red" if session_cost >= 2.0 else ("yellow" if session_cost >= 0.50 else "green")
                    used_text = Text.from_markup(f"[{cost_style}]{_format_cost(session_cost)}[/{cost_style}]  [{color}]{delta}[/{color}]")
                else:
                    used_text = Text(delta, style=color)
            except Exception:
                used_text = Text(delta, style=color)

            # Per-session account from session_locks (falls back to active account)
            sess_acct = session.get("account") or active_label
            sess_acct_color = acct_color_map.get(sess_acct, "dim") if sess_acct else acct_color_local

            self.add_row(
                Text(start_str, style="dim"),
                Text.from_markup(f"[{dot_color}]● [/{dot_color}][cyan]{sid}[/cyan]"),
                Text(sess_acct, style=sess_acct_color),
                Text(source, style=src_color),
                Text(co_name, style=co_style),
                Text(project, style="dim"),
                Text(mdl, style=mdl_style),
                mem_text,
                Text(age, style="dim"),
                used_text,
                Text(directive),
                key=f"active-{pid}",
            )

            # Sub-row: tool state detail
            if secs_since is not None:
                m, s = divmod(secs_since, 60)
                elapsed_str = f"{m}m{s:02d}s" if m else f"{s}s"
            else:
                elapsed_str = "—"

            tok_str = (
                f"{token_delta / 1000:.1f}k" if token_delta >= 1000
                else str(token_delta)
            )

            cpu_str = f"{cpu:.0f}%"
            cpu_style = "bold yellow" if cpu > 50 else ("dim" if cpu < 5 else "")

            # Directive second line: show continuation if directive is long
            directive_line2 = ""
            if directive and len(directive) > 40:
                directive_line2 = directive[40:]

            self.add_row(
                Text(""),
                Text(""),
                Text(""),
                Text(""),
                Text(""),
                Text(""),
                state_txt,
                Text(""),
                Text(f"ago: {elapsed_str}  cpu: {cpu_str}", style="dim"),
                Text(f"tok: {tok_str}", style="dim"),
                Text(directive_line2, style="dim") if directive_line2 else Text(""),
                key=f"sub-{pid}",
            )

            # Blank separator between sessions
            self.add_row(
                Text(""), Text(""), Text(""), Text(""), Text(""),
                Text(""), Text(""), Text(""), Text(""), Text(""), Text(""),
                key=f"gap-{pid}",
            )

        # ── Remote peer sessions (from Supabase) ─────
        _seen_peer_ids = set()
        for peer in remote_peers:
            p_sid = peer.get("session_id", "?")
            if p_sid in _seen_peer_ids:
                continue
            _seen_peer_ids.add(p_sid)
            p_repo = peer.get("repo", "—")
            p_task = peer.get("task_name", "—") or "—"
            p_account = peer.get("account", "?")
            p_tool = peer.get("tool", "?")

            # Heartbeat staleness
            hb_str = ""
            hb_style = "dim"
            heartbeat_raw = peer.get("heartbeat_at", "")
            if heartbeat_raw:
                try:
                    hb_dt = datetime.fromisoformat(
                        heartbeat_raw.replace("Z", "+00:00")
                    )
                    hb_age_s = int((now_utc - hb_dt).total_seconds())
                    if hb_age_s < 60:
                        hb_str = "{}s ago".format(hb_age_s)
                        hb_style = "green"
                    elif hb_age_s < 600:
                        hb_str = "{}m ago".format(hb_age_s // 60)
                        hb_style = "dim"
                    else:
                        hb_str = "stale"
                        hb_style = "dim italic"
                except Exception:
                    hb_str = "?"

            # Claimed-at as start time
            claimed_str = ""
            claimed_raw = peer.get("claimed_at", "")
            if claimed_raw:
                try:
                    claimed_dt = datetime.fromisoformat(
                        claimed_raw.replace("Z", "+00:00")
                    ).astimezone()
                    claimed_str = claimed_dt.strftime("%m/%d %H:%M")
                except Exception:
                    claimed_str = "?"

            # Account color
            acct_color = {"A": "cyan", "B": "magenta", "C": "yellow"}.get(p_account, "dim")

            # Project/company from repo
            co_name, co_style = _project_to_company(p_repo)

            # Telemetry from new session_locks columns
            p_mem_mb = peer.get("mem_mb") or 0
            p_out_tok = peer.get("output_tokens") or 0
            p_model = _abbrev_model(peer.get("model") or "")
            p_five = peer.get("five_pct")

            p_mdl_style = "magenta" if "opus" in p_model else ("cyan" if "sonnet" in p_model else "dim")
            p_mem_text = Text.from_markup(_mem_mini_gauge(p_mem_mb)) if p_mem_mb > 0 else Text("—", style="dim")
            p_tok_str = (f"+{p_five:.1f}%" if p_five else (
                f"{p_out_tok / 1000:.1f}k tok" if p_out_tok >= 1000 else (
                    str(p_out_tok) + " tok" if p_out_tok else "—"
                )
            ))

            self.add_row(
                Text(claimed_str, style="dim"),
                Text.from_markup("[blue]☁ [/blue][dim]{}[/dim]".format(p_sid)),
                Text(p_account, style=acct_color),
                Text(p_tool, style="dim"),
                Text(co_name, style=co_style),
                Text(p_repo, style="dim"),
                Text(p_model, style=p_mdl_style),
                p_mem_text,
                Text(hb_str, style=hb_style),
                Text(p_tok_str, style="dim"),
                Text(p_task),
                key="peer-{}".format(p_sid),
            )

            # Blank separator
            self.add_row(
                Text(""), Text(""), Text(""), Text(""), Text(""),
                Text(""), Text(""), Text(""), Text(""), Text(""), Text(""),
                key="peergap-{}".format(p_sid),
            )

        # Totals footer row with gauges
        # Totals moved to SystemStatusPanel

        try:
            if cur_row < self.row_count:
                self.move_cursor(row=cur_row, scroll=False)
        except Exception:
            pass
        try:
            self.scroll_to(y=saved_y, animate=False)
        except Exception:
            pass

    def _get_pid_from_cursor(self):
        # type: () -> Optional[str]
        """Extract PID from the currently selected row key."""
        try:
            key = self.get_row_at(self.cursor_row)
            # key is the row data, we need the row_key
            row_key = None
            for rk in self.rows:
                if rk == self.cursor_row:
                    row_key = rk
                    break
        except Exception:
            pass

        # Use the rows mapping: iterate to find current cursor row's key
        try:
            keys = list(self.rows.keys())
            if self.cursor_row < len(keys):
                row_key = keys[self.cursor_row]
                key_str = row_key.value if hasattr(row_key, "value") else str(row_key)
                if key_str.startswith("active-"):
                    return key_str.replace("active-", "")
                elif key_str.startswith("sub-"):
                    return key_str.replace("sub-", "")
                elif key_str.startswith("gap-"):
                    return key_str.replace("gap-", "")
        except Exception:
            pass
        return None

    def on_data_table_row_selected(self, event):
        """Handle Enter key — focus the terminal for the selected session."""
        self._focus_terminal_for_row(event.row_key)

    def action_focus_selected(self):
        """Handle 'f' key — focus the terminal for the currently highlighted session."""
        pid = self._get_pid_from_cursor()
        if pid:
            ok, hint = focus_session_terminal(pid)
            if ok:
                self.app.notify(f"Focused: {hint}", severity="information", timeout=2)
            else:
                self.app.notify(f"Opened Warp — find: {hint}", severity="warning", timeout=3)

    def action_kill_selected(self):
        """Handle 'k' key — open Force Quit confirm modal for the selected session."""
        pid = self._get_pid_from_cursor()
        if not pid:
            self.app.notify("No session selected", severity="warning", timeout=2)
            return
        my_pid = str(os.getpid())
        parent_pid = str(os.getppid())
        if pid in (my_pid, parent_pid):
            self.app.notify("Can't kill token-watch's own process", severity="warning", timeout=3)
            return

        # Look up the full session dict by PID for rich modal display
        from token_watch_data import _get_engine_status
        try:
            engine = _get_engine_status()
        except Exception:
            engine = {"sessions": [], "peers": []}
        session = next(
            (s for s in engine.get("sessions", []) if str(s.get("pid")) == str(pid)),
            None,
        )
        if not session:
            # Could be a peer-only session — build a minimal dict so the modal still renders
            session = {
                "pid": pid,
                "sid": f"cc-{pid}",
                "directive": "(unknown — peer session)",
                "age": "?",
                "delta": "?",
                "mem_mb": 0,
                "cpu": 0,
                "health": "yellow",
                "health_reason": "no local data",
                "source": "?",
                "repo": "—",
            }

        self.app.push_screen(ForceQuitScreen(session, self._do_force_quit))

    def _do_force_quit(self, session):
        """Callback after Force Quit confirm — runs SIGTERM/grace/SIGKILL + cleanup + log.

        Runs in a background thread so the TUI stays responsive during the 3s grace.
        """
        import signal
        import time
        import threading

        pid_str = str(session.get("pid"))
        sid = session.get("sid", f"cc-{pid_str}")
        directive = session.get("directive") or "no directive"
        repo = session.get("repo") or ""

        try:
            pid_int = int(pid_str)
        except (TypeError, ValueError):
            self.app.notify(f"Invalid PID for {sid}", severity="error", timeout=3)
            return

        def _notify(msg, severity="information", timeout=3):
            self.app.call_from_thread(
                self.app.notify, msg, severity=severity, timeout=timeout
            )

        def _worker():
            # 1. SIGTERM
            term_ok = False
            try:
                os.kill(pid_int, signal.SIGTERM)
                term_ok = True
            except ProcessLookupError:
                _notify(f"{sid} already gone — cleaning up locks", "information", 2)
            except PermissionError:
                _notify(f"No permission to kill {sid}", "error", 4)
                return
            except Exception as e:
                _notify(f"SIGTERM failed: {e}", "error", 4)
                return

            # 2. 3s grace, then SIGKILL if still alive
            if term_ok:
                alive = True
                for _ in range(6):
                    time.sleep(0.5)
                    try:
                        os.kill(pid_int, 0)  # signal 0 = liveness check
                    except ProcessLookupError:
                        alive = False
                        break
                    except Exception:
                        alive = False
                        break
                if alive:
                    try:
                        os.kill(pid_int, signal.SIGKILL)
                        _notify(f"{sid} unresponsive — SIGKILL sent", "warning", 3)
                    except Exception as e:
                        _notify(f"SIGKILL failed: {e}", "error", 4)

            # 3. Cleanup Supabase locks
            from token_watch_data import (
                _expire_session_lock,
                _release_session_files,
                _post_build_ledger_event,
            )
            try:
                _expire_session_lock(sid)
            except Exception as e:
                _notify(f"session_lock cleanup warning: {e}", "warning", 3)
            try:
                _release_session_files(sid)
            except Exception:
                pass

            # 4. Log to build_ledger
            try:
                _post_build_ledger_event(
                    session_id=sid,
                    item_type="force_quit",
                    title=f"Force-quit {sid} — {directive}",
                    project=repo,
                    source="token-watch",
                )
            except Exception:
                pass

            _notify(f"✓ Killed {sid} + released locks", "information", 3)

        threading.Thread(target=_worker, daemon=True).start()

    def action_release_files(self):
        """Handle 'K' key — clear files_touched for the selected session in Supabase."""
        pid = self._get_pid_from_cursor()
        if not pid:
            self.app.notify("No session selected", severity="warning", timeout=2)
            return
        sid = f"cc-{pid}"
        from token_watch_data import _release_session_files
        ok = _release_session_files(sid)
        if ok:
            self.app.notify(f"Released file locks for {sid}", severity="information", timeout=3)
        else:
            self.app.notify(f"Failed to release files for {sid}", severity="error", timeout=3)

    def _focus_terminal_for_row(self, row_key):
        """Extract PID from row key and focus the corresponding terminal."""
        if not row_key:
            return
        key_str = row_key.value if hasattr(row_key, "value") else str(row_key)
        pid = None
        if key_str.startswith("active-"):
            pid = key_str.replace("active-", "")
        elif key_str.startswith("sub-"):
            pid = key_str.replace("sub-", "")
        elif key_str.startswith("gap-"):
            pid = key_str.replace("gap-", "")
        if pid and pid != "empty":
            ok, hint = focus_session_terminal(pid)
            if ok:
                self.app.notify(f"Focused: {hint}", severity="information", timeout=2)
            else:
                self.app.notify(f"Opened Warp — find: {hint}", severity="warning", timeout=3)


class ToolFrequency(Static):
    def update_content(self):
        self.update(make_tool_stats())


class SkillsPanel(Static):
    def update_content(self):
        self.update(make_skills_panel())


class AgentsPanel(Static):
    def update_content(self):
        from token_watch_data import _get_agent_stats
        stats = _get_agent_stats(days=7)
        t = RichTable(
            show_header=True, header_style="bold yellow",
            box=None, padding=(0, 1), expand=True,
        )
        t.add_column("Agent Description", overflow="ellipsis", no_wrap=True, ratio=3)
        t.add_column("Spawns", min_width=7, justify="right", no_wrap=True)
        t.add_column("Last", min_width=6, no_wrap=True)
        if not stats:
            t.add_row(Text("no agent spawns yet", style="dim"), "", "")
        else:
            for desc, count, last in stats[:10]:
                t.add_row(
                    Text(desc, overflow="ellipsis"),
                    Text(str(count), justify="right"),
                    Text(last, style="dim"),
                )
        self.update(Panel(
            t,
            title="[bold]Agent Spawns[/bold]  [dim](7d)[/dim]",
            border_style="yellow",
        ))


class SessionNarrativePanel(Static):
    """Compact narrative of what was built in the current 5h window, grouped by project."""

    def update_content(self):
        # Get current 5h window bounds
        _, _, five_reset_ts, _ = _current_pct()
        window_start = None
        if five_reset_ts:
            try:
                reset_dt = datetime.fromisoformat(five_reset_ts.replace("Z", "+00:00"))
                window_start = reset_dt - timedelta(hours=5)
            except Exception:
                pass

        # Filter session history to current window
        sessions = _get_session_history()
        if window_start:
            sessions = [s for s in sessions if s["last_ts"] >= window_start]

        if not sessions:
            self.update("")
            self.display = False
            return

        # Group by project
        from collections import defaultdict
        project_sessions = defaultdict(list)
        for s in sessions:
            project = s.get("project", "\u2014")
            project_sessions[project].append(s)

        # Build narrative lines
        lines = []
        # Color map for known projects
        color_map = {
            "atlas": "blue",
            "Token Window": "cyan",
            "paperclip": "green",
            "openclaw": "magenta",
            "frank": "magenta",
            "kaa": "green",
        }

        for project, proj_sessions in sorted(project_sessions.items(), key=lambda x: len(x[1]), reverse=True):
            descriptions = []
            for s in proj_sessions:
                # Prefer directive as summary
                directive = s.get("directive", "")
                if directive and directive != "\u2014":
                    descriptions.append(directive)
                else:
                    # Fall back to gravity center from accomplishments
                    acc = _extract_accomplishments(s["session_id"])
                    gc = _gravity_center(acc, fallback="")
                    if gc:
                        descriptions.append(gc)

            if not descriptions:
                continue

            # Deduplicate while preserving order
            seen = set()
            unique = []
            for d in descriptions:
                d_lower = d.lower().strip()
                if d_lower not in seen:
                    seen.add(d_lower)
                    unique.append(d)

            # Build the description string — join with commas, truncate if needed
            desc_str = ", ".join(unique)
            if len(desc_str) > 100:
                desc_str = desc_str[:97] + "..."

            p_color = color_map.get(project.lower(), "white")
            lines.append(f"[bold {p_color}]{project}[/bold {p_color}]: {desc_str}")

        if not lines:
            self.update("")
            self.display = False
            return

        content = "\n".join(lines)
        self.update(Panel(
            content,
            title="[bold]Session Narrative[/bold]",
            border_style="green",
        ))
        self.display = True


class DrainPanel(Static):
    def update_content(self):
        self.update(make_drain_panel())



class TokenAttributionPanel(Static):
    """Compact per-session token attribution bar on main dashboard."""

    def update_content(self):
        data = _get_token_attribution()
        if not data or not data.get("sessions"):
            self.update("[dim]No attribution data[/dim]")
            self.display = False
            return
        self.display = True
        sessions = data["sessions"]
        total = data["total_used_pct"]
        unaccounted = data.get("unaccounted_pct", 0)
        try:
            bar_width = max(20, self.size.width - 6)
        except Exception:
            bar_width = 50

        # Filter sessions: >= 1% get bar + legend, < 1% collapse into "others"
        display_sessions = [s for s in sessions if s["pct_used"] >= 1.0]
        others = [s for s in sessions if 0 < s["pct_used"] < 1.0]
        others_pct = sum(s["pct_used"] for s in others)
        others_tok = sum(s.get("output_tokens", 0) or 0 for s in others)

        # Segments: display_sessions + others (if any) + unaccounted
        all_segments = list(display_sessions)
        has_others = others_pct > 0.1 and len(others) > 0
        has_unaccounted = unaccounted > 0.5
        n_segments = len(all_segments) + (1 if has_others else 0) + (1 if has_unaccounted else 0)
        min_cols = 8
        reserved = min_cols * max(n_segments, 1)
        extra = max(0, bar_width - reserved)
        sum_pct = sum(s["pct_used"] for s in all_segments) + others_pct + max(unaccounted, 0)
        if sum_pct <= 0:
            sum_pct = 1

        bar_chars = []
        for s in all_segments:
            pct = s["pct_used"]
            cols = min_cols + int(pct / sum_pct * extra)
            color = s["color"]
            label = f"{pct:.0f}%"
            segment = label.center(cols) if cols >= len(label) + 2 else "\u2588" * cols
            bar_chars.append(f"[bold white on {color}]{segment}[/]")
        if has_others:
            cols = min_cols + int(others_pct / sum_pct * extra)
            label = f"{others_pct:.0f}%"
            segment = label.center(cols) if cols >= len(label) + 2 else "\u2591" * cols
            bar_chars.append(f"[bold white on grey50]{segment}[/]")
        if has_unaccounted:
            cols = min_cols + int(unaccounted / sum_pct * extra)
            segment = f"{unaccounted:.0f}%".center(cols) if cols >= 6 else "\u2591" * cols
            bar_chars.append(f"[dim]{segment}[/dim]")
        bar_line = "".join(bar_chars)

        # Legend — uses same display_sessions so colors match the bar exactly
        # Enrich with company/project from engine status
        from token_watch_data import _get_engine_status
        engine = _get_engine_status()
        _sid_info = {}
        for _es in engine.get("sessions", []):
            _sid_info[_es["sid"]] = _es
        for _ep in engine.get("peers", []):
            _sid_info[_ep.get("session_id", "")] = _ep

        legend_parts = []
        for s in display_sessions:
            pct = s["pct_used"]
            color = s["color"]
            sid = s["session_id"]

            # Build label: [company/project] directive or session_id
            directive = s["directive"]
            if not directive or directive == "\u2014":
                _pid = sid.replace("cc-", "") if sid.startswith("cc-") else ""
                try:
                    directive = open(f"/tmp/claude-directive-{_pid}").read().strip()
                except Exception:
                    directive = ""
            if not directive or directive == "\u2014":
                directive = sid[:16]

            # Get company/project context from engine data
            info = _sid_info.get(sid, {})
            project = info.get("repo", "") or ""
            if not project:
                d_lower = directive.lower()
                for p in ("atlas", "paperclip", "openclaw", "frank", "token-watch", "token watch"):
                    if p in d_lower:
                        project = p
                        break
            co = ""
            if project:
                co, _ = _project_to_company(project)

            prefix = ""
            if co and project:
                prefix = f"[dim]{co}/{project}[/dim] "
            elif project:
                prefix = f"[dim]{project}[/dim] "

            directive = directive[:40]
            out_tokens = s.get("output_tokens", 0) or 0
            if out_tokens >= 1_000_000:
                tok_str = f"{out_tokens / 1_000_000:.1f}M tok"
            elif out_tokens >= 1_000:
                tok_str = f"{out_tokens / 1_000:.0f}K tok"
            else:
                tok_str = f"{out_tokens} tok"
            legend_parts.append(f"[{color}]\u2588\u2588[/{color}] {prefix}{directive}  [bold]{pct:.1f}%[/bold]  [dim]{tok_str}[/dim]")
        if has_others:
            if others_tok >= 1_000_000:
                ot_str = f"{others_tok / 1_000_000:.1f}M tok"
            elif others_tok >= 1_000:
                ot_str = f"{others_tok / 1_000:.0f}K tok"
            else:
                ot_str = f"{others_tok} tok"
            legend_parts.append(f"[grey50]\u2591\u2591[/grey50] [dim]+ {len(others)} others  {others_pct:.1f}%  {ot_str}[/dim]")
        if has_unaccounted:
            candidates = data.get("unaccounted_candidates", [])
            if candidates:
                cand_str = ", ".join(f"cc-{c['pid']}" for c in candidates[:3])
                legend_parts.append(f"[dim]\u2591\u2591 unattributed  {unaccounted:.1f}%  likely: {cand_str}[/dim]")
            else:
                legend_parts.append(f"[dim]\u2591\u2591 unattributed  {unaccounted:.1f}%[/dim]")
        content = bar_line + "\n" + "\n".join(legend_parts)
        self.update(Panel(
            content,
            title=f"[bold]Who Ate My {total:.0f}%?[/bold]",
            border_style="yellow",
        ))


class BurndownChart(Static):
    """Token burndown chart — full 5h window with past, now marker, and projected future."""

    _BLOCKS = " ▁▂▃▄▅▆▇█"

    def update_content(self):
        data = _get_burndown_data()
        if not data or not data.get("actual"):
            self.update("[dim]No burndown data yet[/dim]")
            return

        actual = data["actual"]
        remaining = data["remaining_pct"]
        rate = data["current_rate"]
        status = data["status"]
        mins_to_reset = data["mins_to_reset"]
        wall_mins = data.get("projected_wall_mins")
        proj_remaining = data.get("projected_remaining_at_reset", remaining)
        mins_elapsed = data["mins_elapsed"]
        mins_total = data["mins_total"]  # 300 min
        window_start = data["window_start"]
        window_reset = data["window_reset"]

        # Chart spans the FULL 5h window, edge to edge
        # Dynamic width: subtract frame (panel border + "100%│" prefix + "│" suffix)
        try:
            available = self.size.width - 10  # 2 border + 5 label + 2 bars + 1 pad
            chart_width = max(20, min(available, 70))
        except Exception:
            chart_width = 50
        now_col = int(mins_elapsed / mins_total * chart_width)
        now_col = max(1, min(now_col, chart_width - 1))

        # Build data for every column across the full window.
        # Past columns use linear interpolation between actual data points
        # (not closest-point lookup) so the chart shows a smooth decline
        # instead of a flat/stepped line when data is sparse.
        full_data = []  # type: list  # (remaining_pct, zone) per column
        for col in range(chart_width):
            col_min = col * mins_total / chart_width

            if col <= now_col:
                # PAST — interpolate between actual data points
                if not actual:
                    val = 100.0
                elif col_min <= actual[0][0]:
                    val = actual[0][1]
                elif col_min >= actual[-1][0]:
                    val = actual[-1][1]
                else:
                    # Find bracketing points and interpolate
                    val = actual[-1][1]
                    for i in range(len(actual) - 1):
                        m0, r0 = actual[i]
                        m1, r1 = actual[i + 1]
                        if m0 <= col_min <= m1:
                            if m1 > m0:
                                t = (col_min - m0) / (m1 - m0)
                                val = r0 + t * (r1 - r0)
                            else:
                                val = r0
                            break
                full_data.append((val, "past"))
            else:
                # FUTURE — project from current remaining at current rate
                future_mins = col_min - mins_elapsed
                if rate > 0:
                    projected = max(0.0, remaining - rate * future_mins)
                else:
                    projected = remaining
                full_data.append((projected, "future"))

        # Ideal pace line: straight diagonal 100% → 0% across full window
        ideal_at = []  # type: list
        for col in range(chart_width):
            col_min = col * mins_total / chart_width
            ideal_at.append(max(0.0, 100.0 * (1.0 - col_min / mins_total)))

        # Pomodoro markers — columns that fall on 30-minute boundaries
        pomo_cols = set()
        for pomo_min in range(30, int(mins_total), 30):
            pomo_col = int(pomo_min / mins_total * chart_width)
            if 0 < pomo_col < chart_width:
                pomo_cols.add(pomo_col)

        # Budget per 10 minutes (to use it all evenly)
        budget_per_10 = (remaining / mins_to_reset * 10) if mins_to_reset > 0 else 0

        # Per-Pomodoro block stats
        from token_watch_data import _get_current_pomodoro, _get_pomodoro_stats, _get_current_cycle
        pomo_num = _get_current_pomodoro()
        pomo_stats = None
        pomo_strip = ""
        if pomo_num:
            try:
                current_cycle = _get_current_cycle()
                if current_cycle:
                    all_blocks = _get_pomodoro_stats(current_cycle["cycle_id"])
                    if all_blocks and 0 < pomo_num <= len(all_blocks):
                        pomo_stats = all_blocks[pomo_num - 1]
                    # Build mini strip: past=█ current=▓ future=░
                    chars = []
                    for b in all_blocks:
                        if b["is_current"]:
                            chars.append("[bold white]\u2593[/bold white]")
                        elif b["is_future"]:
                            chars.append("[dim]\u2591[/dim]")
                        else:
                            chars.append("[cyan]\u2588[/cyan]")
                    pomo_strip = "".join(chars)
            except Exception:
                pass

        block_used = abs(pomo_stats["delta_pct"]) if pomo_stats else 0.0
        block_color = "green" if block_used <= 10 else ("yellow" if block_used <= 15 else "red")

        # Render 8 chart rows (fills full vertical space)
        num_rows = 8
        row_height = 100.0 / num_rows
        rows = []
        for row_idx in range(num_rows):
            row_min = (num_rows - 1 - row_idx) * row_height
            row_max = row_min + row_height
            chars = []
            for col in range(chart_width):
                val, zone = full_data[col]
                ideal_val = ideal_at[col]

                # Now marker
                if col == now_col:
                    chars.append("[bold white]│[/bold white]")
                    continue

                # Pomodoro 30-min boundary — dotted vertical line
                if col in pomo_cols:
                    chars.append("[dim]·[/dim]")
                    continue

                # Map value to block char
                if val <= row_min:
                    block = " "
                elif val >= row_max:
                    block = "█"
                else:
                    frac = (val - row_min) / row_height
                    idx = int(frac * 8)
                    block = self._BLOCKS[min(idx, 8)]

                if zone == "future":
                    # Future: show projection as dim line, ideal as dots
                    if block == " ":
                        if row_min < ideal_val < row_max:
                            chars.append("[dim green]·[/dim green]")
                        else:
                            chars.append(" ")
                    else:
                        chars.append(f"[dim]{block}[/dim]")
                else:
                    # Past: colored based on actual vs ideal
                    if block == " ":
                        if row_min < ideal_val < row_max:
                            chars.append("[dim]·[/dim]")
                        else:
                            chars.append(" ")
                    else:
                        if val > ideal_val + 10:
                            color = "green"
                        elif val > ideal_val - 10:
                            color = "yellow"
                        else:
                            color = "red"
                        chars.append(f"[{color}]{block}[/{color}]")

            rows.append("".join(chars))

        # Stats
        rate_color = "red" if rate > 3 else ("yellow" if rate > 1 else "green")
        remaining_color = "red" if remaining < 20 else ("yellow" if remaining < 40 else "green")

        if status == "critical":
            proj_str = f"[bold red]WALL in ~{wall_mins:.0f}m[/bold red]"
        elif status == "burning_fast" and wall_mins:
            proj_str = f"[yellow]Wall in ~{wall_mins:.0f}m[/yellow]"
        elif status == "wasting":
            proj_str = f"[yellow]~{proj_remaining:.0f}% wasted at reset[/yellow]"
        else:
            proj_str = f"[green]~{proj_remaining:.0f}% at reset[/green]"

        h_reset = int(mins_to_reset // 60)
        m_reset = int(mins_to_reset % 60)
        reset_str = f"{h_reset}h{m_reset:02d}m" if h_reset else f"{m_reset}m"

        budget_color = "green" if budget_per_10 < 5 else ("yellow" if budget_per_10 < 10 else "red")

        start_label = window_start.astimezone().strftime("%H:%M")
        now_label = datetime.now().strftime("%H:%M")
        reset_label = window_reset.astimezone().strftime("%H:%M")

        # Time axis — position labels under chart
        axis = [" "] * chart_width
        # Start label
        for i, c in enumerate(start_label):
            if i < chart_width:
                axis[i] = c
        # Now label (center on now_col)
        now_start = max(0, now_col - 2)
        for i, c in enumerate(now_label):
            pos = now_start + i
            if 0 <= pos < chart_width:
                axis[pos] = c
        # Reset label at end
        reset_start = max(0, chart_width - len(reset_label))
        for i, c in enumerate(reset_label):
            pos = reset_start + i
            if 0 <= pos < chart_width:
                axis[pos] = c
        axis_str = "".join(axis)

        # Bottom border with now marker
        border = []
        for col in range(chart_width):
            if col == now_col:
                border.append("[bold white]┴[/bold white]")
            else:
                border.append("─")
        border_str = "".join(border)

        # Pacing verdict — the key "am I wasting tokens?" indicator
        needed_rate = remaining / mins_to_reset if mins_to_reset > 0 else 0.0
        if remaining < 3:
            verdict = "[bold green]✓ USED UP[/bold green]"
        elif rate >= needed_rate * 0.9:
            verdict = "[bold green]✓ ON PACE[/bold green]"
        elif status == "critical":
            wall_str = f"~{wall_mins:.0f}m" if wall_mins else "soon"
            verdict = f"[bold red]⚡ WALL in {wall_str}[/bold red]"
        elif status == "burning_fast":
            verdict = "[bold yellow]⚡ FAST[/bold yellow]"
        elif (status == "wasting" or rate < needed_rate * 0.5) and rate > 0:
            wasted = proj_remaining if proj_remaining > 0 else 0
            verdict = f"[bold red]⚠ WASTING ~{wasted:.0f}%[/bold red]"
        elif rate == 0 and remaining > 3:
            verdict = "[dim]~ Calibrating...[/dim]"
        else:
            verdict = "[yellow]~ SLOW[/yellow]"

        verdict_line = (
            f"{verdict}  [{rate_color}]{rate:.1f}%/min[/{rate_color}]"
            f"  →  [dim]{needed_rate:.1f}%/min needed[/dim]"
            f"  │  [dim]Resets in {reset_str}[/dim]"
        )

        if remaining < 3 or (rate >= needed_rate * 0.9 and status not in ("critical", "burning_fast")):
            proj_label = f"[green]~{proj_remaining:.0f}% at reset[/green]"
        else:
            proj_label = f"[yellow]~{proj_remaining:.0f}% wasted at reset[/yellow]"

        details_line = (
            f"[{remaining_color}]{remaining:.0f}% left[/{remaining_color}]"
            f"  │  [{budget_color}]Budget: {budget_per_10:.1f}%/10m[/{budget_color}]"
            f"  │  {proj_label}"
        )

        # ── Right side: converged Token Monitor info ──
        from token_watch_data import (
            _current_pct, _countdown, _reset_day,
            _get_active_account, _token_pacing, _burn_mode,
        )
        five, seven, fr, sr = _current_pct()

        def mini_bar(pct, width=12):
            try:
                pct_f = float(pct)
                filled = round(pct_f * width / 100)
                # Ensure at least 1 block when pct > 0 (avoids empty bar for small values)
                if pct_f > 0 and filled == 0:
                    filled = 1
                # Cap at width (100% should fill completely)
                filled = min(filled, width)
                color = "green" if pct_f < 50 else ("yellow" if pct_f < 75 else "red")
                return f"[{color}]{'█' * filled}{'░' * (width - filled)}[/{color}]"
            except Exception:
                return f"[dim]{'░' * width}[/dim]"

        used_pct = 100.0 - remaining
        used_color = "red" if used_pct > 80 else ("yellow" if used_pct > 60 else "green")
        left_color = "red" if remaining < 20 else ("yellow" if remaining < 40 else "green")

        label, name, lane = _get_active_account()
        acct_color = "cyan" if label == "A" else ("magenta" if label == "B" else "yellow")

        h_reset = int(mins_to_reset // 60)
        m_reset = int(mins_to_reset % 60)
        reset_str = f"{h_reset}h{m_reset:02d}m" if h_reset else f"{m_reset}m"

        # Pacing line
        pacing = _token_pacing()
        pace_str = ""
        if pacing:
            if pacing["status"] == "at_limit":
                pace_str = "[red bold]AT LIMIT[/red bold]"
            else:
                m100 = pacing["mins_to_100"]
                mr = pacing["mins_to_reset"]
                burn = pacing["avg_burn"]
                if m100 < mr:
                    pace_str = f"[yellow]100% in ~{m100:.0f}m[/yellow] at {burn:.1f}%/min"
                else:
                    pace_str = f"[green]OK[/green] at {burn:.1f}%/min"

        # Burn mode for title
        burn_active, burn_secs = _burn_mode()
        burn_title = ""
        if burn_active:
            bm, bs = burn_secs // 60, burn_secs % 60
            burn_title = f"  [bold magenta]BURN {bm}m {bs:02d}s[/bold magenta]"

        # Live window score
        from token_watch_data import (
            _score_window as _sw, _get_window_scores, _get_streak, _stars_display,
        )
        live_score = _sw(window_start, window_reset)
        if live_score:
            stars = live_score["stars"]
            ov = live_score["overall"]
            star_color = "green" if ov >= 4 else ("yellow" if ov >= 3 else "red")
            def _dim_c(val):
                return "green" if val >= 4 else ("yellow" if val >= 2.5 else "red")
            b, p, sh, br, ve = live_score['burn'], live_score['parallelism'], live_score['shipping'], live_score['breadth'], live_score['velocity']
            score_line = (
                f"  [{star_color}]{stars} {ov}[/{star_color}]"
                f"  [dim]Burn:[/dim][{_dim_c(b)}]{b:.0f}[/{_dim_c(b)}]"
                f" [dim]Parallel:[/dim][{_dim_c(p)}]{p:.0f}[/{_dim_c(p)}]"
                f" [dim]Ship:[/dim][{_dim_c(sh)}]{sh:.0f}[/{_dim_c(sh)}]"
                f" [dim]Breadth:[/dim][{_dim_c(br)}]{br:.0f}[/{_dim_c(br)}]"
                f" [dim]Velocity:[/dim][{_dim_c(ve)}]{ve:.0f}[/{_dim_c(ve)}]"
            )
            streak = _get_streak()
            if streak >= 3:
                score_line += f"  [bold yellow]🔥{streak}-streak[/bold yellow]"
        else:
            score_line = ""

        # Token zone classification
        def _token_zone(pct):
            try:
                p = float(pct)
            except Exception:
                return ("?", "dim")
            if p < 40:
                return ("COOL", "green")
            if p < 70:
                return ("WARM", "yellow")
            if p < 85:
                return ("HOT", "red")
            return ("REDLINE", "bold red")

        five_zone, five_zcolor = _token_zone(five)
        seven_zone, seven_zcolor = _token_zone(seven)

        # Build right-side lines (aligned with 8 chart rows + border + axis = 10 lines)
        r = [
            f"  [bold {used_color}]{used_pct:.0f}% Used[/bold {used_color}]  [bold {left_color}]{remaining:.0f}% Left[/bold {left_color}]",
            f"  [bold]5h[/bold] {mini_bar(five)} {_safe_float(five):.0f}%  [dim]resets {reset_str}[/dim]",
            f"  [bold]7d[/bold] {mini_bar(seven)} {_safe_float(seven):.0f}%  [dim]{_reset_day(sr)[:10]}[/dim]",
            f"  [{five_zcolor}]{five_zone}[/{five_zcolor}] 5h  [{seven_zcolor}]{seven_zone}[/{seven_zcolor}] 7d",
            f"  [{acct_color}]Acct {label}[/{acct_color}]: {name} [dim]({lane})[/dim]",
            f"  P: {pomo_strip} {pomo_num or '?'}/10" if pomo_num else "",
            f"  {verdict}",
            (f"  P{pomo_num}: [{block_color}]{block_used:.1f}%[/{block_color}] (budget: 10%)"
             f"  [{budget_color}]{budget_per_10:.1f}%/10m[/{budget_color}]") if pomo_stats else
            f"  [{remaining_color}]{remaining:.0f}% left[/{remaining_color}]  [{budget_color}]Budget: {budget_per_10:.1f}%/10m[/{budget_color}]",
            f"  {pace_str}",
            f"  {score_line}",
        ]

        lines = []
        for i in range(num_rows):
            ylbl = "100%" if i == 0 else ("  0%" if i == num_rows - 1 else "    ")
            right = r[i] if i < len(r) else ""
            lines.append(f"{ylbl}\u2502{rows[i]}\u2502{right}")
        r_border = r[num_rows] if num_rows < len(r) else ""
        r_axis = r[num_rows + 1] if num_rows + 1 < len(r) else ""
        lines.append(f"    \u2514{border_str}\u2518{r_border}")
        lines.append(f"     [dim]{axis_str}[/dim]{r_axis}")

        content = "\n".join(lines)
        self.update(
            Panel(content, title=f"[bold]Token Burndown[/bold]  [dim](5h window)[/dim]{burn_title}",
                  border_style="bright_blue")
        )


    def on_click(self, event):
        """Show detailed burndown stats on click."""
        try:
            self.app.push_screen(BurndownDetailScreen())
        except Exception:
            pass


class BurndownDetailScreen(Screen):
    """Detail view for Token Burndown — exact %, time to reset, burn rate."""

    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="burndown-detail-content")

    def on_mount(self):
        data = _get_burndown_data()
        if not data:
            self.query_one("#burndown-detail-content").update("[dim]No burndown data available[/dim]")
            return

        remaining = data["remaining_pct"]
        used = 100.0 - remaining
        rate = data["current_rate"]
        mins_to_reset = data["mins_to_reset"]
        mins_elapsed = data["mins_elapsed"]
        mins_total = data["mins_total"]
        status = data["status"]
        wall_mins = data.get("projected_wall_mins")
        proj_remaining = data.get("projected_remaining_at_reset", remaining)

        from token_watch_data import (
            _current_pct, _countdown, _reset_day,
            _get_active_account, _token_pacing,
        )
        five, seven, fr, sr = _current_pct()
        label, name, lane = _get_active_account()
        pacing = _token_pacing()

        # Colors
        used_color = "red" if used > 80 else ("yellow" if used > 60 else "green")
        rem_color = "red" if remaining < 20 else ("yellow" if remaining < 40 else "green")
        rate_color = "red" if rate > 3 else ("yellow" if rate > 1 else "green")

        h_reset = int(mins_to_reset // 60)
        m_reset = int(mins_to_reset % 60)

        # Needed rate to use all remaining by reset
        needed_rate = remaining / mins_to_reset if mins_to_reset > 0 else 0.0

        lines = []
        lines.append(f"[bold]Token Burndown Detail[/bold]")
        lines.append("")
        lines.append(f"  Account:     [bold]{label}[/bold] — {name} ({lane})")
        lines.append("")
        lines.append(f"  [bold]5h Usage[/bold]")
        lines.append(f"    Used:      [{used_color}][bold]{used:.1f}%[/bold][/{used_color}]")
        lines.append(f"    Remaining: [{rem_color}][bold]{remaining:.1f}%[/bold][/{rem_color}]")
        lines.append(f"    Resets in: [bold]{h_reset}h{m_reset:02d}m[/bold]  ({_countdown(fr)})")
        lines.append("")
        lines.append(f"  [bold]7d Usage[/bold]")
        lines.append(f"    Used:      [bold]{_safe_float(seven):.1f}%[/bold]")
        lines.append(f"    Resets:    {_reset_day(sr)}")
        lines.append("")
        lines.append(f"  [bold]Burn Rate[/bold]")
        lines.append(f"    Current:   [{rate_color}]{rate:.2f}%/min[/{rate_color}]  ({rate * 60:.1f}%/hr)")
        lines.append(f"    Needed:    {needed_rate:.2f}%/min to use all by reset")
        if pacing:
            avg_burn = pacing.get("avg_burn", 0)
            lines.append(f"    Avg (10m): {avg_burn:.2f}%/min")

        lines.append("")
        lines.append(f"  [bold]Window[/bold]")
        lines.append(f"    Elapsed:   {int(mins_elapsed)}m / {int(mins_total)}m")
        lines.append(f"    Status:    [bold]{status.upper()}[/bold]")

        if wall_mins is not None:
            wh = int(wall_mins // 60)
            wm = int(wall_mins % 60)
            wall_str = f"{wh}h{wm:02d}m" if wh else f"{wm}m"
            lines.append(f"    Wall in:   {wall_str}")

        lines.append(f"    At reset:  ~{proj_remaining:.0f}% remaining")
        lines.append("")
        lines.append("[dim]Press Escape or q to close[/dim]")

        content = "\n".join(lines)
        self.query_one("#burndown-detail-content").update(
            Panel(content, title="[bold]Burndown Detail[/bold]", border_style="bright_blue")
        )

    def action_pop_screen(self):
        self.app.pop_screen()


class SystemHealthPanel(Static):
    """System health — CPU and memory for Claude ecosystem processes."""

    def update_content(self):
        health = _get_system_health()
        if not health:
            self.update("")
            self.display = False
            return

        t = RichTable(show_header=True, header_style="bold", box=None, padding=(0, 1), expand=True)
        t.add_column("When", width=9, no_wrap=True)
        t.add_column("Process", width=10, no_wrap=True)
        t.add_column("Src", width=10, no_wrap=True)
        t.add_column("Project", width=18, no_wrap=True)
        t.add_column("Mdl", width=10, no_wrap=True)
        t.add_column("Mem", width=8, justify="right", no_wrap=True)
        t.add_column("Status", overflow="ellipsis", no_wrap=True)

        # Build model map from ledger
        entries = _load_ledger(last_n=500)
        model_map = {}  # type: dict
        for e in entries:
            sid = e.get("session", "")
            mdl = e.get("model")
            if sid and mdl and mdl != "?":
                model_map[sid] = mdl

        # Claude sessions
        for s in health.get("claude_sessions", []):
            pid = s["pid"]
            cpu = s["cpu"]
            mem = s["mem_mb"]
            directive = s["directive"]
            st = s["status"]
            source = s.get("source", "?")

            # Derive project and company
            project = "—"
            if source in ("atlas-be", "atlas-fe"):
                project = "atlas"
            elif source in ("openclaw", "frank", "paperclip"):
                project = source
            elif "/" in source:
                project = source.split("/")[0].lower()
            else:
                d_lower = directive.lower() if directive else ""
                for p in ("Token Window", "atlas", "paperclip", "openclaw", "frank"):
                    if p in d_lower:
                        project = p
                        break
            co_name, co_style = _project_to_company(project)

            src_color = (
                "yellow" if ("/" in source or source == "paperclip")
                else ("green" if source == "cli"
                       else ("cyan" if "atlas" in source else "dim"))
            )

            mem_str = _mem_mini_gauge(mem)
            if st == "runaway":
                dot = "[bold red]⚠ [/bold red]"
                status_str = f"[bold red]runaway[/bold red] ({directive[:20]})"
            elif st == "active":
                dot = "[bold green]● [/bold green]"
                status_str = f"[green]active[/green] ({directive[:20]})"
            else:
                dot = "  "
                status_str = f"[dim]{st}[/dim]"

            start_time = s.get("start_time", "?")
            mdl = _abbrev_model(model_map.get(f"cc-{pid}", "?"))
            mdl_style = "magenta" if "opus" in mdl else ("cyan" if "sonnet" in mdl else "dim")
            project_display = f"[{co_style}]{co_name}[/{co_style}]/[dim]{project}[/dim]"
            t.add_row(
                f"[dim]{start_time}[/dim]",
                f"{dot}[cyan]cc-{pid}[/cyan]",
                f"[{src_color}]{source}[/{src_color}]",
                project_display,
                f"[{mdl_style}]{mdl}[/{mdl_style}]",
                Text.from_markup(mem_str),
                status_str,
            )

        # Infrastructure
        for inf in health.get("infrastructure", []):
            name = inf["name"]
            cpu = inf["cpu"]
            mem = inf["mem_mb"]
            count = inf["count"]
            pid = inf["pid"]

            mem_str = f"{mem/1024:.1f}GB" if mem >= 1024 else f"{mem:.0f}MB"
            display_name = f"{name} (x{count})" if count > 1 else name

            # Hog alert
            alert = ""
            if mem > 3000:
                alert = " [red]← hog[/red]"

            t.add_row(
                "",
                f"[dim]{display_name}[/dim]",
                "",
                "",
                "",
                mem_str,
                f"[dim]infra[/dim]{alert}",
            )

        # Totals
        totals = health.get("totals", {})
        total_cpu = totals.get("cpu", 0)
        total_mem = totals.get("mem_mb", 0)
        mem_pct = totals.get("mem_pct", 0)
        sys_mem = totals.get("system_mem_mb", 16384)

        total_mem_str = f"{total_mem/1024:.1f}GB" if total_mem >= 1024 else f"{total_mem:.0f}MB"
        mem_pct_color = "red" if mem_pct > 80 else ("yellow" if mem_pct > 60 else "green")

        mem_zone, mem_zc = _zone_label(mem_pct)
        cpu_capped = min(total_cpu, 100)
        cpu_zone, cpu_zc = _zone_label(cpu_capped)
        mem_gb = total_mem / 1024
        sys_gb = sys_mem / 1024

        t.add_row(
            "",
            Text.from_markup(f"MEM {_gauge_bar(mem_pct)} {mem_gb:.1f}GB/{sys_gb:.0f}GB [{mem_zc}]{mem_zone}[/{mem_zc}]"),
            "",
            "",
            Text.from_markup(f"CPU {_gauge_bar(cpu_capped)} {total_cpu:.0f}% [{cpu_zc}]{cpu_zone}[/{cpu_zc}]"),
            "",
            "",
        )

        t.add_row(
            "",
            "[bold]Total AI stack[/bold]",
            "",
            "",
            "",
            f"[bold]{total_mem_str}[/bold]",
            f"[{mem_pct_color}]{mem_pct:.0f}% of {sys_mem/1024:.0f}GB[/{mem_pct_color}]",
        )

        # Paperclip ghost instance check
        import subprocess as _sp
        try:
            _scan = _sp.run(
                ["/bin/bash", "/Users/a13xperi/battlestation/scripts/paperclip-scan.sh", "--json"],
                capture_output=True, text=True, timeout=3
            )
            if _scan.returncode == 0 and _scan.stdout.strip():
                import json as _json
                _d = _json.loads(_scan.stdout.strip())
                if _d.get("ghost_running"):
                    _pid = _d.get("ghost_pid", "?")
                    t.add_row("", "[bold red]⚠ GHOST[/bold red]", "localhost", "[red]Personal[/red]", "", "", f"[bold red]Paperclip LIVE pid={_pid} — BURNING TOKENS[/bold red]")
                else:
                    _bc = _d.get("backup_count", 0)
                    _bwarn = f" [yellow]({_bc} backups)[/yellow]" if _bc > 14 else ""
                    t.add_row("", "[dim green]● ghost[/dim green]", "localhost", "[dim]Personal[/dim]", "", "", f"[dim green]Paperclip DOWN (safe)[/dim green]{_bwarn}")
        except Exception:
            pass

        self.display = True
        self.update(Panel(t, title="[bold]System Health[/bold]", border_style="magenta"))


class EngineUsagePanel(Static):
    """External engine capacity bars — Kimi, MiniMax, Codex, Gemini, Grok."""

    def update_content(self):
        try:
            usage = _get_external_engine_usage()
        except Exception:
            self.update("")
            self.display = False
            return

        lines = []
        for engine in ("kimi", "minimax", "codex", "gemini", "grok"):
            data = usage.get(engine, {})
            count = data.get("count", 0)
            quota = data.get("quota")
            pct = data.get("pct", 0.0)
            reset_str = data.get("reset_str")
            balance = data.get("balance")

            name = engine.capitalize()
            if engine == "minimax":
                name = "MiniMax"

            if balance:
                lines.append(f"{name:8s} [dim]{balance}[/dim]")
            elif quota is None:
                # unlimited (gemini)
                reset_display = f"  resets {reset_str}" if reset_str else ""
                lines.append(f"{name:8s} [green]unlimited[/green]{reset_display}")
            else:
                width = 10
                filled = min(int(pct * width / 100), width)
                color = "green" if pct < 40 else ("yellow" if pct < 75 else "red")
                bar = f"[{'█' * filled}{'░' * (width - filled)}]"
                pct_str = f" ({pct:.0f}%)" if pct >= 1 else ""
                reset_display = f"  resets {reset_str}" if reset_str else ""
                lines.append(
                    f"{name:8s} [{color}]{bar}[/{color}] {count}/{quota}{pct_str}{reset_display}"
                )

        self.display = True
        self.update(Panel("\n".join(lines), title="[bold]Engines[/bold]", border_style="cyan"))


class ReloadBanner(Static):
    """Banner shown when source files have changed. Click or press Shift+R to reload."""

    DEFAULT_CSS = """
    ReloadBanner {
        display: none;
        height: 1;
        dock: top;
        background: $warning;
        color: $text;
        text-align: center;
        text-style: bold;
    }
    ReloadBanner.reverted {
        background: $error;
    }
    """

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self._mode = "hidden"

    def show_pending(self):
        self._mode = "pending"
        self.update("[reverse] FILES CHANGED [/reverse]  Press [bold]Shift+R[/bold] to reload build")
        self.remove_class("reverted")
        self.display = True

    def show_reverted(self, error_msg=""):
        self._mode = "reverted"
        short_err = error_msg.strip().split("\n")[-1][:80] if error_msg else "import error"
        self.update(f"[reverse] BUILD BROKEN \u2014 REVERTED [/reverse]  {short_err}")
        self.add_class("reverted")
        self.display = True

    def hide_banner(self):
        self._mode = "hidden"
        self.display = False

    def on_click(self):
        if self._mode == "pending":
            self.app.action_reload_build()


# ── Navigation bar ───────────────────────────────────────────────────────────


class NavBar(Horizontal):
    """Top navigation bar with clickable buttons."""

    def __init__(self, active: str = "nav-dashboard", **kwargs):
        super().__init__(**kwargs)
        self._active = active

    # All tabs — used by NavScreen and button routing
    # Grouped by function for the clustered nav layout
    ALL_TAB_GROUPS = [
        ("Core", [
            ("Dashboard", "nav-dashboard"),
            ("Advisor", "nav-advisor"),
            ("Plan", "nav-plan"),
            ("Artifacts", "nav-artifacts"),
            ("Health", "nav-health"),
            ("Capacity", "nav-capacity"),
        ]),
        ("Work", [
            ("Cycle", "nav-sessions"),
            ("Projects", "nav-projects"),
            ("Dispatch", "nav-dispatch"),
            ("Inbox", "nav-inbox"),
            ("Team", "nav-team"),
        ]),
        ("Intelligence", [
            ("Mission", "nav-mission"),
            ("Proj Stats", "nav-proj-stats"),
            ("Delphi", "nav-delphi"),
            ("Analytics", "nav-analytics"),
            ("Audit", "nav-audit"),
            ("Plans", "nav-plans"),
        ]),
        ("Operations", [
            ("Wire", "nav-wire"),
            ("Test", "nav-test"),
            ("Rules", "nav-rules"),
            ("MCP", "nav-mcp"),
            ("Feed", "nav-forge-feed"),
            ("Forges", "nav-forge-status"),
            ("Pipeline", "nav-forge-pipeline"),
        ]),
        ("History", [
            ("Cycles", "nav-cycles"),
            ("Weekly", "nav-weekly"),
            ("Usage", "nav-usage"),
            ("Leaderboard", "nav-leaderboard"),
        ]),
    ]

    # Flat list for backward compat
    ALL_TABS = [
        tab for _, tabs in ALL_TAB_GROUPS for tab in tabs
    ]

    def compose(self) -> ComposeResult:
        # Compact top bar — key tabs + Nav button for the rest
        buttons = [
            ("Dashboard", "nav-dashboard"),
            ("Advisor", "nav-advisor"),
            ("Plan", "nav-plan"),
            ("Artifacts", "nav-artifacts"),
            ("Dispatch", "nav-dispatch"),
            ("Cycle", "nav-sessions"),
            ("Mission", "nav-mission"),
            ("Nav", "nav-open-nav"),
        ]
        for label, btn_id in buttons:
            variant = "primary" if btn_id == self._active else "default"
            yield Button(label, id=btn_id, variant=variant)



class NavigationScreen(Screen):
    """Full-screen navigation — all tabs as large buttons."""

    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("[bold]Navigation[/bold]  [dim]click a tab or press Esc to go back[/dim]", id="navscreen-header")
        with Vertical(id="navscreen-grid"):
            for group_name, tabs in NavBar.ALL_TAB_GROUPS:
                yield Static(f"[dim]{group_name}[/dim]", classes="navscreen-group-label")
                with Horizontal(classes="navscreen-row"):
                    for label, btn_id in tabs:
                        yield Button(label, id=f"ns-{btn_id}", variant="default", classes="navscreen-btn")

    def on_button_pressed(self, event):
        btn_id = event.button.id or ""
        if btn_id.startswith("ns-nav-"):
            view_key = btn_id[3:]  # strip "ns-" prefix → "nav-xxx"
            # Map nav button to view
            btn_map = {
                "nav-dashboard": "view-dashboard",
                "nav-sessions": "view-sessions",
                "nav-projects": "view-projects",
                "nav-leaderboard": "view-leaderboard",
                "nav-usage": "view-usage",
                "nav-mcp": "view-mcp",
                "nav-cycles": "view-cycles",
                "nav-weekly": "view-weekly",
                "nav-test": "view-test",
                "nav-rules": "view-rules",
                "nav-audit": "view-audit",
                "nav-mission": "view-mission",
                "nav-proj-stats": "view-proj-stats",
                "nav-delphi": "view-delphi",
                "nav-wire": "view-wire",
                "nav-forge-feed": "view-forge-feed",
                "nav-forge-status": "view-forge-status",
                "nav-forge-pipeline": "view-forge-pipeline",
                "nav-advisor": "view-advisor",
                "nav-analytics": "view-analytics",
                "nav-dispatch": "view-dispatch",
                "nav-capacity": "view-capacity",
                "nav-inbox": "view-inbox",
                "nav-plans": "view-plans",
                "nav-plan": "view-advisor-plan",
                "nav-team": "view-team",
                "nav-artifacts": "view-artifacts",
            }
            view_id = btn_map.get(view_key)
            if view_id:
                self.app.pop_screen()
                self.app.switch_view(view_id)
            elif view_key == "nav-health":
                self.app.pop_screen()
                self.app.action_toggle_health()

    def action_pop_screen(self):
        self.app.pop_screen()


class HealthScreen(Screen):
    """Full-screen system health view."""

    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield NavBar(active="nav-health")
        yield Static(id="health-header")
        yield SystemHealthPanel(id="health-panel")
        yield EngineUsagePanel(id="engine-usage-panel")

    def on_mount(self):
        self.query_one("#health-header", Static).update(
            "[bold]System Health[/bold]"
        )
        self.query_one("#health-panel", SystemHealthPanel).update_content()
        self.query_one("#engine-usage-panel", EngineUsagePanel).update_content()

    def action_pop_screen(self):
        self.app.pop_screen()


# ── Drill-down screen ────────────────────────────────────────────────────────


class SessionDrillDown(Screen):
    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
        Binding("t", "toggle_view", "Toggle tokens/accomplishments"),
        Binding("s", "toggle_sort", "Sort by cost/turn#"),
    ]

    def __init__(self, session_id, directive="", project="—"):
        super().__init__()
        self.session_id = session_id
        self.session_directive = directive
        self.session_project = project
        self.showing_tokens = True
        self.sort_by_cost = True  # default: most expensive first

    def compose(self) -> ComposeResult:
        yield NavBar(active="nav-sessions")
        yield Static(id="drilldown-header")
        yield Static(id="accomplishments-view")
        yield DataTable(id="drilldown-table")

    def _update_header(self):
        hint = "[dim](t=accomplishments)[/dim]" if self.showing_tokens else "[dim](t=token usage)[/dim]"
        sort_hint = ""
        if self.showing_tokens:
            sort_label = "cost↓" if self.sort_by_cost else "turn#"
            sort_hint = f"  [dim](s=sort:{sort_label})[/dim]"
        self.query_one("#drilldown-header", Static).update(
            f"[bold]Session:[/bold] {self.session_id}  "
            f"[bold]Project:[/bold] {self.session_project}  "
            f"[bold]Directive:[/bold] {self.session_directive}  "
            + hint + sort_hint
        )

    def on_mount(self):
        self._update_header()
        self.query_one("#accomplishments-view", Static).display = False
        self._show_tokens()

    def _show_accomplishments(self):
        acc = _extract_accomplishments(self.session_id)
        view = self.query_one("#accomplishments-view", Static)

        if not acc:
            view.update("[dim]No accomplishment data available.[/dim]")
            return

        lines = []

        # Summary bar
        turns = acc.get("turn_count", 0)
        files = len(acc.get("files_edited", [])) + len(acc.get("files_created", []))
        commits = len(acc.get("git_commits", []))
        errors = acc.get("errors", 0)
        # Get output tokens + model from index cache for cost estimate
        with _index_lock:
            idx_entry = _index_cache.get(self.session_id, {})
        out_tok = idx_entry.get("output_tokens", 0)
        session_cost = _estimate_cost(out_tok, idx_entry.get("model", ""))
        summary_parts = [f"[bold]{turns}[/bold] turns"]
        if files:
            summary_parts.append(f"[bold]{files}[/bold] files")
        if commits:
            summary_parts.append(f"[bold]{commits}[/bold] commits")
        if out_tok:
            cost_style = "red" if session_cost >= 2.0 else ("yellow" if session_cost >= 0.50 else "green")
            summary_parts.append(f"[{cost_style}]{_format_cost(session_cost)}[/{cost_style}]")
        if errors:
            summary_parts.append(f"[bold red]{errors}[/bold red] errors")
        lines.append("  ".join(summary_parts))
        lines.append("")

        # Git commits
        if acc.get("git_commits"):
            lines.append("[bold green]GIT COMMITS[/bold green]")
            for c in acc["git_commits"]:
                lines.append(f"  [green]•[/green] {c}")
            lines.append("")

        # Git pushes
        if acc.get("git_pushes"):
            lines.append("[bold cyan]PUSHED[/bold cyan]")
            for b in acc["git_pushes"]:
                lines.append(f"  [cyan]→[/cyan] {b}")
            lines.append("")

        # Files edited
        if acc.get("files_edited"):
            lines.append("[bold yellow]FILES EDITED[/bold yellow]")
            for fp in acc["files_edited"][:15]:
                lines.append(f"  [yellow]✎[/yellow] {fp}")
            if len(acc["files_edited"]) > 15:
                lines.append(f"  [dim]...and {len(acc['files_edited']) - 15} more[/dim]")
            lines.append("")

        # Files created
        if acc.get("files_created"):
            lines.append("[bold blue]FILES CREATED[/bold blue]")
            for fp in acc["files_created"][:10]:
                lines.append(f"  [blue]+[/blue] {fp}")
            lines.append("")

        # Skills
        if acc.get("skills"):
            lines.append("[bold magenta]SKILLS USED[/bold magenta]")
            for s in acc["skills"]:
                lines.append(f"  [magenta]⚡[/magenta] /{s}")
            lines.append("")

        # MCP operations
        if acc.get("mcp_ops"):
            lines.append("[bold cyan]MCP OPERATIONS[/bold cyan]")
            for op in acc["mcp_ops"][:10]:
                lines.append(f"  [cyan]⟐[/cyan] {op}")
            if len(acc["mcp_ops"]) > 10:
                lines.append(f"  [dim]...and {len(acc['mcp_ops']) - 10} more[/dim]")
            lines.append("")

        # Notable commands
        if acc.get("bash_notable"):
            lines.append("[bold]NOTABLE COMMANDS[/bold]")
            for cmd in acc["bash_notable"][:8]:
                lines.append(f"  [dim]$[/dim] {cmd}")
            lines.append("")

        # User prompts
        if acc.get("user_prompts"):
            lines.append("[bold]USER PROMPTS[/bold]")
            for p in acc["user_prompts"]:
                lines.append(f"  [dim]>[/dim] {p}")
            lines.append("")

        if not any(acc.get(k) for k in ("git_commits", "files_edited", "files_created",
                                         "skills", "mcp_ops", "bash_notable", "user_prompts")):
            lines.append("[dim]No significant accomplishments recorded.[/dim]")

        view.update("\n".join(lines))

    def _show_tokens(self):
        table = self.query_one("#drilldown-table", DataTable)
        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("#", width=4)
        table.add_column("In", width=7)
        table.add_column("Out", width=7)
        table.add_column("Cost", width=7)
        table.add_column("5h%", width=5)
        table.add_column("Mdl", width=6)
        table.add_column("Tools", width=28)
        table.add_column("User Prompt")

        turns = _get_session_turns(self.session_id)
        if not turns:
            table.add_row("—", "", "", "", "", "", "", Text("no turns found", style="dim"))
            return

        if self.sort_by_cost:
            turns = sorted(turns, key=lambda x: x["tokens_out"], reverse=True)

        total_in = total_out = total_pct = total_cost = 0
        for t in turns:
            tokens_in = t["tokens_in"]
            tokens_out = t["tokens_out"]
            total_in += tokens_in
            total_out += tokens_out
            total_pct += t["pct_est"]

            turn_cost = _estimate_turn_cost(tokens_in, tokens_out, t["model"])
            total_cost += turn_cost

            in_str = f"{tokens_in/1000:.1f}k" if tokens_in >= 1000 else str(tokens_in)
            out_str = f"{tokens_out/1000:.1f}k" if tokens_out >= 1000 else str(tokens_out)
            cost_str = _format_cost(turn_cost)

            pct = t["pct_est"]
            pct_style = "red" if pct > 1 else ("yellow" if pct > 0.3 else "dim")
            cost_style = "red" if turn_cost >= 0.20 else ("yellow" if turn_cost >= 0.05 else "green")
            mdl_style = "magenta" if t["model"] == "opus" else ("cyan" if t["model"] == "sonnet" else "dim")

            table.add_row(
                str(t["turn"]),
                Text(in_str, style="dim"),
                Text(out_str),
                Text(cost_str, style=cost_style),
                Text(f"{pct:.1f}%", style=pct_style),
                Text(t["model"], style=mdl_style),
                Text(t["tools"][:28], style="dim"),
                Text(t["prompt"][:60]),
            )

        total_cost_str = f"${total_cost:.2f}" if total_cost >= 0.01 else "<$0.01"
        table.add_row(
            Text("Σ", style="bold"),
            Text(f"{total_in/1000:.0f}k", style="bold"),
            Text(f"{total_out/1000:.0f}k", style="bold"),
            Text(total_cost_str, style="bold red" if total_cost >= 1.0 else "bold yellow"),
            Text(f"{total_pct:.1f}%", style="bold yellow"),
            "",
            "",
            Text(f"{len(turns)} turns", style="bold"),
        )

    def action_toggle_view(self):
        self.showing_tokens = not self.showing_tokens
        acc_view = self.query_one("#accomplishments-view", Static)
        table = self.query_one("#drilldown-table", DataTable)
        self._update_header()

        if self.showing_tokens:
            acc_view.display = False
            table.display = True
            self._show_tokens()
        else:
            table.display = False
            acc_view.display = True
            self._show_accomplishments()

    def action_toggle_sort(self):
        if not self.showing_tokens:
            return
        self.sort_by_cost = not self.sort_by_cost
        self._update_header()
        table = self.query_one("#drilldown-table", DataTable)
        table.clear(columns=True)
        self._show_tokens()

    def action_pop_screen(self):
        self.app.pop_screen()


class DailySparklinePanel(Static):
    _SPARKS = " ▁▂▃▄▅▆▇█"

    def update_content(self):
        from token_watch_data import _get_daily_usage
        data = _get_daily_usage(days=7)
        if not data:
            self.update(Panel("[dim]No data yet[/dim]", title="7-Day Output Tokens", border_style="cyan"))
            return

        values = [v for _, v in data]
        max_val = max(values) if any(v > 0 for v in values) else 1

        spark_chars = []
        for v in values:
            idx = int(v / max_val * 8) if max_val else 0
            spark_chars.append(self._SPARKS[min(idx, 8)])

        # Align: each column is 5 chars wide (3 label + 2 separator)
        spark_line = "  ".join(f"  {c}  " for c in spark_chars)
        label_line = "  ".join(f"{label[:5]:5}" for label, _ in data)
        count_line = "  ".join(
            f"{v // 1000:3}k " if v >= 1000 else f" ~0  "
            for _, v in data
        )

        content = "\n".join([
            f"[bold cyan]{spark_line}[/bold cyan]",
            f"[dim]{label_line}[/dim]",
            f"[dim]{count_line}[/dim]",
        ])
        self.update(Panel(
            content,
            title="[bold]7-Day Output Tokens[/bold]",
            border_style="cyan",
        ))


class TokenAccessPanel(Static):
    """Shows token access systems with toggles to enable/disable heartbeats."""

    _STATUS_ON = "[bold green]●[/bold green]"
    _STATUS_OFF = "[bold red]○[/bold red]"
    _STATUS_MIXED = "[bold yellow]◐[/bold yellow]"

    def update_content(self):
        from token_watch_data import (
            _get_paperclip_heartbeats, _get_blocked_attempts,
            _get_paperclip_routines, _get_gate_state, _is_auto_gated,
        )

        try:
            agents = _get_paperclip_heartbeats()
        except Exception:
            agents = []
        try:
            routines = _get_paperclip_routines()
        except Exception:
            routines = []
        blocked = _get_blocked_attempts(minutes=60)
        gate = _get_gate_state()
        auto = _is_auto_gated()

        total_agents = len(agents)
        active_agents = sum(1 for a in agents if a.get("heartbeatEnabled") and a.get("schedulerActive"))
        total_routines = len(routines)
        active_routines = sum(1 for r in routines if r.get("status") == "active")

        lines = []

        if gate == "off":
            auto_tag = " [dim](auto @ 70%)[/dim]" if auto else ""
            gate_label = f"[bold red]GATED[/bold red]{auto_tag}"
            lines.append(f"[bold]Token Access[/bold]  {gate_label}  [dim]click to manage[/dim]")
            lines.append("")
            lines.append(f"  {self._STATUS_OFF} [red]all paused[/red]  "
                         f"[dim]{total_agents} agents · {total_routines} routines[/dim]")
        else:
            gate_label = "[bold green]GATE: ON[/bold green]"
            lines.append(f"[bold]Token Access[/bold]  {gate_label}  [dim]click to manage[/dim]")
            lines.append("")
            lines.append(f"  {self._STATUS_ON} [green]CLI[/green] [dim](always on)[/dim]")
            lines.append(f"  {active_agents} agents · {active_routines} routines active")

        if blocked:
            lines.append("")
            lines.append(f"  [yellow]⚠ {len(blocked)} blocked attempt(s) in last hour[/yellow]")

        self.update(Panel(
            "\n".join(lines),
            title="[bold]Token Access Control[/bold]",
            border_style="magenta",
        ))

    def on_click(self):
        self.app.push_screen(TokenAccessScreen())


class TokenAccessScreen(Screen):
    """Full screen for toggling individual agent heartbeats and routines."""

    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("r", "refresh", "Refresh"),
        Binding("g", "gate", "Gate All"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="taccess-header")
        yield DataTable(id="taccess-table")
        yield Static(id="taccess-routines-header")
        yield DataTable(id="taccess-routines")
        yield Static(id="taccess-blocked-header")
        yield DataTable(id="taccess-blocked")
        yield Static(id="taccess-footer")

    def on_mount(self):
        self._load()

    def _load(self):
        from token_watch_data import (
            _get_paperclip_heartbeats, _get_blocked_attempts,
            _get_paperclip_routines, _get_gate_state, _is_auto_gated,
        )

        agents = _get_paperclip_heartbeats()
        routines = _get_paperclip_routines()
        blocked = _get_blocked_attempts(minutes=60)
        gate = _get_gate_state()
        auto = _is_auto_gated()

        if gate == "on":
            gate_label = "[green]GATE: ON[/green]  [dim]auto-gates at 70%[/dim]"
        elif auto:
            gate_label = "[red]GATE: OFF[/red]  [yellow](auto — resumes on window reset)[/yellow]"
        else:
            gate_label = "[red]GATE: OFF[/red]  [dim](manual)[/dim]"
        self.query_one("#taccess-header", Static).update(
            f"[bold]Token Access Control[/bold]    {gate_label}    "
            "[dim]Enter to toggle · g = gate all · Esc to go back[/dim]"
        )

        # ── Heartbeats table ──
        table = self.query_one("#taccess-table", DataTable)
        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("Status", width=8)
        table.add_column("Company", width=14)
        table.add_column("Agent", width=22)
        table.add_column("Frequency", width=12)
        table.add_column("Last Run", width=14)
        table.add_column("Agent ID", width=38)

        # CLI row (not toggleable)
        table.add_row(
            Text("● ON", style="green"),
            Text("—", style="dim"),
            Text("CLI (human sessions)", style="green"),
            Text("—", style="dim"),
            Text("—", style="dim"),
            Text("always-on", style="dim"),
        )

        for a in agents:
            on = a.get("heartbeatEnabled", False)
            active = a.get("schedulerActive", False)
            interval = a.get("intervalSec", 0)

            if interval >= 86400:
                freq = f"every {interval // 86400}d"
            elif interval >= 3600:
                freq = f"every {interval // 3600}h"
            elif interval >= 60:
                freq = f"every {interval // 60}m"
            else:
                freq = f"every {interval}s" if interval else "manual"

            last = a.get("lastHeartbeatAt", "")
            age_str = ""
            if last:
                try:
                    last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                    age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
                    if age_min < 60:
                        age_str = f"{age_min:.0f}m ago"
                    elif age_min < 1440:
                        age_str = f"{age_min / 60:.0f}h ago"
                    else:
                        age_str = f"{age_min / 1440:.0f}d ago"
                except Exception:
                    pass

            status_text = "● ON" if on else "○ OFF"
            status_style = "green" if (on and active) else ("yellow" if on else "red")

            table.add_row(
                Text(status_text, style=status_style),
                Text(a.get("companyName", "?"), style="cyan"),
                Text(a.get("agentName", "?"), style="white" if on else "dim"),
                Text(freq, style="dim"),
                Text(age_str if age_str else "—", style="dim"),
                Text(a.get("id", "?"), style="dim"),
            )

        # ── Routines table ──
        active_routines = sum(1 for r in routines if r.get("status") == "active")
        paused_routines = sum(1 for r in routines if r.get("status") == "paused")
        self.query_one("#taccess-routines-header", Static).update(
            f"[bold]── Routines ──[/bold]  [dim]{active_routines} active · {paused_routines} paused[/dim]"
        )

        rt = self.query_one("#taccess-routines", DataTable)
        rt.clear(columns=True)
        rt.cursor_type = "row"
        rt.zebra_stripes = True
        rt.add_column("Status", width=10)
        rt.add_column("Company", width=14)
        rt.add_column("Description", width=30)
        rt.add_column("Schedule", width=16)
        rt.add_column("Last Run", width=14)
        rt.add_column("ID", width=38)

        for r in routines:
            r_status = r.get("status", "unknown")
            if r_status == "active":
                status_text = "● active"
                status_style = "green"
            else:
                status_text = "○ paused"
                status_style = "red"

            # Parse triggers for schedule info
            triggers = r.get("triggers", [])
            schedule = "—"
            if triggers:
                t = triggers[0]
                if isinstance(t, dict):
                    cron = t.get("cron", "")
                    interval_val = t.get("interval", "")
                    if cron:
                        schedule = cron
                    elif interval_val:
                        schedule = str(interval_val)
                    else:
                        schedule = t.get("type", "trigger")
                elif isinstance(t, str):
                    schedule = t

            last = r.get("lastTriggeredAt", "")
            age_str = ""
            if last:
                try:
                    last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                    age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
                    if age_min < 60:
                        age_str = f"{age_min:.0f}m ago"
                    elif age_min < 1440:
                        age_str = f"{age_min / 60:.0f}h ago"
                    else:
                        age_str = f"{age_min / 1440:.0f}d ago"
                except Exception:
                    pass

            rt.add_row(
                Text(status_text, style=status_style),
                Text(r.get("companyName", "?"), style="cyan"),
                Text(r.get("description", "—"), style="white" if r_status == "active" else "dim"),
                Text(schedule, style="dim"),
                Text(age_str if age_str else "—", style="dim"),
                Text(r.get("id", "?"), style="dim"),
            )

        # ── Blocked attempts ──
        self.query_one("#taccess-blocked-header", Static).update(
            f"[bold]Blocked Attempts[/bold]  [dim]{len(blocked)} in last hour[/dim]"
            if blocked else "[bold]Blocked Attempts[/bold]  [dim]none in last hour[/dim]"
        )

        bt = self.query_one("#taccess-blocked", DataTable)
        bt.clear(columns=True)
        bt.cursor_type = "none"
        bt.zebra_stripes = True
        bt.add_column("Time", width=20)
        bt.add_column("System", width=16)
        bt.add_column("Agent", width=24)
        bt.add_column("Detail")

        for b in blocked[-20:]:
            bt.add_row(
                Text(b.get("ts", "?")[:19], style="dim"),
                Text(b.get("system", "?"), style="yellow"),
                Text(b.get("agent", "?"), style="white"),
                Text(b.get("detail", ""), style="dim"),
            )

        self.query_one("#taccess-footer", Static).update(
            "[dim]Enter = toggle · g = gate all · r = refresh · Esc = back[/dim]"
        )

    def on_data_table_row_selected(self, event):
        table_id = event.data_table.id

        if table_id == "taccess-table":
            from token_watch_data import _get_paperclip_heartbeats, _toggle_heartbeat

            row_idx = event.cursor_row

            # Row 0 is CLI (not toggleable)
            if row_idx == 0:
                return

            agents = _get_paperclip_heartbeats()
            agent_idx = row_idx - 1
            if agent_idx >= len(agents):
                return

            agent = agents[agent_idx]
            new_state = not agent.get("heartbeatEnabled", False)
            if _toggle_heartbeat(agent["id"], new_state):
                self._load()

        elif table_id == "taccess-routines":
            from token_watch_data import _get_paperclip_routines, _toggle_routine

            row_idx = event.cursor_row
            routines = _get_paperclip_routines()
            if row_idx >= len(routines):
                return

            routine = routines[row_idx]
            new_active = routine.get("status") != "active"
            if _toggle_routine(routine["id"], new_active):
                self._load()

    def action_gate(self):
        from token_watch_data import _gate_all, _get_gate_state, _set_auto_gated

        current = _get_gate_state()
        new_state = current != "on"  # toggle
        _gate_all(new_state)
        _set_auto_gated(False)  # manual override clears auto-gate flag
        self._load()

    def action_refresh(self):
        import token_watch_data
        token_watch_data._heartbeat_cache = (0.0, [])
        token_watch_data._routine_cache = (0.0, [])
        self._load()

    def action_pop_screen(self):
        self.app.pop_screen()


class UsageMetricsView(BaseMetricsView):
    _table_ids = ["#metrics-table", "#scores-table"]

    def compose(self) -> ComposeResult:
        yield Static(id="metrics-header")
        yield DailySparklinePanel(id="metrics-sparkline")
        yield TokenAccessPanel(id="token-access")
        yield DataTable(id="metrics-table")
        yield Static(id="metrics-summary")
        yield Static(id="scores-header")
        yield DataTable(id="scores-table")

    def load_content(self):
        metrics, total = _get_usage_metrics(days=7)
        self.query_one("#metrics-sparkline", DailySparklinePanel).update_content()
        try:
            self.query_one("#token-access", TokenAccessPanel).update_content()
        except Exception:
            pass
        _, seven, _, _ = _current_pct()

        # Estimate total cost across all sources
        total_cost = sum(
            _estimate_cost(m["output_tokens"], m.get("model", "sonnet"))
            for m in metrics
        )

        self.query_one("#metrics-header", Static).update(
            f"[bold]Usage Metrics — last 7 days[/bold]  "
            f"[dim]Total output: {total/1000:.0f}k tokens  "
            f"Est. cost: [/dim][yellow]{_format_cost(total_cost)}[/yellow]  "
            f"[dim]Account 7d: {seven}%[/dim]"
        )

        table = self.query_one("#metrics-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("Source", width=20)
        table.add_column("Sessions", width=9)
        table.add_column("Output Tok", width=11)
        table.add_column("Avg/Session", width=12)
        table.add_column("% of Total", width=11)
        table.add_column("Share")

        for m in metrics:
            src = m["source"]
            src_style = "yellow" if ("/" in src or src == "paperclip") else (
                "green" if src == "cli" else ("cyan" if "atlas" in src else "dim")
            )
            out_k = m["output_tokens"]
            out_str = f"{out_k/1000:.1f}k" if out_k >= 1000 else str(out_k)
            avg_k = m["avg_tokens"]
            avg_str = f"{avg_k/1000:.1f}k" if avg_k >= 1000 else str(avg_k)
            pct = m["pct_of_total"]
            bar_len = max(1, int(pct / 2.5))  # 40 chars = 100%
            bar = "█" * bar_len + "░" * (40 - bar_len)
            bar_color = "yellow" if ("/" in src) else ("green" if src == "cli" else "cyan")
            table.add_row(
                Text(src, style=src_style),
                Text(str(m["sessions"]), justify="right"),
                Text(out_str, justify="right"),
                Text(avg_str, justify="right"),
                Text(f"{pct:.1f}%", justify="right"),
                Text(bar[:40], style=bar_color),
            )

        self.query_one("#metrics-summary", Static).update(
            f"[dim]Sessions above represent all indexed transcripts from the last 7 days. "
            f"7d account budget usage ({seven}%) is account-level and not split per source.[/dim]"
        )

        # Window Scores
        from token_watch_data import _get_window_scores, _get_streak, _stars_display
        scores = _get_window_scores(limit=10)
        streak = _get_streak(scores)

        streak_str = f"  [bold yellow]🔥 {streak}-window streak[/bold yellow]" if streak >= 3 else ""
        self.query_one("#scores-header", Static).update(
            f"[bold]Window Scores[/bold]  [dim]{len(scores)} scored windows[/dim]{streak_str}"
        )

        st = self.query_one("#scores-table", DataTable)
        st.cursor_type = "row"
        st.zebra_stripes = True
        st.add_column("Window", width=18)
        st.add_column("Stars", width=8)
        st.add_column("Overall", width=8)
        st.add_column("Burn", width=6)
        st.add_column("Para", width=6)
        st.add_column("Ship", width=6)
        st.add_column("Breadth", width=8)
        st.add_column("Vel", width=6)
        st.add_column("Details")

        for s in scores:
            try:
                ws = datetime.fromisoformat(s["window_start"].replace("Z", "+00:00"))
                window_label = ws.astimezone().strftime("%b %d %H:%M")
            except Exception:
                window_label = "?"
            ov = s.get("overall", 0)
            ov_color = "green" if ov >= 4 else ("yellow" if ov >= 3 else "red")
            details = (
                f"{s.get('burn_pct', 0):.0f}% burn, "
                f"{s.get('max_parallel', 0)} parallel, "
                f"{s.get('commits', 0)} commits, "
                f"{s.get('projects', 0)} projects"
            )
            st.add_row(
                Text(window_label, style="dim"),
                Text(s.get("stars", "?"), style=ov_color),
                Text(f"{ov}", style=ov_color, justify="right"),
                Text(f"{s.get('burn', 0):.0f}", justify="right"),
                Text(f"{s.get('parallelism', 0):.0f}", justify="right"),
                Text(f"{s.get('shipping', 0):.0f}", justify="right"),
                Text(f"{s.get('breadth', 0):.0f}", justify="right"),
                Text(f"{s.get('velocity', 0):.0f}", justify="right"),
                Text(details, style="dim"),
            )

        if not scores:
            st.add_row(
                Text("No scored windows yet", style="dim"),
                "", "", "", "", "", "", "", "",
            )


class MCPStatsView(BaseMetricsView):
    _table_ids = ["#mcp-servers-table", "#mcp-actions-table"]

    def compose(self) -> ComposeResult:
        yield Static(id="mcp-header")
        with Horizontal(id="mcp-body"):
            yield DataTable(id="mcp-servers-table")
            yield DataTable(id="mcp-actions-table")

    def load_content(self):
        from token_watch_data import _get_mcp_stats
        stats = _get_mcp_stats(days=7)

        self.query_one("#mcp-header", Static).update(
            f"[bold]MCP Tool Usage — last 7 days[/bold]  "
            f"[dim]Total calls: {stats['total_calls']}  "
            f"Sessions using MCP: {stats['sessions_with_mcp']}[/dim]"
        )

        st = self.query_one("#mcp-servers-table", DataTable)
        st.cursor_type = "row"
        st.zebra_stripes = True
        st.add_column("Server", width=18)
        st.add_column("Calls", width=7)
        st.add_column("Top Actions")
        for s in stats["by_server"]:
            top3 = ", ".join(a for a, _ in s["actions"][:3])
            st.add_row(
                Text(s["server"], style="cyan"),
                Text(str(s["calls"]), justify="right"),
                Text(top3, style="dim"),
            )
        if not stats["by_server"]:
            st.add_row(Text("no MCP calls in last 7 days", style="dim"), "", "")

        at = self.query_one("#mcp-actions-table", DataTable)
        at.cursor_type = "row"
        at.zebra_stripes = True
        at.add_column("Action", width=40)
        at.add_column("Count", width=7)
        for action, count in stats["top_actions"]:
            server, _, act = action.partition(":")
            at.add_row(
                Text.from_markup(f"[cyan]{server}[/cyan][dim]:{act}[/dim]"),
                Text(str(count), justify="right"),
            )
        if not stats["top_actions"]:
            at.add_row(Text("no data", style="dim"), "")


class ForceQuitScreen(Screen):
    """Modal force-quit dialog mirroring macOS Force Quit Applications.

    Shows full session context (sid, account, project, directive, runtime, mem,
    used%, last activity) and warns about what cleanup will run. Requires extra
    typed confirmation for HIGH BURN sessions to prevent panic-misclicks.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "Force Quit"),
    ]

    def __init__(self, session, callback):
        # type: (dict, Any) -> None
        super().__init__()
        self._session = session
        self._callback = callback
        self._high_burn = session.get("health") == "red"

    def compose(self) -> ComposeResult:
        from textual.widgets import Input
        s = self._session
        sid = s.get("sid", "?")
        directive = (s.get("directive") or "—")
        if len(directive) > 60:
            directive = directive[:57] + "..."
        runtime = s.get("age", "?")
        mem = s.get("mem_mb", 0)
        delta = s.get("delta", "?")
        cpu = s.get("cpu", 0) or 0
        source = s.get("source", "?")
        repo = s.get("repo") or "—"
        health = s.get("health", "green")
        reason = s.get("health_reason", "") or ""
        dot = {
            "green": "[bold green]●[/bold green]",
            "yellow": "[bold yellow]●[/bold yellow]",
            "red": "[bold red]●[/bold red]",
        }.get(health, "[dim]●[/dim]")

        warn_color = "red" if self._high_burn else "yellow"
        body = (
            "[bold]Force Quit Session[/bold]\n\n"
            f"{dot} [cyan]{sid}[/cyan]    [dim]({reason})[/dim]\n"
            f"[dim]source:[/dim] {source}    [dim]repo:[/dim] {repo}\n\n"
            f"[dim]directive:[/dim] {directive}\n"
            f"[dim]runtime:  [/dim]{runtime}    [dim]mem:[/dim] {mem} MB    [dim]cpu:[/dim] {cpu:.0f}%\n"
            f"[dim]used:     [/dim]{delta}\n\n"
            f"[{warn_color}]⚠ This will:[/{warn_color}]\n"
            "  • SIGTERM (3s grace) → SIGKILL\n"
            "  • Release session_lock + file locks in Supabase\n"
            "  • Log force_quit event to build_ledger\n"
        )
        yield Static(body, id="fq-body")
        if self._high_burn:
            yield Static(
                "[bold red]HIGH BURN — type 'kill' and press Enter to confirm:[/bold red]",
                id="fq-warn",
            )
            yield Input(id="fq-confirm-input", placeholder="kill")
        yield Static(
            "[dim]Enter[/dim] = Force Quit    [dim]Esc[/dim] = Cancel",
            id="fq-footer",
        )

    def on_mount(self):
        if self._high_burn:
            from textual.widgets import Input
            try:
                self.query_one("#fq-confirm-input", Input).focus()
            except Exception:
                pass

    def on_input_submitted(self, event):
        if event.value.strip().lower() == "kill":
            self._fire()
        else:
            try:
                self.query_one("#fq-warn", Static).update(
                    "[bold red]Type exactly 'kill' to confirm[/bold red]"
                )
            except Exception:
                pass

    def action_confirm(self):
        # In HIGH BURN mode, Enter is consumed by the Input widget and routed
        # through on_input_submitted. This screen-level binding only fires for
        # normal sessions (no Input present).
        if self._high_burn:
            return
        self._fire()

    def action_cancel(self):
        self.app.pop_screen()

    def _fire(self):
        self.app.pop_screen()
        try:
            self._callback(self._session)
        except Exception as e:
            self.app.notify(f"Force quit failed: {e}", severity="error", timeout=4)


class BlockAssignScreen(Screen):
    """Modal for assigning a cycle item to a Pomodoro block (1-10)."""

    BINDINGS = [Binding("escape", "pop_screen", "Cancel")]

    def __init__(self, item_title, callback):
        # type: (str, Any) -> None
        super().__init__()
        self._item_title = item_title
        self._callback = callback

    def compose(self) -> ComposeResult:
        from textual.widgets import Input
        yield Static(
            f"Assign to Pomodoro block (1-10):\n[cyan]{self._item_title}[/cyan]",
            id="ba-prompt",
        )
        yield Input(id="ba-input", placeholder="Block number (1-10)")

    def on_mount(self):
        from textual.widgets import Input
        self.query_one("#ba-input", Input).focus()

    def on_input_submitted(self, event):
        try:
            num = int(event.value.strip())
            if 1 <= num <= 10:
                self._callback(num)
                self.app.pop_screen()
            else:
                self.query_one("#ba-prompt", Static).update("[red]Must be 1-10[/red]")
        except ValueError:
            self.query_one("#ba-prompt", Static).update("[red]Enter a number 1-10[/red]")

    def action_pop_screen(self):
        self.app.pop_screen()


class SessionTasksView(LazyView):
    """Cycle Monitor — freeform items for the current 5h window."""

    CAT_ICONS = {"bug": "\U0001f41b", "task": "\u2610", "idea": "\U0001f4a1", "direction": "\U0001f9ed"}
    STATUS_ICONS = {"open": "\u25cf", "done": "\u2713", "rolled": "\u2192"}
    CAT_ORDER = ["bug", "task", "idea", "direction"]
    PROJECTS = ["", "delphi", "kaa", "frank", "sage"]
    COMPANY_PROJECTS = {
        "delphi": ["atlas", "Atlas"],
        "kaa": ["kaa", "KAA"],
        "frank": ["frank", "Frank"],
        "sage": ["token-watch", "TW", "openclaw", "OClaw", "paperclip", "Paper", "battlestation"],
    }
    COMPANY_LABELS = {"delphi": "Delphi", "kaa": "KAA", "frank": "Frank", "sage": "SAGE"}
    PROJECT_LABELS = {"": "None", "atlas": "Atlas", "token-watch": "TW", "paperclip": "Paper",
                      "openclaw": "OClaw", "frank": "Frank", "kaa": "KAA"}

    BINDINGS = [
        Binding("n", "focus_add", "New"),
        Binding("enter", "edit_item", "Edit"),
        Binding("x", "toggle_done", "Done"),
        Binding("r", "roll_item", "Roll"),
        Binding("d", "delete_item", "Delete"),
        Binding("b", "assign_block", "Block"),
        Binding("slash", "start_filter", "Filter"),
        Binding("a", "show_all", "All"),
        Binding("i", "import_cycle_sessions", "Import sessions"),
    ]

    def compose(self) -> ComposeResult:
        from textual.widgets import Input
        yield Static(id="cm-header")
        yield Static(id="cm-objective")
        yield Static(id="cm-pomodoro")
        with Horizontal(id="cm-add-row"):
            yield Input(id="cm-add-input", placeholder="Add item... (Tab=cat, Shift+Tab=project, Enter=save)")
            yield Button("Task \u2610", id="cm-cat-task", classes="cm-cat", variant="primary")
            yield Button("Bug \U0001f41b", id="cm-cat-bug", classes="cm-cat", variant="default")
            yield Button("Idea \U0001f4a1", id="cm-cat-idea", classes="cm-cat", variant="default")
            yield Button("Dir \U0001f9ed", id="cm-cat-dir", classes="cm-cat", variant="default")
        with Horizontal(id="cm-project-row"):
            yield Button("All", id="cm-proj-all", classes="cm-proj", variant="primary")
            yield Button("None", id="cm-proj-none", classes="cm-proj")
            yield Button("Delphi", id="cm-proj-delphi", classes="cm-proj")
            yield Button("KAA", id="cm-proj-kaa", classes="cm-proj")
            yield Button("Frank", id="cm-proj-frank", classes="cm-proj")
            yield Button("SAGE", id="cm-proj-sage", classes="cm-proj")
        yield DataTable(id="cm-table")
        with Horizontal(id="cm-lanes-row"):
            yield Static(id="cm-lane-1")
            yield Static(id="cm-lane-2")
            yield Static(id="cm-lane-3")
        yield Static(id="cm-prev")

    def refresh_content(self):
        """Auto-refresh every 10s when this tab is visible."""
        now = time.time()
        if not hasattr(self, '_last_refresh') or (now - self._last_refresh) > 10:
            self._last_refresh = now
            if hasattr(self, '_items'):
                self._reload()

    def load_content(self):
        self._category = "task"
        self._project = None  # None = All (no filter), "" = None/unassigned
        self._items = []
        self._window_start = ""
        self._editing_id = None
        self._filtering = False
        self._filter_text = ""
        self._show_all_windows = False

        # Compute window_start from burndown data
        bd = _get_burndown_data()
        if bd and bd.get("window_start"):
            ws = bd["window_start"]
            if isinstance(ws, datetime):
                self._window_start = ws.isoformat()
            else:
                self._window_start = str(ws)
        else:
            # Fallback: compute from current time
            now_utc = datetime.now(timezone.utc)
            self._window_start = now_utc.isoformat()

        dt = self.query_one("#cm-table", DataTable)
        dt.cursor_type = "row"
        dt.zebra_stripes = True
        dt.add_column("Cat", width=5)
        dt.add_column("St", width=3)
        dt.add_column("Title", width=45)
        dt.add_column("Co", width=8)
        dt.add_column("Project", width=10)
        dt.add_column("Lane", width=6)
        dt.add_column("Age", width=6)

        self._reload()

    def action_import_cycle_sessions(self):
        from token_watch_data import _populate_cycle_from_sessions, _get_current_cycle
        cycle = _get_current_cycle()
        if cycle:
            count = _populate_cycle_from_sessions(cycle_id=cycle["cycle_id"])
            self._reload()
            self.notify(f"Imported {count} items from current cycle sessions")
        else:
            self.notify("No current cycle detected")

    @staticmethod
    def _fmt_age(created_at_str):
        """Format age of an item as short string."""
        try:
            created = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            mins = int((datetime.now(timezone.utc) - created).total_seconds() / 60)
            if mins < 60:
                return f"{mins}m"
            if mins < 1440:
                return f"{mins // 60}h"
            return f"{mins // 1440}d"
        except Exception:
            return ""

    def _reload(self):
        from token_watch_data import (
            _get_cycle_items, _get_recent_cycle_summaries, _get_current_cycle,
            _get_cycle_plan, _get_pomodoro_stats, _get_current_pomodoro,
            _format_cost,
        )

        # Load lane map from cycle plan
        plan = _get_cycle_plan(self._window_start) if self._window_start else None
        self._lane_map = plan.get("lanes", {}) if plan else {}

        # Fetch items
        self._items = _get_cycle_items(self._window_start, all_windows=self._show_all_windows)

        # Apply company filter (None = All/no filter, "" = unassigned, else = company key)
        display_items = self._items
        if self._project is not None and self._project in self.COMPANY_PROJECTS:
            allowed = [p.lower() for p in self.COMPANY_PROJECTS[self._project]]
            display_items = [
                i for i in display_items
                if (i.get("project") or "").lower() in allowed
            ]
        elif self._project == "":
            # "None" = show items not belonging to any company
            all_known = set()
            for projs in self.COMPANY_PROJECTS.values():
                all_known.update(p.lower() for p in projs)
            display_items = [
                i for i in display_items
                if (i.get("project") or "").lower() not in all_known
            ]

        # Apply text filter if active
        if self._filter_text:
            ft = self._filter_text.lower()
            display_items = [
                i for i in display_items
                if ft in (i.get("title") or "").lower()
                or ft in (i.get("project") or "").lower()
            ]

        # Rebuild table
        dt = self.query_one("#cm-table", DataTable)
        dt.clear()

        open_count = sum(1 for i in display_items if i.get("status") == "open")
        done_count = sum(1 for i in display_items if i.get("status") == "done")

        # Group by category
        groups = {}
        for item in display_items:
            cat = item.get("category", "task")
            groups.setdefault(cat, []).append(item)

        first_group = True
        for cat in self.CAT_ORDER:
            if cat not in groups:
                continue
            if not first_group:
                # Separator row
                dt.add_row(
                    Text("---", style="dim"),
                    Text("", style="dim"),
                    Text("", style="dim"),
                    Text("", style="dim"),
                    Text("", style="dim"),
                    Text("", style="dim"),
                    Text("", style="dim"),
                    key=f"sep-{cat}",
                )
            first_group = False
            for item in groups[cat]:
                cat_icon = self.CAT_ICONS.get(cat, "\u2610")
                status = item.get("status", "open")
                st_icon = self.STATUS_ICONS.get(status, "\u25cf")
                st_style = "green" if status == "done" else ("dim" if status == "rolled" else "")
                title = item.get("title", "")[:45]
                ref = item.get("source_ref", "")
                if ref:
                    short_ref = ref.split(":")[0][-8:] if ":" in ref else ref[-8:]
                    title = f"{title} [dim][{short_ref}][/dim]"
                project = item.get("project", "")[:12]
                # Derive company from project
                item_proj_lower = (item.get("project") or "").lower()
                company = ""
                for comp, projs in self.COMPANY_PROJECTS.items():
                    if item_proj_lower in [p.lower() for p in projs]:
                        company = self.COMPANY_LABELS.get(comp, comp)
                        break
                age = self._fmt_age(item.get("created_at", ""))

                # Auto-assign lane from cycle plan
                lane = ""
                if hasattr(self, "_lane_map") and self._lane_map:
                    item_title_lower = (item.get("title") or "").lower()
                    item_proj_lower = (item.get("project") or "").lower()
                    for lane_name, lane_info in self._lane_map.items():
                        lane_proj = (lane_info.get("project") or "").lower()
                        if lane_proj and lane_proj in item_proj_lower:
                            lane = lane_name[:6]
                            break
                        for task in lane_info.get("tasks", []):
                            if task.lower() in item_title_lower:
                                lane = lane_name[:6]
                                break
                        if lane:
                            break

                dt.add_row(
                    Text(cat_icon),
                    Text(st_icon, style=st_style),
                    Text(title, style="strike" if status == "done" else ""),
                    Text(company, style="magenta"),
                    Text(project, style="cyan"),
                    Text(lane, style="yellow"),
                    Text(age, style="dim"),
                    key=f"ci-{item['id']}",
                )

        if not display_items:
            dt.add_row(
                Text(""),
                Text(""),
                Text("No items yet — press n to add", style="dim"),
                Text(""),
                Text(""),
                Text(""),
                Text(""),
            )

        # Header
        bd = _get_burndown_data()
        mins_left = int(bd.get("mins_to_reset", 0)) if bd else 0
        hrs = mins_left // 60
        mins = mins_left % 60
        time_str = f"{hrs}h{mins:02d}m" if hrs else f"{mins}m"

        cycle = _get_current_cycle()
        stars = cycle.get("stars", "") if cycle else ""

        filter_str = f"  [yellow][filter: \"{self._filter_text}\"][/yellow]" if self._filter_text else ""
        mode_label = "[bold yellow]ALL CYCLES[/bold yellow]" if self._show_all_windows else f"resets in {time_str}  {stars}"
        self.query_one("#cm-header", Static).update(
            f"[bold]CYCLE MONITOR[/bold]  {mode_label}  "
            f"[green]{open_count} open[/green]  [dim]{done_count} done[/dim]{filter_str}  "
            f"[dim](n=add  /=filter  a=all  Enter=edit  x=done  r=roll  b=block  d=delete  q=back)[/dim]"
        )

        # Objective banner from cycle plan
        obj_text = ""
        plan = _get_cycle_plan(self._window_start) if self._window_start else None
        if plan:
            obj = plan.get("objective", "")
            process = plan.get("process", "")
            lanes = plan.get("lanes", {})
            if obj:
                parts = [f"[bold magenta]OBJ:[/bold magenta] [bold white]{obj}[/bold white]"]
                if process:
                    parts.append(f"  [dim]{process}[/dim]")
                if lanes:
                    lane_strs = [f"[cyan]{k}[/cyan]" for k in lanes]
                    parts.append(f"  Lanes: {' | '.join(lane_strs)}")
                obj_text = "\n".join(parts)
        self.query_one("#cm-objective", Static).update(obj_text)

        # Pomodoro block summary — show what got done in each block
        pomo_text = ""
        pomo_num = _get_current_pomodoro()
        if cycle and pomo_num:
            from token_watch_data import _get_cycle_sessions
            blocks = _get_pomodoro_stats(cycle["cycle_id"])
            all_sessions = _get_cycle_sessions(cycle["cycle_id"])

            if blocks:
                # Map session_id -> directive
                dir_map = {}
                for s in all_sessions:
                    sid = s.get("session_id", "")
                    d = s.get("directive", "") or ""
                    if sid and d:
                        dir_map[sid] = d

                # Map cycle items to block numbers by P-prefix
                import re
                item_by_block = {}  # type: dict
                for item in self._items:
                    title = item.get("title", "")
                    m = re.match(r"^P(\d+)", title)
                    if m:
                        bnum = int(m.group(1))
                        clean = re.sub(r"^P\d+[-:\s]*(?:FE|BE|QA)?[-:\s]*", "", title).strip()
                        if clean:
                            item_by_block.setdefault(bnum, []).append(
                                (clean[:30], item.get("status") == "done")
                            )

                lines = []
                for b in blocks:
                    bn = b["block_num"]
                    delta = abs(b["delta_pct"])

                    if b["is_current"]:
                        label = f"[bold cyan]\u25b8P{bn}[/bold cyan]"
                    elif b["is_future"]:
                        label = f"[dim]P{bn}[/dim]"
                    else:
                        color = "green" if delta <= 10 else ("yellow" if delta <= 15 else "red")
                        label = f"[{color}]P{bn}[/{color}]"

                    # Gather what happened: cycle items first, then session directives
                    things = []
                    if bn in item_by_block:
                        for title, done in item_by_block[bn]:
                            if done:
                                things.append(f"[green]\u2713{title}[/green]")
                            else:
                                things.append(title)
                    else:
                        # Fall back to unique session directives
                        seen = set()
                        for sid in b.get("session_ids", []):
                            d = dir_map.get(sid, "")
                            if d and d not in seen:
                                seen.add(d)
                                things.append(d[:25])

                    if b["is_future"]:
                        desc = "[dim]\u2500\u2500[/dim]"
                    elif not things and b["tool_calls"] == 0:
                        desc = "[dim]idle[/dim]"
                    elif things:
                        desc = "[dim], [/dim]".join(things[:2])
                        if len(things) > 2:
                            desc += f" [dim]+{len(things)-2}[/dim]"
                    else:
                        desc = f"[dim]{len(b['session_ids'])} sessions[/dim]"

                    pct = f" [dim]{delta:.0f}%[/dim]" if not b["is_future"] and delta > 0 else ""
                    lines.append(f" {label}{pct} {desc}")

                pomo_text = "[bold]POMODORO[/bold]\n" + "\n".join(lines)
        self.query_one("#cm-pomodoro", Static).update(pomo_text)

        # Lane visualization
        lane_widgets = ["#cm-lane-1", "#cm-lane-2", "#cm-lane-3"]
        if plan and plan.get("lanes"):
            lane_keys = list(plan["lanes"].keys())
            for idx, widget_id in enumerate(lane_widgets):
                if idx < len(lane_keys):
                    lane_name = lane_keys[idx]
                    lane_data = plan["lanes"][lane_name]
                    lane_tasks = lane_data.get("tasks", [])
                    # Match cycle items to lane tasks
                    lane_lines = [f"[bold cyan]{lane_name}[/bold cyan]"]
                    for task_title in lane_tasks:
                        # Check if this task is done in cycle items
                        done = any(
                            task_title.lower() in (i.get("title") or "").lower()
                            and i.get("status") == "done"
                            for i in self._items
                        )
                        icon = "[green]\u2713[/green]" if done else "[dim]\u25cb[/dim]"
                        lane_lines.append(f" {icon} {task_title[:30]}")
                    self.query_one(widget_id, Static).update("\n".join(lane_lines))
                else:
                    self.query_one(widget_id, Static).update("")
        else:
            for widget_id in lane_widgets:
                self.query_one(widget_id, Static).update("")

        # Previous cycles
        summaries = _get_recent_cycle_summaries(limit=3)
        lines = []
        for s in summaries:
            parts = [s.get("when_str", "?")]
            if s.get("stars"):
                parts.append(s["stars"])
            total = s.get("items_total", 0)
            done = s.get("items_done", 0)
            rolled = s.get("items_rolled", 0)
            detail = f"{total} items ({done} done"
            if rolled:
                detail += f", {rolled} rolled"
            detail += ")"
            parts.append(detail)
            projs = s.get("projects", [])
            if projs:
                parts.append(", ".join(projs))
            lines.append("  ".join(parts))
        self.query_one("#cm-prev", Static).update(
            "\n".join(lines) if lines else "[dim]No previous cycles[/dim]"
        )

    def _get_item_by_row_key(self, row_key_str):
        """Find item dict by row key string like 'ci-<uuid>'."""
        if not row_key_str or row_key_str.startswith("sep-"):
            return None
        item_id = row_key_str.removeprefix("ci-")
        for item in self._items:
            if str(item.get("id")) == item_id:
                return item
        return None

    def action_start_filter(self):
        from textual.widgets import Input
        self._filtering = True
        inp = self.query_one("#cm-add-input", Input)
        inp.value = self._filter_text
        inp.placeholder = "Filter items... (Enter=apply, Esc=clear)"
        inp.focus()

    def action_show_all(self):
        self._filtering = False
        self._filter_text = ""
        from textual.widgets import Input
        inp = self.query_one("#cm-add-input", Input)
        inp.placeholder = "Add item... (Tab=cat, Shift+Tab=project, Enter=save)"
        self._reload()
        self.query_one("#cm-table", DataTable).focus()

    def on_input_changed(self, event):
        from textual.widgets import Input
        if event.input != self.query_one("#cm-add-input", Input):
            return
        if self._filtering:
            self._filter_text = event.value.strip()
            self._reload()

    def action_focus_add(self):
        from textual.widgets import Input
        self.query_one("#cm-add-input", Input).focus()

    def _update_project_buttons(self):
        all_btn = self.query_one("#cm-proj-all", Button)
        all_btn.variant = "primary" if (self._project is None and not self._show_all_windows) or self._show_all_windows else "default"
        none_btn = self.query_one("#cm-proj-none", Button)
        none_btn.variant = "primary" if self._project == "" and not self._show_all_windows else "default"
        for company in ["delphi", "kaa", "frank", "sage"]:
            btn = self.query_one(f"#cm-proj-{company}", Button)
            btn.variant = "primary" if company == self._project else "default"

    def on_key(self, event):
        from textual.widgets import Input
        inp = self.query_one("#cm-add-input", Input)
        if not inp.has_focus:
            return
        if event.key == "escape" and self._filtering:
            event.prevent_default()
            event.stop()
            self._filtering = False
            self._filter_text = ""
            inp.value = ""
            inp.placeholder = "Add item... (Tab=cat, Shift+Tab=project, Enter=save)"
            self._reload()
            self.query_one("#cm-table", DataTable).focus()
            return
        if event.key == "tab":
            event.prevent_default()
            event.stop()
            cats = ["task", "bug", "idea", "direction"]
            idx = cats.index(self._category) if self._category in cats else 0
            self._category = cats[(idx + 1) % len(cats)]
            # Update button variants
            cat_map = {"task": "#cm-cat-task", "bug": "#cm-cat-bug",
                       "idea": "#cm-cat-idea", "direction": "#cm-cat-dir"}
            for cat, btn_id in cat_map.items():
                btn = self.query_one(btn_id, Button)
                btn.variant = "primary" if cat == self._category else "default"
        elif event.key == "shift+tab":
            event.prevent_default()
            event.stop()
            company_cycle = [None, "delphi", "kaa", "frank", "sage"]
            idx = company_cycle.index(self._project) if self._project in company_cycle else 0
            self._project = company_cycle[(idx + 1) % len(company_cycle)]
            self._update_project_buttons()

    def on_input_submitted(self, event):
        from textual.widgets import Input
        from token_watch_data import _post_cycle_item, _update_cycle_item
        inp = self.query_one("#cm-add-input", Input)
        if event.input != inp:
            return
        if self._filtering:
            self._filtering = False
            self._filter_text = event.value.strip()
            inp.placeholder = "Add item... (Tab=cat, Shift+Tab=project, Enter=save)"
            self._reload()
            self.query_one("#cm-table", DataTable).focus()
            return
        title = event.value.strip()
        if not title:
            return
        proj_map = {"delphi": "Atlas", "kaa": "KAA", "frank": "Frank", "sage": "TW"}
        store_proj = proj_map.get(self._project, self._project or "")
        if self._editing_id:
            _update_cycle_item(self._editing_id, {"title": title, "category": self._category, "project": store_proj})
            self._editing_id = None
        else:
            _post_cycle_item(self._window_start, self._category, title, project=store_proj)
        inp.value = ""
        self._reload()
        self.query_one("#cm-table", DataTable).focus()

    def on_button_pressed(self, event):
        cat_map = {
            "cm-cat-task": "task", "cm-cat-bug": "bug",
            "cm-cat-idea": "idea", "cm-cat-dir": "direction",
        }
        btn_id = event.button.id or ""
        if btn_id in cat_map:
            self._category = cat_map[btn_id]
            for bid, cat in cat_map.items():
                btn = self.query_one(f"#{bid}", Button)
                btn.variant = "primary" if cat == self._category else "default"
        elif btn_id == "cm-proj-all":
            if self._show_all_windows:
                self._show_all_windows = False
            self._project = None  # All = no filter
            self._update_project_buttons()
            self._reload()
        elif btn_id == "cm-proj-none":
            self._project = ""  # None = unassigned items
            self._show_all_windows = False
            self._update_project_buttons()
            self._reload()
        elif btn_id.startswith("cm-proj-"):
            proj_key = btn_id.removeprefix("cm-proj-")
            self._project = proj_key
            self._show_all_windows = False
            self._update_project_buttons()
            self._reload()

    def action_edit_item(self):
        from textual.widgets import Input
        dt = self.query_one("#cm-table", DataTable)
        if dt.cursor_row is None:
            return
        try:
            row_key_str = str(dt._row_order[dt.cursor_row])
        except Exception:
            return
        if row_key_str.startswith("sep-"):
            return
        item = self._get_item_by_row_key(row_key_str)
        if not item:
            return
        # Populate input with item's title
        inp = self.query_one("#cm-add-input", Input)
        inp.value = item.get("title", "")
        # Set category and update buttons
        self._category = item.get("category", "task")
        cat_map = {"task": "#cm-cat-task", "bug": "#cm-cat-bug",
                   "idea": "#cm-cat-idea", "direction": "#cm-cat-dir"}
        for cat, btn_id in cat_map.items():
            btn = self.query_one(btn_id, Button)
            btn.variant = "primary" if cat == self._category else "default"
        # Reverse-map project name to company key
        item_proj = (item.get("project") or "").lower()
        company = ""
        for comp, projs in self.COMPANY_PROJECTS.items():
            if item_proj in [p.lower() for p in projs]:
                company = comp
                break
        self._project = company
        self._update_project_buttons()
        # Store editing state
        self._editing_id = item["id"]
        # Focus input
        inp.focus()

    def on_data_table_row_selected(self, event):
        """Handle click/Enter on a row — open edit mode."""
        if event.data_table.id != "cm-table":
            return
        self.action_edit_item()

    def action_toggle_done(self):
        from token_watch_data import _update_cycle_item
        dt = self.query_one("#cm-table", DataTable)
        if dt.cursor_row is None:
            return
        try:
            row_key = dt.get_row_at(dt.cursor_row)
            row_key_str = str(dt._row_order[dt.cursor_row])
        except Exception:
            return
        item = self._get_item_by_row_key(row_key_str)
        if not item:
            return
        new_status = "open" if item.get("status") == "done" else "done"
        updates = {"status": new_status}
        if new_status == "done":
            updates["resolved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            updates["resolved_at"] = None
        _update_cycle_item(item["id"], updates)
        self._reload()

    def action_roll_item(self):
        from token_watch_data import _update_cycle_item
        dt = self.query_one("#cm-table", DataTable)
        if dt.cursor_row is None:
            return
        try:
            row_key_str = str(dt._row_order[dt.cursor_row])
        except Exception:
            return
        item = self._get_item_by_row_key(row_key_str)
        if not item:
            return
        _update_cycle_item(item["id"], {
            "status": "rolled",
            "resolved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        self._reload()

    def action_assign_block(self):
        from token_watch_data import _assign_item_to_pomodoro
        dt = self.query_one("#cm-table", DataTable)
        if dt.cursor_row is None:
            return
        try:
            row_key_str = str(dt._row_order[dt.cursor_row])
        except Exception:
            return
        item = self._get_item_by_row_key(row_key_str)
        if not item:
            return
        item_id = item["id"]
        item_title = item.get("title", "")
        view = self

        def _do_assign(block_num):
            # type: (int) -> None
            _assign_item_to_pomodoro(item_id, block_num)
            view._reload()
            view.notify(f"Assigned to P{block_num}")

        self.app.push_screen(BlockAssignScreen(item_title, _do_assign))

    def action_delete_item(self):
        from token_watch_data import _delete_cycle_item
        dt = self.query_one("#cm-table", DataTable)
        if dt.cursor_row is None:
            return
        try:
            row_key_str = str(dt._row_order[dt.cursor_row])
        except Exception:
            return
        item = self._get_item_by_row_key(row_key_str)
        if not item:
            return
        _delete_cycle_item(item["id"])
        self._reload()


class ProjectBoardView(LazyView):
    """Project Monitor — strategic task board."""

    _company_filter = ""
    _COMPANY_CYCLE = ["", "personal", "delphi", "kaa", "frank", "sage", "adinkra"]
    _sort_col: str = ""   # column label currently sorted by
    _sort_asc: bool = True

    BINDINGS = [
        Binding("p", "cycle_company", "Company"),
    ]

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        label = str(event.label)
        if self._sort_col == label:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = label
            self._sort_asc = True
        arrow = "↑" if self._sort_asc else "↓"
        self.notify(f"Sort by {label} {arrow}")
        self._last_refresh = 0
        self.refresh_content()

    def action_cycle_company(self):
        """Cycle company filter: all → personal → delphi → kaa → frank → sage → adinkra → all."""
        try:
            idx = self._COMPANY_CYCLE.index(self._company_filter)
        except ValueError:
            idx = -1
        self._company_filter = self._COMPANY_CYCLE[(idx + 1) % len(self._COMPANY_CYCLE)]
        label = self._company_filter or "all"
        self.notify(f"Company filter: {label}")
        self._last_refresh = 0
        self.refresh_content()

    def refresh_content(self):
        now = time.time()
        if not hasattr(self, '_last_refresh') or (now - self._last_refresh) > 30:
            self._last_refresh = now
            try:
                self.query_one("#pboard-table", DataTable).clear(columns=True)
                self.load_content()
            except Exception:
                pass

    def compose(self) -> ComposeResult:
        yield Static(id="pboard-header")
        yield Static(id="pboard-summary")
        yield DataTable(id="pboard-table")

    def load_content(self):
        from token_watch_data import _get_project_tasks
        tasks = _get_project_tasks()

        if self._company_filter:
            tasks = [t for t in tasks if (t.get("company") or "").lower() == self._company_filter]

        total = len(tasks)
        by_status = {}
        by_project = {}
        total_points = 0
        done_points = 0
        dispatch_ready = 0
        total_tokens_k = 0
        for t in tasks:
            s = t.get("status", "?")
            p = t.get("project", "?")
            pts = t.get("points") or 0
            by_status[s] = by_status.get(s, 0) + 1
            if p not in by_project:
                by_project[p] = {"ready": 0, "in_progress": 0, "built": 0, "blocked": 0}
            if s in by_project[p]:
                by_project[p][s] += 1
            total_points += pts
            if s == "built":
                done_points += pts
            if s == "ready" and t.get("dispatch_prompt") and (t.get("tier") or "auto") == "auto" and not t.get("blocked_by"):
                dispatch_ready += 1
            if s in ("ready", "in_progress"):
                total_tokens_k += t.get("est_tokens_k") or 0

        ready = by_status.get("ready", 0)
        in_prog = by_status.get("in_progress", 0)
        built = by_status.get("built", 0)
        blocked = by_status.get("blocked", 0)
        remaining_points = total_points - done_points

        company_label = f"  [bold green]co:{self._company_filter}[/bold green]" if self._company_filter else ""
        board_title = "Personal To-Do" if self._company_filter == "personal" else "Project Board"
        self.query_one("#pboard-header", Static).update(
            f"[bold]{board_title}[/bold]  "
            f"[yellow]{ready} ready[/yellow]  "
            f"[green]{in_prog} active[/green]  "
            f"[dim]{built} built  {blocked} blocked  {total} total[/dim]  "
            f"│  [bold magenta]{dispatch_ready} dispatchable[/bold magenta]  "
            f"[cyan]{remaining_points}pts left[/cyan]  "
            f"[magenta]~{total_tokens_k}kT queued[/magenta]{company_label}  "
            f"[dim italic]p=company[/dim italic]"
        )

        # Top panel: project summary
        summary_table = RichTable(show_header=True, show_edge=False, pad_edge=False, expand=True)
        summary_table.add_column("Co", style="dim")
        summary_table.add_column("Project", style="bold")
        summary_table.add_column("Rdy", style="yellow", justify="right")
        summary_table.add_column("Act", style="green", justify="right")
        summary_table.add_column("Blt", style="dim", justify="right")
        summary_table.add_column("Pts", style="cyan", justify="right")

        for proj in sorted(by_project.keys()):
            counts = by_project[proj]
            proj_companies = [t.get("company", "") for t in tasks if t.get("project") == proj and t.get("company")]
            company_val = max(set(proj_companies), key=proj_companies.count) if proj_companies else ""
            co_name, co_style = _project_to_company(proj, company_val)
            proj_pts = sum(t.get("points") or 0 for t in tasks
                          if t.get("project") == proj and t.get("status") in ("ready", "in_progress"))
            summary_table.add_row(
                Text(co_name, style=co_style),
                proj,
                str(counts["ready"]),
                str(counts["in_progress"]),
                str(counts["built"]),
                str(proj_pts) if proj_pts else "—",
            )

        self.query_one("#pboard-summary", Static).update(
            Panel(summary_table, title="[bold]Summary[/bold]", border_style="cyan")
        )

        # Bottom panel: task list (in_progress first, then ready)
        dt = self.query_one("#pboard-table", DataTable)
        dt.cursor_type = "row"
        dt.zebra_stripes = True
        dt.add_column("Created", width=12)
        dt.add_column("Sess", width=9)
        dt.add_column("Co", width=8)
        dt.add_column("Project", width=12)
        dt.add_column("#", width=5)
        dt.add_column("Pri", width=4)
        dt.add_column("Diff", width=5)
        dt.add_column("Pts", width=3)
        dt.add_column("Tier", width=5)
        dt.add_column("~kT", width=4)
        dt.add_column("St", width=12)
        dt.add_column("🚀", width=2)
        dt.add_column("Task", width=36)

        # Sort
        status_order = {"in_progress": 0, "ready": 1, "blocked": 2, "built": 3, "archived": 4}
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        col = self._sort_col
        rev = not self._sort_asc
        _col_key = {
            "Co":      lambda t: (
                            (t.get("company") or "").lower(),
                            (t.get("project") or "").lower(),
                            status_order.get(t.get("status", ""), 9),
                        ),
            "Project": lambda t: (
                            (t.get("project") or "").lower(),
                            status_order.get(t.get("status", ""), 9),
                        ),
            "#":       lambda t: t.get("id") or 0,
            "Pri":     lambda t: priority_order.get((t.get("priority") or "medium").lower(), 9),
            "Task":    lambda t: (t.get("task_name") or "").lower(),
            "Created": lambda t: t.get("created_at") or "",
            "St":      lambda t: status_order.get(t.get("status", ""), 9),
            "Pts":     lambda t: t.get("points") or 0,
            "~kT":     lambda t: t.get("est_tokens_k") or 0,
            "Sess":    lambda t: (t.get("created_by_session") or "").lower(),
        }
        if col and col in _col_key:
            # Column sort: single flat list, no status-based split
            sorted_tasks = sorted(tasks, key=_col_key[col], reverse=rev)
            shown = sorted_tasks
        else:
            sorted_tasks = sorted(tasks, key=lambda t: (
                status_order.get(t.get("status", ""), 9),
                priority_order.get((t.get("priority") or "medium").lower(), 9),
                t.get("build_order") or 9999,
            ))
            # Default: in_progress + ready + blocked first, then up to 10 built
            shown = [t for t in sorted_tasks if t.get("status") in ("in_progress", "ready", "blocked")]
            shown.extend([t for t in sorted_tasks if t.get("status") == "built"][:10])

        _pri_label = {"critical": "P0", "high": "P1", "medium": "P2", "low": "P3"}
        _pri_style = {"critical": "bold red", "high": "bold yellow", "medium": "cyan", "low": "dim"}
        _diff_label = {"quick": "⚡", "easy": "📝", "medium": "🔨", "complex": "⚙️", "major": "🏗️"}
        _diff_style = {"quick": "green", "easy": "blue", "medium": "yellow", "complex": "bold yellow", "major": "bold red"}
        _tier_style = {"auto": "green bold", "assisted": "yellow", "manual": "red"}

        for t in shown:
            tid = str(t.get("id", ""))
            status = t.get("status", "?")
            status_icon = {"in_progress": "●", "ready": "○", "blocked": "◼", "built": "✓", "archived": "—"}.get(status, "?")
            status_style = {"in_progress": "green bold", "ready": "yellow", "blocked": "red", "built": "dim"}.get(status, "")
            pri = (t.get("priority") or "medium").lower()
            diff = (t.get("difficulty") or "").lower()
            tier = (t.get("tier") or "auto").lower()
            pts = t.get("points")
            tok = t.get("est_tokens_k")
            has_prompt = bool(t.get("dispatch_prompt"))
            blocked = t.get("blocked_by")

            # Dispatch-ready: has prompt, is auto tier, not blocked
            dispatch_ready = has_prompt and tier == "auto" and not blocked
            dispatch_icon = "✓" if dispatch_ready else ("⊘" if blocked else "")
            dispatch_style = "green bold" if dispatch_ready else ("red" if blocked else "dim")

            created_at = t.get("created_at", "")
            created_display = created_at[5:16].replace("T", " ") if created_at else "—"
            company_raw = t.get("company", "")
            co_name, co_style = _project_to_company(t.get("project", ""), company_raw)
            session_raw = t.get("created_by_session", "")
            session_display = session_raw if session_raw else "—"

            dt.add_row(
                Text(created_display, style="dim"),
                Text(session_display[:9], style="dim"),
                Text(co_name[:8], style=co_style),
                Text(t.get("project", "—")[:12], style="cyan"),
                Text(tid, justify="right"),
                Text(_pri_label.get(pri, "—"), style=_pri_style.get(pri, "dim")),
                Text(_diff_label.get(diff, "—"), style=_diff_style.get(diff, "dim")),
                Text(str(pts) if pts else "—", justify="right", style="bold" if pts else "dim"),
                Text(tier[:4], style=_tier_style.get(tier, "dim")),
                Text(str(tok) if tok else "—", justify="right", style="magenta" if tok else "dim"),
                Text(f"{status_icon} {status}", style=status_style),
                Text(dispatch_icon, style=dispatch_style),
                Text((t.get("task_name") or "—")[:36]),
            )

        if not shown:
            dt.add_row(*[""] * 13)

        if any(t.get("status") == "built" for t in sorted_tasks):
            remaining_built = len([t for t in sorted_tasks if t.get("status") == "built"]) - 10
            if remaining_built > 0:
                row = ["", "", "", "", "", "", Text(f"... +{remaining_built} built", style="dim"), "", "", "", "", "", ""]
                dt.add_row(*row)



class AccountCapacityView(LazyView):
    """Full-screen multi-account capacity view (A / B / C)."""

    def refresh_content(self):
        now = time.time()
        if not hasattr(self, '_last_refresh') or (now - self._last_refresh) > 30:
            self._last_refresh = now
            self.load_content()

    def compose(self) -> ComposeResult:
        yield Static(id="cap-header")
        with Horizontal(id="cap-panels"):
            yield Static(id="cap-panel-a")
            yield Static(id="cap-panel-b")
            yield Static(id="cap-panel-c")
        yield Static(id="cap-guardian-events")
        yield Static(id="cap-footer")

    def load_content(self):
        accounts = get_account_capacity_display()

        # Header — highlight active account
        labels = []
        for a in accounts:
            color = {"A": "cyan", "B": "magenta", "C": "yellow"}.get(a["label"], "white")
            if a["is_active"]:
                labels.append("[bold {c}][ {l} ][/bold {c}]".format(c=color, l=a["label"]))
            else:
                labels.append("[dim]{l}[/dim]".format(l=a["label"]))

        self.query_one("#cap-header", Static).update(
            "[bold]Account Capacity[/bold]  {joined}".format(joined="  /  ".join(labels))
        )

        # Build each panel
        panel_ids = {"A": "#cap-panel-a", "B": "#cap-panel-b", "C": "#cap-panel-c"}
        colors = {"A": "cyan", "B": "magenta", "C": "yellow"}

        total_five = 0.0
        total_seven = 0.0
        healthy = 0

        for a in accounts:
            label = a["label"]
            color = colors.get(label, "white")
            panel_widget = self.query_one(panel_ids[label], Static)

            # Active indicator
            if a["is_active"]:
                lock_badge = " [bold red]LOCKED[/bold red]" if a.get("is_locked") else ""
                title_line = "[green]●[/green] [bold {c}]Account {l}[/bold {c}] [dim]({n})[/dim]{lock}".format(
                    c=color, l=label, n=a["name"], lock=lock_badge
                )
            else:
                lock_badge = " [bold red]LOCKED[/bold red]" if a.get("is_locked") else ""
                title_line = "[dim]○[/dim] [{c}]Account {l}[/{c}] [dim]({n})[/dim]{lock}".format(
                    c=color, l=label, n=a["name"], lock=lock_badge
                )

            # Lane
            lane_style = {"builder": "blue", "operator": "green", "overflow": "yellow"}.get(a["lane"], "dim")
            lane_line = "[dim]Lane:[/dim] [{s}]{v}[/{s}]".format(s=lane_style, v=a["lane"])

            # Repos
            repos = a.get("repos", [])
            if repos:
                repos_line = "[dim]Repos:[/dim] " + ", ".join(repos)
            else:
                repos_line = "[dim]Repos: any[/dim]"

            # Usage bars
            five_bar = self._bar(a["five_pct"])
            seven_bar = self._bar(a["seven_pct"])

            # Sparklines from capacity history
            from token_watch_data import _get_capacity_history
            acct_history = [h for h in _get_capacity_history(limit=100) if h.get("account") == label]
            five_vals = []
            seven_vals = []
            for h in acct_history:
                try:
                    five_vals.append(float(h.get("five_hour_used_pct", 0) or 0))
                except (ValueError, TypeError):
                    five_vals.append(0.0)
                try:
                    seven_vals.append(float(h.get("seven_day_used_pct", 0) or 0))
                except (ValueError, TypeError):
                    seven_vals.append(0.0)
            five_spark = self._sparkline(five_vals)
            seven_spark = self._sparkline(seven_vals)

            # Reset countdowns
            five_cd = _countdown(a["five_reset"]) if a["five_reset"] else "---"
            seven_cd = _countdown(a["seven_reset"]) if a["seven_reset"] else "---"

            # Data freshness
            age = a["snapshot_age_min"]
            if a["is_active"]:
                freshness = "[green]live[/green]"
            elif age < 0:
                freshness = "[dim]no data[/dim]"
            elif age < 2:
                freshness = "[green]<2m ago[/green]"
            elif age < 60:
                freshness = "[yellow]{:.0f}m ago[/yellow]".format(age)
            else:
                freshness = "[red]{:.0f}h ago[/red]".format(age / 60)

            # Track totals for footer
            try:
                total_five += float(a["five_pct"])
                total_seven += float(a["seven_pct"])
            except (ValueError, TypeError):
                pass
            try:
                if float(a["seven_pct"]) < 70:
                    healthy += 1
            except (ValueError, TypeError):
                pass

            # Compose the panel content
            lines = [
                title_line,
                lane_line,
                repos_line,
                "",
                "[bold]5h usage:[/bold]  " + five_bar,
                "[dim]  trend:[/dim]  " + five_spark,
                "[dim]  resets:[/dim] " + five_cd,
                "",
                "[bold]7d usage:[/bold]  " + seven_bar,
                "[dim]  trend:[/dim]  " + seven_spark,
                "[dim]  resets:[/dim] " + seven_cd,
                "",
                "[dim]data:[/dim] " + freshness,
            ]

            if a.get("is_locked"):
                border_style = "bold red"
            elif a["is_active"]:
                border_style = "bold " + color
            else:
                border_style = "dim"
            panel_widget.update(
                Panel(
                    "\n".join(lines),
                    title="[bold {c}]{l}[/bold {c}]".format(c=color, l=label),
                    border_style=border_style,
                    expand=True,
                )
            )

        # Guardian events
        from token_watch_data import _get_guardian_events
        events = _get_guardian_events(limit=10)
        if events:
            level_colors = {
                "WARN": "yellow",
                "LOCK": "red",
                "UNLOCK": "green",
                "SWITCH": "magenta",
                "CRITICAL": "bold red",
            }
            evt_lines = []
            for e in events:
                color = level_colors.get(e["level"], "dim")
                ts_short = e["ts"][11:19] if len(e["ts"]) >= 19 else e["ts"]
                evt_lines.append(
                    "[dim]{ts}[/dim] [{c}]{lvl:>8s}[/{c}]  {msg}".format(
                        ts=ts_short, c=color, lvl=e["level"], msg=e["message"]
                    )
                )
            self.query_one("#cap-guardian-events", Static).update(
                Panel(
                    "\n".join(evt_lines),
                    title="[bold]Guardian Events[/bold]",
                    border_style="dim",
                )
            )
        else:
            self.query_one("#cap-guardian-events", Static).update("")

        # Footer: capacity health summary + guardian state
        avg_five = total_five / 3
        avg_seven = total_seven / 3
        health_color = "green" if healthy >= 2 else ("yellow" if healthy >= 1 else "red")

        from token_watch_data import _get_guardian_state
        gstate = _get_guardian_state()
        last_run = gstate.get("last_run_min", -1)
        all_critical = gstate.get("all_critical", False)

        if last_run < 0:
            guard_str = "[dim]guardian: no data[/dim]"
        elif last_run < 6:
            guard_str = "[green]guardian: {:.0f}m ago[/green]".format(last_run)
        elif last_run < 15:
            guard_str = "[yellow]guardian: {:.0f}m ago[/yellow]".format(last_run)
        else:
            guard_str = "[red]guardian: {:.0f}m ago[/red]".format(last_run)

        level_parts = []
        for lbl in ("A", "B", "C"):
            acct_state = gstate.get(lbl, {})
            level = acct_state.get("level", 0)
            lbl_color = {"A": "cyan", "B": "magenta", "C": "yellow"}[lbl]
            if level >= 97:
                level_parts.append("[{c}]{l}[/{c}]:[red]{v}[/red]".format(c=lbl_color, l=lbl, v=level))
            elif level >= 90:
                level_parts.append("[{c}]{l}[/{c}]:[yellow]{v}[/yellow]".format(c=lbl_color, l=lbl, v=level))
            else:
                level_parts.append("[{c}]{l}[/{c}]:[green]ok[/green]".format(c=lbl_color, l=lbl))

        crit_str = "  [bold red]ALL CRITICAL[/bold red]" if all_critical else ""

        self.query_one("#cap-footer", Static).update(
            "[dim]Avg 5h: {five:.0f}%  Avg 7d: {seven:.0f}%  "
            "[/dim][{hc}]{h}/3 healthy[/{hc}]  |  "
            "{guard}  levels: {levels}{crit}".format(
                five=avg_five, seven=avg_seven, hc=health_color, h=healthy,
                guard=guard_str, levels=" ".join(level_parts), crit=crit_str,
            )
        )

    @staticmethod
    def _bar(pct_val, width=20):
        # type: (Any, int) -> str
        """Render a usage bar from a percentage value."""
        try:
            pct_f = float(pct_val)
            filled = int(pct_f * width / 100)
            bar_color = "green" if pct_f < 50 else ("yellow" if pct_f < 75 else "red")
            pct_display = "{:.1f}".format(pct_f) if pct_f != int(pct_f) else str(int(pct_f))
            return "[{c}]{f}{e}[/{c}] {p}%".format(
                c=bar_color,
                f="█" * filled,
                e="░" * (width - filled),
                p=pct_display,
            )
        except (ValueError, TypeError):
            return "[dim]{e}[/dim]  ---".format(e="░" * width)

    @staticmethod
    def _sparkline(values, width=20):
        # type: (list, int) -> str
        """Render an ASCII sparkline from percentage values (0-100)."""
        _SPARKS = " ▁▂▃▄▅▆▇█"
        if not values:
            return "[dim]no history[/dim]"
        # Take most recent `width` values, reverse to chronological (left=oldest, right=newest)
        recent = list(reversed(values[:width]))
        # Normalize against 100 (fixed scale for percentages)
        chars = []
        for v in recent:
            idx = int(v * 8 / 100) if v > 0 else 0
            chars.append(_SPARKS[min(idx, 8)])
        return "".join(chars)



# ── DataTable widgets (scrollable) ───────────────────────────────────────────


class SessionHistoryTable(DataTable):
    BORDER_TITLE = "Session History"
    BORDER_SUBTITLE = "Tab to focus · Enter to drill down · arrows to scroll"

    def on_mount(self):
        self.cursor_type = "row"
        self.zebra_stripes = False
        self.add_column("When", width=9)
        self.add_column("Session", width=10)
        self.add_column("Src", width=10)
        self.add_column("Co", width=8)
        self.add_column("Project", width=12)
        self.add_column("Mdl", width=10)
        self.add_column("Dur", width=7)
        self.add_column("~5h%", width=7)
        self.add_column("Out", width=6)
        self.add_column("Cost", width=7)
        self.add_column("Directive")

    def refresh_rows(self):
        sessions = _get_session_history()
        pid_map = _build_pid_map()
        active = _active_pids()

        # Per-session attribution (scaled to current window)
        attr_data = _get_token_attribution()
        attr_by_ccid = {}  # cc-PID -> attributed pct
        if attr_data:
            for _as in attr_data.get("sessions", []):
                attr_by_ccid[_as["session_id"]] = _as["pct_used"]

        call_map = _get_call_data_map()
        call_by_uuid = {}
        for uuid, pid in pid_map.items():
            if pid in call_map:
                call_by_uuid[uuid] = call_map[pid]

        # Filter to current 5h window
        _, _, five_reset_ts, _ = _current_pct()
        window_start = None
        if five_reset_ts:
            try:
                reset_dt = datetime.fromisoformat(five_reset_ts.replace("Z", "+00:00"))
                window_start = reset_dt - timedelta(hours=5)
            except Exception:
                pass
        if window_start:
            sessions = [s for s in sessions if s["last_ts"] >= window_start]

        n = len(sessions)
        self.border_title = f"Session History — {n}" if n else "Session History"

        # Get filter text from app
        filter_text = ""
        try:
            filter_text = self.app._filter_text
        except Exception:
            pass

        try:
            cur_row = self.cursor_row
            saved_y = self.scroll_y
        except Exception:
            cur_row = 0
            saved_y = 0

        self.clear()

        with _index_lock:
            _building = _index_building
        if not sessions:
            self.add_row(
                "...", "", "", "", "", "", "", "", "", "",
                Text("building index..." if _building else "no sessions in this window", style="dim"),
            )
            return

        today = datetime.now(timezone.utc).astimezone().date()
        yesterday = today - timedelta(days=1)
        current_group = None

        for s in sessions:
            date = s["date"]
            if date == today:
                group = "Today"
            elif date == yesterday:
                group = "Yesterday"
            else:
                group = date.strftime("%b %-d")

            if group != current_group:
                self.add_row("", "", "", "", "", "", "", "", "", "", Text(f"— {group} —", style="bold dim"), key=f"sep-{group}")
                current_group = group

            when_str = s["last_ts"].astimezone().strftime("%H:%M:%S")

            # Show cc-PID if we can match, otherwise short UUID
            session_display = pid_map.get(s["session_id"], s["session_id"][:10])
            is_active = s["session_id"] in pid_map and pid_map[s["session_id"]] in active

            # Apply search filter
            if filter_text:
                searchable = " ".join([
                    session_display, s.get("source", ""),
                    s.get("project", ""), s.get("directive", ""),
                    s["session_id"],
                ]).lower()
                if filter_text not in searchable:
                    continue

            mdl = _abbrev_model(s.get("model", ""))
            mdl_style = "magenta" if mdl == "opus" else ("cyan" if mdl == "sonnet" else "dim")

            # Use per-session attribution instead of raw window delta
            ccid = pid_map.get(s["session_id"])
            ccid_str = f"cc-{ccid}" if ccid else ""
            attr_pct = attr_by_ccid.get(ccid_str) or attr_by_ccid.get(s["session_id"])
            if attr_pct is not None and attr_pct > 0:
                pct = f"+{attr_pct:.1f}%"
            else:
                pct = s["pct_str"]
            if pct == "—":
                pct_style = "dim"
            else:
                try:
                    v = float(pct.strip("+%↻"))
                    pct_style = "red" if v > 10 else ("yellow" if v > 5 else "green")
                except Exception:
                    pct_style = "dim"

            out_k = s["output_tokens"]
            out_str = f"{out_k/1000:.1f}k" if out_k >= 1000 else str(out_k)

            directive = (s["directive"] or "—")[:60]
            project = s.get("project", "—")

            src = s.get("source", "?")
            src_style = "yellow" if ("/" in src or src == "paperclip") else ("green" if src == "cli" else ("cyan" if "atlas" in src else "dim"))

            if "/" in src:
                co_name = src.split("/", 1)[0]
                co_style = "yellow"
                project = src.split("/", 1)[1]
            else:
                co_name, co_style = _project_to_company(project)

            cost = _estimate_cost(out_k, s.get("model", ""))
            cost_str = _format_cost(cost)
            cost_style = "red" if cost >= 2.0 else ("yellow" if cost >= 0.50 else "green")

            dot = "[bold green]● [/bold green]" if is_active else "  "
            self.add_row(
                Text(when_str, style="dim"),
                Text.from_markup(f"{dot}[cyan]{session_display}[/cyan]"),
                Text(src, style=src_style),
                Text(co_name, style=co_style),
                Text(project, style="dim"),
                Text(mdl, style=mdl_style),
                Text(s["dur_str"], style="dim"),
                Text(pct, style=pct_style),
                Text(out_str, style="dim", justify="right"),
                Text(cost_str, style=cost_style),
                directive,
                key=s["session_id"],
            )

            # Sub-row: tool call summary (matches Active Sessions sub-row style)
            cd = call_by_uuid.get(s["session_id"])
            if cd:
                calls_str = f"{cd['calls']} calls"
                tools_detail = cd['tools_str'][:30]
                last_tool = cd.get('recent_str', '')[:30]
            else:
                calls_str = ""
                tools_detail = ""
                last_tool = ""

            self.add_row(
                Text(""),
                Text(""),
                Text(""),
                Text(""),
                Text(""),
                Text(calls_str, style="dim italic"),
                Text(""),
                Text(""),
                Text(""),
                Text(tools_detail, style="dim italic"),
                Text(last_tool, style="dim italic"),
                key=f"sub-{s['session_id']}",
            )

        try:
            if cur_row < self.row_count:
                self.move_cursor(row=cur_row, scroll=False)
        except Exception:
            pass
        try:
            self.scroll_to(y=saved_y, animate=False)
        except Exception:
            pass

    def on_data_table_row_selected(self, event):
        key = event.row_key
        if key and key.value and not key.value.startswith("sep-") and not key.value.startswith("sub-"):
            session_id = key.value
            # Find directive from index
            sessions = _get_session_history()
            directive = "—"
            project = "—"
            for s in sessions:
                if s["session_id"] == session_id:
                    directive = s.get("directive", "—")
                    project = s.get("project", "—")
                    break
            self.app.push_screen(SessionDrillDown(session_id, directive, project))


class CallHistoryTable(DataTable):
    BORDER_TITLE = "Call History"
    BORDER_SUBTITLE = "Tab to focus"

    def on_mount(self):
        self.cursor_type = "row"
        self.zebra_stripes = False
        self.add_column("When", width=9)
        self.add_column("Session", width=10)
        self.add_column("Src", width=10)
        self.add_column("Co", width=8)
        self.add_column("Project", width=12)
        self.add_column("Mdl", width=10)
        self.add_column("#", width=4)
        self.add_column("Tools", width=20)
        self.add_column("Last Tool", width=22)
        self.add_column("5h%", width=7)
        self.add_column("Directive")

    def refresh_rows(self):
        history = _get_call_history()
        active = _active_pids()

        try:
            cur_row = self.cursor_row
            saved_y = self.scroll_y
        except Exception:
            cur_row = 0
            saved_y = 0

        self.clear()

        if not history:
            self.add_row("...", "", "", "", "", "", "", "", "", "", Text("no data", style="dim"))
            return

        today = datetime.now(timezone.utc).astimezone().date()
        current_group = None

        for h in history:
            date = h.get("when_date")
            if date == today:
                group = "Today"
            elif date:
                group = date.strftime("%b %-d")
            else:
                group = "Unknown"

            if group != current_group:
                sep = f"— {group} —"
                self.add_row(Text(sep, style="dim"), "", "", "", "", "", "", "", "", "", "", key=f"ch-sep-{group}")
                current_group = group

            src = h["source"]
            src_style = "yellow" if ("/" in src or src == "paperclip") else ("green" if src == "cli" else "dim")

            mdl = h.get("model", "?")
            mdl_style = "magenta" if "opus" in mdl else ("cyan" if "sonnet" in mdl else "dim")

            pct = h["pct_str"]
            try:
                v = float(pct.strip("+%"))
                pct_style = "red" if v > 5 else ("yellow" if v > 2 else "green")
            except Exception:
                pct_style = "dim"

            # Green dot for active sessions
            sid = h["session"]
            dot = "[bold green]● [/bold green]" if sid in active else "  "

            project = h.get("project", "—")
            if "/" in src:
                co_name = src.split("/", 1)[0]
                co_style = "yellow"
                project = src.split("/", 1)[1]
            else:
                co_name, co_style = _project_to_company(project)

            self.add_row(
                Text(h["when"], style="dim"),
                Text.from_markup(f"{dot}[cyan]{sid[:10]}[/cyan]"),
                Text(src, style=src_style),
                Text(co_name, style=co_style),
                Text(project, style="dim"),
                Text(mdl, style=mdl_style),
                Text(str(h["calls"]), justify="right"),
                Text(h["tools_str"][:20], style="dim"),
                Text(h.get("recent_str", "—")[:22]),
                Text(pct, style=pct_style),
                Text((h["directive"] or "—")[:40]),
                key=f"ch-{h['session']}",
            )

            # Sub-row: recent tool detail
            recent = h.get("recent_str", "")
            tools = h.get("tools_str", "")
            self.add_row(
                Text(""), Text(""), Text(""), Text(""), Text(""),
                Text(""),
                Text(""),
                Text(tools[:20], style="dim italic") if tools else Text(""),
                Text(recent[:22], style="dim italic") if recent else Text(""),
                Text(""),
                Text(""),
                key=f"chsub-{h['session']}",
            )

        try:
            if cur_row < self.row_count:
                self.move_cursor(row=cur_row, scroll=False)
        except Exception:
            pass
        try:
            self.scroll_to(y=saved_y, animate=False)
        except Exception:
            pass


# ── Nav Bar ──────────────────────────────────────────────────────────────────


class LeaderboardView(LazyView):
    """Multiplayer leaderboard — team competition on window scores."""

    def refresh_content(self):
        now = time.time()
        if not hasattr(self, '_last_refresh') or (now - self._last_refresh) > 30:
            self._last_refresh = now
            try:
                self.query_one("#lb-table", DataTable).clear(columns=True)
                self.load_content()
            except Exception:
                pass

    def compose(self) -> ComposeResult:
        yield Static(id="lb-header")
        yield DataTable(id="lb-table")

    def load_content(self):
        from token_watch_data import _get_leaderboard, _get_battlestation_config
        config = _get_battlestation_config()
        my_id = config.get("user_id", "")
        lb = _get_leaderboard(days=7)

        total_windows = sum(u.get("windows", 0) for u in lb)
        self.query_one("#lb-header", Static).update(
            f"[bold]Leaderboard — Last 7 Days[/bold]  "
            f"[dim]{len(lb)} users  {total_windows} windows[/dim]"
        )

        dt = self.query_one("#lb-table", DataTable)
        dt.cursor_type = "row"
        dt.zebra_stripes = True
        dt.add_column("#", width=4)
        dt.add_column("User", width=18)
        dt.add_column("Avg Stars", width=10)
        dt.add_column("Avg", width=5)
        dt.add_column("Windows", width=8)
        dt.add_column("Best", width=8)
        dt.add_column("Burn", width=6)
        dt.add_column("Ship", width=6)
        dt.add_column("Velocity", width=9)
        dt.add_column("Streak")

        for rank, u in enumerate(lb, 1):
            is_me = u["user_id"] == my_id
            ov = u["avg_overall"]
            ov_color = "green" if ov >= 4 else ("yellow" if ov >= 3 else "red")

            def _sc(v):
                return "green" if v >= 4 else ("yellow" if v >= 2.5 else "red")

            name_style = "bold cyan" if is_me else ""
            dot = "[bold green]● [/bold green]" if is_me else "  "
            streak = u.get("streak", 0)
            streak_str = f"🔥{streak}" if streak >= 3 else str(streak)

            dt.add_row(
                Text(str(rank), justify="right"),
                Text.from_markup(f"{dot}[{name_style}]{u['display_name']}[/{name_style}]") if name_style else Text.from_markup(f"{dot}{u['display_name']}"),
                Text(u["avg_stars"], style=ov_color),
                Text(f"{ov}", style=ov_color, justify="right"),
                Text(str(u["windows"]), justify="right"),
                Text(u.get("best_stars", ""), style="dim"),
                Text(f"{u['avg_burn']}", style=_sc(u['avg_burn']), justify="right"),
                Text(f"{u['avg_ship']}", style=_sc(u['avg_ship']), justify="right"),
                Text(f"{u['avg_velocity']}", style=_sc(u['avg_velocity']), justify="right"),
                Text(streak_str),
            )

        if not lb:
            dt.add_row(
                "", Text("No scores yet — complete a 5h window to appear here", style="dim"),
                "", "", "", "", "", "", "", "",
            )


# ── Employee / Team Dashboard ────────────────────────────────────────────────


class ForgeCommsPanel(Static):
    """Live forge peer comms status — green/yellow/red dots per session."""

    DEFAULT_CSS = """
    ForgeCommsPanel {
        height: auto;
        margin-bottom: 1;
    }
    """

    def refresh_panel(self, fleet_sessions):
        # Filter to peers we care about: forges, advisor, worker
        peers = []
        for s in fleet_sessions or []:
            role = (s.get("role") or "").lower()
            if role.startswith("forge") or role == "advisor" or role == "worker":
                peers.append(s)

        # Pull wire reliability for this session (cc-PPID is default sender)
        try:
            wire = _get_wire_reliability() or {}
        except Exception:
            wire = {}
        wire_dict = {}
        for rec in wire.get("by_recipient", []) or []:
            to_sid = rec.get("to")
            if to_sid:
                wire_dict[to_sid] = rec

        table = RichTable(
            show_header=True,
            header_style="bold",
            box=None,
            padding=(0, 1),
            expand=False,
        )
        table.add_column("●", width=2, no_wrap=True)
        table.add_column("Session", width=14, no_wrap=True)
        table.add_column("Role", width=15, no_wrap=True)
        table.add_column("Task", width=42, no_wrap=True)
        table.add_column("Hb", width=7, no_wrap=True)
        table.add_column("Wire", width=24, no_wrap=True)

        active_count = 0
        for p in peers:
            hb_age = p.get("hb_age_s")
            is_stale = p.get("is_stale", True)
            w = wire_dict.get(p.get("session_id"), {})
            sent = int(w.get("sent", 0) or 0)
            read = int(w.get("read", 0) or 0)
            unread = int(w.get("unread", 0) or 0)
            likely_dead = bool(w.get("likely_dead", False))

            # Dot color logic
            if hb_age is None:
                dot = "[dim]○[/dim]"
            elif (hb_age >= 300) or is_stale or likely_dead:
                dot = "[red]●[/red]"
            elif hb_age < 120:
                if sent == 0:
                    dot = "[green]●[/green]"
                elif unread == 0:
                    dot = "[green]●[/green]"
                else:
                    dot = "[yellow]●[/yellow]"
            else:
                # 120s <= hb_age < 300s
                dot = "[yellow]●[/yellow]"

            if dot.startswith("[green]") or dot.startswith("[yellow]"):
                active_count += 1

            # Heartbeat string
            if hb_age is None:
                hb_str = "—"
            elif is_stale:
                hb_str = "stale"
            elif hb_age < 60:
                hb_str = f"{hb_age}s"
            elif hb_age < 3600:
                hb_str = f"{hb_age//60}m"
            else:
                hb_str = f"{hb_age//3600}h"

            # Wire summary
            if sent == 0:
                wire_str = "—"
            else:
                status = "read" if unread == 0 else (
                    f"{unread}/{sent} unread"
                )
                if likely_dead:
                    wire_str = f"[red]{sent} sent, 0 read[/red]"
                else:
                    wire_str = f"{sent} sent · {status}"

            task = (p.get("task") or "—")[:40]
            role = (p.get("role") or "worker")[:15]
            session_id = (p.get("session_id") or "?")[:14]

            table.add_row(
                Text.from_markup(dot),
                Text(session_id, style="dim" if is_stale else ""),
                Text(role, style="dim" if is_stale else ""),
                Text(task, style="dim" if is_stale else "", overflow="ellipsis"),
                Text(hb_str, style="dim" if is_stale else ""),
                Text.from_markup(wire_str),
            )

        if not peers:
            table.add_row(
                Text.from_markup("[dim]○[/dim]"),
                Text("—"),
                Text("no peers", style="dim"),
                Text(""),
                Text(""),
                Text(""),
            )

        title = f"Forge Comms — {active_count} active peers"
        self.update(Panel(table, title=title, border_style="blue"))


# ===================================================================
# Forge Handouts Feed — persistent agent decision surface
# ===================================================================

FORGE_HANDOUT_TYPE_COLORS = {
    "dispatch":     "cyan",
    "blocker":      "red",
    "nextsteps":    "yellow",
    "opus-verdict": "green",
    "directive":    "magenta",
}

FORGE_HANDOUT_PRIORITY_STYLES = {
    "low":      "dim",
    "normal":   "",
    "high":     "bold",
    "critical": "bold reverse",
}

FORGE_ROLE_SHORT = {
    "forge-prime":    "PRIME",
    "forge-bugs":     "BUGS",
    "forge-audit":    "AUDIT",
    "forge-research": "RSRCH",
    "forge-ops":      "OPS",
    "advisor":        "ADV",
    "unknown":        "?",
}


def _fetch_handouts(limit: int = 50, max_age_hours: int = 24) -> list:
    """Fetch recent handouts from Supabase. Returns list of dicts, newest first."""
    import os
    try:
        import requests as _req
    except ImportError:
        return []
    from datetime import datetime, timezone, timedelta

    url = os.environ.get("SUPA_URL") or os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPA_KEY") or os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    params = {
        "select": "id,session_id,forge_role,handout_type,priority,title,body,project,created_at,seen_at",
        "order":  "created_at.desc",
        "limit":  str(limit),
        "created_at": f"gte.{cutoff}",
    }
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    try:
        r = _req.get(f"{url}/rest/v1/forge_handouts",
                     params=params, headers=headers, timeout=5)
        if r.status_code == 200:
            return r.json() or []
    except Exception:
        pass
    return []


def _mark_handout_seen(handout_id: int) -> bool:
    """PATCH seen_at=now() for one handout."""
    import os
    try:
        import requests as _req
    except ImportError:
        return False
    from datetime import datetime, timezone
    url = os.environ.get("SUPA_URL") or os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPA_KEY") or os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        return False
    try:
        _req.patch(
            f"{url}/rest/v1/forge_handouts",
            params={"id": f"eq.{handout_id}"},
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json={"seen_at": datetime.now(timezone.utc).isoformat()},
            timeout=3,
        )
        return True
    except Exception:
        return False


def _handout_relative_time(iso_ts: str) -> str:
    """'2026-04-15T12:03:11Z' -> '3m ago'."""
    from datetime import datetime, timezone
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        s = int(delta.total_seconds())
        if s < 60:   return f"{s}s ago"
        if s < 3600: return f"{s // 60}m ago"
        if s < 86400: return f"{s // 3600}h ago"
        return f"{s // 86400}d ago"
    except Exception:
        return iso_ts[:16] if iso_ts else ""


class HandoutsTable(DataTable):
    """DataTable of forge handouts. Cursor-selectable rows."""

    def __init__(self, **kwargs):
        super().__init__(cursor_type="row", zebra_stripes=True, **kwargs)
        self._row_to_handout: dict = {}

    def on_mount(self) -> None:
        self.add_columns("Time", "Forge", "Type", "Pri", "Title", "Project")

    def load_rows(self, handouts: list) -> None:
        self.clear()
        self._row_to_handout.clear()
        for h in handouts:
            htype  = h.get("handout_type", "?")
            prio   = h.get("priority", "normal")
            color  = FORGE_HANDOUT_TYPE_COLORS.get(htype, "white")
            style  = FORGE_HANDOUT_PRIORITY_STYLES.get(prio, "")
            role_s = FORGE_ROLE_SHORT.get(h.get("forge_role", ""), h.get("forge_role", "?")[:6])
            unread = "" if h.get("seen_at") else "●"
            title  = h.get("title", "")
            title_styled = Text(f"{unread} {title}".strip(), style=style or "")
            type_styled  = Text(htype, style=f"{color}")
            row_key = self.add_row(
                _handout_relative_time(h.get("created_at", "")),
                role_s,
                type_styled,
                prio[:4],
                title_styled,
                h.get("project", ""),
            )
            self._row_to_handout[str(row_key)] = h

    def get_selected_handout(self) -> dict:
        try:
            if self.cursor_row is None or self.cursor_row < 0:
                return {}
            row_key = self.coordinate_to_cell_key((self.cursor_row, 0)).row_key.value
            return self._row_to_handout.get(str(row_key), {})
        except Exception:
            return {}


class ForgeHandoutsView(LazyView):
    """Operations > Feed tab — persistent Forge decision surface."""

    DEFAULT_CSS = """
    ForgeHandoutsView {
        layout: vertical;
    }
    ForgeHandoutsView HandoutsTable {
        height: 60%;
    }
    ForgeHandoutsView #handout-detail {
        height: 1fr;
        border: solid $accent;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("m", "mark_seen", "Mark seen"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._table = None
        self._detail = None
        self._interval_handle = None

    def compose(self):
        yield HandoutsTable(id="handouts-table")
        yield RichLog(id="handout-detail", wrap=True, highlight=True, markup=True)

    def load_content(self) -> None:
        self._table  = self.query_one("#handouts-table", HandoutsTable)
        self._detail = self.query_one("#handout-detail", RichLog)
        self._detail.write("[dim]Select a row to view full body.[/dim]")
        self._do_refresh()
        if self._interval_handle is None:
            self._interval_handle = self.set_interval(15.0, self._do_refresh)

    def refresh_content(self) -> None:
        self._do_refresh()

    def _do_refresh(self) -> None:
        import threading as _threading
        def work():
            rows = _fetch_handouts(limit=50, max_age_hours=24)
            self.app.call_from_thread(self._apply_rows, rows)
        _threading.Thread(target=work, daemon=True).start()

    def _apply_rows(self, rows: list) -> None:
        if self._table is not None:
            self._table.load_rows(rows)

    def action_refresh(self) -> None:
        self._do_refresh()

    def action_mark_seen(self) -> None:
        if self._table is None:
            return
        h = self._table.get_selected_handout()
        hid = h.get("id")
        if hid is not None:
            import threading as _threading
            _threading.Thread(
                target=lambda: _mark_handout_seen(int(hid)),
                daemon=True,
            ).start()
            h["seen_at"] = "local"
            self._do_refresh()

    def on_data_table_row_highlighted(self, event) -> None:
        self._show_detail()

    def _show_detail(self) -> None:
        if self._table is None or self._detail is None:
            return
        h = self._table.get_selected_handout()
        if not h:
            return
        self._detail.clear()
        htype = h.get("handout_type", "?")
        color = FORGE_HANDOUT_TYPE_COLORS.get(htype, "white")
        self._detail.write(
            f"[bold {color}]{htype.upper()}[/] · [bold]{h.get('title','')}[/]"
        )
        self._detail.write(
            f"[dim]{h.get('forge_role','?')} · "
            f"{h.get('project','?')} · "
            f"prio={h.get('priority','normal')} · "
            f"{_handout_relative_time(h.get('created_at',''))} · "
            f"session={h.get('session_id','?')}[/]"
        )
        self._detail.write("")
        body = h.get("body") or "[dim](no body)[/dim]"
        self._detail.write(body)


# ===================================================================
# Forge Status Dashboard — per-forge cards with progress + plan context
# ===================================================================

def _fetch_forge_status() -> list:
    """One dict per active forge, merged from session_locks + forge_handouts."""
    import os, re
    try:
        import requests as _req
    except ImportError:
        return []
    from datetime import datetime, timezone

    url = os.environ.get("SUPA_URL") or os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPA_KEY") or os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        return []
    hdrs = {"apikey": key, "Authorization": f"Bearer {key}"}

    def _age(ts: str) -> float:
        if not ts:
            return 9999.0
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - t).total_seconds()
        except Exception:
            return 9999.0

    def _humanize(secs: float) -> str:
        s = int(secs)
        if s < 60:   return f"{s}s"
        if s < 3600: return f"{s//60}m"
        return f"{s//3600}h"

    def _parse_status(body: str) -> dict:
        if not body:
            return {}
        def grab(field):
            m = re.search(rf"^\*\*{field}:\*\*\s*(.+?)\s*$", body, re.MULTILINE)
            return m.group(1).strip() if m else ""
        progress = grab("Progress").split()[0] if grab("Progress") else "0/0/0"
        parts = progress.split("/")
        done, rem, inf = (int(parts[0]), int(parts[1]), int(parts[2])) if len(parts) == 3 else (0,0,0)
        return {
            "progress": progress,
            "done": done, "remaining": rem, "in_flight": inf,
            "branch": grab("Branch"),
            "plan_ref": grab("Plan"),
            "blockers": grab("Blockers"),
            "eta": grab("ETA"),
            "confidence": grab("Confidence"),
            "body": body,
        }

    try:
        r = _req.get(f"{url}/rest/v1/session_locks",
                     params={"status": "eq.active", "role": "like.forge-%",
                             "select": "session_id,role,task_name,heartbeat_at,files_touched,notes",
                             "order": "role"},
                     headers=hdrs, timeout=5)
        locks = r.json() if r.status_code == 200 else []
    except Exception:
        return []

    if not locks:
        return []

    sids = [l["session_id"] for l in locks]
    sid_filter = ",".join(sids)

    try:
        r2 = _req.get(f"{url}/rest/v1/forge_handouts",
                      params={"session_id": f"in.({sid_filter})",
                              "select": "session_id,forge_role,handout_type,title,body,created_at",
                              "order": "created_at.desc", "limit": "60"},
                      headers=hdrs, timeout=5)
        handouts = r2.json() if r2.status_code == 200 else []
    except Exception:
        handouts = []

    latest_any: dict = {}
    latest_status: dict = {}
    for h in handouts:
        sid = h["session_id"]
        if sid not in latest_any:
            latest_any[sid] = h
        if h["handout_type"] == "status" and sid not in latest_status:
            latest_status[sid] = h

    now = datetime.now(timezone.utc)
    out = []
    for l in locks:
        sid = l["session_id"]
        age = _age(l.get("heartbeat_at", ""))
        health = "green" if age < 300 else "yellow" if age < 900 else "red"
        lh = latest_any.get(sid)
        ls = latest_status.get(sid)
        sp = _parse_status(ls["body"]) if ls else {}
        lh_age = _age(lh["created_at"]) if lh else 9999.0

        out.append({
            "session_id": sid,
            "short_id": sid.replace("cc-", "")[:8],
            "role": (l.get("role") or "").replace("forge-", "").upper()[:8],
            "task_name": l.get("task_name") or "(idle)",
            "heartbeat_age_s": int(age),
            "health": health,
            "health_dot": {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(health, "⚪"),
            "files_touched": l.get("files_touched") or [],
            "latest_handout": {"type": lh["handout_type"], "title": lh["title"],
                               "age": _humanize(lh_age)} if lh else None,
            "latest_status": sp,
            "is_blocked": bool(lh and lh["handout_type"] == "blocker"),
        })
    return out


def _fetch_forge_detail(session_id: str) -> dict:
    """Last 5 handouts + lock row for detail pane."""
    import os
    try:
        import requests as _req
    except ImportError:
        return {}
    url = os.environ.get("SUPA_URL") or os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPA_KEY") or os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        return {}
    hdrs = {"apikey": key, "Authorization": f"Bearer {key}"}
    try:
        r1 = _req.get(f"{url}/rest/v1/forge_handouts",
                      params={"session_id": f"eq.{session_id}",
                              "select": "handout_type,title,body,created_at,priority",
                              "order": "created_at.desc", "limit": "5"},
                      headers=hdrs, timeout=5)
        handouts = r1.json() if r1.status_code == 200 else []
    except Exception:
        handouts = []
    return {"handouts": handouts}


class ForgeCardsTable(DataTable):
    """Top panel — one row per active forge."""

    def __init__(self, **kwargs):
        super().__init__(cursor_type="row", zebra_stripes=True, **kwargs)
        self._row_to_forge: dict = {}

    def on_mount(self) -> None:
        self.add_columns("●", "ROLE", "SESSION", "TASK", "PROGRESS", "PLAN", "LAST", "!")

    def populate(self, forges: list) -> None:
        self.clear()
        self._row_to_forge.clear()
        for f in forges:
            lh = f.get("latest_handout")
            sp = f.get("latest_status") or {}
            progress = sp.get("progress", "—")
            plan = sp.get("plan_ref", "—")
            plan_s = (plan[:28] + "..") if len(plan) > 30 else plan
            last = f"{lh['type']} {lh['age']}" if lh else "—"
            flag = Text("!", style="bold red") if f.get("is_blocked") else Text("")
            task = f["task_name"]
            task_s = (task[:48] + "..") if len(task) > 50 else task
            row_key = self.add_row(
                f["health_dot"], f["role"], f["short_id"], task_s,
                progress, plan_s, last, flag,
            )
            self._row_to_forge[str(row_key)] = f

    def get_selected_forge(self) -> dict:
        try:
            if self.cursor_row is None or self.cursor_row < 0:
                return {}
            rk = self.coordinate_to_cell_key((self.cursor_row, 0)).row_key.value
            return self._row_to_forge.get(str(rk), {})
        except Exception:
            return {}


class ForgeStatusView(LazyView):
    """Operations > Forges — per-forge status cards with progress + plan context."""

    DEFAULT_CSS = """
    ForgeStatusView { layout: vertical; }
    ForgeStatusView ForgeCardsTable { height: 55%; border-bottom: solid $accent; }
    ForgeStatusView #forge-status-detail { height: 1fr; padding: 0 1; }
    """

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._forges: list = []
        self._selected_sid: str = ""
        self._table = None
        self._detail = None
        self._interval_handle = None

    def compose(self):
        yield ForgeCardsTable(id="forge-cards")
        yield RichLog(id="forge-status-detail", wrap=True, highlight=True, markup=True)

    def load_content(self) -> None:
        self._table = self.query_one("#forge-cards", ForgeCardsTable)
        self._detail = self.query_one("#forge-status-detail", RichLog)
        self._detail.write("[dim]Select a forge to see status detail.[/dim]")
        self._do_refresh()
        if self._interval_handle is None:
            self._interval_handle = self.set_interval(30.0, self._do_refresh)

    def refresh_content(self) -> None:
        self._do_refresh()

    def _do_refresh(self) -> None:
        import threading as _threading
        def work():
            forges = _fetch_forge_status()
            self.app.call_from_thread(self._apply_forges, forges)
        _threading.Thread(target=work, daemon=True).start()

    def _apply_forges(self, forges: list) -> None:
        self._forges = forges
        if self._table is not None:
            self._table.populate(forges)
        if forges and not self._selected_sid:
            self._selected_sid = forges[0]["session_id"]
            self._show_detail_for(self._selected_sid)

    def on_data_table_row_highlighted(self, event) -> None:
        f = self._table.get_selected_forge() if self._table else {}
        sid = f.get("session_id", "")
        if sid:
            self._selected_sid = sid
            self._show_detail_for(sid)

    def _show_detail_for(self, session_id: str) -> None:
        import threading as _threading
        def work():
            detail = _fetch_forge_detail(session_id)
            forge = next((f for f in self._forges if f["session_id"] == session_id), {})
            self.app.call_from_thread(self._render_detail, forge, detail)
        _threading.Thread(target=work, daemon=True).start()

    def _render_detail(self, forge: dict, detail: dict) -> None:
        if self._detail is None:
            return
        self._detail.clear()
        role = forge.get("role", "?")
        sid = forge.get("session_id", "?")
        health = forge.get("health_dot", "⚪")
        self._detail.write(f"{health} [bold cyan]{role}[/bold cyan]  [dim]{sid}[/dim]")
        self._detail.write(f"[bold]Task:[/bold] {forge.get('task_name', '(none)')}")
        self._detail.write("")

        sp = forge.get("latest_status") or {}
        if sp:
            done = sp.get("done", 0)
            rem  = sp.get("remaining", 0)
            inf  = sp.get("in_flight", 0)
            self._detail.write(
                f"[bold yellow]Progress:[/bold yellow]  "
                f"[green]{done} done[/green] / [yellow]{rem} remaining[/yellow] / [cyan]{inf} in-flight[/cyan]"
            )
            self._detail.write(f"[bold]Plan:[/bold]      {sp.get('plan_ref','—')}")
            self._detail.write(f"[bold]Branch:[/bold]    {sp.get('branch','—')}")
            blockers = sp.get("blockers", "none")
            blocker_style = "bold red" if blockers.lower() != "none" else "green"
            self._detail.write(f"[bold]Blockers:[/bold]  [{blocker_style}]{blockers}[/]")
            self._detail.write(f"[bold]ETA:[/bold]       {sp.get('eta','unknown')}  ({sp.get('confidence','medium')})")
            self._detail.write("")

        handouts = detail.get("handouts", [])
        if handouts:
            self._detail.write("[bold yellow]Recent handouts[/bold yellow]")
            for h in handouts:
                age = _handout_relative_time(h.get("created_at", ""))
                htype = h.get("handout_type", "?")
                color = FORGE_HANDOUT_TYPE_COLORS.get(htype, "white")
                self._detail.write(
                    f"  [dim]{age:>8}[/dim]  [bold {color}]{htype:>12}[/bold {color}]  {h.get('title','')}"
                )

        files = forge.get("files_touched", [])
        if files:
            self._detail.write("")
            self._detail.write("[bold yellow]Files touched[/bold yellow]")
            for fp in files[:15]:
                self._detail.write(f"  [dim]{fp}[/dim]")
            if len(files) > 15:
                self._detail.write(f"  [dim]… +{len(files)-15} more[/dim]")

    def action_refresh(self) -> None:
        self._do_refresh()


# ── Forge dependency constants ─────────────────────────────────────────────
DEFAULT_BLOCKS: dict[str, list[str]] = {
    "forge-research": ["forge-prime"],
    "forge-bugs":     ["forge-audit", "forge-ops"],
    "forge-audit":    ["forge-prime"],
    "forge-ops":      ["forge-prime"],
    "forge-prime":    [],
}
FORGE_ORDER = ["forge-research", "forge-bugs", "forge-audit", "forge-ops", "forge-prime"]


def _progress_bar(done: int, total: int, width: int = 10) -> str:
    """Return a █/░ progress bar string."""
    if total <= 0:
        return "░" * width
    filled = min(width, int(round((done / total) * width)))
    return "█" * filled + "░" * (width - filled)


def _parse_blocks_field(body: str, field: str) -> list[str]:
    """Extract **Blocks:** or **Blocked-by:** values from status body."""
    import re
    m = re.search(rf"\*\*{re.escape(field)}:\*\*\s*([^\n]+)", body or "")
    if not m:
        return []
    val = m.group(1).strip()
    if val.lower() in ("none", "n/a", "-", ""):
        return []
    return [x.strip().lower() for x in re.split(r"[,;]+", val) if x.strip()]


def _find_critical_path(blocks_graph: dict[str, list[str]], blocked_forges: list[str]) -> list[str]:
    """DFS to find longest downstream chain from a blocked forge."""
    def dfs(node: str, visited: set) -> list[str]:
        if node in visited:
            return []
        visited.add(node)
        best = []
        for downstream in blocks_graph.get(node, []):
            path = dfs(downstream, visited)
            if len(path) > len(best):
                best = path
        return [node] + best

    longest: list[str] = []
    for bf in blocked_forges:
        path = dfs(bf, set())
        if len(path) > len(longest):
            longest = path
    return longest


def _needs_attention(forge: dict) -> bool:
    """True if forge has a blocker and hasn't posted a handout in >10 min."""
    import datetime
    body = forge.get("last_status_body", "") or ""
    blockers_match = __import__("re").search(r"\*\*Blockers:\*\*\s*([^\n]+)", body)
    if not blockers_match:
        return False
    bl = blockers_match.group(1).strip().lower()
    if bl in ("none", "n/a", "-", ""):
        return False
    last_hb = forge.get("heartbeat_at", "")
    if last_hb:
        try:
            ts = datetime.datetime.fromisoformat(last_hb.replace("Z", "+00:00"))
            age = (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds()
            return age > 600
        except Exception:
            pass
    return True


def _render_pipeline(forges: list[dict]) -> str:
    """Return Rich markup string for pipeline ASCII diagram."""
    # Build blocks graph from last status handouts
    blocks_graph: dict[str, list[str]] = dict(DEFAULT_BLOCKS)
    forge_by_role: dict[str, dict] = {f["role"]: f for f in forges if f.get("role")}

    # Override with dynamic BLOCKS/BLOCKED_BY from last status body
    for f in forges:
        role = f.get("role", "")
        body = f.get("last_status_body", "") or ""
        dyn_blocks = _parse_blocks_field(body, "Blocks")
        if dyn_blocks:
            blocks_graph[role] = dyn_blocks

    blocked_roles = []
    for f in forges:
        body = f.get("last_status_body", "") or ""
        bb = _parse_blocks_field(body, "Blocked-by")
        if bb:
            blocked_roles.append(f.get("role", ""))

    critical_path = _find_critical_path(blocks_graph, blocked_roles)

    lines = []

    # Critical path banner
    if critical_path:
        cp_display = " → ".join(r.replace("forge-", "").upper() for r in critical_path)
        lines.append(f"[bold red]⚠  CRITICAL PATH: {cp_display}[/bold red]")
        lines.append(f"[red]   {critical_path[0].replace('forge-','').upper()} is blocked — needs your attention[/red]")
        lines.append("")

    # Render each forge row in FORGE_ORDER
    rendered_roles = set()
    for role in FORGE_ORDER:
        f = forge_by_role.get(role)
        if not f:
            continue
        rendered_roles.add(role)

        # Parse progress X/Y/Z
        body = f.get("last_status_body", "") or ""
        prog_m = __import__("re").search(r"\*\*Progress:\*\*\s*(\d+)/(\d+)/(\d+)", body)
        if prog_m:
            done, remaining, inflight = int(prog_m.group(1)), int(prog_m.group(2)), int(prog_m.group(3))
            total = done + remaining
            bar = _progress_bar(done, total)
            prog_str = f"[{bar}]  {done}/{total}"
        else:
            bar = "░" * 10
            prog_str = f"[{bar}]  ?/?"

        # Health dot + color
        health = f.get("health_dot", "⚪")
        role_short = role.replace("forge-", "").upper()
        task = (f.get("task_name") or "idle")[:40]

        # Attention flag
        attn = " [bold red]⚠ BLOCKED[/bold red]" if role in blocked_roles else ""
        cp_flag = " [bold red]← critical path[/bold red]" if role in critical_path else ""

        role_color = {
            "forge-prime": "bold cyan",
            "forge-research": "bold green",
            "forge-bugs": "bold red",
            "forge-audit": "bold yellow",
            "forge-ops": "bold blue",
        }.get(role, "white")

        lines.append(f"{health} [{role_color}]{role_short:<12}[/{role_color}] {prog_str}  [dim]{task}[/dim]{attn}{cp_flag}")

        # Dependency arrows
        downstream = blocks_graph.get(role, [])
        if downstream:
            ds_str = ", ".join(r.replace("forge-", "").upper() for r in downstream)
            lines.append(f"    [dim]│ unblocks → {ds_str}[/dim]")
            lines.append(f"    [dim]↓[/dim]")

    lines.append("")
    return "\n".join(lines)


def _render_attention(forges: list[dict]) -> str:
    """Return Rich markup for the attention/action needed section."""
    lines = ["[bold]Action needed:[/bold]", ""]
    any_action = False
    for f in forges:
        if _needs_attention(f):
            role = (f.get("role") or "unknown").replace("forge-", "").upper()
            body = f.get("last_status_body", "") or ""
            bl_m = __import__("re").search(r"\*\*Blockers:\*\*\s*([^\n]+)", body)
            bl_text = bl_m.group(1).strip() if bl_m else "unspecified blocker"
            lines.append(f"  [bold red]→ {role}[/bold red]: {bl_text}")
            any_action = True
    if not any_action:
        lines.append("  [dim]All forges unblocked — no action needed.[/dim]")
    return "\n".join(lines)


class ForgePipelineView(LazyView):
    """Operations > Pipeline — Gantt-style forge dependency + progress view."""

    DEFAULT_CSS = """
    ForgePipelineView {
        layout: vertical;
        padding: 1 2;
    }
    ForgePipelineView #pipeline-diagram {
        height: 2fr;
        border-bottom: solid $accent;
        padding: 0 1;
    }
    ForgePipelineView #pipeline-attention {
        height: 1fr;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("r", "action_refresh", "Refresh"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._interval_handle = None

    def compose(self):
        yield RichLog(id="pipeline-diagram", wrap=True, highlight=True, markup=True)
        yield RichLog(id="pipeline-attention", wrap=True, highlight=True, markup=True)

    def load_content(self) -> None:
        self._do_refresh()
        if self._interval_handle is None:
            self._interval_handle = self.set_interval(30.0, self._do_refresh)

    def refresh_content(self) -> None:
        self._do_refresh()

    def _do_refresh(self) -> None:
        import threading as _threading
        def work():
            forges = _fetch_forge_status()
            self.app.call_from_thread(self._apply_data, forges)
        _threading.Thread(target=work, daemon=True).start()

    def _apply_data(self, forges: list) -> None:
        try:
            diagram = self.query_one("#pipeline-diagram", RichLog)
            attention = self.query_one("#pipeline-attention", RichLog)
            diagram.clear()
            attention.clear()
            if not forges:
                diagram.write("[dim]No active forges found.[/dim]")
                attention.write("[dim]—[/dim]")
                return
            diagram.write(_render_pipeline(forges))
            attention.write(_render_attention(forges))
        except Exception as e:
            pass  # widget not mounted yet

    def action_refresh(self) -> None:
        self._do_refresh()


class EmployeeView(LazyView):
    """Team dashboard — account summary + live fleet view of all sessions (press T)."""

    def refresh_content(self):
        now = time.time()
        if not hasattr(self, "_last_refresh") or (now - self._last_refresh) > 15:
            self._last_refresh = now
            self.load_content()

    def compose(self) -> ComposeResult:
        yield ForgeCommsPanel(id="forge-comms")
        yield Static(id="emp-header")
        yield DataTable(id="emp-table")
        yield Static(id="fleet-header")
        yield DataTable(id="fleet-table")
        yield Static(id="emp-footer")

    def load_content(self):
        from token_watch_data import get_employee_dashboard, get_fleet_sessions
        employees = get_employee_dashboard()
        fleet = get_fleet_sessions()

        try:
            forge_panel = self.query_one("#forge-comms", ForgeCommsPanel)
            forge_panel.refresh_panel(fleet)
        except Exception:
            pass

        # ── Account summary ──────────────────────────────────────────────────
        self.query_one("#emp-header", Static).update(
            "[bold]Account Summary[/bold]  "
            "[dim]A/B/C capacity · builds · score[/dim]"
        )

        dt = self.query_one("#emp-table", DataTable)
        dt.clear(columns=True)
        dt.cursor_type = "row"
        dt.zebra_stripes = True
        dt.add_column("", width=3)
        dt.add_column("Acct", width=6)
        dt.add_column("Name", width=14)
        dt.add_column("Sess", width=6)
        dt.add_column("Current Task", width=46)
        dt.add_column("5h%", width=6)
        dt.add_column("7d%", width=6)
        dt.add_column("Bld/d", width=7)
        dt.add_column("Bld/w", width=7)
        dt.add_column("Score", width=7)

        acct_colors = {"A": "cyan", "B": "magenta", "C": "yellow"}

        for emp in employees:
            label = emp["label"]
            color = acct_colors.get(label, "white")
            indicator = "[green]●[/green]" if emp["is_active"] else "[dim]○[/dim]"

            five = emp["five_pct"]
            seven = emp["seven_pct"]
            five_color = "red" if five >= 80 else ("yellow" if five >= 60 else "green")
            seven_color = "red" if seven >= 80 else ("yellow" if seven >= 60 else "green")

            score = emp["score"]
            score_color = "green" if score >= 10 else ("yellow" if score >= 4 else "dim")

            sess_str = (
                f"{emp['sessions_worker']}w/{emp['sessions_total']}t"
                if emp["sessions_total"] > emp["sessions_worker"]
                else str(emp["sessions_worker"])
            )

            dt.add_row(
                Text.from_markup(indicator),
                Text.from_markup(f"[{color}]{label}[/{color}]"),
                Text(emp["name"]),
                Text(sess_str, justify="center"),
                Text(emp["task"], overflow="fold"),
                Text(f"{five:.0f}%", style=five_color),
                Text(f"{seven:.0f}%", style=seven_color),
                Text(str(emp["builds_today"]), justify="right"),
                Text(str(emp["builds_week"]), justify="right"),
                Text(f"{score:.0f}", style=score_color, justify="right"),
            )

        if not employees:
            dt.add_row("", Text("No account data available", style="dim"),
                       "", "", "", "", "", "", "", "")

        # ── Fleet view ───────────────────────────────────────────────────────
        active_count = sum(1 for s in fleet if not s["is_stale"])
        stale_count = sum(1 for s in fleet if s["is_stale"])
        self.query_one("#fleet-header", Static).update(
            f"[bold]Fleet View[/bold]  "
            f"[dim]{active_count} active · {stale_count} stale · "
            "all sessions across all accounts · Hb=heartbeat age[/dim]"
        )

        ft = self.query_one("#fleet-table", DataTable)
        ft.clear(columns=True)
        ft.cursor_type = "row"
        ft.zebra_stripes = True
        ft.add_column("Acct", width=6)
        ft.add_column("Session", width=14)
        ft.add_column("Role", width=8)
        ft.add_column("Repo", width=14)
        ft.add_column("Task / Directive", width=46)
        ft.add_column("Files Editing", width=30)
        ft.add_column("Hb", width=7)
        ft.add_column("5h%", width=6)
        ft.add_column("Tok/k", width=7)

        for s in fleet:
            label = s["account"]
            color = acct_colors.get(label, "white")

            age_s = s.get("hb_age_s")
            if age_s is None:
                age_str = "?"
            elif age_s < 60:
                age_str = f"{age_s}s"
            elif age_s < 3600:
                age_str = f"{age_s//60}m"
            else:
                age_str = f"{age_s//3600}h"

            hb_color = "dim" if s["is_stale"] else (
                "green" if (age_s or 999) < 120 else "yellow"
            )
            row_style = "dim" if s["is_stale"] else ""

            five = s["five_pct"]
            five_color = "dim" if s["is_stale"] else (
                "red" if five >= 80 else ("yellow" if five >= 60 else "green")
            )

            raw_role = s["role"] or "worker"
            role_str = raw_role[:7]  # advisor/worker/builder — truncate to fit
            role_color = "dim" if raw_role == "advisor" else ""

            acct_cell = (
                Text.from_markup(f"[{color}]{label}[/{color}]")
                if not row_style else Text(label, style="dim")
            )

            ft.add_row(
                acct_cell,
                Text(s["session_id"][:13], style=row_style or "dim"),
                Text(role_str, style=role_color or row_style),
                Text(s["repo"][:13], style=row_style),
                Text(s["task"][:46], style=row_style, overflow="fold"),
                Text(s["files"][:29], style="dim"),
                Text(age_str, style=hb_color),
                Text(f"{five:.0f}%", style=five_color),
                Text(f"{s['output_tokens_k']:.1f}", style=row_style or "dim", justify="right"),
            )

        if not fleet:
            ft.add_row("", Text("No active sessions found", style="dim"),
                       "", "", "", "", "", "", "")

        self.query_one("#emp-footer", Static).update(
            f"[dim]Updated {datetime.now().strftime('%H:%M:%S')} · "
            "T key · score=3×bld_today+1×bld_week+2×sessions[/dim]"
        )


# ── Cycles screens ──────────────────────────────────────────────────────────


class CyclesView(LazyView):
    """Overview of all 5-hour usage cycles (windows)."""

    BINDINGS = [
        Binding("p", "show_plan", "Plan"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="cycles-current")
        yield DataTable(id="cycles-list")

    def refresh_content(self):
        """Auto-refresh the current cycle banner every 15s."""
        now = time.time()
        if not hasattr(self, '_last_refresh') or (now - self._last_refresh) > 15:
            self._last_refresh = now
            from token_watch_data import (
                _get_current_cycle, _get_cycle_sessions,
                _countdown, _current_pct,
            )
            try:
                panel = self.query_one("#cycles-current", Static)
            except Exception:
                return
            current = _get_current_cycle()
            if not current:
                panel.update("[dim]No active cycle[/dim]")
                return
            five, _seven, five_reset, _sr = _current_pct()
            reset_str = _countdown(five_reset) if five_reset else "?"
            try:
                burn_pct = float(five)
            except (ValueError, TypeError):
                burn_pct = 0.0
            bar_len = 20
            filled = int(burn_pct / 100 * bar_len)
            bar = "\u2588" * filled + "\u2591" * (bar_len - filled)
            bar_color = "green" if burn_pct < 50 else ("yellow" if burn_pct < 80 else "red")
            sessions = _get_cycle_sessions(current["cycle_id"])
            projects = sorted({s.get("project", "?") for s in sessions if s.get("project")})
            proj_str = ", ".join(projects[:5]) if projects else "\u2014"
            from token_watch_data import _get_cycle_plan
            plan = _get_cycle_plan(current["cycle_id"])
            plan_str = "[green]plan set[/green]" if plan else "[dim]no plan[/dim]"
            panel.update(
                f"[bold]CURRENT CYCLE[/bold]  resets in {reset_str}\n"
                f"  [{bar_color}]{bar}[/{bar_color}] {burn_pct:.0f}%  "
                f"[bold]{current['session_count']}[/bold] sessions  "
                f"Projects: {proj_str}  {plan_str}  "
                f"Cost: {current['cost_str']}  "
                f"Gravity: [cyan]{current['gravity_label'] or chr(8212)}[/cyan]"
            )

    def load_content(self):
        from token_watch_data import (
            _get_current_cycle, _get_all_cycles, _get_cycle_sessions,
            _get_cycle_plan, _countdown, _format_cost, _current_pct,
        )

        current = _get_current_cycle()
        all_cycles = _get_all_cycles()

        # ── Current cycle panel ──
        panel = self.query_one("#cycles-current", Static)
        if current:
            five, _seven, five_reset, _sr = _current_pct()
            reset_str = _countdown(five_reset) if five_reset else "?"
            try:
                burn_pct = float(five)
            except (ValueError, TypeError):
                burn_pct = 0.0
            bar_len = 20
            filled = int(burn_pct / 100 * bar_len)
            bar = "\u2588" * filled + "\u2591" * (bar_len - filled)
            bar_color = "green" if burn_pct < 50 else ("yellow" if burn_pct < 80 else "red")

            # Unique projects
            sessions = _get_cycle_sessions(current["cycle_id"])
            projects = sorted({s.get("project", "?") for s in sessions if s.get("project")})
            proj_str = ", ".join(projects[:5]) if projects else "\u2014"

            plan = _get_cycle_plan(current["cycle_id"])
            plan_str = "[green]plan set[/green]" if plan else "[dim]no plan[/dim]"

            panel.update(
                f"[bold]CURRENT CYCLE[/bold]  resets in {reset_str}\n"
                f"  [{bar_color}]{bar}[/{bar_color}] {burn_pct:.0f}%  "
                f"[bold]{current['session_count']}[/bold] sessions  "
                f"Projects: {proj_str}  {plan_str}  "
                f"Cost: {current['cost_str']}  "
                f"Gravity: [cyan]{current['gravity_label'] or chr(8212)}[/cyan]"
            )
        else:
            panel.update("[dim]No active cycle[/dim]")

        # ── Past cycles table ──
        dt = self.query_one("#cycles-list", DataTable)
        dt.cursor_type = "row"
        dt.zebra_stripes = True
        dt.add_column("#", width=4)
        dt.add_column("Start", width=18)
        dt.add_column("End", width=10)
        dt.add_column("Peak%", width=7)
        dt.add_column("Sessions", width=9)
        dt.add_column("Projects", width=18)
        dt.add_column("Stars", width=10)
        dt.add_column("Cost", width=8)
        dt.add_column("Gravity", width=14)

        self._cycle_map = {}  # row_key -> cycle dict
        # Number oldest = #1, but display newest first (top of table)
        ordered = list(reversed(all_cycles))  # oldest first for numbering
        display_order = []
        for rank, c in enumerate(ordered, 1):
            display_order.append((rank, c))
        display_order.reverse()  # newest first for display
        for rank, c in display_order:
            try:
                start_dt = datetime.fromisoformat(c["start_ts"])
                start_str = start_dt.strftime("%b %-d %-I:%M %p")
            except Exception:
                start_str = c["start_ts"][:16]

            try:
                end_dt = datetime.fromisoformat(c["end_ts"])
                end_str = end_dt.strftime("%-I:%M %p")
            except Exception:
                end_str = "?"

            peak = c.get("peak_five_pct", 0)
            peak_color = "green" if peak >= 80 else ("yellow" if peak >= 40 else "dim")

            # Projects from sessions
            sessions = _get_cycle_sessions(c["cycle_id"])
            projects = sorted({s.get("project", "?") for s in sessions if s.get("project")})
            proj_str = ", ".join(projects[:3]) if projects else "\u2014"

            row_key = f"cyc-{rank}"
            self._cycle_map[row_key] = c
            dt.add_row(
                Text(str(rank), justify="right"),
                Text(start_str),
                Text(end_str, style="dim"),
                Text(f"{peak:.0f}%", style=peak_color, justify="right"),
                Text(str(c.get("session_count", 0)), justify="right"),
                Text(proj_str),
                Text(c.get("stars", ""), style="yellow"),
                Text(c.get("cost_str", "")),
                Text(c.get("gravity_label", "") or "\u2014", style="cyan"),
                key=row_key,
            )

        if not ordered:
            dt.add_row("", Text("No cycles recorded yet", style="dim"),
                        "", "", "", "", "", "", "")

    def on_data_table_row_selected(self, event):
        row_key = str(event.row_key.value) if hasattr(event.row_key, 'value') else str(event.row_key)
        cycle = self._cycle_map.get(row_key)
        if cycle:
            self.app.push_screen(CycleDetailScreen(cycle))

    def action_show_plan(self):
        self.app.push_screen(CyclePlanScreen())



class CycleDetailScreen(Screen):
    """Detailed view of a single 5-hour cycle."""

    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
    ]

    def __init__(self, cycle):
        super().__init__()
        self.cycle = cycle

    def compose(self) -> ComposeResult:
        yield NavBar(active="nav-cycles")
        yield Static(id="cdetail-header")
        yield Static(id="cdetail-scores")
        yield Static(id="cdetail-accomplishments")
        yield Static(id="cdetail-sources-header")
        yield DataTable(id="cdetail-sources")
        yield DataTable(id="cdetail-sessions")
        yield Static(id="cdetail-plan")

    def on_mount(self):
        from token_watch_data import (
            _get_cycle_sessions, _get_cycle_plan, _stars_display,
            _format_cost, _estimate_cost, _countdown,
            _get_pomodoro_stats,
        )

        c = self.cycle

        # ── Header ──
        try:
            start_dt = datetime.fromisoformat(c["start_ts"])
            end_dt = datetime.fromisoformat(c["end_ts"])
            start_str = start_dt.strftime("%b %-d %-I:%M %p")
            end_str = end_dt.strftime("%-I:%M %p")
        except Exception:
            start_str = c["start_ts"][:16]
            end_str = c["end_ts"][:16]

        stars = c.get("stars", "")
        acc = c.get("accomplishments", {})
        commits = len(acc.get("git_commits", []))
        peak = c.get("peak_five_pct", 0)

        self.query_one("#cdetail-header", Static).update(
            f"[bold]CYCLE:[/bold] {start_str} \u2014 {end_str}  {stars}\n"
            f"  Peak: [bold]{peak:.0f}%[/bold]  "
            f"Sessions: [bold]{c.get('session_count', 0)}[/bold]  "
            f"Cost: [bold]{c.get('cost_str', '')}[/bold]  "
            f"Commits: [bold]{commits}[/bold]"
        )

        # ── Scores ──
        ws = c.get("window_score")
        scores_panel = self.query_one("#cdetail-scores", Static)
        if ws:
            dims = []
            for dim_key in ("burn", "parallel", "ship", "breadth", "velocity"):
                val = ws.get(dim_key, 0)
                dim_stars = _stars_display(val)
                color = "green" if val >= 4 else ("yellow" if val >= 2.5 else "red")
                dims.append(f"{dim_key.capitalize()}: [{color}]{dim_stars} ({val})[/{color}]")
            scores_panel.update("  ".join(dims))
        else:
            scores_panel.update("[dim]No window score available[/dim]")

        # ── Accomplishments ──
        acc_panel = self.query_one("#cdetail-accomplishments", Static)
        files_edited = len(acc.get("files_edited", []))
        files_created = len(acc.get("files_created", []))
        errors = acc.get("errors", 0)
        skills = acc.get("skills", [])
        turns = acc.get("turn_count", 0)

        acc_lines = []
        err_style = "red" if errors else "dim"
        acc_lines.append(
            f"  Files edited: [bold]{files_edited}[/bold]  "
            f"Files created: [bold]{files_created}[/bold]  "
            f"Commits: [bold]{commits}[/bold]  "
            f"Errors: [bold {err_style}]{errors}[/bold {err_style}]  "
            f"Turns: [bold]{turns}[/bold]"
        )
        if skills:
            acc_lines.append(f"  Skills: {', '.join(skills[:10])}")
        acc_panel.update("\n".join(acc_lines))

        # ── Source Breakdown ──
        sessions = _get_cycle_sessions(c["cycle_id"])
        by_source = {}  # type: dict
        cycle_total = 0
        for s in sessions:
            src = s.get("source", "?")
            tokens = s.get("output_tokens", 0) or 0
            if src not in by_source:
                by_source[src] = {"output_tokens": 0, "sessions": 0}
            by_source[src]["output_tokens"] += tokens
            by_source[src]["sessions"] += 1
            cycle_total += tokens

        self.query_one("#cdetail-sources-header", Static).update(
            f"[bold]Token Distribution[/bold]  [dim]{len(by_source)} sources · "
            f"{cycle_total / 1000:.0f}k total output[/dim]"
        )

        src_table = self.query_one("#cdetail-sources", DataTable)
        src_table.cursor_type = "none"
        src_table.zebra_stripes = True
        src_table.add_column("Source", width=20)
        src_table.add_column("Sessions", width=9)
        src_table.add_column("Output Tok", width=11)
        src_table.add_column("% of Cycle", width=11)
        src_table.add_column("Share")

        sorted_sources = sorted(by_source.items(), key=lambda x: x[1]["output_tokens"], reverse=True)
        for src, data in sorted_sources:
            out = data["output_tokens"]
            out_str = f"{out / 1000:.1f}k" if out >= 1000 else str(out)
            pct = (out / cycle_total * 100) if cycle_total else 0
            bar_len = max(1, int(pct / 2.5))
            bar = "\u2588" * bar_len + "\u2591" * (40 - bar_len)
            src_style = "yellow" if ("/" in src or src == "paperclip") else (
                "green" if src == "cli" else ("cyan" if "atlas" in src else "dim")
            )
            bar_color = "yellow" if ("/" in src) else ("green" if src == "cli" else "cyan")
            src_table.add_row(
                Text(src, style=src_style),
                Text(str(data["sessions"]), justify="right"),
                Text(out_str, justify="right"),
                Text(f"{pct:.1f}%", justify="right"),
                Text(bar[:40], style=bar_color),
            )

        # ── Sessions DataTable ──
        dt = self.query_one("#cdetail-sessions", DataTable)
        dt.cursor_type = "row"
        dt.zebra_stripes = True
        dt.add_column("Pomo", width=28)
        dt.add_column("Session", width=12)
        dt.add_column("Project", width=18)
        dt.add_column("Duration", width=10)
        dt.add_column("Tokens", width=10)
        dt.add_column("Cost", width=8)
        dt.add_column("Directive", width=30)

        self._session_map = {}  # row_key -> session dict

        blocks = _get_pomodoro_stats(c["cycle_id"])
        session_to_block = {}
        if blocks:
            for b in blocks:
                for sid in b.get("session_ids", []):
                    session_to_block[sid] = b["block_num"]

        if blocks:
            row_idx = 0
            for b in blocks:
                # Block separator row
                try:
                    bstart = datetime.fromisoformat(b["start_ts"]).astimezone()
                    bend = datetime.fromisoformat(b["end_ts"]).astimezone()
                    t_start = bstart.strftime("%-I:%M")
                    t_end = bend.strftime("%-I:%M")
                except Exception:
                    t_start = "?"
                    t_end = "?"
                tok = b["output_tokens"]
                tok_str = f"{tok // 1000}k" if tok >= 1000 else str(tok)
                n_sess = len(b["session_ids"])
                delta = abs(b["delta_pct"])

                if b["is_current"]:
                    hdr_style = "bold cyan"
                elif b["is_future"]:
                    hdr_style = "dim"
                else:
                    hdr_style = "bold"

                if n_sess > 0:
                    hdr = f"P{b['block_num']} ({t_start}-{t_end}) \u2014 {n_sess} sess, {tok_str}, {delta:.1f}%"
                else:
                    hdr = f"P{b['block_num']} ({t_start}-{t_end}) \u2014 idle"

                dt.add_row(
                    Text(hdr, style=hdr_style),
                    Text(""), Text(""), Text(""), Text(""), Text(""), Text(""),
                    key=f"pomo-hdr-{b['block_num']}",
                )

                # Sessions in this block
                block_sessions = [s for s in sessions if session_to_block.get(s.get("session_id", "")) == b["block_num"]]
                for s in block_sessions:
                    sid = s.get("session_id", "?")
                    short_sid = sid[:10] if len(sid) > 10 else sid
                    project = s.get("project", "\u2014")
                    tokens = s.get("output_tokens", 0) or 0
                    model = s.get("model", "")
                    cost = _estimate_cost(tokens, model)
                    directive = s.get("directive", "") or ""
                    try:
                        first = s.get("first_ts")
                        last = s.get("last_ts")
                        if first and last:
                            if not isinstance(first, datetime):
                                first = datetime.fromisoformat(str(first).replace("Z", "+00:00"))
                            if not isinstance(last, datetime):
                                last = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                            dur_secs = int((last - first).total_seconds())
                            dur_m = dur_secs // 60
                            dur_str = f"{dur_m}m" if dur_m < 60 else f"{dur_m // 60}h{dur_m % 60:02d}m"
                        else:
                            dur_str = "\u2014"
                    except Exception:
                        dur_str = "\u2014"
                    tok_s = f"{tokens // 1000}k" if tokens >= 1000 else str(tokens)
                    row_key = f"csess-{row_idx}"
                    self._session_map[row_key] = s
                    dt.add_row(
                        Text(""),
                        Text(short_sid, style="cyan"),
                        Text(project),
                        Text(dur_str, justify="right"),
                        Text(tok_s, justify="right"),
                        Text(_format_cost(cost)),
                        Text(directive[:30], style="dim"),
                        key=row_key,
                    )
                    row_idx += 1
        else:
            # Fallback: flat list (no block data)
            for i, s in enumerate(sessions):
                sid = s.get("session_id", "?")
                short_sid = sid[:10] if len(sid) > 10 else sid
                project = s.get("project", "\u2014")
                tokens = s.get("output_tokens", 0) or 0
                model = s.get("model", "")
                cost = _estimate_cost(tokens, model)
                directive = s.get("directive", "") or ""
                try:
                    first = s.get("first_ts")
                    last = s.get("last_ts")
                    if first and last:
                        if not isinstance(first, datetime):
                            first = datetime.fromisoformat(str(first).replace("Z", "+00:00"))
                        if not isinstance(last, datetime):
                            last = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                        dur_secs = int((last - first).total_seconds())
                        dur_m = dur_secs // 60
                        dur_str = f"{dur_m}m" if dur_m < 60 else f"{dur_m // 60}h{dur_m % 60:02d}m"
                    else:
                        dur_str = "\u2014"
                except Exception:
                    dur_str = "\u2014"
                tok_str = f"{tokens // 1000}k" if tokens >= 1000 else str(tokens)
                row_key = f"csess-{i}"
                self._session_map[row_key] = s
                dt.add_row(
                    Text(""),
                    Text(short_sid, style="cyan"),
                    Text(project),
                    Text(dur_str, justify="right"),
                    Text(tok_str, justify="right"),
                    Text(_format_cost(cost)),
                    Text(directive[:30], style="dim"),
                    key=row_key,
                )

        if not sessions:
            dt.add_row(
                Text("\u2014", style="dim"), Text(""), Text("No sessions in this cycle", style="dim"),
                Text(""), Text(""), Text(""), Text(""),
            )

        # ── Plan ──
        plan_panel = self.query_one("#cdetail-plan", Static)
        plan = _get_cycle_plan(c["cycle_id"])
        if plan:
            tasks = plan.get("tasks", [])
            if tasks:
                lines = ["[bold]PLAN[/bold]"]
                for t in tasks:
                    status = t.get("status", "pending")
                    icon = "\u2713" if status == "done" else ("\u2298" if status == "skipped" else "\u25cb")
                    color = "green" if status == "done" else ("dim" if status == "skipped" else "white")
                    lines.append(
                        f"  [{color}]{icon} {t.get('title', '?')}  "
                        f"({t.get('project', '?')})  "
                        f"est:{t.get('est_pct', 0):.0f}%  "
                        f"act:{t.get('act_pct', 0):.0f}%[/{color}]"
                    )
                plan_panel.update("\n".join(lines))
            else:
                plan_panel.update("[dim]Plan exists but has no tasks[/dim]")
        else:
            plan_panel.update("[dim]No plan for this cycle[/dim]")

    def on_data_table_row_selected(self, event):
        row_key = str(event.row_key.value) if hasattr(event.row_key, 'value') else str(event.row_key)
        s = self._session_map.get(row_key)
        if s:
            self.app.push_screen(SessionDrillDown(
                session_id=s.get("session_id", ""),
                directive=s.get("directive", ""),
                project=s.get("project", "\u2014"),
            ))

    def action_pop_screen(self):
        self.app.pop_screen()


class WeeklyCyclesView(LazyView):
    """7-day rolling cycle narratives — aggregates sessions across A/B/C."""

    def compose(self) -> ComposeResult:
        yield Static(id="weekly-current")
        yield DataTable(id="weekly-list")

    def refresh_content(self):
        """Refresh the current-week banner every 30s."""
        now = time.time()
        if hasattr(self, "_last_refresh") and (now - self._last_refresh) < 30:
            return
        self._last_refresh = now
        try:
            self._render_banner()
        except Exception:
            pass

    def _render_banner(self):
        from token_watch_data import get_weekly_cycles
        panel = self.query_one("#weekly-current", Static)
        weeks = get_weekly_cycles(limit=1)
        if not weeks:
            panel.update("[dim]No weekly data[/dim]")
            return
        w = weeks[0]
        util = w.get("utilization_pct", 0.0)
        bar_len = 20
        filled = min(bar_len, int(util / 100 * bar_len))
        bar = "\u2588" * filled + "\u2591" * (bar_len - filled)
        bar_color = "green" if util < 60 else ("yellow" if util < 85 else "red")
        by = w.get("by_account", {})

        def _acct_chunk(label):
            info = by.get(label, {})
            peak5 = info.get("five_hour_peak", 0) or 0
            peak7 = info.get("seven_day_peak", 0) or 0
            sess = info.get("sessions", 0)
            col = "green" if peak7 < 60 else ("yellow" if peak7 < 85 else "red")
            return (
                f"[bold]{label}[/bold] {sess}s "
                f"[{col}]{peak5:.0f}/{peak7:.0f}[/{col}]"
            )

        acct_str = "  ".join(_acct_chunk(x) for x in ("A", "B", "C"))
        proj_str = ", ".join(w.get("projects", [])[:4]) or "\u2014"
        panel.update(
            f"[bold]CURRENT WEEK[/bold]  {w['label']}\n"
            f"  [{bar_color}]{bar}[/{bar_color}] {util:.0f}% util  "
            f"[bold]{w['total_sessions']}[/bold] sessions  "
            f"[bold]{w['active_hours']:.1f}h[/bold] active  "
            f"[bold]{w['build_count']}[/bold] built  "
            f"Cost: [bold]{w.get('cost_str', '')}[/bold]\n"
            f"  {acct_str}  [dim]\u2014 {proj_str}[/dim]\n"
            f"  [cyan]{w.get('narrative', '')}[/cyan]"
        )

    def load_content(self):
        from token_watch_data import get_weekly_cycles

        weeks = get_weekly_cycles(limit=12)

        # ── Banner ──
        try:
            self._render_banner()
        except Exception:
            pass

        # ── Weekly table ──
        dt = self.query_one("#weekly-list", DataTable)
        dt.cursor_type = "row"
        dt.zebra_stripes = True
        dt.add_column("#", width=4)
        dt.add_column("Week", width=14)
        dt.add_column("Sess", width=6)
        dt.add_column("Hours", width=8)
        dt.add_column("Util%", width=7)
        dt.add_column("A burn", width=10)
        dt.add_column("B burn", width=10)
        dt.add_column("C burn", width=10)
        dt.add_column("Built", width=7)
        dt.add_column("Cost", width=8)
        dt.add_column("Top Projects", width=28)

        self._week_map = {}  # type: dict

        # Newest first, number oldest = 1
        ordered = list(reversed(weeks))
        display_order = []
        for rank, w in enumerate(ordered, 1):
            display_order.append((rank, w))
        display_order.reverse()

        for rank, w in display_order:
            by = w.get("by_account", {})

            def _burn_cell(label):
                info = by.get(label, {})
                peak5 = info.get("five_hour_peak", 0) or 0
                peak7 = info.get("seven_day_peak", 0) or 0
                col = (
                    "green" if peak7 < 60
                    else ("yellow" if peak7 < 85 else "red")
                )
                return Text(f"{peak5:.0f}/{peak7:.0f}", style=col, justify="right")

            util = w.get("utilization_pct", 0.0)
            util_col = "green" if util < 60 else ("yellow" if util < 85 else "red")

            proj_str = ", ".join(w.get("projects", [])[:3]) or "\u2014"

            row_key = f"wk-{rank}"
            self._week_map[row_key] = w

            is_cur_mark = "*" if w.get("is_current") else str(rank)
            dt.add_row(
                Text(is_cur_mark, justify="right",
                     style="bold cyan" if w.get("is_current") else ""),
                Text(w.get("label", "")),
                Text(str(w.get("total_sessions", 0)), justify="right"),
                Text(f"{w.get('active_hours', 0):.1f}h", justify="right"),
                Text(f"{util:.0f}%", style=util_col, justify="right"),
                _burn_cell("A"),
                _burn_cell("B"),
                _burn_cell("C"),
                Text(str(w.get("build_count", 0)), justify="right"),
                Text(w.get("cost_str", "")),
                Text(proj_str),
                key=row_key,
            )

        if not weeks:
            dt.add_row(
                "", Text("No weekly data", style="dim"),
                "", "", "", "", "", "", "", "", "",
            )

    def on_data_table_row_selected(self, event):
        row_key = (
            str(event.row_key.value)
            if hasattr(event.row_key, "value")
            else str(event.row_key)
        )
        week = getattr(self, "_week_map", {}).get(row_key)
        if week:
            self.app.push_screen(WeeklyDetailScreen(week))


class WeeklyDetailScreen(Screen):
    """Detailed view of a single 7-day cycle."""

    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
    ]

    def __init__(self, week):
        super().__init__()
        self.week = week

    def compose(self) -> ComposeResult:
        yield NavBar(active="nav-weekly")
        yield Static(id="wdetail-header")
        yield Static(id="wdetail-accounts-title")
        yield DataTable(id="wdetail-accounts")
        yield Static(id="wdetail-builds-title")
        yield DataTable(id="wdetail-builds")
        yield Static(id="wdetail-narrative")

    def on_mount(self):
        w = self.week

        # Header
        try:
            start_dt = datetime.fromisoformat(w["week_start"])
            end_dt = datetime.fromisoformat(w["week_end"])
            start_str = start_dt.astimezone().strftime("%b %-d, %Y")
            end_str = end_dt.astimezone().strftime("%b %-d, %Y")
        except Exception:
            start_str = w.get("week_start", "")[:10]
            end_str = w.get("week_end", "")[:10]

        util = w.get("utilization_pct", 0.0)
        bar_len = 30
        filled = min(bar_len, int(util / 100 * bar_len))
        bar = "\u2588" * filled + "\u2591" * (bar_len - filled)
        bar_color = "green" if util < 60 else ("yellow" if util < 85 else "red")

        self.query_one("#wdetail-header", Static).update(
            f"[bold]WEEK:[/bold] {w.get('label', '')}   "
            f"[dim]{start_str} \u2014 {end_str}[/dim]\n"
            f"  [{bar_color}]{bar}[/{bar_color}] {util:.0f}% utilization\n"
            f"  Sessions: [bold]{w.get('total_sessions', 0)}[/bold]   "
            f"Active: [bold]{w.get('active_hours', 0):.1f}h[/bold] "
            f"/ {w.get('available_hours', 0):.0f}h   "
            f"Built: [bold]{w.get('build_count', 0)}[/bold]   "
            f"Cost: [bold]{w.get('cost_str', '')}[/bold]"
        )

        # Per-account breakdown
        self.query_one("#wdetail-accounts-title", Static).update(
            "[bold]Per-Account Breakdown[/bold]"
        )
        at = self.query_one("#wdetail-accounts", DataTable)
        at.cursor_type = "none"
        at.zebra_stripes = True
        at.add_column("Account", width=10)
        at.add_column("Sessions", width=10)
        at.add_column("Hours", width=10)
        at.add_column("Peak 5h%", width=10)
        at.add_column("Peak 7d%", width=10)

        by = w.get("by_account", {})
        for label in ("A", "B", "C"):
            info = by.get(label, {})
            peak5 = info.get("five_hour_peak", 0) or 0
            peak7 = info.get("seven_day_peak", 0) or 0
            col7 = (
                "green" if peak7 < 60
                else ("yellow" if peak7 < 85 else "red")
            )
            col5 = (
                "green" if peak5 < 60
                else ("yellow" if peak5 < 85 else "red")
            )
            at.add_row(
                Text(label, style="bold cyan"),
                Text(str(info.get("sessions", 0)), justify="right"),
                Text(f"{info.get('hours', 0):.1f}h", justify="right"),
                Text(f"{peak5:.0f}%", style=col5, justify="right"),
                Text(f"{peak7:.0f}%", style=col7, justify="right"),
            )

        # Top built items
        self.query_one("#wdetail-builds-title", Static).update(
            "[bold]Top Built Items[/bold]"
        )
        bt = self.query_one("#wdetail-builds", DataTable)
        bt.cursor_type = "none"
        bt.zebra_stripes = True
        bt.add_column("Type", width=12)
        bt.add_column("Project", width=18)
        bt.add_column("Title", width=60)

        build_items = w.get("build_items", [])[:10]
        if build_items:
            for b in build_items:
                itype = b.get("item_type") or "?"
                proj = b.get("project") or "\u2014"
                title = (b.get("title") or "")[:60]
                bt.add_row(
                    Text(itype, style="dim"),
                    Text(proj, style="cyan"),
                    Text(title),
                )
        else:
            bt.add_row(
                Text("\u2014", style="dim"),
                Text("", style="dim"),
                Text("No build items in this window", style="dim"),
            )

        # Narrative
        self.query_one("#wdetail-narrative", Static).update(
            f"[bold]Narrative[/bold]\n  [cyan]{w.get('narrative', '')}[/cyan]"
        )

    def action_pop_screen(self):
        self.app.pop_screen()


class CyclePlanScreen(Screen):
    """Plan tasks for the current 5-hour cycle."""

    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
        Binding("a", "add_task", "Add"),
        Binding("d", "done_task", "Done"),
        Binding("s", "skip_task", "Skip"),
        Binding("R", "toggle_rolled", "Rolled"),
    ]

    # Hide items whose title starts with "[rolled]" by default so the
    # plan focuses on fresh work. Press R to reveal them.
    _show_rolled = False

    def compose(self) -> ComposeResult:
        yield NavBar(active="nav-cycles")
        yield Static(id="cplan-header")
        yield DataTable(id="cplan-tasks")
        yield DataTable(id="cplan-available")

    def on_mount(self):
        self._load_and_render()

    def _load_and_render(self):
        from token_watch_data import (
            _get_current_cycle, _get_cycle_plan, _save_cycle_plan,
            _get_plannable_tasks, _current_pct, _format_cost,
        )

        current = _get_current_cycle()
        header = self.query_one("#cplan-header", Static)

        if not current:
            header.update("[dim]No active cycle \u2014 start a session to create one[/dim]")
            return

        self._cycle_id = current["cycle_id"]
        plan = _get_cycle_plan(self._cycle_id)
        if not plan:
            plan = {"cycle_id": self._cycle_id, "tasks": [], "budget_pct": 100.0}
        self._plan = plan

        # Budget calculation
        five, _seven, _fr, _sr = _current_pct()
        try:
            burned = float(five)
        except (ValueError, TypeError):
            burned = 0.0

        allocated = sum(t.get("est_pct", 0) for t in plan.get("tasks", []))
        remaining = 100.0 - burned
        plan_remaining = remaining - allocated

        bar_len = 30
        burned_chars = int(burned / 100 * bar_len)
        alloc_chars = int(allocated / 100 * bar_len)
        free_chars = bar_len - burned_chars - alloc_chars
        if free_chars < 0:
            free_chars = 0
            alloc_chars = bar_len - burned_chars

        _full = '\u2588'
        _light = '\u2591'
        bar = (
            f"[red]{_full * burned_chars}[/red]"
            f"[yellow]{_full * alloc_chars}[/yellow]"
            f"[green]{_light * free_chars}[/green]"
        )

        tasks = plan.get("tasks", [])
        rolled_count = sum(
            1 for t in tasks if (t.get("title", "") or "").startswith("[rolled]")
        )
        rolled_note = ""
        if rolled_count and not self._show_rolled:
            rolled_note = f"  [dim yellow]{rolled_count} rolled hidden (R to show)[/dim yellow]"
        elif rolled_count and self._show_rolled:
            rolled_note = f"  [yellow]{rolled_count} rolled shown (R to hide)[/yellow]"

        header.update(
            f"[bold]CYCLE PLAN[/bold]  {bar}  "
            f"Burned: [red]{burned:.0f}%[/red]  "
            f"Allocated: [yellow]{allocated:.0f}%[/yellow]  "
            f"Free: [green]{plan_remaining:.0f}%[/green]{rolled_note}\n"
            f"  [dim](a=add task  d=mark done  s=skip  R=toggle rolled)[/dim]"
        )

        # ── Planned tasks table ──
        pt = self.query_one("#cplan-tasks", DataTable)
        pt.clear(columns=True)
        pt.cursor_type = "row"
        pt.zebra_stripes = True
        pt.add_column("#", width=4)
        pt.add_column("Status", width=8)
        pt.add_column("Task", width=35)
        pt.add_column("Project", width=15)
        pt.add_column("Est%", width=6)
        pt.add_column("Act%", width=6)

        # Row index → index into self._plan["tasks"]. Needed because we
        # may hide rolled rows; action_done_task / action_skip_task use
        # cursor_row, which won't match the raw task list otherwise.
        self._visible_task_indexes: list = []
        for i, t in enumerate(tasks):
            title = t.get("title", "?") or ""
            if title.startswith("[rolled]") and not self._show_rolled:
                continue
            status = t.get("status", "pending")
            icon = "\u2713 done" if status == "done" else ("\u2298 skip" if status == "skipped" else "\u25cb pend")
            color = "green" if status == "done" else ("dim" if status == "skipped" else "white")
            if title.startswith("[rolled]"):
                color = "dim " + color if color != "dim" else color

            pt.add_row(
                Text(str(i + 1), justify="right"),
                Text(icon, style=color),
                Text(title[:35], style=color if title.startswith("[rolled]") else ""),
                Text(t.get("project", "?")),
                Text(f"{t.get('est_pct', 0):.0f}%", justify="right"),
                Text(f"{t.get('act_pct', 0):.0f}%", justify="right"),
                key=f"ptask-{i}",
            )
            self._visible_task_indexes.append(i)

        if not tasks:
            pt.add_row("", Text("No tasks planned \u2014 press 'a' on available tasks below", style="dim"),
                        "", "", "", "")
        elif not self._visible_task_indexes:
            pt.add_row(
                "",
                Text(f"All {rolled_count} tasks are rolled \u2014 press R to show", style="dim yellow"),
                "", "", "", "",
            )

        # ── Available tasks table ──
        at = self.query_one("#cplan-available", DataTable)
        at.clear(columns=True)
        at.cursor_type = "row"
        at.zebra_stripes = True
        at.add_column("ID", width=6)
        at.add_column("Title", width=35)
        at.add_column("Project", width=15)
        at.add_column("~kT", width=6)
        at.add_column("Est%", width=6)
        at.add_column("Tier", width=6)
        at.add_column("Pri", width=5)

        available = _get_plannable_tasks()
        # Filter out already-planned task IDs
        planned_ids = {t.get("id") for t in tasks}
        self._available_tasks = []
        for t in available:
            if t.get("id") in planned_ids:
                continue
            self._available_tasks.append(t)
            at.add_row(
                Text(str(t.get("id", "?"))[:6]),
                Text(t.get("title", "?")[:35]),
                Text(t.get("project", "?")),
                Text(str(t.get("est_tokens_k", "?"))),
                Text(f"{t.get('est_pct', 0):.0f}%", justify="right"),
                Text(str(t.get("tier", "?"))),
                Text(str(t.get("priority", "?"))),
                key=f"avail-{t.get('id', '')}",
            )

        if not self._available_tasks:
            at.add_row("", Text("No ready tasks in Build Tracker", style="dim"),
                        "", "", "", "", "")

    def action_add_task(self):
        from token_watch_data import _save_cycle_plan
        at = self.query_one("#cplan-available", DataTable)
        if not at.row_count or not hasattr(self, '_available_tasks') or not self._available_tasks:
            return

        # Find the task by cursor position
        try:
            idx = at.cursor_row
            if idx >= len(self._available_tasks):
                return
            task = self._available_tasks[idx]
        except (IndexError, AttributeError):
            return

        new_entry = {
            "id": task.get("id"),
            "title": task.get("title", "?"),
            "project": task.get("project", "?"),
            "est_pct": task.get("est_pct", 0),
            "status": "pending",
            "act_pct": 0,
        }
        self._plan.setdefault("tasks", []).append(new_entry)
        _save_cycle_plan(self._plan)
        self._load_and_render()

    def _resolve_visible_task_index(self, cursor_row):
        """Map the DataTable cursor row to an index into self._plan['tasks']."""
        visible = getattr(self, "_visible_task_indexes", None) or []
        if cursor_row is None or cursor_row < 0:
            return None
        if cursor_row >= len(visible):
            return None
        return visible[cursor_row]

    def action_done_task(self):
        from token_watch_data import _save_cycle_plan
        pt = self.query_one("#cplan-tasks", DataTable)
        tasks = self._plan.get("tasks", [])
        if not pt.row_count or not tasks:
            return
        try:
            idx = self._resolve_visible_task_index(pt.cursor_row)
            if idx is None or idx >= len(tasks):
                return
            tasks[idx]["status"] = "done"
            _save_cycle_plan(self._plan)
            self._load_and_render()
        except (IndexError, AttributeError):
            return

    def action_skip_task(self):
        from token_watch_data import _save_cycle_plan
        pt = self.query_one("#cplan-tasks", DataTable)
        tasks = self._plan.get("tasks", [])
        if not pt.row_count or not tasks:
            return
        try:
            idx = self._resolve_visible_task_index(pt.cursor_row)
            if idx is None or idx >= len(tasks):
                return
            tasks[idx]["status"] = "skipped"
            _save_cycle_plan(self._plan)
            self._load_and_render()
        except (IndexError, AttributeError):
            return

    def action_toggle_rolled(self):
        """Toggle visibility of [rolled] tasks in the plan."""
        self._show_rolled = not self._show_rolled
        self._load_and_render()
        self.notify(
            "Showing rolled items" if self._show_rolled else "Hiding rolled items"
        )

    def action_pop_screen(self):
        self.app.pop_screen()


# ── App ──────────────────────────────────────────────────────────────────────


def _render_pie_chart(sessions, width=30, height=15):
    """Render an ASCII pie chart using Unicode blocks."""
    if not sessions:
        return "[dim]No data[/dim]"

    # Use output_tokens for proportions (more meaningful than % which are all similar)
    total_tokens = sum(s.get("output_tokens", 0) or 1 for s in sessions)

    # Build angle ranges for each session
    slices = []  # (start_angle, end_angle, color, label)
    current_angle = -math.pi / 2  # Start from top (12 o'clock)
    for s in sessions:
        tokens = s.get("output_tokens", 0) or 1
        sweep = 2 * math.pi * tokens / total_tokens
        slices.append((current_angle, current_angle + sweep, s["color"], s.get("directive", "")[:15] or s["session_id"][:10]))
        current_angle += sweep

    # Render circle
    cx, cy = width / 2, height / 2
    # Account for terminal character aspect ratio (~2:1 width:height)
    rx = width / 2 - 1  # radius x
    ry = height / 2 - 0.5  # radius y

    lines = []
    for row in range(height):
        line_chars = []
        for col in range(width):
            # Normalize to unit circle
            dx = (col - cx) / rx if rx else 0
            dy = (row - cy) / ry if ry else 0
            dist = math.sqrt(dx * dx + dy * dy)

            if dist > 1.0:
                line_chars.append(" ")
                continue

            # Calculate angle
            angle = math.atan2(dy, dx)

            # Find which slice this angle belongs to
            color = "white"
            for start, end, c, _ in slices:
                # Normalize angles
                a = angle
                s_a = start
                # Handle wrap-around
                while a < s_a:
                    a += 2 * math.pi
                if s_a <= a < end:
                    color = c
                    break

            line_chars.append(f"[{color}]\u2588[/{color}]")

        lines.append("".join(line_chars))

    return "\n".join(lines)


class TokenAttributionScreen(Screen):
    """Full-screen token attribution breakdown."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q", "app.pop_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        from textual.widgets import Footer
        yield NavBar(active="nav-dashboard")
        yield Static(id="attr-header")
        with Horizontal(id="attr-chart-row"):
            yield Static(id="attr-pie")
            yield Static(id="attr-legend")
        yield DataTable(id="attr-table")
        yield Footer()

    def on_mount(self):
        table = self.query_one("#attr-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "\u25a0", "Session", "Directive", "Time",
            "% Used", "Out Tokens", "Model", "Tools"
        )
        self.refresh_data()

    def refresh_data(self):
        data = _get_token_attribution()
        header = self.query_one("#attr-header", Static)
        pie_widget = self.query_one("#attr-pie", Static)
        legend_widget = self.query_one("#attr-legend", Static)

        if not data or not data.get("sessions"):
            header.update("[bold]Token Attribution[/bold] \u2014 No data yet")
            pie_widget.update("")
            legend_widget.update("")
            return

        total = data["total_used_pct"]
        unaccounted = data.get("unaccounted_pct", 0)

        try:
            bar_width = max(20, self.size.width - 6)
        except Exception:
            bar_width = 60

        bar_chars = []
        sessions = data["sessions"]
        for s in sessions:
            pct = s["pct_used"]
            if pct < 0.3:
                continue
            cols = max(1, int(pct / max(total, 1) * bar_width))
            color = s["color"]
            label = f"{pct:.0f}%"
            segment = label.center(cols) if cols >= len(label) + 2 else "\u2588" * cols
            bar_chars.append(f"[bold white on {color}]{segment}[/]")

        if unaccounted > 0.5:
            cols = max(1, int(unaccounted / max(total, 1) * bar_width))
            segment = f"{unaccounted:.0f}%".center(cols) if cols >= 6 else "\u2591" * cols
            bar_chars.append(f"[dim]{segment}[/dim]")

        bar = "".join(bar_chars)
        header.update(f"[bold]Who Ate My {total:.0f}%?[/bold]  5h rolling window\n{bar}")

        # Render pie chart
        pie_text = _render_pie_chart(sessions)
        pie_widget.update(pie_text)

        # Render legend
        total_tokens = sum(s.get("output_tokens", 0) or 0 for s in sessions)
        legend_lines = []
        for s in sessions:
            color = s["color"]
            directive = s.get("directive", "")[:25] if s.get("directive") else s["session_id"][:12]
            out_tokens = s.get("output_tokens", 0) or 0
            if out_tokens >= 1_000_000:
                tok_str = f"{out_tokens / 1_000_000:.1f}M"
            elif out_tokens >= 1_000:
                tok_str = f"{out_tokens / 1_000:.0f}K"
            else:
                tok_str = str(out_tokens)
            pct_of_total = (out_tokens / total_tokens * 100) if total_tokens > 0 else 0
            legend_lines.append(
                f"[{color}]\u2588\u2588[/{color}] {directive:<25s} {tok_str:>6s} {pct_of_total:>4.0f}%"
            )
        legend_widget.update("\n".join(legend_lines))

        # Populate table
        table = self.query_one("#attr-table", DataTable)
        table.clear()

        for s in sessions:
            first = s["first_ts"].astimezone().strftime("%H:%M")
            last = s["last_ts"].astimezone().strftime("%H:%M")
            time_range = f"{first}\u2013{last}"

            color = s["color"]
            color_block = Text("\u2588\u2588", style=color)
            directive = s["directive"][:30] if s["directive"] else "\u2014"
            pct_str = f"{s['pct_used']:.1f}%"
            tokens = f"{s['output_tokens']:,}" if s["output_tokens"] else "\u2014"
            model = s.get("model", "?")
            tools = str(s["tool_count"])
            sid = s["session_id"][:12]

            table.add_row(color_block, sid, directive, time_range, pct_str, tokens, model, tools)

        if unaccounted > 0.5:
            table.add_row(
                Text("\u2591\u2591", style="dim"),
                "\u2014", "Rolled out of window", "\u2014",
                f"{unaccounted:.1f}%", "\u2014", "\u2014", "\u2014"
            )



class RulesView(LazyView):
    """Rules — all active hooks, budget limits, and permission rules with trigger history."""

    def compose(self) -> ComposeResult:
        yield Static(id="rules-header")
        yield DataTable(id="rules-table")
        yield Static(id="rules-detail-header")
        yield DataTable(id="rules-detail")

    def load_content(self):
        from token_watch_data import _get_rules_summary

        rules, block_events = _get_rules_summary()

        total_triggers = sum(r.get("triggers", 0) for r in rules)
        total_blocks = sum(r.get("blocks", 0) for r in rules)
        active_count = sum(1 for r in rules if r.get("enabled"))

        self.query_one("#rules-header", Static).update(
            f"[bold]Rules[/bold]  [dim]{active_count} active rules · "
            f"{total_triggers} triggers this cycle · "
            f"{total_blocks} blocks this cycle[/dim]"
        )

        dt = self.query_one("#rules-table", DataTable)
        dt.cursor_type = "row"
        dt.zebra_stripes = True
        dt.add_column("Type", width=12)
        dt.add_column("Name", width=20)
        dt.add_column("Phase", width=12)
        dt.add_column("Status", width=8)
        dt.add_column("Triggers", width=9)
        dt.add_column("Blocks", width=8)
        dt.add_column("Last Triggered", width=16)
        dt.add_column("Description")

        self._rules_list = rules

        for i, r in enumerate(rules):
            rtype = r["type"]
            type_style = "magenta" if rtype == "hook" else ("yellow" if rtype == "budget" else "cyan")
            enabled = r.get("enabled", True)
            status = "ON" if enabled else "OFF"
            status_style = "green" if enabled else "red"
            blocks = r.get("blocks", 0)
            block_style = "bold red" if blocks > 0 else "dim"

            last = r.get("last_triggered", "")
            if last:
                try:
                    last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                    age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
                    if age_min < 60:
                        last_str = f"{age_min:.0f}m ago"
                    elif age_min < 1440:
                        last_str = f"{age_min / 60:.0f}h ago"
                    else:
                        last_str = f"{age_min / 1440:.0f}d ago"
                except Exception:
                    last_str = last[:16]
            else:
                last_str = "\u2014"

            dt.add_row(
                Text(rtype, style=type_style),
                Text(r["name"], style="white" if enabled else "dim"),
                Text(r.get("phase", "\u2014"), style="dim"),
                Text(status, style=status_style),
                Text(str(r.get("triggers", 0)), justify="right"),
                Text(str(blocks), style=block_style, justify="right"),
                Text(last_str, style="dim"),
                Text(r.get("desc", ""), style="dim"),
                key=f"rule-{i}",
            )

        # Block events detail
        self.query_one("#rules-detail-header", Static).update(
            f"[bold]Recent Blocks[/bold]  [dim]{len(block_events)} this cycle · "
            f"select a rule above to filter[/dim]"
        )

        bt = self.query_one("#rules-detail", DataTable)
        bt.cursor_type = "none"
        bt.zebra_stripes = True
        bt.add_column("Time", width=22)
        bt.add_column("Rule", width=18)
        bt.add_column("Detail")

        for evt in block_events[-20:]:
            bt.add_row(
                Text(evt.get("ts", "?"), style="dim"),
                Text(evt.get("rule", "?"), style="yellow"),
                Text(evt.get("detail", "")[:80], style="white"),
            )

        if not block_events:
            bt.add_row(
                Text("\u2014", style="dim"),
                Text("No blocks this cycle", style="dim"),
                Text("", style="dim"),
            )

    def on_data_table_row_selected(self, event):
        """When a rule row is selected, filter the detail table to show only that rule's events."""
        from token_watch_data import _get_rule_events

        row_key = str(event.row_key.value) if hasattr(event.row_key, 'value') else str(event.row_key)
        if not row_key.startswith("rule-"):
            return

        try:
            idx = int(row_key.split("-")[1])
            rule = self._rules_list[idx]
        except (IndexError, ValueError):
            return

        rule_name = rule["name"]
        events = _get_rule_events(rule_name, limit=30)

        self.query_one("#rules-detail-header", Static).update(
            f"[bold]Events for {rule_name}[/bold]  [dim]{len(events)} total[/dim]"
        )

        bt = self.query_one("#rules-detail", DataTable)
        bt.clear(columns=True)
        bt.cursor_type = "none"
        bt.zebra_stripes = True
        bt.add_column("Time", width=22)
        bt.add_column("Level", width=8)
        bt.add_column("Detail")

        for evt in events:
            level = evt.get("level", "?")
            level_style = "red" if level == "WARN" else ("yellow" if level == "INFO" else "dim")
            bt.add_row(
                Text(evt.get("ts", "?"), style="dim"),
                Text(level, style=level_style),
                Text(evt.get("detail", "")[:80], style="white"),
            )

        if not events:
            bt.add_row(
                Text("\u2014", style="dim"),
                Text("\u2014", style="dim"),
                Text(f"No events for {rule_name}", style="dim"),
            )


class TestDetailScreen(Screen):
    """Detail view for a build_ledger / test queue item."""

    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
        Binding("p", "mark_pass", "Pass"),
        Binding("f", "mark_fail", "Fail"),
        Binding("s", "mark_skip", "Skip"),
    ]

    def __init__(self, item, **kwargs):
        super().__init__(**kwargs)
        self._item = item

    def compose(self) -> ComposeResult:
        yield Static(id="td-content")

    def on_mount(self):
        item = self._item
        files = item.get("files") or []

        # Format timestamp: "01:10 — Apr 8, 2026" 
        raw_ts = item.get("created_at", "")
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            formatted_ts = dt.strftime("%H:%M — %b %-d, %Y")
        except Exception:
            formatted_ts = raw_ts
        files_list = "\n".join(f"    {f}" for f in files) if files else "    (none)"
        hint = item.get("test_hint", "") or item.get("route", "") or "No instructions"
        notes = item.get("notes", "") or ""
        status = item.get("test_status", item.get("status", "untested"))

        status_style = {"untested": "yellow", "tested": "green", "failed": "red", "skipped": "dim",
                        "pending": "yellow", "pass": "green", "fail": "red", "skip": "dim"}
        st_color = status_style.get(status, "white")

        body = f"""[bold]{item.get('title', '—')}[/bold]
[dim]{formatted_ts}[/dim]

[dim]{'─' * 60}[/dim]

[bold cyan]How to Verify[/bold cyan]
    [italic]{hint}[/italic]

[bold cyan]Details[/bold cyan]
    Project:  [cyan]{item.get('project', '—')}[/cyan]  ({item.get('company', '—')})
    Type:     {item.get('item_type', '—')}
    Difficulty: {item.get('difficulty', 'medium')}  |  Points: {item.get('points', 1)}
    Session:  {item.get('session_id', '—')}
    Commit:   {item.get('commit_sha', '—')}
    Source:   {item.get('source', '—')}
    Status:   [{st_color}]{status}[/{st_color}]

[bold cyan]Files Changed[/bold cyan]
{files_list}
"""
        if notes:
            body += f"""
[bold cyan]Notes[/bold cyan]
    {notes}
"""
        body += """
[dim]p=pass  f=fail  s=skip  q/Esc=back[/dim]"""

        self.query_one("#td-content", Static).update(body)

    def _mark(self, status):
        from token_watch_data import _update_test_item
        _update_test_item(self._item["id"], status)
        self.app.pop_screen()

    def action_mark_pass(self):
        self._mark("pass")

    def action_mark_fail(self):
        self._mark("fail")

    def action_mark_skip(self):
        self._mark("skip")

    def action_pop_screen(self):
        self.app.pop_screen()


class TestQueueView(LazyView):
    """Test Queue — things that need manual verification after shipping."""

    def refresh_content(self):
        now = time.time()
        if not hasattr(self, '_last_refresh') or (now - self._last_refresh) > 15:
            self._last_refresh = now
            self._reload_data()

    BINDINGS = [
        Binding("enter", "show_detail", "Detail", show=False),
        Binding("p", "mark_pass", "Pass"),
        Binding("f", "mark_fail", "Fail"),
        Binding("s", "mark_skip", "Skip"),
        Binding("d", "delete_item", "Delete"),
        Binding("i", "import_qa", "Import QA"),
        Binding("r", "reload", "Reload"),
        Binding("a", "toggle_all", "All/Pending"),
        Binding("y", "import_sessions", "Import Sessions"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._items = []
        self._filter_project = ""
        self._show_all = False

    def compose(self) -> ComposeResult:
        yield Static(id="tq-header")
        yield Static(id="tq-filters")
        yield DataTable(id="tq-table")
        yield Static(id="tq-footer")

    def load_content(self):
        self._reload_data()

    def _reload_data(self):
        status_filter = None if self._show_all else "pending"
        project_filter = self._filter_project if self._filter_project else None
        cycle_id = getattr(self.app, '_active_cycle_id', None) if hasattr(self, 'app') else None
        self._items = _get_test_queue(project=project_filter, status=status_filter, cycle_id=cycle_id)
        self._render_table()

    def _render_table(self):
        pending = sum(1 for i in self._items if i.get("status") == "pending")
        passed  = sum(1 for i in self._items if i.get("status") == "pass")
        failed  = sum(1 for i in self._items if i.get("status") == "fail")
        skipped = sum(1 for i in self._items if i.get("status") == "skip")

        mode_label = "all" if self._show_all else "pending only"
        proj_label = self._filter_project or "all projects"
        total_points = sum(i.get("points", 1) for i in self._items if i.get("status") in ("pass",))
        pending_points = sum(i.get("points", 1) for i in self._items if i.get("status") == "pending")
        cycle_id = getattr(self.app, '_active_cycle_id', None) if hasattr(self, 'app') else None
        cycle_tag = "[magenta]ALL[/magenta]" if cycle_id is None else "[dim]cycle[/dim]"
        self.query_one("#tq-header", Static).update(
            f"[bold cyan]Test Queue[/bold cyan] {cycle_tag}  "
            f"[yellow]{pending} pending ({pending_points}pts)[/yellow]  "
            f"[green]{passed} passed ({total_points}pts)[/green]  "
            f"[red]{failed} failed[/red]  "
            f"[dim]{skipped} skipped  ·  {mode_label}  ·  {proj_label}[/dim]"
        )

        # Project filter pills
        projects = sorted({i.get("project", "") for i in self._items if i.get("project")})
        pills = []
        for p in [""] + projects:
            active = p == self._filter_project
            label = p or "all"
            pills.append(f"[{'bold cyan' if active else 'dim'}][{label}][/{'bold cyan' if active else 'dim'}]")
        self.query_one("#tq-filters", Static).update("  ".join(pills))

        dt = self.query_one("#tq-table", DataTable)
        dt.clear(columns=True)
        dt.cursor_type = "row"
        dt.zebra_stripes = True
        dt.add_column("#",       width=4)
        dt.add_column("Project", width=10)
        dt.add_column("Title",   width=30)
        dt.add_column("How to Verify", width=26)
        dt.add_column("Diff",    width=6)
        dt.add_column("Pts",     width=4)
        dt.add_column("Src",     width=8)
        dt.add_column("St",      width=4)

        if not self._items:
            dt.add_row(
                "—", "", Text("no items — press i to import Atlas QA tests", style="dim"),
                "", "", "", "", "",
            )
        else:
            now = datetime.now(timezone.utc)
            for idx, item in enumerate(self._items, 1):
                status = item.get("status", "pending")
                st_icon = {
                    "pending": Text("·", style="yellow"),
                    "pass":    Text("✓", style="green"),
                    "fail":    Text("✗", style="red"),
                    "skip":    Text("−", style="dim"),
                }.get(status, Text("·"))

                pri = item.get("priority", "normal")
                pri_style = "red" if pri in ("high", "critical") else ("yellow" if pri == "normal" else "dim")

                try:
                    created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
                    delta = now - created
                    age = f"{delta.days}d" if delta.days > 0 else f"{delta.seconds // 3600}h"
                except Exception:
                    age = "—"

                proj = item.get("project", "—")
                proj_style = (
                    "cyan" if proj == "atlas" else
                    ("yellow" if proj == "kaa" else
                    ("magenta" if proj == "paperclip" else "dim"))
                )

                src = item.get("source", "—")
                src_ref = item.get("source_ref", "")
                if src == "session" and src_ref:
                    # Look up session slug for readable display
                    from token_watch_data import _get_session_history
                    slug = ""
                    for _s in _get_session_history():
                        if _s.get("session_id", "").startswith(src_ref[:8]):
                            slug = _s.get("slug", "")[:12]
                            break
                    src_display = slug if slug else f"cc/{src_ref[:6]}"
                elif src == "qa":
                    src_display = f"qa/{src_ref[:4]}" if src_ref else "qa"
                else:
                    src_display = src[:8] if src else "—"

                hint = item.get("test_hint", "") or item.get("route", "") or ""
                diff = item.get("difficulty", "medium")
                diff_style = {"easy": "green", "medium": "yellow", "hard": "red", "complex": "bold red"}.get(diff, "white")
                pts = str(item.get("points", 1))
                dt.add_row(
                    str(idx),
                    Text(proj, style=proj_style),
                    Text(item.get("title", "—")[:30]),
                    Text(hint[:26], style="italic"),
                    Text(diff[:5], style=diff_style),
                    Text(pts, style="bold"),
                    Text(src_display, style="dim"),
                    st_icon,
                    key=item["id"],
                )

        self.query_one("#tq-footer", Static).update(
            "[dim]p[/dim]=pass  [dim]f[/dim]=fail  [dim]s[/dim]=skip  "
            "[dim]d[/dim]=delete  [dim]i[/dim]=import Atlas QA  "
            "[dim]y[/dim]=import sessions  [dim]a[/dim]=toggle all/pending  [dim]r[/dim]=reload"
        )

    def _get_selected_index(self):
        """Get the index into self._items for the cursor row."""
        dt = self.query_one("#tq-table", DataTable)
        if not dt.row_count or not self._items:
            return -1
        row = dt.cursor_row
        # Row 0 = item 0 (if items exist and no header rows)
        if 0 <= row < len(self._items):
            return row
        return -1

    def _get_selected_id(self):
        idx = self._get_selected_index()
        if idx >= 0:
            return str(self._items[idx].get("id", ""))
        return ""

    def _mark_selected(self, status):
        item_id = self._get_selected_id()
        if item_id:
            _update_test_item(item_id, status)
            self._reload_data()

    def action_mark_pass(self):
        self._mark_selected("pass")

    def action_mark_fail(self):
        self._mark_selected("fail")

    def action_mark_skip(self):
        self._mark_selected("skip")

    def action_delete_item(self):
        item_id = self._get_selected_id()
        if item_id and not item_id.startswith("—"):
            _delete_test_item(item_id)
            self._reload_data()

    def action_import_qa(self):
        self.query_one("#tq-header", Static).update(
            "[yellow]Importing Atlas QA tests...[/yellow]"
        )
        try:
            count = _import_atlas_qa_tests()
            self._reload_data()
            # Brief success message (will be overwritten by next _render)
            if count == 0:
                self.query_one("#tq-header", Static).update(
                    "[dim]All Atlas QA tests already imported (nothing new)[/dim]"
                )
        except Exception as e:
            self.query_one("#tq-header", Static).update(
                f"[red]Import failed: {e}[/red]"
            )

    def action_import_sessions(self):
        self.query_one("#tq-header", Static).update(
            "[yellow]Scraping cycle sessions...[/yellow]"
        )
        try:
            count = _scrape_cycle_sessions()
            self._reload_data()
            if count == 0:
                self.query_one("#tq-header", Static).update(
                    "[dim]All cycle sessions already scraped (nothing new)[/dim]"
                )
        except Exception as e:
            self.query_one("#tq-header", Static).update(
                f"[red]Session scrape failed: {e}[/red]"
            )

    def action_reload(self):
        self._reload_data()

    def action_toggle_all(self):
        self._show_all = not self._show_all
        self._reload_data()

    def action_show_detail(self):
        """Open detail view for selected test item."""
        idx = self._get_selected_index()
        if idx >= 0:
            self.app.push_screen(TestDetailScreen(self._items[idx]))

    def on_data_table_row_selected(self, event):
        """Enter pressed on a row — open detail view."""
        idx = self._get_selected_index()
        if idx >= 0:
            self.app.push_screen(TestDetailScreen(self._items[idx]))


class MissionControlView(LazyView):
    """Mission Control — unified view of everything built, grouped by company/project."""

    BINDINGS = [
        Binding("t", "toggle_test_status", "Test"),
    ]

    def refresh_content(self):
        now = time.time()
        if not hasattr(self, '_last_refresh') or (now - self._last_refresh) > 15:
            self._last_refresh = now
            try:
                self.query_one("#mission-table", DataTable).clear(columns=True)
                self.load_content()
            except Exception:
                pass

    def compose(self) -> ComposeResult:
        yield Static(id="mission-header")
        yield DataTable(id="mission-table")

    _recovery_filter = False

    def load_content(self):
        from token_watch_data import _get_build_ledger
        cycle_id = getattr(self.app, '_active_cycle_id', None)
        source = "recovery" if self._recovery_filter else None
        data = _get_build_ledger(days=7, limit=100, cycle_id=cycle_id, source=source)
        stats = data["stats"]

        # Cycle navigation indicator
        cycle_id = getattr(self.app, '_active_cycle_id', None)
        if cycle_id is None:
            cycle_label = "[bold magenta]ALL CYCLES[/bold magenta]"
        else:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(cycle_id)
                cycle_label = f"[bold]Cycle: {dt.strftime('%b %-d, %H:%M')}[/bold]"
                if getattr(self.app, '_cycle_idx', 0) == 0:
                    cycle_label += " [green](current)[/green]"
            except Exception:
                cycle_label = f"[bold]Cycle: {cycle_id[:16]}[/bold]"

        recovery_tag = "  [bold yellow][RECOVERY FILTER][/bold yellow]" if self._recovery_filter else ""
        self.query_one("#mission-header", Static).update(
            f"{cycle_label}  "
            f"[dim]\u25C0 [  ] \u25B6  |  0=all  |  r=recovery[/dim]"
            f"{recovery_tag}  "
            f"[dim]{stats['total']} shipped  ·  "
            f"[yellow]{stats['untested']} untested[/yellow]  ·  "
            f"[cyan]{stats['decisions']} decisions[/cyan]  ·  "
            f"{stats['sessions']} sessions[/dim]"
        )

        table = self.query_one("#mission-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("", width=2)       # status icon
        table.add_column("Time", width=11)
        table.add_column("SHA", width=8)
        table.add_column("Session", width=7)
        table.add_column("File", width=4)
        table.add_column("Test", width=9)
        table.add_column("Title", width=33)
        table.add_column("How to Verify", width=38)

        status_icons = {
            "tested": ("\u2713", "green"),
            "untested": ("\u25CB", "yellow"),
            "failed": ("\u2717", "red"),
        }
        type_styles = {
            "feature": ("feat", "bold"),
            "fix": ("fix", "red"),
            "refactor": ("refac", "blue"),
            "decision": ("\u25B3", "cyan"),
            "docs": ("docs", "dim"),
            "test": ("test", "green"),
            "chore": ("chore", "dim"),
            "infra": ("infra", "magenta"),
        }

        # Company display order
        company_order = ["delphi", "kaa", "frank", "personal"]
        company_labels = {"delphi": "DELPHI", "kaa": "KAA", "frank": "FRANK", "personal": "PERSONAL"}

        # Track row keys and data for t-hotkey lookup.
        # _row_keys_ordered is parallel to the DataTable rows: each entry is
        # either a row_key string (for item rows) or None (for separator rows).
        self._row_data = {}
        self._row_keys_ordered = []

        for co in company_order:
            projects = data["by_company"].get(co, {})
            if not projects:
                continue
            for proj in sorted(projects.keys()):
                items = projects[proj]
                # Separator row
                label = f"{company_labels.get(co, co.upper())} / {proj}"
                table.add_row(
                    Text(""), Text(""), Text(""), Text(""), Text(""), Text(""),
                    Text(f"\u2500\u2500 {label} \u2500\u2500", style="bold yellow"),
                    Text(""),
                )
                self._row_keys_ordered.append(None)  # separator row

                for _row_idx, item in enumerate(items):
                    ts_raw = item.get("created_at", "")
                    if "T" in ts_raw:
                        try:
                            from datetime import datetime as _dt
                            _d = _dt.fromisoformat(ts_raw.replace("Z", "+00:00")).astimezone()
                            ts = _d.strftime("%-m/%-d %H:%M")
                        except Exception:
                            ts = ts_raw.split("T")[1][:5]
                    else:
                        ts = ts_raw[:10] if ts_raw else ""

                    test = item.get("test_status", "untested")
                    icon, icon_style = status_icons.get(test, ("?", "white"))
                    if item.get("item_type") == "decision":
                        icon, icon_style = ("\u25B3", "cyan")

                    type_label, type_style = type_styles.get(item.get("item_type", ""), ("?", "white"))

                    files = item.get("files") or []
                    file_count = str(len(files)) if files else ""

                    hint = item.get("test_hint", "") or ""
                    sha = (item.get("commit_sha") or "")[:7]
                    row_key = (
                        f"{item.get('project', '')}|{sha}|{_row_idx}|{item.get('id', '')}"
                        if sha else None
                    )
                    table.add_row(
                        Text(icon, style=icon_style),
                        Text(ts, style="dim"),
                        Text(sha, style="cyan"),
                        Text(item.get("session_id", ""), style="dim"),
                        Text(file_count, style="dim"),
                        Text(test, style=icon_style),
                        Text(item.get("title", "")[:33]),
                        Text(hint[:38], style="italic dim"),
                        key=row_key,
                    )
                    self._row_keys_ordered.append(row_key)
                    if row_key:
                        self._row_data[row_key] = item

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        try:
            table = self.query_one("#mission-table", DataTable)
        except Exception:
            return
        cursor_key = str(event.row_key.value or "")
        if not cursor_key or "|" not in cursor_key:
            table.tooltip = None
            return
        item = self._row_data.get(cursor_key, {})
        title = item.get("title", "") or ""
        hint = item.get("test_hint", "") or ""
        sha = (item.get("commit_sha") or "")[:7]
        project = item.get("project", "") or ""
        session = (item.get("session_id") or "")
        ts = item.get("created_at", "")
        if "T" in ts:
            ts = ts.split("T")[1][:5]
        files = item.get("files") or []
        parts = []
        if title:
            parts.append(title)
        if files:
            names = [f.split("/")[-1] for f in files[:3]]
            suffix = f" +{len(files)-3} more" if len(files) > 3 else ""
            parts.append(f"files: {', '.join(names)}{suffix}")
        if hint:
            parts.append(f"verify: {hint}")
        meta = "  ".join(x for x in [session[:16], project, sha, ts] if x)
        if meta:
            parts.append(meta)
        table.tooltip = "\n".join(parts) if parts else None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = str(event.row_key.value or "")
        parts = key.split("|")
        if len(parts) < 2:
            return
        project, sha = parts[0], parts[1]
        if not sha:
            return
        repo_map = {
            "atlas": "atlas-portal",
            "atlas-backend": "atlas-backend",
            "token-watch": "token-watch",
            "battlestation": "battlestation",
            "paperclip": "paperclip",
            "frank-pilot": "frank-pilot",
            "openclaw": "openclaw",
            "kaa": "openclaw",
        }
        repo = repo_map.get(project, project)
        import subprocess
        gh_user = os.environ.get("BS_GITHUB_USER", "")
        if gh_user:
            subprocess.run(["open", f"https://github.com/{gh_user}/{repo}/commit/{sha}"])

    def action_toggle_test_status(self):
        """Cycle the test_status on the currently-selected row:
        untested → tested → failed → untested.
        """
        from token_watch_data import _update_test_item
        try:
            table = self.query_one("#mission-table", DataTable)
        except Exception:
            return
        row_idx = getattr(table, "cursor_row", None)
        if row_idx is None or row_idx < 0:
            return
        row_keys = getattr(self, "_row_keys_ordered", [])
        if row_idx >= len(row_keys):
            return
        cursor_key = row_keys[row_idx]
        if not cursor_key or "|" not in cursor_key:
            return
        parts = cursor_key.split("|")
        if len(parts) < 4:
            return
        item_id = parts[3]
        if not item_id:
            return
        # Look up current status
        item = self._row_data.get(cursor_key, {})
        current = item.get("test_status", "untested")
        cycle = {"untested": "tested", "tested": "failed", "failed": "untested"}
        new_status = cycle.get(current, "tested")
        if _update_test_item(item_id, new_status):
            try:
                self.notify(f"{item.get('title', 'item')[:40]} → {new_status}")
            except Exception:
                pass
        # Force reload
        self._last_refresh = 0
        self.refresh_content()


class ProjectsView(LazyView):
    """Projects — per-project time/token spend with status and recommendations."""

    def refresh_content(self):
        now = time.time()
        if not hasattr(self, '_last_refresh') or (now - self._last_refresh) > 30:
            self._last_refresh = now
            try:
                self.query_one("#projects-table", DataTable).clear(columns=True)
                self.load_content()
            except Exception:
                pass

    def compose(self) -> ComposeResult:
        yield Static(id="projects-header")
        yield DataTable(id="projects-table")
        yield Static(id="projects-recs")

    def load_content(self):
        from datetime import datetime, timezone
        from token_watch_data import _get_project_stats

        try:
            stats = _get_project_stats(days=7)
        except Exception:
            stats = {}

        now_utc = datetime.now(timezone.utc)

        # Compute header totals
        total_tokens = sum(v.get("tokens", 0) for v in stats.values())
        total_cost = sum(v.get("cost", 0.0) for v in stats.values())
        n_projects = len(stats)
        self.query_one("#projects-header", Static).update(
            f"[bold]Projects[/bold]  [dim]last 7 days  ·  "
            f"{n_projects} projects  ·  "
            f"{total_tokens/1000:.0f}k total tokens  ·  "
            f"${total_cost:.2f}[/dim]"
        )

        table = self.query_one("#projects-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("Company", width=10)
        table.add_column("Project", width=16)
        table.add_column("Commits", width=8)
        table.add_column("Tokens", width=10)
        table.add_column("Cost", width=8)
        table.add_column("Bar", width=22)
        table.add_column("Untested", width=9)
        table.add_column("Status", width=14)

        self._row_data = {}  # tooltip + selection data

        if not stats:
            table.add_row(
                Text("—", style="dim"),
                Text("(no data)", style="dim italic"),
                Text(""), Text(""), Text(""), Text(""), Text(""),
                Text("NO_DATA", style="dim"),
            )
            self.query_one("#projects-recs", Static).update(
                "[dim]No project activity in the last 7 days.[/dim]"
            )
            return

        # Rank by tokens for bar chart scale (fallback to commits if no tokens)
        max_tokens = max((v.get("tokens", 0) for v in stats.values()), default=0)
        max_commits = max((v.get("commits", 0) for v in stats.values()), default=0)
        use_commits_for_bar = (max_tokens == 0 and max_commits > 0)

        # Sort projects by tokens desc, then commits desc
        ordered = sorted(
            stats.items(),
            key=lambda kv: (kv[1].get("tokens", 0), kv[1].get("commits", 0)),
            reverse=True,
        )

        for proj, d in ordered:
            company_raw = d.get("company", "") or ""
            co_label, co_style = _project_to_company(proj, company_raw)
            commits = d.get("commits", 0)
            commits_2d = d.get("commits_recent_2d", 0)
            tokens = d.get("tokens", 0)
            cost = d.get("cost", 0.0)
            untested = d.get("untested", 0)
            last_dt = d.get("last_commit_dt")

            # Days since last commit
            days_since = None
            if last_dt is not None:
                try:
                    days_since = (now_utc - last_dt).total_seconds() / 86400.0
                except Exception:
                    days_since = None

            # Status determination — HOT uses precise 2-day commit count per spec
            is_hot = (commits_2d > 3)
            is_stalled = (days_since is not None and days_since > 4 and commits > 0)
            needs_tests = untested > 5

            if commits == 0 and tokens == 0:
                status, status_style = "NO_DATA", "dim"
            elif needs_tests:
                status, status_style = "NEEDS_TESTS", "red"
            elif is_hot:
                status, status_style = "HOT", "green"
            elif is_stalled:
                status, status_style = "STALLED", "yellow"
            else:
                status, status_style = "OK", "dim"

            # ASCII bar (20 cells wide)
            bar_scale = (tokens if not use_commits_for_bar else commits)
            bar_max = (max_tokens if not use_commits_for_bar else max_commits) or 1
            filled = int(round(20 * bar_scale / bar_max)) if bar_max > 0 else 0
            bar = "\u2588" * filled + " " * (20 - filled)

            tok_str = f"{tokens/1000:.1f}k" if tokens >= 1000 else str(tokens)
            cost_str = f"${cost:.2f}" if cost > 0 else "—"

            row_key = f"proj|{proj}"
            table.add_row(
                Text(co_label, style=co_style),
                Text(proj[:16]),
                Text(str(commits), style="cyan"),
                Text(tok_str),
                Text(cost_str, style="green" if cost > 0 else "dim"),
                Text(bar, style=co_style),
                Text(str(untested), style="red" if untested > 5 else "dim"),
                Text(status, style=status_style),
                key=row_key,
            )
            self._row_data[row_key] = {
                "project": proj,
                "company": company_raw or co_label,
                "tokens": tokens,
                "cost": cost,
                "commits": commits,
                "commits_2d": commits_2d,
                "untested": untested,
                "last_dt": last_dt,
                "days_since": days_since,
                "status": status,
            }

        # Recommendations panel
        recs = self._build_recommendations(stats)
        if recs:
            self.query_one("#projects-recs", Static).update(
                "[bold]Recommendations[/bold]\n" + "\n".join(f"• {r}" for r in recs[:4])
            )
        else:
            self.query_one("#projects-recs", Static).update(
                "[dim]No recommendations — all projects healthy.[/dim]"
            )

    def _build_recommendations(self, stats):
        from datetime import datetime, timezone
        recs = []
        now_utc = datetime.now(timezone.utc)

        # Most active by commits
        if stats:
            top = max(stats.items(), key=lambda kv: kv[1].get("commits", 0))
            if top[1].get("commits", 0) > 0:
                recs.append(
                    f"[green]{top[0]}[/green] most active this week "
                    f"({top[1]['commits']} commits)"
                )

        # Untested rollup
        heavy_untested = sorted(
            ((p, d) for p, d in stats.items() if d.get("untested", 0) > 5),
            key=lambda kv: kv[1]["untested"],
            reverse=True,
        )
        for p, d in heavy_untested[:2]:
            recs.append(
                f"[red]{p}[/red] has {d['untested']} untested items "
                f"— run test verification before next ship"
            )

        # Stalled projects
        for p, d in stats.items():
            last_dt = d.get("last_commit_dt")
            if last_dt is None or d.get("commits", 0) == 0:
                continue
            try:
                days_since = (now_utc - last_dt).total_seconds() / 86400.0
            except Exception:
                continue
            if days_since > 4:
                recs.append(
                    f"[yellow]{p}[/yellow] stalled {int(days_since)} days — needs a session"
                )
                if len(recs) >= 4:
                    break

        # Biggest token spender
        token_sorted = sorted(
            stats.items(), key=lambda kv: kv[1].get("tokens", 0), reverse=True
        )
        if token_sorted and token_sorted[0][1].get("tokens", 0) > 0:
            p, d = token_sorted[0]
            recs.append(
                f"{p} burned {d['tokens']/1000:.0f}k tokens (${d['cost']:.2f})"
            )

        return recs

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        try:
            table = self.query_one("#projects-table", DataTable)
        except Exception:
            return
        key = str(event.row_key.value or "")
        if not key:
            table.tooltip = None
            return
        d = getattr(self, "_row_data", {}).get(key)
        if not d:
            table.tooltip = None
            return
        parts = [d["project"]]
        last_dt = d.get("last_dt")
        if last_dt is not None:
            parts.append(f"last commit: {last_dt.strftime('%Y-%m-%d %H:%M UTC')}")
        parts.append(f"tokens: {d['tokens']:,}  ·  ${d['cost']:.2f}")
        parts.append(
            f"commits: {d['commits']} (2d: {d['commits_2d']})  ·  "
            f"untested: {d['untested']}  ·  {d['status']}"
        )
        table.tooltip = "\n".join(parts)


class DelphiView(LazyView):
    """Delphi — token/activity usage scoped to Atlas + Paperclip."""

    def refresh_content(self):
        now = time.time()
        if not hasattr(self, '_last_refresh') or (now - self._last_refresh) > 15:
            self._last_refresh = now
            try:
                self.query_one("#delphi-apps", DataTable).clear(columns=True)
                self.query_one("#delphi-sessions", DataTable).clear(columns=True)
                self.query_one("#delphi-builds", DataTable).clear(columns=True)
                self.load_content()
            except Exception:
                pass

    def compose(self) -> ComposeResult:
        yield Static(id="delphi-header")
        yield Static("[bold]App Breakdown[/bold]  [dim]Delphi OS — Atlas + Paperclip[/dim]", id="delphi-apps-label")
        yield DataTable(id="delphi-apps")
        yield Static("[bold]Active Sessions[/bold]  [dim]working on Delphi right now[/dim]", id="delphi-sessions-label")
        yield DataTable(id="delphi-sessions")
        yield Static("[bold]Recent Builds[/bold]  [dim]last 48h[/dim]", id="delphi-builds-label")
        yield DataTable(id="delphi-builds")

    def load_content(self):
        from token_watch_data import (
            _get_delphi_app_breakdown,
            _get_delphi_active_sessions,
            _get_delphi_recent_builds,
        )

        breakdown = _get_delphi_app_breakdown()
        sessions = _get_delphi_active_sessions()
        builds = _get_delphi_recent_builds()

        total_items = sum(row.get("item_count", 0) for row in breakdown)
        total_sessions = len(sessions)
        total_builds = len(builds)

        self.query_one("#delphi-header", Static).update(
            f"[bold blue]DELPHI[/bold blue]  [dim]Atlas + Paperclip[/dim]  "
            f"[dim]{total_items} items shipped (7d)  ·  "
            f"[green]{total_sessions} active sessions[/green]  ·  "
            f"[cyan]{total_builds} builds (48h)[/cyan][/dim]"
        )

        # ── Table 1: App Breakdown ────────────────────────────────────────
        apps = self.query_one("#delphi-apps", DataTable)
        apps.cursor_type = "row"
        apps.zebra_stripes = True
        apps.add_column("Project", width=22)
        apps.add_column("Company", width=10)
        apps.add_column("Items", width=7)
        apps.add_column("Sessions", width=9)
        apps.add_column("Last Activity", width=16)

        # Index active sessions by project for quick lookup
        active_by_project = {}  # type: dict
        for s in sessions:
            proj = (s.get("repo") or "").strip().lower()
            if not proj:
                continue
            active_by_project[proj] = active_by_project.get(proj, 0) + 1

        if not breakdown:
            apps.add_row(
                Text("(no data yet — warming)", style="dim italic"),
                Text(""), Text(""), Text(""), Text(""),
            )
        else:
            for row in breakdown:
                proj = row.get("project", "")
                last = row.get("last_activity", "") or ""
                if "T" in last:
                    # Render as "MM-DD HH:MM"
                    last = last.replace("T", " ")[5:16]
                active_count = active_by_project.get(proj.lower(), 0)
                apps.add_row(
                    Text(proj, style="bold"),
                    Text(row.get("company", "") or "—", style="blue"),
                    Text(str(row.get("item_count", 0)), style="cyan"),
                    Text(str(active_count) if active_count else "—",
                         style="green" if active_count else "dim"),
                    Text(last, style="dim"),
                )

        # ── Table 2: Active Sessions ──────────────────────────────────────
        sess_table = self.query_one("#delphi-sessions", DataTable)
        sess_table.cursor_type = "row"
        sess_table.zebra_stripes = True
        sess_table.add_column("Session", width=11)
        sess_table.add_column("Repo", width=20)
        sess_table.add_column("Task", width=40)
        sess_table.add_column("Heartbeat", width=10)

        if not sessions:
            sess_table.add_row(
                Text("(no active Delphi sessions)", style="dim italic"),
                Text(""), Text(""), Text(""),
            )
        else:
            for s in sessions:
                sid = (s.get("session_id") or "").replace("cc-", "")
                hb = s.get("heartbeat_at") or ""
                if "T" in hb:
                    hb = hb.split("T")[1][:8]
                sess_table.add_row(
                    Text(sid, style="bold"),
                    Text((s.get("repo") or "—")[:20]),
                    Text((s.get("task_name") or "—")[:40], style="italic"),
                    Text(hb, style="dim"),
                )

        # ── Table 3: Recent Builds ────────────────────────────────────────
        builds_table = self.query_one("#delphi-builds", DataTable)
        builds_table.cursor_type = "row"
        builds_table.zebra_stripes = True
        builds_table.add_column("Time", width=11)
        builds_table.add_column("Project", width=18)
        builds_table.add_column("Type", width=9)
        builds_table.add_column("Title", width=55)

        type_styles = {
            "feature": ("feat", "bold"),
            "fix": ("fix", "red"),
            "refactor": ("refac", "blue"),
            "decision": ("\u25B3", "cyan"),
            "docs": ("docs", "dim"),
            "test": ("test", "green"),
            "chore": ("chore", "dim"),
            "infra": ("infra", "magenta"),
        }

        if not builds:
            builds_table.add_row(
                Text("(no recent Delphi builds)", style="dim italic"),
                Text(""), Text(""), Text(""),
            )
        else:
            for item in builds:
                ts = item.get("created_at", "") or ""
                if "T" in ts:
                    ts = ts.replace("T", " ")[5:16]
                type_label, type_style = type_styles.get(
                    item.get("item_type", ""), ("?", "white")
                )
                builds_table.add_row(
                    Text(ts, style="dim"),
                    Text((item.get("project") or "—")[:18]),
                    Text(type_label, style=type_style),
                    Text((item.get("title") or "")[:55]),
                )


class WireView(LazyView):
    """Wire — inter-session message log."""

    def refresh_content(self):
        now = time.time()
        if not hasattr(self, '_last_refresh') or (now - self._last_refresh) > 15:
            self._last_refresh = now
            try:
                self.query_one("#wire-table", DataTable).clear(columns=True)
                self.load_content()
            except Exception:
                pass

    def compose(self) -> ComposeResult:
        yield Static(id="wire-header")
        yield DataTable(id="wire-table")

    def load_content(self):
        from token_watch_data import _get_wire_messages
        cycle_id = getattr(self.app, '_active_cycle_id', None)
        data = _get_wire_messages(limit=50, cycle_id=cycle_id)

        self.query_one("#wire-header", Static).update(
            f"[bold]Wire — Inter-Session Messages[/bold]  "
            f"[dim]{data['total']} messages  ·  "
            f"{data['sessions']} sessions  ·  "
            f"{data['unread']} unread[/dim]"
        )

        table = self.query_one("#wire-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("Time", width=8)
        table.add_column("From", width=11)
        table.add_column("To", width=11)
        table.add_column("Type", width=10)
        table.add_column("Message")

        type_styles = {
            "ack": "green",
            "question": "yellow",
            "info": "blue",
            "status": "cyan",
            "file_release": "magenta",
            "patch": "red",
        }

        for m in data["messages"]:
            ts = m["created_at"]
            if "T" in ts:
                ts = ts.split("T")[1][:8]
            style = type_styles.get(m["type"], "white")
            type_icons = {
                "ack": "ACK",
                "question": "? Q",
                "info": "i",
                "status": ">>",
                "file_release": "FILE",
                "patch": "PATCH",
            }
            type_label = type_icons.get(m["type"], m["type"])
            read_mark = "" if m["read"] else " *"
            table.add_row(
                Text(ts, style="dim"),
                Text(m["from"].replace("cc-", ""), style="bold"),
                Text(m["to"].replace("cc-", ""), style="dim"),
                Text(type_label, style=style),
                Text(f"{m['message']}{read_mark}"),
            )


class AuditView(LazyView):
    """Comprehensive audit of all cycles and sessions."""

    BORDER_TITLE = "Cycle Audit"

    BINDINGS = [
        Binding("e", "export_audit", "Export MD"),
        Binding("r", "reload_audit", "Reload"),
        Binding("i", "import_sessions", "Import"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="audit-header")
        yield Static(id="audit-projects")
        yield DataTable(id="audit-cycles")
        yield Static(id="audit-footer")

    def load_content(self):
        self._refresh_audit()

    def _refresh_audit(self):
        from token_watch_data import _build_full_audit
        audit = _build_full_audit()
        totals = audit["totals"]

        # Header — executive summary
        header_text = (
            f"[bold]Cycle Audit[/bold]  "
            f"[cyan]{totals['cycle_count']}[/cyan] cycles  "
            f"[cyan]{totals['total_sessions']}[/cyan] sessions  "
            f"[cyan]{totals['total_commits']}[/cyan] commits  "
            f"[cyan]{totals['cost_str']}[/cyan] cost  "
            f"[cyan]{totals['avg_score']:.1f}/5.0[/cyan] avg score  "
            f"[cyan]{len(audit['by_project_global'])}[/cyan] projects"
        )
        self.query_one("#audit-header", Static).update(header_text)

        # Projects summary — RichTable in a Panel
        from rich.table import Table as RichTable
        from rich.panel import Panel
        pt = RichTable(show_header=True, header_style="bold", box=None, padding=(0, 2), expand=True)
        pt.add_column("Project", no_wrap=True)
        pt.add_column("Sessions", justify="right", no_wrap=True)
        pt.add_column("Commits", justify="right", no_wrap=True)
        pt.add_column("Files Ed", justify="right", no_wrap=True)
        pt.add_column("Files New", justify="right", no_wrap=True)
        pt.add_column("Cost", justify="right", no_wrap=True)

        # Sort projects by session count descending
        for proj, stats in sorted(audit["by_project_global"].items(), key=lambda x: x[1]["sessions"], reverse=True):
            from token_watch_data import _format_cost
            pt.add_row(
                f"[cyan]{proj}[/cyan]",
                str(stats["sessions"]),
                str(stats["commits"]),
                str(stats["files_edited"]),
                str(stats["files_created"]),
                _format_cost(stats["cost"]),
            )
        self.query_one("#audit-projects", Static).update(Panel(pt, title="[bold]Cross-Project Summary[/bold]", border_style="yellow"))

        # Cycles DataTable
        table = self.query_one("#audit-cycles", DataTable)
        table.clear(columns=True)
        table.add_columns("#", "Date", "Time", "Score", "Sessions", "Peak%", "Cost", "Done/Plan", "Projects", "Gravity")
        table.border_title = f"All Cycles ({totals['cycle_count']})"

        self._cycle_data = {}  # store for drill-down
        for i, cycle in enumerate(audit["cycles"], 1):
            from datetime import datetime as _dt
            try:
                start = _dt.fromisoformat(cycle["start_ts"].replace("Z", "+00:00"))
                date_str = start.strftime("%b %d")
                time_str = start.astimezone().strftime("%H:%M")
            except Exception:
                date_str = "?"
                time_str = "?"

            stars = cycle.get("stars", "")
            score = f"{cycle.get('overall_score', 0):.1f}" if cycle.get("overall_score") else "\u2014"
            peak = f"{cycle.get('peak_five_pct', 0):.0f}%"
            done = cycle.get("items_done", 0)
            total_items = done + cycle.get("items_open", 0) + cycle.get("items_rolled", 0)
            done_plan = f"{done}/{total_items}" if total_items else "\u2014"

            projects = ", ".join(sorted(cycle.get("by_project", {}).keys())[:3])
            if len(cycle.get("by_project", {})) > 3:
                projects += f" +{len(cycle['by_project']) - 3}"

            gravity = (cycle.get("gravity_label", "") or "")[:40]

            row_key = table.add_row(
                str(i), date_str, time_str, f"{stars} {score}",
                str(cycle.get("session_count", 0)), peak,
                cycle.get("cost_str", "\u2014"), done_plan, projects, gravity
            )
            self._cycle_data[row_key] = cycle

        # Footer
        self.query_one("#audit-footer", Static).update(
            "[dim]e[/dim] Export MD  [dim]r[/dim] Reload  [dim]Enter[/dim] Cycle detail"
        )

    def action_export_audit(self):
        import os
        from datetime import datetime
        from token_watch_data import export_audit_markdown
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        filepath = os.path.expanduser(f"~/Downloads/cycle-audit-{ts}.md")
        export_audit_markdown(filepath)
        self.notify(f"Exported to {filepath}")

    def action_reload_audit(self):
        self._refresh_audit()
        self.notify("Audit refreshed")

    def action_import_sessions(self):
        from token_watch_data import _populate_cycle_from_sessions
        count = _populate_cycle_from_sessions()  # all cycles
        self._refresh_audit()
        self.notify(f"Imported {count} items from sessions across all cycles")


class ClaimConfirmScreen(Screen):
    """Quick confirm dialog for auto-claiming the next ready task for a project.

    Lives one keystroke from an Advisor PIPELINE row: Enter to claim,
    Esc to cancel. Keeps the user out of the Dispatch → cursor → x dance.
    """

    BINDINGS = [
        Binding("enter", "confirm", "Claim"),
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "cancel", "Cancel"),
    ]

    def __init__(self, project: str, reason: str = "", **kwargs):
        super().__init__(**kwargs)
        self._project = project or ""
        self._reason = reason or ""

    def compose(self) -> ComposeResult:
        proj = self._project or "(unknown)"
        reason = self._reason or ""
        body = (
            "[bold]Claim next ready task[/bold]\n\n"
            f"[cyan]Project:[/cyan]  [bold]{proj}[/bold]\n"
        )
        if reason:
            body += f"[dim]{reason}[/dim]\n"
        body += (
            "\n"
            "[green]Enter[/green] = claim the first ready task\n"
            "[dim]Esc[/dim]   = cancel"
        )
        yield Static(body, id="claim-confirm-body")

    def action_confirm(self):
        from token_watch_data import _get_dispatch_queue_sync, _dispatch_claim_task
        self.app.pop_screen()
        project = (self._project or "").strip().lower()
        try:
            queue_data = _get_dispatch_queue_sync() or {}
        except Exception as e:
            self.app.notify(f"Fetch failed: {e}", severity="error")
            return

        queue = queue_data.get("queue", []) or []
        # Prefer exact-project matches, fall back to any ready task if project unknown.
        candidates = []
        if project:
            candidates = [
                t for t in queue
                if (t.get("project") or "").strip().lower() == project
            ]
            if not candidates:
                # Loose match on substring (e.g. "atlas" matches "atlas-portal")
                candidates = [
                    t for t in queue
                    if project in (t.get("project") or "").strip().lower()
                    or (t.get("project") or "").strip().lower() in project
                ]
        if not candidates:
            candidates = queue

        if not candidates:
            self.app.notify("No ready tasks to claim", severity="warning")
            return

        task = candidates[0]
        task_id = task.get("id")
        task_name = (task.get("task_name") or "")[:40]
        if task_id is None:
            self.app.notify("Task has no id", severity="error")
            return

        try:
            ok = _dispatch_claim_task(task_id)
        except Exception as e:
            self.app.notify(f"Claim failed: {e}", severity="error")
            return

        if ok:
            self.app.notify(f"Claimed #{task_id} — {task_name}")
        else:
            self.app.notify("Claim failed — task may already be taken",
                            severity="warning")

    def action_cancel(self):
        self.app.pop_screen()


class PlanDetailScreen(Screen):
    """Detail view for a single Claude Code plan — shows full markdown content."""

    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
    ]

    def __init__(self, plan: dict):
        super().__init__()
        self._plan = plan or {}

    def compose(self) -> ComposeResult:
        yield Static(id="pd-header")
        with ScrollableContainer(id="pd-scroll"):
            yield Static(id="pd-body")

    def on_mount(self):
        p = self._plan
        title = p.get("title") or p.get("plan_slug") or "(untitled)"
        slug = p.get("plan_slug", "")
        session = p.get("session_id", "")
        status = p.get("status", "draft")
        status_style = {
            "draft": "yellow",
            "approved": "green",
            "abandoned": "dim",
        }.get(status, "white")

        # Age / updated display
        updated = p.get("updated_at", "")
        age_str = ""
        if updated:
            try:
                ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                delta_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
                if delta_min < 60:
                    age_str = f"{delta_min:.0f}m ago"
                elif delta_min < 1440:
                    age_str = f"{delta_min / 60:.0f}h ago"
                else:
                    age_str = f"{delta_min / 1440:.0f}d ago"
            except Exception:
                age_str = updated[:16]

        header = (
            f"[bold]{_rich_escape(title)}[/bold]\n"
            f"[dim]{_rich_escape(slug)}[/dim]  "
            f"[cyan]{_rich_escape(session)}[/cyan]  "
            f"[{status_style}]{status}[/{status_style}]  "
            f"[dim]{age_str}[/dim]"
        )
        self.query_one("#pd-header", Static).update(header)

        body = p.get("content", "") or "(no content)"
        # Render raw markdown — escape rich markup to avoid conflicts
        self.query_one("#pd-body", Static).update(_rich_escape(body))

    def action_pop_screen(self):
        self.app.pop_screen()


class AdvisorPlanView(LazyView):
    """Advisor execution plan — reads /tmp/advisor-plan-active.md and renders with colors."""

    BINDINGS = [
        Binding("r", "reload", "Reload"),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_mtime = 0.0
        self._plan_path = Path("/tmp/advisor-plan-active.md")

    def compose(self) -> ComposeResult:
        yield Static(id="advisor-plan-content")

    def load_content(self):
        self._render_plan()

    def refresh_content(self):
        """Auto-refresh when file mtime changes."""
        try:
            mtime = self._plan_path.stat().st_mtime if self._plan_path.exists() else 0.0
        except OSError:
            mtime = 0.0
        if mtime != self._last_mtime:
            self._last_mtime = mtime
            self._render_plan()

    def _render_plan(self):
        widget = self.query_one("#advisor-plan-content", Static)
        if not self._plan_path.exists():
            widget.update("[dim]No active plan found at /tmp/advisor-plan-active.md[/dim]")
            return
        try:
            raw = self._plan_path.read_text()
        except OSError:
            widget.update("[red]Error reading plan file[/red]")
            return

        from advisor_plan import parse_phases, compute_overall
        from token_watch_data import _active_sessions

        rendered = []

        # ── Active Work section ──────────────────────────────────────
        try:
            sessions = _active_sessions()
        except Exception:
            sessions = []
        if sessions:
            rendered.append("[bold magenta]Active Work[/bold magenta]")
            for item in sessions:
                pid, etime, directive, delta = item[0], item[1], item[2], item[3]
                source = item[4] if len(item) > 4 else "?"
                rendered.append(
                    f"  [cyan]cc-{pid}[/cyan]  {_rich_escape(directive)}  "
                    f"[dim]{etime} · {source} · {delta}[/dim]"
                )
            rendered.append("")

        # ── Progress bars from phases ────────────────────────────────
        phases = parse_phases(raw)
        overall_done, overall_total = compute_overall(phases)

        if overall_total > 0:
            rendered.append(self._make_bar("Overall", overall_done, overall_total, width=30, bold=True))
            rendered.append("")

        # ── Render plan content with per-phase bars ──────────────────
        lines = raw.splitlines()
        phase_idx = 0
        for line in lines:
            stripped = line.strip()
            # Phase headers — inject progress bar
            phase_match = __import__("re").match(r"^##\s+Phase\s+(\d+)", stripped)
            if phase_match:
                pnum = int(phase_match.group(1))
                # Find matching parsed phase
                phase_data = None
                for p in phases:
                    if p["number"] == pnum:
                        phase_data = p
                        break
                rendered.append(f"[bold cyan]{_rich_escape(stripped)}[/bold cyan]")
                if phase_data and phase_data["total"] > 0:
                    # Show current step indicator instead of duplicate bar
                    next_step = phase_data["done"] + 1
                    if next_step <= phase_data["total"]:
                        # Find the name of the current step (first unchecked item)
                        step_name = phase_data.get("current_step", "")
                        rendered.append(f"  [dim]→ Step {next_step} of {phase_data['total']}[/dim]" +
                                        (f" [yellow]{_rich_escape(step_name)}[/yellow]" if step_name else ""))
                    else:
                        rendered.append(f"  [green]✓ All {phase_data['total']} steps complete[/green]")
                continue

            # Firehose entries. New format: "HH:MM | STATUS | ENGINE | Scope | description"
            # Legacy:                       "HH:MM | STATUS | Scope | description"
            # Legacy:                       "HH:MM | STATUS | description"
            _re = __import__("re")
            firehose_match = _re.match(r"^(\d{2}:\d{2})\s*\|\s*(\w+)\s*\|\s*(.+)$", stripped)
            if firehose_match:
                ts, status = firehose_match.group(1), firehose_match.group(2)
                rest = firehose_match.group(3)
                status_colors = {
                    "RUNNING": "bold yellow",
                    "DONE": "green",
                    "BUILT": "bold green",
                    "FIX": "green",
                    "PR": "bold cyan",
                    "QUEUED": "dim cyan",
                    "STARTED": "bold magenta",
                    "VAULT": "dim magenta",
                    "FAILED": "bold red",
                    "BLOCKED": "bold red",
                    "MERGED": "bold green",
                    "STUCK": "bold red on yellow",
                }
                sc = status_colors.get(status, "white")

                # Try new 5-column format first: ENGINE | Scope | desc
                engine_badge = ""
                scope_badge = ""
                desc = rest
                engine_colors = {
                    "Claude":  "bold magenta",
                    "Sonnet":  "magenta",
                    "Opus":    "bold magenta",
                    "Codex":   "bold cyan",
                    "M2.7":    "bold yellow",
                    "MiniMax": "bold yellow",
                    "Gemini":  "bold blue",
                    "Grok":    "bold red",
                }
                scope_colors = {
                    "Delphi/Atlas":   "bold bright_blue",
                    "Personal/TW":    "bold bright_green",
                    "Personal/SAGE":  "bold bright_magenta",
                    "Personal/Infra": "bold bright_yellow",
                    "Personal/BS":    "bold white",
                    "KAA/OpenClaw":   "bold bright_red",
                }
                # Split rest by " | " — could be 2 or 3 segments
                segs = [s.strip() for s in rest.split(" | ")]
                model_badge = ""
                if len(segs) >= 4:
                    # NEW: ENGINE | Scope | MODEL | desc
                    engine = segs[0]
                    scope = segs[1]
                    model = segs[2]
                    desc = " | ".join(segs[3:])
                    ec = engine_colors.get(engine, "dim")
                    engine_badge = f"[{ec}]{_rich_escape(engine).ljust(7)}[/{ec}]  "
                    sp = scope_colors.get(scope, "bold cyan")
                    scope_badge = f"[{sp}]{_rich_escape(scope).ljust(16)}[/{sp}]  "
                    # Model color — tie to engine
                    model_badge = f"[{ec}]{_rich_escape(model).ljust(10)}[/{ec}]  "
                elif len(segs) == 3:
                    # ENGINE | Scope | desc (pre-MODEL)
                    engine = segs[0]
                    scope = segs[1]
                    desc = segs[2]
                    ec = engine_colors.get(engine, "dim")
                    engine_badge = f"[{ec}]{_rich_escape(engine).ljust(7)}[/{ec}]  "
                    sp = scope_colors.get(scope, "bold cyan")
                    scope_badge = f"[{sp}]{_rich_escape(scope).ljust(16)}[/{sp}]  "
                    model_badge = f"[dim]{'—':10s}[/dim]  "
                elif len(segs) == 2:
                    # Scope | desc (legacy)
                    scope = segs[0]
                    desc = segs[1]
                    sp = scope_colors.get(scope, "bold cyan")
                    engine_badge = f"[dim]{'—':7s}[/dim]  "
                    scope_badge = f"[{sp}]{_rich_escape(scope).ljust(16)}[/{sp}]  "
                    model_badge = f"[dim]{'—':10s}[/dim]  "
                else:
                    # Just desc (oldest legacy)
                    engine_badge = f"[dim]{'—':7s}[/dim]  "
                    model_badge = f"[dim]{'—':10s}[/dim]  "
                rendered.append(f"  [dim]{ts}[/dim]  [{sc}]{status:8s}[/{sc}]  {engine_badge}{scope_badge}{model_badge}{_rich_escape(desc)}")
                continue

            # Firehose section header — inject column labels right after
            if stripped == "## Firehose":
                rendered.append(f"[bold white]{_rich_escape(stripped)}[/bold white]")
                rendered.append(
                    f"  [dim underline]{'TIME':5s}[/dim underline]  "
                    f"[dim underline]{'STATUS':8s}[/dim underline]  "
                    f"[dim underline]{'ENGINE':7s}[/dim underline]  "
                    f"[dim underline]{'COMPANY':16s}[/dim underline]  "
                    f"[dim underline]{'MODEL':10s}[/dim underline]  "
                    f"[dim underline]TASK[/dim underline]"
                )
                continue

            # Section headers
            if stripped.startswith("# "):
                rendered.append(f"[bold white]{_rich_escape(stripped)}[/bold white]")
            elif stripped.startswith("> "):
                rendered.append(f"[dim italic]{_rich_escape(stripped)}[/dim italic]")
            # Checkbox items
            elif "- [x]" in stripped or "- [X]" in stripped:
                esc = _rich_escape(stripped)
                esc = esc.replace("- \\[x]", "  [green]\u2713[/green]").replace("- \\[X]", "  [green]\u2713[/green]")
                rendered.append(f"[dim]{esc}[/dim]")
            elif "- [ ]" in stripped:
                esc = _rich_escape(stripped)
                esc = esc.replace("- \\[ ]", "  \u25cb")
                if "BLOCKED" in stripped or "\U0001f534" in stripped:
                    # 🔴 BLOCKED — needs human action
                    rendered.append(f"[bold red on grey15]  \u26a0 {esc}[/bold red on grey15]")
                elif "IN PROGRESS" in stripped or "\U0001f7e1" in stripped or "\u23f3" in stripped:
                    # 🟡 IN PROGRESS — being worked on
                    rendered.append(f"[bold yellow]{esc}[/bold yellow]")
                elif "DEFERRED" in stripped or "\u23ed" in stripped:
                    # ⏭ DEFERRED — pushed to next session
                    rendered.append(f"[dim italic]{esc}[/dim italic]")
                else:
                    # Plain unchecked — needs work
                    rendered.append(f"[bold bright_white]{esc}[/bold bright_white]")
            else:
                esc = _rich_escape(stripped)
                if "\u2705" in stripped:
                    esc = f"[green]{esc}[/green]"
                elif "\u23f3" in stripped:
                    esc = f"[yellow]{esc}[/yellow]"
                rendered.append(esc)

        # ── Plan History from vault ──────────────────────────────────
        rendered.append("")
        rendered.append("[bold magenta]Plan History[/bold magenta]")
        vault_dir = Path.home() / "obsidian" / "sage-vault" / "forge" / "sessions"
        try:
            plan_files = sorted(vault_dir.glob("plan-*.md"), reverse=True) if vault_dir.exists() else []
        except OSError:
            plan_files = []
        if not plan_files:
            rendered.append("  [dim]No archived plans yet[/dim]")
        else:
            for pf in plan_files[:10]:  # last 10 plans
                try:
                    content = pf.read_text()
                    hist_phases = parse_phases(content)
                    h_done, h_total = compute_overall(hist_phases)
                    h_pct = int(h_done / h_total * 100) if h_total > 0 else 0
                    # Extract title from first line
                    first_line = content.splitlines()[0] if content.strip() else pf.stem
                    title = first_line.lstrip("# ").strip()[:60]
                    # Color by completion
                    if h_pct >= 80:
                        color = "green"
                    elif h_pct >= 50:
                        color = "yellow"
                    else:
                        color = "red"
                    bar_w = 15
                    filled = int(bar_w * h_done / h_total) if h_total > 0 else 0
                    bar = f"[{color}]{'█' * filled}[/{color}][dim]{'░' * (bar_w - filled)}[/dim]"
                    rendered.append(f"  {bar} [{color}]{h_pct:3d}%[/{color}]  {h_done}/{h_total}  [dim]{_rich_escape(title)}[/dim]")
                except Exception:
                    rendered.append(f"  [dim]{pf.name} — error reading[/dim]")

        ts = datetime.fromtimestamp(self._last_mtime).strftime("%H:%M:%S") if self._last_mtime else "?"
        header = f"[bold]Advisor Plan[/bold]  [dim]Last modified: {ts} \u00b7 r=Reload[/dim]\n"
        widget.update(header + "\n".join(rendered))

    @staticmethod
    def _make_bar(label, done, total, width=20, bold=False):
        """Render a progress bar like: Phase 1  [██████░░░░] 3/5 60%"""
        pct = (done / total * 100) if total > 0 else 0
        filled = int(width * done / total) if total > 0 else 0
        empty = width - filled
        bar = f"[green]{'█' * filled}[/green][dim]{'░' * empty}[/dim]"
        text = f"{done}/{total} {pct:.0f}%"
        if label:
            prefix = f"[bold]{_rich_escape(label)}[/bold]  " if bold else f"{_rich_escape(label)}  "
            return f"{prefix}{bar} {text}"
        return f"{bar} {text}"

    def action_reload(self):
        self._last_mtime = 0.0
        self._render_plan()


class PlansView(LazyView):
    """Claude Code Plans — cross-session plan files synced from ~/.claude/plans/."""

    BINDINGS = [
        Binding("enter", "show_detail", "Detail"),
        Binding("r", "reload", "Reload"),
        Binding("1", "filter_all", "All"),
        Binding("2", "filter_draft", "Drafts"),
        Binding("3", "filter_approved", "Approved"),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._filter = None
        self._plans: list = []
        self._last_refresh = 0.0

    def compose(self) -> ComposeResult:
        yield Static(id="plans-header")
        yield DataTable(id="plans-table")

    def load_content(self):
        plans = _get_claude_plans(limit=100, status=self._filter)
        self._plans = plans or []
        self._last_refresh = time.time()

        # Compute counts from unfiltered fetch if filter active
        if self._filter is None:
            all_plans = self._plans
        else:
            all_plans = _get_claude_plans(limit=200, status=None)

        total = len(all_plans)
        drafts = sum(1 for p in all_plans if p.get("status") == "draft")
        approved = sum(1 for p in all_plans if p.get("status") == "approved")
        abandoned = sum(1 for p in all_plans if p.get("status") == "abandoned")

        filter_label = self._filter or "all"
        self.query_one("#plans-header", Static).update(
            f"[bold]Plans[/bold]  [dim]{total} total · "
            f"[yellow]{drafts} drafts[/yellow] · "
            f"[green]{approved} approved[/green] · "
            f"{abandoned} abandoned · filter: {filter_label}[/dim]\n"
            f"[dim]1=All  2=Drafts  3=Approved  r=Reload  Enter=Detail[/dim]"
        )

        dt = self.query_one("#plans-table", DataTable)
        dt.clear(columns=True)
        dt.cursor_type = "row"
        dt.zebra_stripes = True
        dt.add_column("Plan", width=28)
        dt.add_column("Title", width=36)
        dt.add_column("Session", width=12)
        dt.add_column("Status", width=10)
        dt.add_column("Updated", width=14)
        dt.add_column("Preview")

        now_utc = datetime.now(timezone.utc)
        for i, p in enumerate(self._plans):
            slug = p.get("plan_slug", "") or ""
            title = p.get("title", "") or slug
            session = p.get("session_id", "") or ""
            status = p.get("status", "draft")
            preview = (p.get("preview") or "").strip()

            # Age display
            updated = p.get("updated_at", "")
            age_str = "\u2014"
            if updated:
                try:
                    ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    delta_min = (now_utc - ts).total_seconds() / 60
                    if delta_min < 60:
                        age_str = f"{delta_min:.0f}m ago"
                    elif delta_min < 1440:
                        age_str = f"{delta_min / 60:.0f}h ago"
                    else:
                        age_str = f"{delta_min / 1440:.0f}d ago"
                except Exception:
                    age_str = updated[:14]

            if status == "draft":
                status_style = "yellow"
            elif status == "approved":
                status_style = "green"
            elif status == "abandoned":
                status_style = "dim"
            else:
                status_style = "white"

            dt.add_row(
                Text(slug[:28], style="white"),
                Text(title[:36], style="white"),
                Text(session[:12], style="cyan"),
                Text(status, style=status_style),
                Text(age_str, style="dim"),
                Text(preview[:120], style="dim"),
                key=f"plan-{i}",
            )

        if not self._plans:
            dt.add_row(
                Text("\u2014", style="dim"),
                Text("No plans found", style="dim"),
                Text("", style="dim"),
                Text("", style="dim"),
                Text("", style="dim"),
                Text("", style="dim"),
            )

    def refresh_content(self):
        now = time.time()
        if (now - self._last_refresh) > 20:
            self.load_content()

    def action_show_detail(self):
        dt = self.query_one("#plans-table", DataTable)
        try:
            row_key = dt.coordinate_to_cell_key(dt.cursor_coordinate).row_key
            key_val = str(row_key.value) if hasattr(row_key, 'value') else str(row_key)
        except Exception:
            return
        if not key_val.startswith("plan-"):
            return
        try:
            idx = int(key_val.split("-")[1])
            plan = self._plans[idx]
        except (IndexError, ValueError):
            return
        # Fetch full content from cache/Supabase (already in _plans payload)
        self.app.push_screen(PlanDetailScreen(plan))

    def on_data_table_row_selected(self, event):
        self.action_show_detail()

    def action_reload(self):
        _get_claude_plans(limit=100, status=self._filter, force=True)
        self.load_content()

    def action_filter_all(self):
        self._filter = None
        self.load_content()

    def action_filter_draft(self):
        self._filter = "draft"
        self.load_content()

    def action_filter_approved(self):
        self._filter = "approved"
        self.load_content()


class AdvisorView(LazyView):
    """Token Window Advisor Agent — actionable intelligence synthesis."""

    BINDINGS = [
        Binding("R", "run_advisor", "Run Now"),
        Binding("enter", "drill_down", "Detail", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="advisor-header")
        yield Static(id="advisor-top-action")
        yield Static(id="advisor-summary")
        yield DataTable(id="advisor-table")

    def load_content(self):
        self._refresh_advisor()

    def refresh_content(self):
        now = time.time()
        if not hasattr(self, '_last_refresh') or (now - self._last_refresh) > 60:
            self._last_refresh = now
            self._refresh_advisor()

    def _refresh_advisor(self):
        from token_watch_advisor import run_advisor, SEVERITY_ORDER
        self._last_refresh = time.time()
        report = run_advisor()

        severity_display = {
            "critical": ("!!!", "bold red"),
            "warning":  (" ! ", "yellow"),
            "info":     (" i ", "blue"),
            "positive": (" + ", "green"),
        }

        # Header
        counts = report.summary
        parts = []
        for sev, label in [("critical", "Critical"), ("warning", "Warning"), ("info", "Info"), ("positive", "Positive")]:
            n = counts.get(sev, 0)
            if n > 0:
                _, style = severity_display[sev]
                parts.append(f"[{style}]{n} {label}[/{style}]")
        header_text = (
            f"[bold]TW Advisor[/bold]  "
            f"[dim]{report.checks_run} checks · {report.duration_ms}ms · "
            f"{len(report.insights)} insights[/dim]"
        )
        self.query_one("#advisor-header", Static).update(header_text)

        summary_text = "  ".join(parts) if parts else "[dim]No insights[/dim]"
        self.query_one("#advisor-summary", Static).update(summary_text)

        # Top-action banner — surfaces the single highest-priority actionable
        # insight so the user has one clear next move without scanning the list.
        self._render_top_action(report.insights)

        # Table
        table = self.query_one("#advisor-table", DataTable)
        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("", width=3)          # severity icon
        table.add_column("Category", width=14)
        table.add_column("Insight")
        table.add_column("Action", width=40)

        # Parallel list of insights for cursor lookup (drill-down).
        self._insights_ordered = []
        for ins in report.insights:
            icon, style = severity_display.get(ins.severity, (" ? ", "white"))
            table.add_row(
                Text(icon, style=style),
                Text(ins.category, style="bold"),
                Text(ins.message),
                Text(ins.action, style="dim"),
            )
            self._insights_ordered.append(ins)

    # Categories that have a well-defined, one-keystroke drill-down path
    # (TEST_DEBT → t, PIPELINE → claim modal, CONTEXT → copy action, etc.)
    # — used to pick the top-action banner target.
    _ACTIONABLE_CATS = {
        "TEST_DEBT", "PIPELINE", "CONTEXT", "CAPACITY",
        "CYCLE", "EFFICIENCY", "QA", "SYSTEM",
    }

    def _render_top_action(self, insights):
        """Update the bold top-of-view banner with the most urgent actionable insight."""
        try:
            banner = self.query_one("#advisor-top-action", Static)
        except Exception:
            return

        # Prefer critical > warning > info, and actionable categories first.
        sev_rank = {"critical": 0, "warning": 1, "info": 2, "positive": 3}
        ranked = sorted(
            insights,
            key=lambda i: (
                sev_rank.get(i.severity, 9),
                0 if i.category in self._ACTIONABLE_CATS else 1,
            ),
        )

        top = next(
            (i for i in ranked if i.severity == "critical" and i.category in self._ACTIONABLE_CATS),
            None,
        )
        if top is None:
            top = next((i for i in ranked if i.category in self._ACTIONABLE_CATS), None)
        if top is None and ranked:
            top = ranked[0]

        if top is None:
            banner.update("")
            return

        hint_map = {
            "TEST_DEBT": "Enter to drill down, then [bold]a[/bold] to mark all tested",
            "PIPELINE":  "Enter to auto-claim the next ready task",
            "CONTEXT":   "Enter for detail, [bold]x[/bold] to copy the fix command",
            "CAPACITY":  "Enter to jump to Capacity tab",
            "CYCLE":     "Enter to jump to Cycles tab",
            "EFFICIENCY": "Enter to jump to Sessions tab",
        }
        hint = hint_map.get(top.category, "Enter to drill down")
        sev_color = {
            "critical": "bold red",
            "warning":  "yellow",
            "info":     "blue",
            "positive": "green",
        }.get(top.severity, "white")

        msg = (top.message or top.title or "").strip()
        if len(msg) > 90:
            msg = msg[:87] + "..."

        banner.update(
            f"[{sev_color}]#1 ACTION:[/{sev_color}] {msg}  [dim]· {hint}[/dim]"
        )

    def action_run_advisor(self):
        from token_watch_advisor import _advisor_cache_ts
        import token_watch_advisor
        token_watch_advisor._advisor_cache = None
        token_watch_advisor._advisor_cache_ts = 0.0
        self._last_refresh = 0
        self._refresh_advisor()
        self.notify("TW Advisor refreshed")

    def _get_selected_insight(self):
        """Return the Insight at the cursor row, or None."""
        try:
            table = self.query_one("#advisor-table", DataTable)
        except Exception:
            return None
        row_idx = getattr(table, "cursor_row", None)
        if row_idx is None or row_idx < 0:
            return None
        insights = getattr(self, "_insights_ordered", [])
        if row_idx >= len(insights):
            return None
        return insights[row_idx]

    # Category → view_id map for smart "jump to the relevant view" navigation.
    _NAV_MAP = {
        "CYCLE": "view-cycles",
        "CAPACITY": "view-capacity",
        "EFFICIENCY": "view-sessions",
        "PIPELINE": "view-dispatch",
    }

    def _resolve_pipeline_project(self, ins):
        """Best-effort extraction of a project name from a PIPELINE insight."""
        data = getattr(ins, "data", {}) or {}
        # cross_session_work_gap → data.gap = {"atlas-backend": 3, ...}
        gap = data.get("gap") or {}
        if isinstance(gap, dict) and gap:
            # Pick the project with the most ready work.
            return sorted(gap.items(), key=lambda kv: -kv[1])[0][0]
        # blocked_tasks, unclaimed_continuations, etc. rarely carry a project;
        # fall back to scanning the message.
        import re
        msg = (ins.message or "") + " " + (ins.title or "")
        m = re.search(r"\b(atlas[-\w]*|token-watch|paperclip|frank-pilot|openclaw|kaa|battlestation)\b", msg, re.I)
        if m:
            return m.group(1).lower()
        return ""

    def _handle_drill_down(self, ins):
        """Route an insight to the right view, or fall back to the generic screen."""
        if ins is None:
            return

        # PIPELINE → one-keystroke auto-claim confirm instead of jumping to
        # Dispatch and making the user hunt for a row to press `x` on.
        if ins.category == "PIPELINE":
            project = self._resolve_pipeline_project(ins)
            reason = (ins.message or ins.title or "")[:100]
            try:
                self.app.push_screen(ClaimConfirmScreen(project, reason))
                return
            except Exception:
                pass  # fall through to legacy nav

        view_id = self._NAV_MAP.get(ins.category)
        if view_id:
            try:
                self.app.switch_view(view_id)
                return
            except Exception:
                # If navigation fails for any reason, fall through to detail screen.
                pass
        # TEST_DEBT + SYSTEM/CONTEXT/COORDINATION/QUALITY/VELOCITY/OPPORTUNITY
        # and anything else → open the generic drill-down screen.
        self.app.push_screen(AdvisorDetailScreen(ins))

    def action_drill_down(self):
        self._handle_drill_down(self._get_selected_insight())

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        try:
            table = self.query_one("#advisor-table", DataTable)
        except Exception:
            return
        insights = getattr(self, "_insights_ordered", [])
        idx = event.cursor_row
        if idx is None or idx >= len(insights):
            table.tooltip = None
            return
        ins = insights[idx]
        parts = []
        if ins.message:
            parts.append(ins.message)
        if ins.action:
            parts.append(f"action: {ins.action}")
        table.tooltip = "\n".join(parts) if parts else None

    def on_data_table_row_selected(self, event) -> None:
        """Enter/click on a row routes to the right view or detail screen."""
        self._handle_drill_down(self._get_selected_insight())


class AdvisorDetailScreen(Screen):
    """Drill-down for an Advisor insight — shows the underlying items."""

    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
        Binding("t", "mark_tested", "Tested"),
        Binding("f", "mark_failed", "Failed"),
        Binding("a", "mark_all_tested", "Mark All"),
        Binding("o", "open_github", "GitHub"),
        Binding("x", "copy_action", "Copy Action"),
    ]

    def __init__(self, insight, **kwargs):
        super().__init__(**kwargs)
        self._insight = insight
        # Parallel list of build_ledger items for TEST_DEBT drill-down.
        self._test_items: list = []

    def compose(self) -> ComposeResult:
        yield Static(id="adetail-header")
        yield Static(id="adetail-summary")
        yield DataTable(id="adetail-table")
        yield Static(id="adetail-footer")

    def on_mount(self):
        ins = self._insight
        severity_style = {
            "critical": "bold red",
            "warning":  "yellow",
            "info":     "blue",
            "positive": "green",
        }.get(ins.severity, "white")

        self.query_one("#adetail-header", Static).update(
            f"[{severity_style}]{ins.severity.upper()}[/{severity_style}]  "
            f"[bold]{ins.category}[/bold]  [dim]·[/dim]  {ins.title}"
        )
        summary_lines = [
            f"[bold]Insight:[/bold] {ins.message}",
        ]
        if ins.action:
            # CONTEXT actions are shell commands or concrete fixes — render them
            # prominently so the user can immediately copy-run them.
            if ins.category == "CONTEXT":
                summary_lines.append(
                    f"[bold yellow]>> FIX:[/bold yellow] [bold]{ins.action}[/bold]"
                )
            else:
                summary_lines.append(f"[bold]Action:[/bold]  [dim]{ins.action}[/dim]")
        self.query_one("#adetail-summary", Static).update("\n".join(summary_lines))

        table = self.query_one("#adetail-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True

        footer = self.query_one("#adetail-footer", Static)
        if ins.category == "TEST_DEBT":
            self._render_test_debt(table)
            footer.update(
                "[dim]t[/dim]=mark tested  [dim]f[/dim]=mark failed  "
                "[bold yellow]a[/bold yellow]=mark ALL tested  "
                "[dim]o[/dim]=open GitHub  [dim]q/Esc[/dim]=back"
            )
        elif ins.category == "CONTEXT":
            self._render_context(table)
            if (ins.action or "").strip():
                footer.update(
                    "[bold yellow]x[/bold yellow]=copy action to clipboard  "
                    "[dim]q/Esc[/dim]=back"
                )
            else:
                footer.update("[dim]q/Esc=back[/dim]")
        else:
            self._render_generic(table)
            footer.update("[dim]q/Esc=back[/dim]")

    def _resolve_test_debt_project(self):
        """Extract the project name from the insight data or message."""
        ins = self._insight
        data = getattr(ins, "data", {}) or {}
        proj = data.get("project")
        if proj:
            return str(proj)
        # Fallback: parse "... untested items in <project> (company)."
        import re
        m = re.search(r"untested items in (\S+?)(?:\s*\(|\.|$)", ins.message or "")
        if m:
            return m.group(1)
        return ""

    def _render_test_debt(self, table: DataTable):
        from token_watch_data import _get_build_ledger
        project = self._resolve_test_debt_project()

        table.clear(columns=True)
        table.add_column("Time", width=12)
        table.add_column("SHA", width=8)
        table.add_column("Title", width=36)
        table.add_column("How to Verify", width=40)

        # Reset parallel list each render.
        self._test_items = []

        if not project:
            table.add_row(
                Text("—"), Text(""),
                Text("Could not resolve project from insight", style="italic dim"),
                Text(""),
            )
            return

        # Pull a wide slice, then filter to project + untested.
        data = _get_build_ledger(days=30, limit=500)
        matches = []
        for item in data.get("items", []):
            if (item.get("project") or "").lower() != project.lower():
                continue
            if (item.get("test_status") or "untested") != "untested":
                continue
            matches.append(item)

        if not matches:
            table.add_row(
                Text("—"), Text(""),
                Text(f"No untested items found for {project}", style="italic dim"),
                Text(""),
            )
            return

        for item in matches:
            ts = item.get("created_at", "") or ""
            if "T" in ts:
                ts = ts.replace("T", " ")[5:16]
            sha = (item.get("commit_sha") or "")[:7]
            title = (item.get("title") or "")[:36]
            hint = (item.get("test_hint") or "")[:40]
            table.add_row(
                Text(ts, style="dim"),
                Text(sha, style="cyan"),
                Text(title),
                Text(hint, style="italic dim"),
                key=item.get("id", "") or None,
            )
            self._test_items.append(item)

    def _reload_test_debt(self):
        """Re-fetch TEST_DEBT items from Supabase and re-render the table."""
        try:
            table = self.query_one("#adetail-table", DataTable)
        except Exception:
            return
        self._render_test_debt(table)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        try:
            table = self.query_one("#adetail-table", DataTable)
        except Exception:
            return
        items = self._test_items
        idx = event.cursor_row
        if idx is None or idx >= len(items):
            table.tooltip = None
            return
        item = items[idx]
        title = item.get("title", "") or ""
        hint = item.get("test_hint", "") or ""
        sha = (item.get("commit_sha") or "")[:7]
        project = item.get("project", "") or ""
        parts = []
        if title:
            parts.append(title)
        if hint:
            parts.append(f"verify: {hint}")
        if sha or project:
            parts.append(f"{project}  {sha}".strip())
        table.tooltip = "\n".join(parts) if parts else None

    def _mark_selected(self, status: str):
        """Mark the currently-selected TEST_DEBT row as tested/failed."""
        from token_watch_data import _update_test_item
        if self._insight.category != "TEST_DEBT":
            return
        try:
            table = self.query_one("#adetail-table", DataTable)
        except Exception:
            return
        row = getattr(table, "cursor_row", -1)
        if row is None or row < 0 or row >= len(self._test_items):
            return
        item = self._test_items[row]
        item_id = item.get("id", "")
        if not item_id:
            self.notify("Item has no id — cannot update", severity="warning")
            return
        api_status = "pass" if status == "tested" else "fail"
        ok = _update_test_item(item_id, api_status)
        if not ok:
            self.notify("Failed to update Supabase", severity="error")
            return
        title = (item.get("title") or "")[:40]
        self.notify(f"{title} → {status}")
        # Reload from Supabase so the list reflects the new state.
        self._reload_test_debt()
        if not self._test_items:
            self.notify("All items marked — press q to go back")

    def action_mark_tested(self):
        self._mark_selected("tested")

    def action_mark_failed(self):
        self._mark_selected("failed")

    def action_mark_all_tested(self):
        """Bulk-mark every TEST_DEBT item in the current drill-down as tested.

        Saves the user from pressing `t` 50+ times on the post-deploy sweep.
        Reloads from Supabase at the end so the list reflects reality.
        """
        from token_watch_data import _update_test_item
        if self._insight.category != "TEST_DEBT":
            self.notify("Not a TEST_DEBT drill-down", severity="warning")
            return

        items = list(self._test_items)
        total = len(items)
        if not total:
            self.notify("Nothing to mark", severity="warning")
            return

        self.notify(f"Marking {total} items tested...")
        ok_count = 0
        fail_count = 0
        for item in items:
            item_id = item.get("id", "")
            if not item_id:
                fail_count += 1
                continue
            try:
                if _update_test_item(item_id, "pass"):
                    ok_count += 1
                else:
                    fail_count += 1
            except Exception:
                fail_count += 1

        if fail_count:
            self.notify(
                f"Marked {ok_count}/{total} tested · {fail_count} failed",
                severity="warning",
            )
        else:
            self.notify(f"Marked all {ok_count} items tested")
        self._reload_test_debt()
        if not self._test_items:
            self.notify("All clear — press q to go back")

    def action_copy_action(self):
        """Copy the insight's action (shell command, note, etc.) to the clipboard."""
        ins = self._insight
        action = (ins.action or "").strip()
        if not action:
            self.notify("No action to copy", severity="warning")
            return
        import subprocess
        try:
            subprocess.run(["pbcopy"], input=action.encode(), check=True)
            self.notify("Copied to clipboard")
        except Exception:
            self.notify("Copy failed", severity="error")

    def action_open_github(self):
        """Open the selected item's commit on GitHub in the browser."""
        if self._insight.category != "TEST_DEBT":
            return
        import subprocess
        try:
            table = self.query_one("#adetail-table", DataTable)
        except Exception:
            return
        row = getattr(table, "cursor_row", -1)
        if row is None or row < 0 or row >= len(self._test_items):
            return
        item = self._test_items[row]
        sha = (item.get("commit_sha") or "")[:7]
        project = item.get("project", "") or ""
        repo_map = {
            "atlas": "atlas-portal",
            "atlas-backend": "atlas-backend",
            "token-watch": "token-watch",
            "battlestation": "battlestation",
            "paperclip": "paperclip",
            "frank-pilot": "frank-pilot",
            "openclaw": "openclaw",
            "kaa": "openclaw",
        }
        repo = repo_map.get(project, project)
        gh_user = os.environ.get("BS_GITHUB_USER", "")
        if sha and repo and gh_user:
            subprocess.run(["open", f"https://github.com/{gh_user}/{repo}/commit/{sha}"])
            self.notify(f"Opening {repo}@{sha}")
        else:
            self.notify("No commit SHA/project on row", severity="warning")

    def _render_context(self, table: DataTable):
        """CONTEXT insights = ~/CONTEXT.md blockers. Show the fix prominently."""
        ins = self._insight
        table.clear(columns=True)
        table.add_column("Field", width=16)
        table.add_column("Value")

        action = (ins.action or "").strip()
        if action:
            table.add_row(
                Text("FIX", style="bold yellow"),
                Text(action, style="bold"),
            )
            table.add_row(
                Text("", style="dim"),
                Text("Press 'x' to copy to clipboard", style="italic dim"),
            )

        table.add_row(Text("Title", style="bold"), Text(ins.title or "—"))
        table.add_row(Text("Message", style="bold"), Text(ins.message or "—"))
        table.add_row(Text("Source", style="bold"), Text(ins.source or "—"))
        data = getattr(ins, "data", {}) or {}
        if data:
            for k, v in data.items():
                table.add_row(Text(f"data.{k}", style="dim"), Text(str(v)))

    def _render_generic(self, table: DataTable):
        ins = self._insight
        table.add_column("Field", width=16)
        table.add_column("Value")
        table.add_row(Text("Category", style="bold"), Text(ins.category))
        table.add_row(Text("Severity", style="bold"), Text(ins.severity))
        table.add_row(Text("Title", style="bold"), Text(ins.title or "—"))
        table.add_row(Text("Message", style="bold"), Text(ins.message or "—"))
        table.add_row(Text("Action", style="bold"), Text(ins.action or "—"))
        table.add_row(Text("Source", style="bold"), Text(ins.source or "—"))
        data = getattr(ins, "data", {}) or {}
        if data:
            for k, v in data.items():
                table.add_row(Text(f"data.{k}", style="dim"), Text(str(v)))

    def action_pop_screen(self):
        self.app.pop_screen()


class AnalyticsView(LazyView):
    """Token utilization analytics — rolling 24h/72h/1w/1m efficiency coaching."""

    _active_window = "24h"

    BINDINGS = [
        Binding("1", "window_24h", "24h"),
        Binding("2", "window_72h", "72h"),
        Binding("3", "window_1w", "1 Week"),
        Binding("4", "window_1m", "1 Month"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="an-header")
        yield Static(id="an-fleet")
        with Horizontal(id="an-accounts-row"):
            yield Static(id="an-acct-a")
            yield Static(id="an-acct-b")
            yield Static(id="an-acct-c")
        yield Static(id="an-heatmap")
        yield Static(id="an-waste")
        yield DataTable(id="an-efficiency")
        yield Static(id="an-suggestions")

    def refresh_content(self):
        now = time.time()
        if not hasattr(self, '_last_refresh') or (now - self._last_refresh) > 30:
            self._last_refresh = now
            try:
                self.query_one("#an-efficiency", DataTable).clear(columns=True)
                self.load_content()
            except Exception:
                pass

    def load_content(self):
        from rich.panel import Panel
        from rich.table import Table as RichTable

        data = _get_utilization_analytics(self._active_window)
        fleet = data.get("fleet", {})
        accounts = data.get("accounts", [])
        waste = data.get("waste", {})
        efficiency = data.get("efficiency", {})
        suggestions = data.get("suggestions", [])
        heatmap = data.get("heatmap", {})

        # ── Header ───────────────────────────────────────────────────
        self.query_one("#an-header", Static).update(
            f"[bold]Token Analytics — {self._active_window}[/bold]  "
            f"[dim]Press 1=24h  2=72h  3=1w  4=1m[/dim]"
        )

        # ── Fleet Scorecard ──────────────────────────────────────────
        util_pct = fleet.get("utilization_pct", 0)
        bar_len = max(1, int(util_pct / 100 * 30))
        bar_color = "green" if util_pct > 70 else ("yellow" if util_pct > 40 else "red")
        bar = f"[{bar_color}]{'█' * bar_len}[/{bar_color}]{'░' * (30 - bar_len)}"

        tokens_k = fleet.get("total_tokens", 0) / 1000
        tok_str = f"{tokens_k:.0f}k" if tokens_k < 1000 else f"{tokens_k/1000:.1f}M"

        fleet_text = (
            f"  Utilization: [bold]{fleet.get('stars', '☆☆☆☆☆')}[/bold] "
            f"({fleet.get('overall_score', 0)}/5)     "
            f"Fleet: [cyan]{fleet.get('active_hours', 0)}h[/cyan] / "
            f"{fleet.get('available_hours', 0)}h active "
            f"({util_pct}%)\n"
            f"  {bar}  {util_pct}% utilized\n"
            f"  Sessions: [cyan]{fleet.get('total_sessions', 0)}[/cyan]  "
            f"Commits: [cyan]{fleet.get('total_commits', 0)}[/cyan]  "
            f"Tokens: [cyan]{tok_str}[/cyan]  "
            f"Run Rate: [yellow]${fleet.get('run_rate_day', 0):.2f}/day[/yellow]"
        )
        self.query_one("#an-fleet", Static).update(
            Panel(fleet_text, title=f"Fleet Utilization — {self._active_window}",
                  border_style="bold cyan")
        )

        # ── Account Cards ────────────────────────────────────────────
        acct_colors = {"A": "cyan", "B": "magenta", "C": "yellow"}
        acct_widgets = {"A": "#an-acct-a", "B": "#an-acct-b", "C": "#an-acct-c"}

        for acct in accounts:
            label = acct.get("label", "?")
            color = acct_colors.get(label, "white")
            widget_id = acct_widgets.get(label)
            if not widget_id:
                continue

            a_util = acct.get("utilization_pct", 0)
            a_bar_len = max(1, int(a_util / 100 * 20))
            a_bar_color = "green" if a_util > 70 else ("yellow" if a_util > 40 else "red")
            a_bar = f"[{a_bar_color}]{'█' * a_bar_len}[/{a_bar_color}]{'░' * (20 - a_bar_len)}"

            tok_k = acct.get("output_tokens", 0) / 1000
            tok_s = f"{tok_k:.0f}k" if tok_k < 1000 else f"{tok_k/1000:.1f}M"

            a_score = _score_dimension(a_util, 85.0)
            a_stars = _stars_display(a_score)

            # Format capacity values — None means no data for inactive accounts
            five_val = acct.get("five_pct")
            seven_val = acct.get("seven_day_pct")
            five_str = f"{five_val:.0f}%" if five_val is not None else "[dim]—[/dim]"
            seven_str = f"{seven_val:.0f}%" if seven_val is not None else "[dim]—[/dim]"
            # Color 7d by urgency
            if seven_val is not None:
                if seven_val >= 95:
                    seven_str = f"[red bold]{seven_val:.0f}%[/red bold]"
                elif seven_val >= 70:
                    seven_str = f"[yellow]{seven_val:.0f}%[/yellow]"
                else:
                    seven_str = f"[green]{seven_val:.0f}%[/green]"

            active_tag = " [green bold]ACTIVE[/green bold]" if acct.get("is_active") else ""
            snap_age = acct.get("snapshot_age_min", 0)
            stale_note = ""
            if not acct.get("is_active") and snap_age > 60:
                stale_note = f"  [dim](snap {snap_age/60:.0f}h ago)[/dim]"

            # 7d reset countdown
            reset_hours = acct.get("seven_day_resets_in_hours")
            if reset_hours is not None:
                if reset_hours < 24:
                    reset_str = f"  resets in [bold cyan]{reset_hours:.0f}h[/bold cyan]"
                else:
                    reset_str = f"  resets in [cyan]{reset_hours/24:.1f}d[/cyan]"
            else:
                reset_str = ""

            card = (
                f"Lane: {acct.get('lane', '?')}{active_tag}\n"
                f"Active: [{color}]{acct.get('active_hours', 0)}h[/{color}] / "
                f"{acct.get('active_hours', 0) + acct.get('idle_hours', 0):.0f}h\n"
                f"{a_bar}  {a_util}%\n"
                f"5h: {five_str}    7d: {seven_str}{reset_str}{stale_note}\n"
                f"Sessions: {acct.get('sessions', 0)}    Tokens: {tok_s}\n"
                f"Score: {a_stars} ({a_score:.1f})"
            )
            self.query_one(widget_id, Static).update(
                Panel(card, title=f"Account {label} ({acct.get('name', '?')})",
                      border_style=color)
            )

        # Fill empty cards for missing accounts
        for label in ["A", "B", "C"]:
            if not any(a["label"] == label for a in accounts):
                widget_id = acct_widgets.get(label)
                if widget_id:
                    self.query_one(widget_id, Static).update(
                        Panel("[dim]No data[/dim]",
                              title=f"Account {label}",
                              border_style="dim")
                    )

        # ── Activity Heatmap ─────────────────────────────────────────
        labels = heatmap.get("labels", [])
        if labels:
            is_hourly = len(labels) <= 72
            bucket_label = "hourly" if is_hourly else "daily"
            max_slots = 12 if is_hourly else 288

            header_line = "      " + " ".join(f"{l:>2}" for l in labels[:48])
            rows = []
            for acct_label in ["A", "B", "C"]:
                color = acct_colors.get(acct_label, "white")
                row_parts = []
                acct_data = heatmap.get(acct_label, [])
                for i, val in enumerate(acct_data[:48]):
                    if val > max_slots * 0.5:
                        row_parts.append(f"[{color}]██[/{color}]")
                    elif val > max_slots * 0.25:
                        row_parts.append(f"[{color}]▓▓[/{color}]")
                    elif val > 0:
                        row_parts.append(f"[dim]░░[/dim]")
                    else:
                        row_parts.append("  ")
                rows.append(f"  {acct_label}:  " + " ".join(row_parts))

            hm_text = f"{header_line}\n" + "\n".join(rows)
            self.query_one("#an-heatmap", Static).update(
                Panel(hm_text,
                      title=f"Activity ({self._active_window}, {bucket_label})",
                      border_style="dim")
            )
        else:
            self.query_one("#an-heatmap", Static).update(
                Panel("[dim]No activity data[/dim]", title="Activity", border_style="dim")
            )

        # ── Waste Analysis ───────────────────────────────────────────
        waste_lines = []
        wasted_h = waste.get("total_wasted_hours", 0)
        waste_p = waste.get("waste_pct", 0)
        waste_lines.append(
            f"Total Fleet Idle: [bold]{wasted_h}h[/bold] ({waste_p}% of available compute)"
        )
        for gap in waste.get("idle_gaps", [])[:5]:
            waste_lines.append(
                f"  [red]Idle gap[/red]: {gap['start_label']} — {gap['end_label']} "
                f"({gap['buckets']} {'hours' if len(labels) <= 72 else 'days'})"
            )
        for u in waste.get("underused", []):
            waste_lines.append(
                f"  [yellow]Underused[/yellow]: Account {u['label']} at {u['utilization_pct']}%"
            )
        if not waste.get("idle_gaps") and not waste.get("underused"):
            waste_lines.append("  [green]No significant waste detected[/green]")

        self.query_one("#an-waste", Static).update(
            Panel("\n".join(waste_lines), title="Waste Analysis", border_style="red")
        )

        # ── Efficiency Metrics Table ─────────────────────────────────
        table = self.query_one("#an-efficiency", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("Metric", width=22)
        table.add_column("Value", width=12)
        table.add_column("Benchmark", width=12)
        table.add_column("Status", width=10)

        def _status(good, warn):
            # type: (bool, bool) -> Tuple[str, str]
            if good:
                return "Good", "green"
            elif warn:
                return "Watch", "yellow"
            return "Action", "red"

        tph = efficiency.get("tokens_per_hour", 0)
        s, sc = _status(tph >= 20000, tph >= 10000)
        table.add_row(
            Text("Tokens/Active Hour"), Text(f"{tph/1000:.1f}k", justify="right"),
            Text("20k+"), Text(s, style=sc),
        )

        cph = efficiency.get("commits_per_hour", 0)
        s, sc = _status(cph >= 0.3, cph >= 0.15)
        table.add_row(
            Text("Commits/Active Hour"), Text(f"{cph:.2f}", justify="right"),
            Text("0.3+"), Text(s, style=sc),
        )

        tpc = efficiency.get("tokens_per_commit", 0)
        s, sc = _status(tpc < 80000, tpc < 120000)
        table.add_row(
            Text("Tokens/Commit"), Text(f"{tpc/1000:.0f}k", justify="right"),
            Text("<80k"), Text(s, style=sc),
        )

        para = efficiency.get("parallelism_avg", 0)
        s, sc = _status(para >= 2.0, para >= 1.2)
        table.add_row(
            Text("Avg Parallelism"), Text(f"{para:.1f}", justify="right"),
            Text("2.0+"), Text(s, style=sc),
        )

        dur = efficiency.get("avg_session_min", 0)
        s, sc = _status(20 <= dur <= 60, 10 <= dur <= 90)
        table.add_row(
            Text("Avg Session Duration"), Text(f"{dur:.0f}m", justify="right"),
            Text("20-60m"), Text(s, style=sc),
        )

        opus_pct = efficiency.get("model_split", {}).get("opus", 0)
        s, sc = _status(50 <= opus_pct <= 70, 35 <= opus_pct <= 85)
        table.add_row(
            Text("Model Split (Opus)"), Text(f"{opus_pct:.0f}%", justify="right"),
            Text("50-70%"), Text(s, style=sc),
        )

        # ── Suggestions ──────────────────────────────────────────────
        if suggestions:
            sug_lines = []
            priority_styles = {
                "high": "red bold", "med": "yellow bold",
                "low": "dim", "info": "green bold",
            }
            for sug in suggestions:
                p = sug.get("priority", "info")
                style = priority_styles.get(p, "white")
                label = p.upper()
                sug_lines.append(f"[{style}]{label}[/{style}] {sug['message']}")
            self.query_one("#an-suggestions", Static).update(
                Panel("\n".join(sug_lines), title="Improvement Suggestions",
                      border_style="green")
            )
        else:
            self.query_one("#an-suggestions", Static).update(
                Panel("[dim]No suggestions — utilization looks healthy[/dim]",
                      title="Improvement Suggestions", border_style="green")
            )

    def action_window_24h(self):
        self._active_window = "24h"
        self._reload()

    def action_window_72h(self):
        self._active_window = "72h"
        self._reload()

    def action_window_1w(self):
        self._active_window = "1w"
        self._reload()

    def action_window_1m(self):
        self._active_window = "1m"
        self._reload()

    def _reload(self):
        try:
            self.query_one("#an-efficiency", DataTable).clear(columns=True)
        except Exception:
            pass
        self.load_content()



class InboxView(LazyView):
    """Token Window Inbox — unified view of everything waiting on you."""

    BINDINGS = [
        Binding("R", "refresh_inbox", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="inbox-header")
        yield DataTable(id="inbox-table")

    def load_content(self):
        self._refresh_inbox()

    def refresh_content(self):
        now = time.time()
        if not hasattr(self, '_last_refresh') or (now - self._last_refresh) > 30:
            self._last_refresh = now
            self._refresh_inbox()

    def _refresh_inbox(self):
        from token_watch_advisor import get_inbox_items
        self._last_refresh = time.time()
        items = get_inbox_items()

        priority_display = {
            1: ("!!!", "bold red"),
            2: (" ! ", "yellow"),
            3: (" · ", "dim"),
        }
        priority_labels = {1: "urgent", 2: "attention", 3: "fyi"}

        urgent = len([i for i in items if i["priority"] == 1])
        attn = len([i for i in items if i["priority"] == 2])
        fyi = len([i for i in items if i["priority"] == 3])

        parts = []
        if urgent:
            parts.append(f"[bold red]{urgent} urgent[/bold red]")
        if attn:
            parts.append(f"[yellow]{attn} attention[/yellow]")
        if fyi:
            parts.append(f"[dim]{fyi} fyi[/dim]")
        counts = "  ".join(parts) if parts else "[dim]empty[/dim]"

        header = f"[bold]Inbox[/bold]  {len(items)} items · {counts}"
        self.query_one("#inbox-header", Static).update(header)

        table = self.query_one("#inbox-table", DataTable)
        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("#", width=3)
        table.add_column("", width=3)
        table.add_column("Category", width=12)
        table.add_column("Source", width=14)
        table.add_column("Summary")
        table.add_column("Action", width=30)

        for idx, item in enumerate(items, 1):
            icon, style = priority_display.get(item["priority"], (" · ", "dim"))
            table.add_row(
                Text(str(idx), style="bold"),
                Text(icon, style=style),
                Text(item["category"], style="bold"),
                Text(item["source"][:14]),
                Text(item["summary"][:80]),
                Text(item["action"], style="dim"),
            )

    def action_refresh_inbox(self):
        self._last_refresh = 0
        self._refresh_inbox()
        self.notify("Inbox refreshed")


class DispatchDetailScreen(Screen):
    """Full dispatch prompt viewer."""
    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
        Binding("c", "copy_prompt", "Copy"),
    ]

    def __init__(self, task: dict, **kwargs):
        super().__init__(**kwargs)
        self._dispatch_item = task

    def compose(self) -> ComposeResult:
        yield Static(id="dd-header")
        yield ScrollableContainer(Static(id="dd-body"), id="dd-scroll")

    def _format_age_detail(self, ts):
        if not ts:
            return "?"
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            delta = datetime.now(timezone.utc) - dt
            hours = delta.total_seconds() / 3600
            date_str = dt.strftime("%b %-d, %H:%M")
            if hours < 24:
                return date_str + " (" + str(int(hours)) + "h ago)"
            return date_str + " (" + str(int(hours / 24)) + "d ago)"
        except Exception:
            return ts[:19] if ts else "?"

    def _find_unlocks(self, task_id):
        try:
            from token_watch_data import _get_dispatch_queue
            data = _get_dispatch_queue()
            unlocks = []
            tid = str(task_id)
            for item in data.get("queue", []) + data.get("active", []):
                blocked = item.get("blocked_by") or ""
                if tid in str(blocked):
                    name = item.get("task_name", "?")[:40]
                    unlocks.append("#" + str(item["id"]) + " " + name)
            return unlocks
        except Exception:
            return []

    def on_mount(self):
        t = self._dispatch_item
        pri_colors = {"critical": "red bold", "high": "yellow", "medium": "white", "low": "dim"}
        pri = t.get("priority", "medium")
        pc = pri_colors.get(pri, "white")

        self.query_one("#dd-header", Static).update(
            "[bold]#" + str(t.get("id", "?")) + "[/bold]  "
            "[" + pc + "]" + pri.upper() + "[/" + pc + "]  "
            "[cyan]" + str(t.get("project", "?")) + "[/cyan]  "
            "[dim italic]c=copy  esc=back[/dim italic]"
        )

        lines = []
        lines.append("[bold]" + str(t.get("task_name", "?")) + "[/bold]")
        lines.append("")

        logged = self._format_age_detail(t.get("created_at", ""))
        session = t.get("created_by_session") or "?"
        source = t.get("source") or "?"
        tier = t.get("tier") or "?"
        difficulty = t.get("difficulty") or "?"
        points = str(t.get("points") or 0)
        tokens = str(t.get("est_tokens_k") or 0)
        runs = str(t.get("run_count") or 0)
        blocked_by = str(t.get("blocked_by") or "none")

        sep = "[dim]" + chr(9472) * 60 + "[/dim]"
        lines.append(sep)
        lane = t.get("lane") or "unassigned"
        lane_colors = {"ui-simplification": "green", "voice-lab": "magenta", "twitter-integration": "cyan"}
        lc = lane_colors.get(lane, "dim")
        lines.append("  [bold]Priority:[/bold]    [" + pc + "]" + pri + "[/" + pc + "]        [bold]Lane:[/bold]        [" + lc + "]" + lane + "[/" + lc + "]")
        lines.append("  [bold]Difficulty:[/bold]  " + difficulty)
        lines.append("  [bold]Points:[/bold]      " + points + "              [bold]Est tokens:[/bold] ~" + tokens + "kT")
        lines.append("  [bold]Tier:[/bold]        " + tier + "             [bold]Run count:[/bold]  " + runs)
        lines.append("  [bold]Source:[/bold]      " + source + "           [bold]Session:[/bold]    " + session)
        lines.append("  [bold]Logged:[/bold]      " + logged)
        lines.append("  [bold]Blocked by:[/bold]  " + blocked_by)

        unlocks = self._find_unlocks(t.get("id"))
        if unlocks:
            joined = ", ".join(unlocks)
            lines.append("  [bold green]Unlocks:[/bold green]    " + joined)
        else:
            lines.append("  [bold]Unlocks:[/bold]    [dim]nothing[/dim]")

        lines.append(sep)
        lines.append("")
        lines.append("[bold cyan]Dispatch Prompt[/bold cyan]")
        lines.append("")
        lines.append(t.get("dispatch_prompt", "(no prompt)"))

        body = chr(10).join(lines)
        self.query_one("#dd-body", Static).update(body)

    def action_copy_prompt(self):
        import subprocess
        prompt = self._dispatch_item.get("dispatch_prompt", "")
        try:
            subprocess.run(["pbcopy"], input=prompt.encode(), check=True)
            self.notify("Copied to clipboard")
        except Exception:
            self.notify("Copy failed", severity="error")

    def action_pop_screen(self):
        self.app.pop_screen()


class DispatchView(LazyView):
    """Dispatch queue — ready tasks with prompts, prioritized for burn sessions."""

    _lane_filter = ""
    _LANE_CYCLE = ["", "ui-simplification", "voice-lab", "twitter-integration"]
    _company_filter = ""
    _COMPANY_CYCLE = ["", "personal", "delphi", "kaa", "frank", "sage", "adinkra"]
    # Default to "atlas" so opening Dispatch for the first time shows the
    # project Alex is shipping. User p-cycles persist as instance attributes
    # that shadow this class default, so manual overrides aren't reset.
    _project_filter = "atlas"
    _PROJECT_CYCLE = ["", "atlas", "atlas-backend", "paperclip", "kaa", "frank-pilot", "token-watch", "battlestation", "openclaw"]

    BINDINGS = [
        Binding("r", "refresh_dispatch", "Refresh"),
        Binding("c", "copy_selected", "Copy"),
        Binding("x", "claim_selected", "Claim"),
        Binding("a", "archive_selected", "Archive"),
        Binding("l", "cycle_lane", "Lane"),
        Binding("p", "cycle_project", "Project"),
        Binding("o", "cycle_company", "Company"),
        Binding("s", "cycle_bug_status", "Bug Status"),
        Binding("f", "fix_bug", "Fix Bug"),
    ]

    def refresh_content(self):
        now = time.time()
        if not hasattr(self, '_last_refresh') or (now - self._last_refresh) > 30:
            self._last_refresh = now
            try:
                self.query_one("#dispatch-table", DataTable).clear(columns=True)
                self.query_one("#bugs-table", DataTable).clear(columns=True)
                self.load_content()
            except Exception:
                pass

    def compose(self) -> ComposeResult:
        # 5-terminal visual grid (#131369) — lazy import keeps tui.py diff narrow.
        from dispatch_grid import DispatchGrid
        yield DispatchGrid(id="dispatch-grid")
        yield Static(id="dispatch-header")
        yield Static(id="dispatch-lanes")
        yield DataTable(id="dispatch-table")
        yield Static(id="dispatch-preview")
        yield Static(id="bugs-header")
        yield DataTable(id="bugs-table")

    def load_content(self):
        from token_watch_data import _get_dispatch_queue
        # Refresh the 5-terminal grid before the dispatch tables.
        try:
            from dispatch_grid import DispatchGrid
            self.query_one("#dispatch-grid", DispatchGrid).update_content()
        except Exception:
            pass
        data = _get_dispatch_queue()
        stats = data["stats"]

        lane_info = ""
        if self._lane_filter:
            lane_info = f"  [bold magenta]lane:{self._lane_filter}[/bold magenta]"
        company_info = ""
        if self._company_filter:
            company_info = f"  [bold green]co:{self._company_filter}[/bold green]"
        project_info = ""
        if self._project_filter:
            project_info = f"  [bold cyan]project:{self._project_filter}[/bold cyan]"

        self.query_one("#dispatch-header", Static).update(
            f"[bold]Dispatch[/bold]  "
            f"[green]{stats['total_ready']} ready[/green]  ·  "
            f"[yellow]{stats['total_active']} active[/yellow]  ·  "
            f"[dim]~{stats['total_tokens_k']}kT total[/dim]{project_info}{lane_info}{company_info}  "
            f"[dim italic]enter=view  c=copy  x=claim  a=archive  l=lane  p=project  o=company  r=refresh[/dim italic]"
        )

        # Lane progress bars for swarm monitoring
        from token_watch_data import _get_lane_progress
        lane_data = _get_lane_progress()
        lane_widget = self.query_one("#dispatch-lanes", Static)
        if lane_data:
            _lc = {"ui-simplification": "green", "voice-lab": "magenta", "twitter-integration": "cyan"}
            lines = []
            for lane_name in sorted(lane_data.keys()):
                info = lane_data[lane_name]
                total = info["total"]
                built = info["built"]
                active = info["active"]
                blocked = info["blocked"]
                color = _lc.get(lane_name, "dim")
                bar_w = 20
                filled = int((built / total) * bar_w) if total else 0
                active_fill = int((active / total) * bar_w) if total else 0
                bar = f"[{color}]{chr(9608) * filled}[/{color}][yellow]{chr(9618) * active_fill}[/yellow]{chr(9617) * (bar_w - filled - active_fill)}"
                agents = [t for t in info["tasks"] if t.get("status") == "in_progress"]
                agent_str = ""
                if agents:
                    parts = [f"{_rich_escape(str(t.get('claimed_by', '?')))}: #{_rich_escape(str(t.get('id', '?')))}" for t in agents[:2]]
                    agent_str = f"  [{color}]{', '.join(parts)}[/{color}]"
                blk_str = f"  [dim]{blocked} blocked[/dim]" if blocked else ""
                safe_lane = _rich_escape(lane_name)
                lines.append(f"  [{color}]{safe_lane:<22}[/{color}] {bar} {built}/{total} built{agent_str}{blk_str}")
            lane_widget.update("\n".join(lines))
            lane_widget.display = True
        else:
            lane_widget.update("")
            lane_widget.display = False

        table = self.query_one("#dispatch-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("#", width=5)
        table.add_column("Pri", width=5)
        table.add_column("Tier", width=5)
        table.add_column("Project", width=12)
        table.add_column("Lane", width=18)
        table.add_column("Task", width=40)
        table.add_column("Source", width=8)
        table.add_column("Age", width=8)

        pri_styles = {
            "critical": "[red bold]P0[/red bold]",
            "high": "[yellow]P1[/yellow]",
            "medium": "P2",
            "low": "[dim]P3[/dim]",
        }
        tier_styles = {
            "auto": "[green]auto[/green]",
            "assisted": "[yellow]asst[/yellow]",
            "manual": "[red]man[/red]",
        }

        lane_colors = {"ui-simplification": "green", "voice-lab": "magenta", "twitter-integration": "cyan"}

        def _safe_str(val, default="?"):
            """Coerce Supabase values (possibly None) to a safe string."""
            if val is None:
                return default
            return str(val)

        def _safe_trunc(val, keep=38, limit=40, default="?"):
            """Truncate a possibly-None string safely, escaping Rich markup."""
            s = _safe_str(val, default)
            if len(s) > limit:
                s = s[:keep] + ".."
            return _rich_escape(s)

        def _lane_cell(item):
            lane = item.get("lane") or ""
            if not lane:
                return "[dim]—[/dim]"
            lc = lane_colors.get(lane, "dim")
            # lane comes from DB — escape to prevent markup injection
            return f"[{lc}]{_rich_escape(lane)}[/{lc}]"

        def _pri_cell(item):
            pri = item.get("priority") or "medium"
            return pri_styles.get(pri, "[dim]?[/dim]")

        def _tier_cell(item):
            tier = item.get("tier") or ""
            return tier_styles.get(tier, "[dim]?[/dim]")

        def _project_cell(item):
            proj = _safe_str(item.get("project"), "?")
            return f"[cyan]{_rich_escape(proj)}[/cyan]"

        self._items = []

        def _row_matches_filters(item):
            if self._lane_filter and (item.get("lane") or "") != self._lane_filter:
                return False
            if self._company_filter and (item.get("company") or "").lower() != self._company_filter:
                return False
            if self._project_filter and (item.get("project") or "").lower() != self._project_filter:
                return False
            return True

        # Active items first (in-progress)
        for item in data["active"]:
            if not _row_matches_filters(item):
                continue
            try:
                age = self._format_age(item.get("created_at", ""))
                table.add_row(
                    _safe_str(item.get("id"), ""),
                    "[magenta]RUN[/magenta]",
                    _tier_cell(item),
                    _project_cell(item),
                    _lane_cell(item),
                    _safe_trunc(item.get("task_name"), keep=38, limit=40),
                    _rich_escape(_safe_str(item.get("source"), "?")),
                    age,
                )
                self._items.append(item)
            except Exception:
                # Drop the bad row rather than abort the whole render.
                continue

        # Queue items (ready)
        for item in data["queue"]:
            if not _row_matches_filters(item):
                continue
            try:
                age = self._format_age(item.get("created_at", ""))
                table.add_row(
                    _safe_str(item.get("id"), ""),
                    _pri_cell(item),
                    _tier_cell(item),
                    _project_cell(item),
                    _lane_cell(item),
                    _safe_trunc(item.get("task_name"), keep=38, limit=40),
                    _rich_escape(_safe_str(item.get("source"), "?")),
                    age,
                )
                self._items.append(item)
            except Exception:
                # Drop the bad row rather than abort the whole render.
                continue

        # ── Bugs section ──
        from token_watch_data import _get_bugs
        try:
            bug_data = _get_bugs()
        except Exception:
            bug_data = {"bugs": [], "stats": {"open": 0, "in_progress": 0, "total": 0}}
        bug_stats = bug_data["stats"]

        self.query_one("#bugs-header", Static).update(
            f"\n[bold red]Bugs[/bold red]  "
            f"[red]{bug_stats['open']} open[/red]  ·  "
            f"[yellow]{bug_stats['in_progress']} in progress[/yellow]  ·  "
            f"[dim italic]/bug to file  s=status  f=fix[/dim italic]"
        )

        bug_table = self.query_one("#bugs-table", DataTable)
        bug_table.cursor_type = "row"
        bug_table.zebra_stripes = True
        bug_table.add_column("#", width=6)
        bug_table.add_column("Sev", width=6)
        bug_table.add_column("Status", width=8)
        bug_table.add_column("Project", width=12)
        bug_table.add_column("Title", width=38)
        bug_table.add_column("Found By", width=12)
        bug_table.add_column("Age", width=8)

        sev_styles = {
            "critical": "[red bold]CRIT[/red bold]",
            "high": "[yellow]HIGH[/yellow]",
            "medium": "MED",
            "low": "[dim]LOW[/dim]",
        }

        self._bug_items = []
        for bug in bug_data["bugs"]:
            try:
                self._bug_items.append(bug)
                age = self._format_age(bug.get("created_at", ""))
                found_raw = _safe_str(bug.get("found_by"), "?")
                found = (found_raw[:10] + "..") if len(found_raw) > 12 else found_raw
                status = bug.get("status", "open") or "open"
                status_styles = {
                    "open": "[red]OPEN[/red]",
                    "in_progress": "[yellow]WIP[/yellow]",
                    "fixed": "[green]FIXED[/green]",
                    "verified": "[bold green]DONE[/bold green]",
                }
                bug_table.add_row(
                    f"BUG-{_safe_str(bug.get('bug_number'), '?')}",
                    sev_styles.get(bug.get("severity", "medium"), "?"),
                    status_styles.get(status, status),
                    f"[cyan]{_rich_escape(_safe_str(bug.get('project'), '?'))}[/cyan]",
                    _safe_trunc(bug.get("title"), keep=36, limit=38),
                    _rich_escape(found),
                    age,
                )
            except Exception:
                # Drop the bad row rather than abort the whole render.
                continue

    def _format_age(self, created_at: str) -> str:
        if not created_at:
            return "?"
        try:
            from datetime import datetime, timezone
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            delta = datetime.now(timezone.utc) - created
            hours = delta.total_seconds() / 3600
            if hours < 1:
                return f"{int(delta.total_seconds()/60)}m"
            elif hours < 24:
                return f"{int(hours)}h"
            else:
                return f"{int(hours/24)}d"
        except Exception:
            return "?"

    def action_cycle_lane(self):
        """Cycle lane filter: all → ui-simplification → voice-lab → twitter-integration → all."""
        try:
            idx = self._LANE_CYCLE.index(self._lane_filter)
        except ValueError:
            idx = -1
        self._lane_filter = self._LANE_CYCLE[(idx + 1) % len(self._LANE_CYCLE)]
        label = self._lane_filter or "all"
        self.notify(f"Lane filter: {label}")
        self.action_refresh_dispatch()

    def action_cycle_project(self):
        """Cycle project filter: all → atlas → atlas-backend → paperclip → … → all."""
        try:
            idx = self._PROJECT_CYCLE.index(self._project_filter)
        except ValueError:
            idx = -1
        self._project_filter = self._PROJECT_CYCLE[(idx + 1) % len(self._PROJECT_CYCLE)]
        label = self._project_filter or "all"
        self.notify(f"Project: {label}")
        self.action_refresh_dispatch()

    def action_cycle_company(self):
        """Cycle company filter: all → personal → delphi → kaa → frank → sage → adinkra → all."""
        try:
            idx = self._COMPANY_CYCLE.index(self._company_filter)
        except ValueError:
            idx = -1
        self._company_filter = self._COMPANY_CYCLE[(idx + 1) % len(self._COMPANY_CYCLE)]
        label = self._company_filter or "all"
        self.notify(f"Company filter: {label}")
        self.action_refresh_dispatch()

    def action_refresh_dispatch(self):
        try:
            self.query_one("#dispatch-table", DataTable).clear(columns=True)
            self.query_one("#bugs-table", DataTable).clear(columns=True)
        except Exception:
            pass
        try:
            self.load_content()
            self.notify("Dispatch refreshed")
        except Exception as e:
            self.notify(f"Dispatch error: {e}", severity="error")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Row hover — show first 3 lines of dispatch_prompt in preview bar."""
        try:
            if getattr(event, "data_table", None) is not None and event.data_table.id != "dispatch-table":
                return
        except Exception:
            pass
        try:
            preview = self.query_one("#dispatch-preview", Static)
        except Exception:
            return
        items = getattr(self, "_items", None)
        if not items:
            preview.update("")
            return
        idx = getattr(event, "cursor_row", None)
        if idx is None or idx < 0 or idx >= len(items):
            preview.update("")
            return
        item = items[idx]
        prompt = item.get("dispatch_prompt") or ""
        if not prompt:
            preview.update(f"[dim]#{item.get('id', '?')} — no dispatch_prompt[/dim]")
            return
        lines = [ln for ln in prompt.splitlines() if ln.strip()][:3]
        snippet = "\n".join(f"  {_rich_escape(ln[:120])}" for ln in lines)
        header = f"[bold cyan]#{item.get('id', '?')}[/bold cyan] [dim]preview:[/dim]"
        preview.update(f"{header}\n{snippet}")

    def on_data_table_row_selected(self, event):
        """Enter pressed on a row — open detail view."""
        if not hasattr(self, '_items') or not self._items:
            return
        try:
            table = self.query_one("#dispatch-table", DataTable)
            row_idx = table.cursor_row
            if 0 <= row_idx < len(self._items):
                self.app.push_screen(DispatchDetailScreen(self._items[row_idx]))
        except Exception:
            pass

    def action_copy_selected(self):
        import subprocess
        table = self.query_one("#dispatch-table", DataTable)
        if not hasattr(self, '_items') or not self._items:
            return
        try:
            row_idx = table.cursor_row
            if 0 <= row_idx < len(self._items):
                prompt = self._items[row_idx].get("dispatch_prompt", "")
                subprocess.run(["pbcopy"], input=prompt.encode(), check=True)
                self.notify("Copied to clipboard")
        except Exception:
            self.notify("Copy failed", severity="error")

    def action_claim_selected(self):
        """Claim the selected task — sets status=in_progress, claimed_by=this session."""
        from token_watch_data import _dispatch_claim_task
        if not hasattr(self, '_items') or not self._items:
            return
        try:
            table = self.query_one("#dispatch-table", DataTable)
            row_idx = table.cursor_row
            if 0 <= row_idx < len(self._items):
                item = self._items[row_idx]
                if item.get("status") != "ready":
                    self.notify("Already claimed", severity="warning")
                    return
                ok = _dispatch_claim_task(item["id"])
                if ok:
                    self.notify("Claimed #" + str(item["id"]) + " — " + item.get("task_name", "")[:30])
                    self.action_refresh_dispatch()
                else:
                    self.notify("Claim failed", severity="error")
        except Exception:
            self.notify("Claim failed", severity="error")

    def action_archive_selected(self):
        """Archive the selected task — removes from queue."""
        from token_watch_data import _dispatch_archive_task
        if not hasattr(self, '_items') or not self._items:
            return
        try:
            table = self.query_one("#dispatch-table", DataTable)
            row_idx = table.cursor_row
            if 0 <= row_idx < len(self._items):
                item = self._items[row_idx]
                ok = _dispatch_archive_task(item["id"])
                if ok:
                    self.notify("Archived #" + str(item["id"]))
                    self.action_refresh_dispatch()
                else:
                    self.notify("Archive failed", severity="error")
        except Exception:
            self.notify("Archive failed", severity="error")

    def _get_focused_bug(self):
        """Return the bug item if the bugs table has focus, else None."""
        try:
            bug_table = self.query_one("#bugs-table", DataTable)
            if not bug_table.has_focus or not hasattr(self, '_bug_items') or not self._bug_items:
                return None
            row_idx = bug_table.cursor_row
            if 0 <= row_idx < len(self._bug_items):
                return self._bug_items[row_idx]
        except Exception:
            pass
        return None

    def action_cycle_bug_status(self):
        """Cycle bug status: open → in_progress → fixed → open."""
        bug = self._get_focused_bug()
        if not bug:
            self.notify("Focus a bug row first", severity="warning")
            return
        from token_watch_data import _update_bug_status
        cycle = {"open": "in_progress", "in_progress": "fixed", "fixed": "open"}
        current = bug.get("status", "open")
        new_status = cycle.get(current, "open")
        ok = _update_bug_status(bug["id"], new_status)
        if ok:
            num = bug.get("bug_number", "?")
            self.notify(f"BUG-{num}: {current} → {new_status}")
            self.action_refresh_dispatch()
        else:
            self.notify("Status update failed", severity="error")

    def action_fix_bug(self):
        """Mark bug as fixed by this session."""
        bug = self._get_focused_bug()
        if not bug:
            self.notify("Focus a bug row first", severity="warning")
            return
        from token_watch_data import _fix_bug
        import os
        session_id = f"cc-{os.getppid()}"
        ok = _fix_bug(bug["id"], session_id)
        if ok:
            num = bug.get("bug_number", "?")
            self.notify(f"BUG-{num} fixed by {session_id}")
            self.action_refresh_dispatch()
        else:
            self.notify("Fix failed", severity="error")


class ExpensiveTurnsView(LazyView):
    """Costliest individual turns across all sessions."""

    BINDINGS = [
        Binding("1", "window_1d", "1 Day"),
        Binding("2", "window_3d", "3 Days"),
        Binding("3", "window_7d", "7 Days"),
    ]

    _active_days = 3

    def compose(self) -> ComposeResult:
        yield Static(id="et-header")
        yield Static(id="et-model-summary")
        yield DataTable(id="et-table")

    def load_content(self):
        self._loaded = True
        self._refresh()

    def refresh_content(self):
        self._refresh()

    def _refresh(self):
        from token_watch_data import _get_expensive_turns, _model_cost_stats, _estimate_turn_cost, _format_cost

        days = self._active_days
        turns = _get_expensive_turns(limit=50, days=days)
        model_stats = _model_cost_stats(days=days)

        # Header
        total_turns = sum(m["turns"] for m in model_stats.values())
        total_cost_pct = sum(m["total_pct"] for m in model_stats.values())
        self.query_one("#et-header", Static).update(
            f"[bold]Expensive Turns[/bold]  [dim]({days}d window)[/dim]  "
            f"{total_turns} turns  {total_cost_pct:.1f}% total budget  "
            f"[dim italic]1/2/3 to change window[/dim italic]"
        )

        # Model summary - one line per model showing avg cost
        model_lines = []
        for model, stats in sorted(model_stats.items(), key=lambda x: x[1]["avg_tokens"], reverse=True):
            model_lines.append(
                f"  [bold]{model}[/bold]: {stats['turns']} turns  "
                f"avg {stats['avg_tokens']}tok ({stats['avg_pct']:.2f}%/turn)  "
                f"total {stats['total_pct']:.1f}%"
            )
        self.query_one("#et-model-summary", Static).update(
            Panel("\n".join(model_lines) if model_lines else "[dim]no data[/dim]",
                  title="[bold]Model Cost Breakdown[/bold]", border_style="yellow")
        )

        # Table
        table = self.query_one("#et-table", DataTable)
        if not table.columns:
            table.add_column("Session", width=12)
            table.add_column("Turn", width=5)
            table.add_column("In", width=8)
            table.add_column("Out", width=8)
            table.add_column("Cost", width=7)
            table.add_column("~5h%", width=6)
            table.add_column("Model", width=10)
            table.add_column("Tools", width=20)
            table.add_column("Prompt")
        table.clear()

        for t in turns:
            cost = _estimate_turn_cost(t["tokens_in"], t["tokens_out"], t["model"])
            cost_str = _format_cost(cost)
            pct_style = "bold red" if t["pct_est"] > 2 else ("yellow" if t["pct_est"] > 1 else "dim")

            in_str = f"{t['tokens_in']/1000:.1f}k" if t['tokens_in'] >= 1000 else str(t['tokens_in'])
            out_str = f"{t['tokens_out']/1000:.1f}k" if t['tokens_out'] >= 1000 else str(t['tokens_out'])

            table.add_row(
                Text(t["session_short"]),
                Text(str(t["turn"])),
                Text(in_str, style="dim"),
                Text(out_str),
                Text(cost_str, style="green"),
                Text(f"{t['pct_est']:.1f}%", style=pct_style),
                Text(t["model"]),
                Text(t["tools"][:20], style="dim"),
                Text(t["prompt"]),
            )

    def action_window_1d(self):
        self._active_days = 1
        self._refresh()

    def action_window_3d(self):
        self._active_days = 3
        self._refresh()

    def action_window_7d(self):
        self._active_days = 7
        self._refresh()


class ArtifactsView(LazyView):
    """Artifacts — strategic diagrams + vision docs. Press B to open."""

    def compose(self) -> ComposeResult:
        yield Static(id="artifacts-header")
        yield Static(id="artifacts-sandwich")
        yield Static(id="artifacts-fleet")
        yield Static(id="artifacts-ctx")
        yield Static(id="artifacts-vault")

    def load_content(self):
        self.query_one("#artifacts-header", Static).update(
            "[bold]Artifacts[/bold]  [dim]strategic diagrams · press B[/dim]"
        )

        sandwich = (
            "\n[bold cyan]Opus Sandwich — Three Moments[/bold cyan]\n\n"
            "┌─────────────────────────────────────────────────────┐\n"
            "│  OPUS #1   Strategic planning + architecture        │\n"
            "│            'what matters, how it fits together'     │\n"
            "├─────────────────────────────────────────────────────┤\n"
            "│  SONNET    Orchestrate, route, decompose            │\n"
            "├─────────────────────────────────────────────────────┤\n"
            "│  CHEAP     M2.7-HS (70%) · Kimi (15%)              │\n"
            "│  ENGINES   Codex (10%) · Gemini (5%) · Grok (rt)   │\n"
            "├─────────────────────────────────────────────────────┤\n"
            "│  SONNET    Collect + integrate                      │\n"
            "├─────────────────────────────────────────────────────┤\n"
            "│  OPUS #3   Validate before ship (audit)             │\n"
            "└─────────────────────────────────────────────────────┘\n"
            "[dim]Rule: if wrong decision costs >1h to reverse → Opus[/dim]"
        )
        self.query_one("#artifacts-sandwich", Static).update(sandwich)

        fleet = (
            "\n[bold cyan]Engine Fleet Routing[/bold cyan]\n\n"
            "  [yellow]Strategic plan / architecture / audit[/yellow]   → Opus Max\n"
            "  [cyan]Orchestrate, route, integrate[/cyan]              → Sonnet (session)\n"
            "  [green]Tests / boilerplate / lint / types[/green]       → M2.7-highspeed\n"
            "  [green]Multi-file refactor, isolated PR[/green]         → Codex GPT-5.4\n"
            "  [green]Quality-critical code[/green]                    → Kimi K2.6-cp\n"
            "  [green]Long-context research / synthesis[/green]        → Gemini 3 Pro\n"
            "  [green]Real-time X / trending[/green]                   → Grok 4.20\n"
            "  [green]Multimodal (image/video/audio)[/green]           → Gemini / M2.7\n\n"
            "  Cost: M2.7 $80/mo · Kimi $39 · Codex $200 · Gemini $20 · Grok PAYG\n"
            "  [dim]Total fleet: ~$986/mo for uncapped 24/7 build[/dim]"
        )
        self.query_one("#artifacts-fleet", Static).update(fleet)

        ctx = (
            "\n[bold cyan]Context Protocol — CTX Zones[/bold cyan]\n\n"
            "  [green]CTX-A   0–60%[/green]    CRUISE     full-depth work\n"
            "  [yellow]CTX-B  60–85%[/yellow]   RAMP       cheap engines only\n"
            "  [red]CTX-C  85–95%[/red]   AFTERBURN  wrap + dump decisions\n"
            "  [bold red]CTX-D  95–100%[/bold red]  EXHAUST    hard stop, /close\n\n"
            "  [dim]/compact works below 200k · above 200k requires /extra-usage[/dim]"
        )
        self.query_one("#artifacts-ctx", Static).update(ctx)

        vault_root = Path.home() / "obsidian" / "sage-vault"
        lines = ["\n[bold cyan]sage-vault index[/bold cyan]\n"]
        for subdir, label in [("sage/vision", "Vision docs"), ("intel/patterns", "Intel patterns")]:
            d = vault_root / subdir
            lines.append(f"[bold]{label}[/bold]  [dim]{subdir}/[/dim]")
            if d.exists():
                for f in sorted(d.glob("*.md"))[:10]:
                    sz = f.stat().st_size // 1024
                    lines.append(f"  [cyan]•[/cyan] {f.name}  [dim]{sz}k[/dim]")
            else:
                lines.append("  [dim](not found)[/dim]")
            lines.append("")
        self.query_one("#artifacts-vault", Static).update("\n".join(lines))


class ClaudeWatchApp(App):
    CSS_PATH = "token_watch_tui.tcss"
    TITLE = "Token Window"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "force_refresh", "Refresh"),
        Binding("e", "export_csv", "Export CSV"),
        Binding("u", "show_usage", "Usage"),
        Binding("m", "show_mcp", "MCP"),
        Binding("s", "show_session_tasks", "Cycle"),
        Binding("p", "show_project_board", "Board"),
        Binding("l", "show_leaderboard", "Leaderboard"),
        Binding("a", "show_audit", "Audit"),
        Binding("c", "show_capacity", "Capacity"),
        Binding("h", "toggle_health", "Health"),
        Binding("y", "show_cycles", "Cycles"),
        Binding("W", "show_weekly", "Weekly"),
        Binding("x", "show_test", "Test"),
        Binding("A", "toggle_accounts", "Accounts"),
        Binding("w", "show_wire", "Wire"),
        Binding("M", "show_mission", "Mission"),
        Binding("j", "show_projects_stats", "Projects"),
        Binding("shift+d", "show_delphi", "Delphi"),
        Binding("v", "show_advisor", "TW Advisor"),
        Binding("i", "show_inbox", "Inbox"),
        Binding("[", "prev_cycle", "Prev Cycle"),
        Binding("]", "next_cycle", "Next Cycle"),
        Binding("0", "all_cycles", "All Cycles"),
        Binding("t", "show_analytics", "Analytics"),
        Binding("g", "show_rules", "Rules"),
        Binding("d", "show_dispatch", "Dispatch"),
        Binding("w", "show_attribution", "Who?"),
        Binding("slash", "start_search", "Search"),
        Binding("tab", "focus_next", "Next panel", show=False),
        Binding("shift+tab", "focus_previous", "Prev panel", show=False),
        Binding("R", "reload_build", "Reload", show=False),
        Binding("E", "show_expensive_turns", "Expensive"),
        Binding("P", "show_advisor_plan", "Plan"),
        Binding("B", "show_artifacts", "Artifacts"),
        Binding("O", "show_plans", "Plans"),
        Binding("T", "show_team", "Team"),
    ]

    _filter_text = ""
    _pending_reload = False
    _revert_cooldown_until = 0.0

    def compose(self) -> ComposeResult:
        from textual.widgets import Input, Footer
        yield ReloadBanner(id="reload-banner")
        yield CompactStatusBar(id="compact-status-bar")
        yield NavBar(id="nav-bar")
        yield Static(id="cycle-banner")
        with ContentSwitcher(initial="view-dashboard", id="content-switcher"):
            with ScrollableContainer(id="view-dashboard"):
                yield AccountCapacityPanel(id="account-capacity")
                yield BurndownChart(id="burndown")
                yield TokenAttributionPanel(id="attribution")
                yield Input(placeholder="Search sessions (ccid, project, directive)...", id="search-input")
                yield UrgentAlerts(id="urgent")
                yield SystemStatusPanel(id="system-status")
                yield EngineTable(id="active-sessions")
                yield SessionNarrativePanel(id="session-narrative")
                yield SessionHistoryTable(id="session-history")
                yield DrainPanel(id="drain")
                with Horizontal(id="feed-row"):
                    yield ToolFrequency(id="tool-freq")
                    yield SkillsPanel(id="skills")
                    yield AgentsPanel(id="agents")
            yield UsageMetricsView(id="view-usage")
            yield MCPStatsView(id="view-mcp")
            yield SessionTasksView(id="view-sessions")
            yield ProjectBoardView(id="view-projects")
            yield AccountCapacityView(id="view-capacity")
            yield LeaderboardView(id="view-leaderboard")
            yield CyclesView(id="view-cycles")
            yield WeeklyCyclesView(id="view-weekly")
            yield TestQueueView(id="view-test")
            yield AuditView(id="view-audit")
            yield MissionControlView(id="view-mission")
            yield ProjectsView(id="view-proj-stats")
            yield DelphiView(id="view-delphi")
            yield WireView(id="view-wire")
            yield ForgeHandoutsView(id="view-forge-feed")
            yield ForgeStatusView(id="view-forge-status")
            yield ForgePipelineView(id="view-forge-pipeline")
            yield RulesView(id="view-rules")
            yield AdvisorView(id="view-advisor")
            yield InboxView(id="view-inbox")
            yield AnalyticsView(id="view-analytics")
            yield DispatchView(id="view-dispatch")
            yield ExpensiveTurnsView(id="view-expensive-turns")
            yield PlansView(id="view-plans")
            yield AdvisorPlanView(id="view-advisor-plan")
            yield EmployeeView(id="view-team")
            yield ArtifactsView(id="view-artifacts")
        yield Footer()

    def switch_view(self, view_id: str) -> None:
        """Switch content view and update NavBar highlight."""
        switcher = self.query_one("#content-switcher", ContentSwitcher)
        switcher.current = view_id
        # Lazy-load on first visit
        if view_id != "view-dashboard":
            view = self.query_one(f"#{view_id}")
            if hasattr(view, '_loaded') and not view._loaded:
                view._loaded = True
                view.load_content()
        # Update NavBar active button
        nav_map = {
            "view-dashboard": "nav-dashboard",
            "view-usage": "nav-usage",
            "view-mcp": "nav-mcp",
            "view-sessions": "nav-sessions",
            "view-projects": "nav-projects",
            "view-capacity": "nav-capacity",
            "view-leaderboard": "nav-leaderboard",
            "view-cycles": "nav-cycles",
            "view-weekly": "nav-weekly",
            "view-test": "nav-test",
            "view-rules": "nav-rules",
            "view-audit": "nav-audit",
            "view-wire": "nav-wire",
            "view-forge-feed": "nav-forge-feed",
            "view-forge-status": "nav-forge-status",
            "view-forge-pipeline": "nav-forge-pipeline",
            "view-mission": "nav-mission",
            "view-proj-stats": "nav-mission",
            "view-delphi": "nav-delphi",
            "view-advisor": "nav-advisor",
            "view-analytics": "nav-analytics",
            "view-dispatch": "nav-dispatch",
            "view-inbox": "nav-inbox",
            "view-expensive-turns": "nav-dashboard",
            "view-plans": "nav-plans",
            "view-advisor-plan": "nav-plan",
            "view-team": "nav-team",
            "view-artifacts": "nav-artifacts",
        }
        active_nav = nav_map.get(view_id, "")
        for btn in self.query("#nav-bar Button"):
            btn.variant = "primary" if btn.id == active_nav else "default"

    def on_mount(self):
        _load_index()
        _backup_working_files()
        self.build_index()
        # Hide search input, account capacity, and header (merged into burndown)
        self.query_one("#search-input").display = False
        self.query_one("#account-capacity").display = False
        self.set_interval(1.0, self.refresh_data)
        self.refresh_data()
        # Cycle navigation state
        from token_watch_data import _get_current_cycle_id, _get_all_cycles
        self._active_cycle_id = _get_current_cycle_id()
        self._cycle_list = []  # populated on first nav
        self._cycle_idx = 0
        # Start hot-reload watcher in background
        import threading
        threading.Thread(target=_start_hot_reload_watcher, args=(self,), daemon=True).start()

    _RESTART_EXIT_CODE = 42

    def _trigger_reload(self):
        """Legacy — redirects to safe reload flow."""
        self._signal_files_changed()

    def _signal_files_changed(self):
        """Called from watcher thread when source files change. Auto-validates and restarts."""
        import time as _time
        if _time.time() < self._revert_cooldown_until:
            return
        self._pending_reload = True
        try:
            self.query_one("#reload-banner", ReloadBanner).show_pending()
        except Exception:
            pass
        # Auto-validate and restart after brief delay
        self.set_timer(1.0, self.action_reload_build)

    def action_reload_build(self):
        """Validate new code and restart if safe, or revert if broken."""
        if not self._pending_reload:
            return

        import subprocess, sys
        project_dir = str(Path(__file__).resolve().parent)

        result = subprocess.run(
            [sys.executable, "-c", "import token_watch_data; import token_watch_tui"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode == 0:
            self._pending_reload = False
            _backup_working_files()
            self.notify("Reloading...", severity="warning", timeout=1)
            self.set_timer(0.5, lambda: self.exit(return_code=self._RESTART_EXIT_CODE))
        else:
            import time as _time
            error_msg = result.stderr or result.stdout or "Unknown import error"
            self._log_build_error(error_msg)

            # Only revert if backup is fresh (set within last 60s = same edit cycle)
            backup_age = _get_backup_age()
            should_revert = backup_age < 60

            if should_revert:
                restored = _restore_backup_files()
            else:
                restored = False

            try:
                banner = self.query_one("#reload-banner", ReloadBanner)
                if restored:
                    banner.show_reverted(error_msg)
                    self.notify("Build broken \u2014 reverted to last working version", severity="error", timeout=10)
                else:
                    banner.show_reverted(error_msg)
                    self.notify("Build broken \u2014 fix the error and save again", severity="error", timeout=15)
            except Exception:
                pass
            self._pending_reload = False
            self._revert_cooldown_until = _time.time() + 5

    def _log_build_error(self, error_msg):
        """Log build error for debugging."""
        try:
            log_dir = Path.home() / ".claude" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "Token Window-build-errors.log"
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(log_file, "a") as f:
                f.write(f"\n{'=' * 60}\n")
                f.write(f"[{timestamp}] Build validation failed\n")
                f.write(f"{'=' * 60}\n")
                f.write(error_msg)
                f.write("\n")
        except Exception:
            pass

    def build_index(self):
        import threading
        t = threading.Thread(target=_build_or_update_index, daemon=True)
        t.start()

    def _update_cycle_banner(self):
        """Update the global cycle status banner shown on all tabs."""
        from token_watch_data import (
            _get_current_cycle, _get_cycle_sessions,
            _countdown, _current_pct, _get_current_pomodoro,
        )
        try:
            banner = self.query_one("#cycle-banner", Static)
        except Exception:
            return
        current = _get_current_cycle()
        if not current:
            banner.update("[dim]No active cycle[/dim]")
            return
        five, seven, five_reset, _sr = _current_pct()
        reset_str = _countdown(five_reset) if five_reset else "?"

        def _bar(pct, w=6):
            try:
                p = float(pct)
                n = max(1, int(p * w / 100)) if p > 0 else 0
                c = "red" if p < 25 else ("yellow" if p < 50 else "green")
                return f"[dim]{chr(9617) * (w - n)}[/dim][{c}]{chr(9608) * n}[/{c}]"
            except Exception:
                return f"[dim]{chr(9617) * w}[/dim]"

        try:
            burn_pct = float(five)
        except (ValueError, TypeError):
            burn_pct = 0.0
        try:
            seven_pct = float(seven)
        except (ValueError, TypeError):
            seven_pct = 0.0

        # Time elapsed in 5h window
        try:
            reset_dt = datetime.fromisoformat(str(five_reset).replace("Z", "+00:00"))
            mins_left = max(0, (reset_dt - datetime.now(timezone.utc)).total_seconds() / 60)
            time_pct = min(100, max(0, mins_left / 300 * 100))
            h_l = int(mins_left // 60)
            m_l = int(mins_left % 60)
            time_str = f"{h_l}h{m_l:02d}m"
        except Exception:
            time_pct = 100
            time_str = "?"

        pomo = _get_current_pomodoro()
        if pomo:
            pomo_color = "cyan" if pomo <= 3 else ("white" if pomo <= 7 else ("yellow" if pomo <= 9 else "red"))
            pomo_str = f"[{pomo_color}]P{pomo}/10[/{pomo_color}]  "
        else:
            pomo_str = ""

        # Detect Pomodoro block transition and notify about next planned task
        if pomo and pomo != getattr(self, "_last_pomo", None):
            self._last_pomo = pomo
            try:
                from token_watch_data import _get_next_pomodoro_task
                next_task = _get_next_pomodoro_task()
                if next_task:
                    task_title = next_task.get("title", "")[:50]
                    self.notify(f"Block P{pomo} started \u2014 next task: {task_title}")
            except Exception:
                pass

        sessions = _get_cycle_sessions(current["cycle_id"])
        from token_watch_data import _get_cycle_items
        bd = _get_burndown_data()
        ws = ""
        if bd and bd.get("window_start"):
            ws_val = bd["window_start"]
            ws = ws_val.isoformat() if isinstance(ws_val, datetime) else str(ws_val)
        items = _get_cycle_items(ws) if ws else []
        done_ct = sum(1 for i in items if i.get("status") == "done")
        open_ct = sum(1 for i in items if i.get("status") == "open")
        items_str = f"[green]{done_ct}[/green]/{done_ct + open_ct} tasks" if items else ""
        five_left = max(0, 100 - burn_pct)
        seven_left = max(0, 100 - seven_pct)
        five_lc = "red" if five_left < 20 else ("yellow" if five_left < 40 else "green")
        seven_lc = "red" if seven_left < 15 else ("yellow" if seven_left < 30 else "green")
        time_lc = "red" if time_pct < 20 else ("yellow" if time_pct < 40 else "green")
        banner.update(
            f"T{_bar(time_pct)}[{time_lc}]{time_str}[/{time_lc}] "
            f"{pomo_str}"
            f"5h{_bar(five_left)}[{five_lc}]{five_left:.0f}%[/{five_lc}] "
            f"7d{_bar(seven_left)}[{seven_lc}]{seven_left:.0f}%[/{seven_lc}]  "
            f"{items_str}  "
            f"{current['cost_str']}  "
            f"[cyan]{current['gravity_label'] or chr(8212)}[/cyan]"
        )

    def refresh_data(self):
        # Compact status bar — always visible regardless of active tab
        try:
            self.query_one("#compact-status-bar", CompactStatusBar).update_content()
        except Exception:
            pass

        # Global cycle banner — always update regardless of active tab
        self._update_cycle_banner()

        switcher = self.query_one("#content-switcher", ContentSwitcher)

        if switcher.current == "view-dashboard":
            acct = self.query_one("#account-capacity", AccountCapacityPanel)
            if acct.display:
                acct.update_content()
            self.query_one("#burndown", BurndownChart).update_content()
            self.query_one("#attribution", TokenAttributionPanel).update_content()
            self.query_one("#urgent", UrgentAlerts).update_content()
            self.query_one("#system-status", SystemStatusPanel).update_content()
            self.query_one("#active-sessions", EngineTable).refresh_rows()
            self.query_one("#session-narrative", SessionNarrativePanel).update_content()
            self.query_one("#session-history", SessionHistoryTable).refresh_rows()
            self.query_one("#tool-freq", ToolFrequency).update_content()
            self.query_one("#skills", SkillsPanel).update_content()
            self.query_one("#agents", AgentsPanel).update_content()
            self.query_one("#drain", DrainPanel).update_content()
        else:
            try:
                view = self.query_one(f"#{switcher.current}")
                if hasattr(view, 'refresh_content'):
                    view.refresh_content()
            except Exception:
                pass

        # Auto-score completed windows + cycle rollover (keep unconditional)
        from token_watch_data import _check_and_score_completed_window
        new_score = _check_and_score_completed_window()
        if new_score:
            rolled = new_score.get("rolled", 0)
            if "stars" in new_score:
                stars = new_score.get("stars", "")
                ov = new_score.get("overall", 0)
                msg = f"Window scored: {stars} ({ov})"
                if rolled:
                    msg += f" | {rolled} items rolled to new cycle"
                self.notify(msg, severity="information", timeout=10)
            elif rolled:
                self.notify(f"{rolled} cycle items rolled to new window", severity="information", timeout=8)

        # System notifications on spike + auto-gate check (keep unconditional)
        try:
            five_f, seven_f = [float(x) for x in _current_pct()[:2]]
            burndown = _get_burndown_data()
            burn_rate = burndown.get("current_rate") if burndown else None
            check_and_notify(five_f, seven_f, burn_rate)
            _check_auto_gate(five_f)
        except (ValueError, TypeError):
            pass

    def action_force_refresh(self):
        self.build_index()
        switcher = self.query_one("#content-switcher", ContentSwitcher)
        if switcher.current == "view-dashboard":
            self.refresh_data()
        else:
            view = self.query_one(f"#{switcher.current}")
            if hasattr(view, '_loaded'):
                view._loaded = False
                view.load_content()
                view._loaded = True

    def action_export_csv(self):
        filename = os.path.expanduser(
            "~/Downloads/Token Window-{}.csv".format(
                datetime.now().strftime("%Y%m%d-%H%M%S")
            )
        )
        try:
            count = export_session_history_csv(filename)
            self.notify(
                "{} rows exported to {}".format(count, filename),
                severity="information",
                timeout=5,
            )
        except Exception as exc:
            self.notify(
                "Export failed: {}".format(exc),
                severity="error",
                timeout=5,
            )

    def action_show_usage(self):
        self.switch_view("view-usage")

    def action_show_mcp(self):
        self.switch_view("view-mcp")

    def action_show_session_tasks(self):
        self.switch_view("view-sessions")

    def action_show_project_board(self):
        self.switch_view("view-projects")

    def action_show_leaderboard(self):
        self.switch_view("view-leaderboard")

    def action_show_capacity(self):
        self.switch_view("view-capacity")

    def action_show_attribution(self):
        self.push_screen(TokenAttributionScreen())

    def action_show_cycles(self):
        self.switch_view("view-cycles")

    def action_show_weekly(self):
        self.switch_view("view-weekly")

    def action_show_test(self):
        self.switch_view("view-test")

    def action_show_rules(self):
        self.switch_view("view-rules")

    def action_show_audit(self):
        self.switch_view("view-audit")

    def action_show_wire(self):
        self.switch_view("view-wire")

    def action_show_mission(self):
        self.switch_view("view-mission")

    def action_show_projects_stats(self):
        self.switch_view("view-proj-stats")

    def action_show_delphi(self):
        self.switch_view("view-delphi")

    def action_show_advisor(self):
        self.switch_view("view-advisor")

    def action_show_inbox(self):
        self.switch_view("view-inbox")

    def action_show_analytics(self):
        self.switch_view("view-analytics")

    def action_show_dispatch(self):
        self.switch_view("view-dispatch")

    def action_show_expensive_turns(self):
        self.switch_view("view-expensive-turns")

    def action_show_advisor_plan(self):
        self.switch_view("view-advisor-plan")

    def action_show_plans(self):
        self.switch_view("view-plans")

    def action_show_team(self):
        self.switch_view("view-team")

    def action_show_artifacts(self):
        self.switch_view("view-artifacts")

    def _ensure_cycle_list(self):
        """Load cycle list if not loaded."""
        if not self._cycle_list:
            try:
                from token_watch_data import _get_all_cycles
                cycles = _get_all_cycles(limit=20)
                self._cycle_list = [(c["start"], c["end"], c.get("is_current", False)) for c in cycles]
                # Find current cycle index
                for i, (s, e, cur) in enumerate(self._cycle_list):
                    if cur:
                        self._cycle_idx = i
                        break
            except Exception:
                pass

    def action_prev_cycle(self):
        self._ensure_cycle_list()
        if self._cycle_list and self._cycle_idx < len(self._cycle_list) - 1:
            self._cycle_idx += 1
            start, end, _ = self._cycle_list[self._cycle_idx]
            self._active_cycle_id = start.isoformat() if hasattr(start, 'isoformat') else str(start)
            self._update_cycle_banner()
            self.refresh_data()

    def action_next_cycle(self):
        self._ensure_cycle_list()
        if self._cycle_list and self._cycle_idx > 0:
            self._cycle_idx -= 1
            start, end, _ = self._cycle_list[self._cycle_idx]
            self._active_cycle_id = start.isoformat() if hasattr(start, 'isoformat') else str(start)
            self._update_cycle_banner()
            self.refresh_data()

    def action_all_cycles(self):
        """Toggle between current cycle and all cycles."""
        if self._active_cycle_id is None:
            # Switch back to current
            from token_watch_data import _get_current_cycle_id
            self._active_cycle_id = _get_current_cycle_id()
            self._cycle_idx = 0
        else:
            # Show all
            self._active_cycle_id = None
        self._update_cycle_banner()
        self.refresh_data()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_map = {
            "nav-dashboard": "view-dashboard",
            "nav-sessions": "view-sessions",
            "nav-projects": "view-projects",
            "nav-leaderboard": "view-leaderboard",
            "nav-usage": "view-usage",
            "nav-mcp": "view-mcp",
            "nav-cycles": "view-cycles",
            "nav-weekly": "view-weekly",
            "nav-test": "view-test",
            "nav-rules": "view-rules",
            "nav-audit": "view-audit",
            "nav-wire": "view-wire",
            "nav-forge-feed": "view-forge-feed",
            "nav-forge-status": "view-forge-status",
            "nav-forge-pipeline": "view-forge-pipeline",
            "nav-mission": "view-mission",
            "nav-delphi": "view-delphi",
            "nav-advisor": "view-advisor",
            "nav-analytics": "view-analytics",
            "nav-dispatch": "view-dispatch",
            "nav-plans": "view-plans",
            "nav-plan": "view-advisor-plan",
            "nav-artifacts": "view-artifacts",
        }
        btn_id = event.button.id or ""
        if not btn_id.startswith("nav-"):
            return
        # Pop to root first (handles nav from detail screens)
        while len(self.screen_stack) > 1:
            self.pop_screen()
        if btn_id in btn_map:
            self.switch_view(btn_map[btn_id])
        elif btn_id == "nav-health":
            self.switch_view("view-dashboard")
            self.action_toggle_health()
        elif btn_id == "nav-open-nav":
            self.push_screen(NavigationScreen())

    def action_toggle_accounts(self):
        acct = self.query_one("#account-capacity", AccountCapacityPanel)
        if acct.display:
            acct.display = False
        else:
            acct.display = True
            acct.update_content()

    def action_toggle_health(self):
        self.push_screen(HealthScreen())

    def action_start_search(self):
        from textual.widgets import Input
        search = self.query_one("#search-input", Input)
        search.display = True
        search.focus()

    def on_input_changed(self, event):
        if event.input.id == "search-input":
            self._filter_text = event.value.strip().lower()
            self.query_one("#session-history", SessionHistoryTable).refresh_rows()

    def on_input_submitted(self, event):
        if event.input.id == "search-input":
            # Hide search if empty, otherwise keep filtering
            if not event.value.strip():
                event.input.display = False
                self._filter_text = ""
                self.query_one("#session-history", SessionHistoryTable).refresh_rows()

    def on_key(self, event):
        switcher = self.query_one("#content-switcher", ContentSwitcher)
        if event.key == "escape":
            from textual.widgets import Input
            search = self.query_one("#search-input", Input)
            if search.display:
                search.display = False
                search.value = ""
                self._filter_text = ""
                self.query_one("#session-history", SessionHistoryTable).refresh_rows()
                event.prevent_default()
                event.stop()
            elif switcher.current != "view-dashboard":
                self.switch_view("view-dashboard")
                event.prevent_default()
                event.stop()
        elif event.key == "p" and switcher.current == "view-cycles":
            self.push_screen(CyclePlanScreen())
            event.prevent_default()
            event.stop()
        elif event.key == "r" and switcher.current == "view-mission":
            view = self.query_one("#view-mission", MissionControlView)
            view._recovery_filter = not view._recovery_filter
            view._last_refresh = 0  # force refresh
            view.refresh_content()
            event.prevent_default()
            event.stop()


def _cli_session_lookup(args):
    """Handle --session CLI lookup."""
    import sys
    _load_index()
    # Force index build synchronously for CLI
    _build_or_update_index()

    entry = lookup_by_ccid(args.session)
    if not entry:
        print(json.dumps({"error": f"Session '{args.session}' not found"}), file=sys.stderr)
        sys.exit(1)

    if args.context:
        # Build context packet
        turns = _get_session_turns(entry["session_id"])
        last_turns = turns[-5:] if turns else []
        packet = {
            "ccid": entry.get("ccid", "?"),
            "uuid": entry["session_id"],
            "directive": entry.get("gravity") or entry.get("directive", "—"),
            "project": entry.get("project", "—"),
            "first_ts": entry.get("first_ts"),
            "last_ts": entry.get("last_ts"),
            "output_tokens": entry.get("output_tokens", 0),
            "model": entry.get("model", "?"),
            "source": entry.get("source", "?"),
            "transcript_path": str(Path(entry.get("project_dir", "")) / f"{entry['session_id']}.jsonl"),
            "accomplishments": entry.get("accomplishments", {}),
            "last_turns": [
                {"turn": t["turn"], "prompt": t["prompt"], "tools": t["tools"], "tokens_out": t["tokens_out"]}
                for t in last_turns
            ],
        }
        print(json.dumps(packet, indent=2))
    else:
        # Basic lookup
        out = {
            "ccid": entry.get("ccid", "?"),
            "uuid": entry["session_id"],
            "directive": entry.get("gravity") or entry.get("directive", "—"),
            "project": entry.get("project", "—"),
            "first_ts": entry.get("first_ts"),
            "last_ts": entry.get("last_ts"),
            "output_tokens": entry.get("output_tokens", 0),
            "model": entry.get("model", "?"),
            "source": entry.get("source", "?"),
            "transcript_path": str(Path(entry.get("project_dir", "")) / f"{entry['session_id']}.jsonl"),
        }
        print(json.dumps(out, indent=2))


def _cli_list_sessions(args):
    """Handle --list CLI command."""
    _load_index()
    _build_or_update_index()

    from token_watch_data import _get_session_history
    sessions = _get_session_history()[:20]

    if not sys.stdout.isatty():
        # JSON output for piping
        result = []
        for s in sessions:
            result.append({
                "session_id": s["session_id"],
                "project": s.get("project", "—"),
                "directive": s.get("directive", "—"),
                "source": s.get("source", "?"),
                "output_tokens": s.get("output_tokens", 0),
                "duration": s.get("dur_str", "?"),
            })
        print(json.dumps(result, indent=2))
    else:
        # Formatted table for terminal
        fmt = "{:<10} {:<10} {:<12} {:<8} {:<7} {}"
        print(fmt.format("Session", "Source", "Project", "Dur", "Out", "Directive"))
        print("-" * 80)
        from token_watch_data import _build_pid_map
        pid_map = _build_pid_map()
        for s in sessions:
            sid = pid_map.get(s["session_id"], s["session_id"][:10])
            out_k = s["output_tokens"]
            out_str = f"{out_k/1000:.1f}k" if out_k >= 1000 else str(out_k)
            directive = (s.get("directive") or "—")[:40]
            print(fmt.format(
                sid, s.get("source", "?"), s.get("project", "—"),
                s.get("dur_str", "?"), out_str, directive,
            ))


def _snapshot_health_indicator(five, seven, alert_count):
    """Return (color, label) for health dot."""
    try:
        f, s = float(five), float(seven)
    except (ValueError, TypeError):
        return "yellow", "UNKNOWN"
    if f > 80 or s > 90 or alert_count > 0:
        return "red", "RED"
    if f > 50 or s > 70:
        return "yellow", "YELLOW"
    return "green", "GREEN"


def _cli_snapshot():
    """Print compact Rich-formatted token capacity snapshot and exit."""
    from rich.console import Console

    five, seven, five_reset_ts, seven_reset_ts = _current_pct()

    # Gather all data
    five_cd = _countdown(five_reset_ts)
    seven_rd = _reset_day(seven_reset_ts)
    acct_label, acct_name, acct_lane = _get_active_account()
    pacing = _token_pacing()
    sessions = _active_sessions()
    daily = _get_daily_usage(days=1)
    capacities = get_account_capacity_display()

    # Compute daily tokens and cost
    if daily and len(daily) > 0:
        day_tokens = daily[0][1] if len(daily[0]) > 1 else 0
    else:
        day_tokens = 0
    day_cost = _estimate_cost(day_tokens, "opus")
    day_cost_str = _format_cost(day_cost)
    if day_tokens >= 1_000_000:
        tok_str = f"{day_tokens / 1_000_000:.1f}M"
    elif day_tokens >= 1000:
        tok_str = f"{day_tokens / 1000:.0f}k"
    else:
        tok_str = str(day_tokens)

    session_count = len(sessions)
    no_data = five == "?" and seven == "?"
    health_color, health_label = _snapshot_health_indicator(five, seven, 0)

    # JSON mode for piped output
    if not sys.stdout.isatty():
        import json as _json
        payload = {
            "account": {"label": acct_label, "name": acct_name, "lane": acct_lane},
            "five_pct": five, "seven_pct": seven,
            "five_reset": five_cd, "seven_reset": seven_rd,
            "health": health_label,
            "pacing": pacing,
            "sessions_active": session_count,
            "today_tokens": day_tokens,
            "today_cost": day_cost,
            "capacities": capacities,
        }
        print(_json.dumps(payload, default=str))
        return

    console = Console()

    def _bar(pct_val, width=20):
        try:
            p = float(pct_val)
        except (ValueError, TypeError):
            return " " * width
        p = max(0, min(100, p))
        filled = int(p / 100 * width)
        empty = width - filled
        if p > 80:
            color = "red"
        elif p > 50:
            color = "yellow"
        else:
            color = "green"
        bar_text = Text()
        bar_text.append("█" * filled, style=color)
        bar_text.append("░" * empty, style="dim")
        return bar_text

    # Build content
    content = Text()

    # Account line
    content.append(f"Account {acct_label}", style="bold")
    content.append(f" ({acct_name}) ", style="")
    content.append(acct_lane, style="dim")
    content.append("\n\n")

    if no_data:
        content.append("No rate data", style="dim")
        content.append("\n")
    else:
        # 5h bar
        content.append("5h  ")
        content.append_text(_bar(five))
        try:
            content.append(f"  {int(float(five)):>3}%", style="bold")
        except (ValueError, TypeError):
            content.append("    ?%")
        # Extract just the time part from countdown
        reset_short = five_cd.split(" (")[0] if " (" in five_cd else five_cd
        content.append("  resets ", style="dim")
        content.append(reset_short, style="dim")
        content.append("\n")

        # 7d bar
        content.append("7d  ")
        content.append_text(_bar(seven))
        try:
            content.append(f"  {int(float(seven)):>3}%", style="bold")
        except (ValueError, TypeError):
            content.append("    ?%")
        content.append("  resets ", style="dim")
        # Extract just day/month/date (e.g. "Mon Apr 13")
        rd_parts = seven_rd.split(" ")
        reset_day_short = " ".join(rd_parts[:3]) if len(rd_parts) >= 3 else seven_rd
        content.append(reset_day_short, style="dim")
        content.append("\n")

    content.append("\n")

    # Pacing line
    if pacing is not None:
        avg_burn = pacing.get("avg_burn", 0)
        mins_to_100 = pacing.get("mins_to_100", 0)
        mins_to_reset = pacing.get("mins_to_reset", 0)
        burn_style = "bold" if avg_burn > 1.0 else ""
        content.append("Burn: ", style="")
        content.append(f"{avg_burn:.1f}%/min", style=burn_style)
        hrs = int(mins_to_100 // 60)
        mins = int(mins_to_100 % 60)
        if mins_to_100 < mins_to_reset:
            content.append(f" — 100% in ~{hrs}h{mins:02d}m")
        else:
            content.append(f" — 100% in ~{hrs}h{mins:02d}m (resets first)")
        content.append("\n")

    # Sessions + cost line
    content.append(f"Sessions: {session_count} active", style="")
    content.append(f"  Today: {tok_str} tok (~{day_cost_str})", style="")
    content.append("\n")

    # Other accounts footer
    other_accts = [c for c in capacities if not c.get("is_active")]
    if other_accts:
        content.append("\n")
        parts = []
        for c in other_accts:
            lbl = c.get("label", "?")
            fp = c.get("five_pct")
            sp = c.get("seven_pct")
            if fp is None:
                fp_str = "—"
            else:
                try:
                    fp_str = f"{int(float(fp))}%"
                except (ValueError, TypeError):
                    fp_str = str(fp)
            if sp is None:
                sp_str = "—"
            else:
                try:
                    sp_str = f"{int(float(sp))}%"
                except (ValueError, TypeError):
                    sp_str = str(sp)
            parts.append(f"{lbl}: 5h:{fp_str} 7d:{sp_str}")
        content.append("  ".join(parts), style="dim")
        content.append("\n")

    # Health indicator for title
    health_dot = Text()
    health_dot.append("● ", style=health_color)
    health_dot.append(health_label, style=health_color)

    title = Text()
    title.append(" Token Capacity ")

    subtitle_text = Text()
    subtitle_text.append(" ")
    subtitle_text.append_text(health_dot)
    subtitle_text.append(" ")

    panel = Panel(
        content,
        title=title,
        subtitle=subtitle_text,
        border_style="bright_cyan",
        width=56,
        padding=(0, 1),
    )
    console.print(panel)


def _cli_advisor(args):
    """Run advisor and print insights to terminal."""
    import json as _json
    from token_watch_advisor import run_advisor

    report = run_advisor(force_refresh=True)

    if getattr(args, 'json', False) or not sys.stdout.isatty():
        payload = {
            "timestamp": report.timestamp,
            "duration_ms": report.duration_ms,
            "checks_run": report.checks_run,
            "summary": report.summary,
            "insights": [
                {
                    "category": i.category,
                    "severity": i.severity,
                    "title": i.title,
                    "message": i.message,
                    "action": i.action,
                }
                for i in report.insights
            ],
        }
        print(_json.dumps(payload, indent=2))
        return

    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table as RichTable

    console = Console()

    severity_display = {
        "critical": ("!!!", "bold red"),
        "warning":  (" ! ", "yellow"),
        "info":     (" i ", "blue"),
        "positive": (" + ", "green"),
    }

    # Summary line
    parts = []
    for sev, label in [("critical", "Critical"), ("warning", "Warning"), ("info", "Info"), ("positive", "Positive")]:
        n = report.summary.get(sev, 0)
        if n > 0:
            _, style = severity_display[sev]
            parts.append(f"[{style}]{n} {label}[/{style}]")
    summary_line = "  ".join(parts) if parts else "[dim]No insights[/dim]"

    # Table
    t = RichTable(show_header=True, header_style="bold cyan", box=None, padding=(0, 1), expand=True)
    t.add_column("", width=3, no_wrap=True)
    t.add_column("Category", width=14, no_wrap=True)
    t.add_column("Insight")
    t.add_column("Action", width=40, style="dim")

    for ins in report.insights:
        icon, style = severity_display.get(ins.severity, (" ? ", "white"))
        t.add_row(f"[{style}]{icon}[/{style}]", f"[bold]{ins.category}[/bold]", ins.message, ins.action)

    panel = Panel(
        t,
        title=f"[bold]TW Advisor[/bold]  {summary_line}",
        subtitle=f"[dim]{report.checks_run} checks · {report.duration_ms}ms[/dim]",
        border_style="magenta",
    )
    console.print(panel)


def main():
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="Token Window — Claude Code token monitor")
    parser.add_argument("-s", "--session", help="Look up session by CCID or UUID prefix")
    parser.add_argument("-l", "--list", action="store_true", help="List recent sessions")
    parser.add_argument("--context", action="store_true", help="Include resume context (with --session)")
    parser.add_argument("--snapshot", action="store_true", help="Print compact capacity snapshot and exit")
    parser.add_argument("--advisor", action="store_true", help="Run advisor and print insights")
    parser.add_argument("--json", action="store_true", help="Output in JSON format (use with --advisor or --snapshot)")
    args = parser.parse_args()

    if args.session:
        _cli_session_lookup(args)
        return
    if args.list:
        _cli_list_sessions(args)
        return
    if args.snapshot:
        _cli_snapshot()
        return
    if args.advisor:
        _cli_advisor(args)
        return

    while True:
        app = ClaudeWatchApp()
        result = app.run()
        if result != ClaudeWatchApp._RESTART_EXIT_CODE:
            break
        # Full process restart so Python re-imports all modules from disk
        os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    main()
