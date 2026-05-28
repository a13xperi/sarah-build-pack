#!/usr/bin/env python3
"""
Token Window — Rich Live version (lightweight fallback).
For the interactive Textual version, run token_watch_tui.py.
"""

import time
from datetime import datetime, timedelta, timezone

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from token_watch_data import (
    _abbrev_model,
    _compute_tool_feed_rows,
    _current_pct,
    _ensure_index,
    _get_session_history,
    _index_building,
    make_drain_panel,
    make_header,
    make_sessions_panel,
    make_tool_stats,
)

console = Console()


def make_session_history_panel():
    sessions = _get_session_history()

    t = Table(show_header=True, header_style="bold blue", box=None, padding=(0, 0), expand=True)
    t.add_column("Time", width=13, no_wrap=True)
    t.add_column("Src", width=10, no_wrap=True)
    t.add_column("Mdl", width=7, no_wrap=True)
    t.add_column("~5h%", width=7, no_wrap=True)
    t.add_column("Out", width=6, no_wrap=True, justify="right")
    t.add_column("Directive", overflow="ellipsis", no_wrap=True, ratio=1)

    if not sessions:
        msg = "[dim]building index...[/dim]" if _index_building else "[dim]no sessions found[/dim]"
        t.add_row(msg, "", "", "", "", "")
        title = "[bold]Session History[/bold]  [dim](indexing...)[/dim]" if _index_building else "[bold]Session History[/bold]"
        return Panel(t, title=title, border_style="blue")

    today = datetime.now(timezone.utc).astimezone().date()
    yesterday = today - timedelta(days=1)
    current_group = None
    shown = 0
    MAX = 25

    for s in sessions:
        if shown >= MAX:
            break
        date = s["date"]
        group = "Today" if date == today else ("Yesterday" if date == yesterday else date.strftime("%b %-d"))

        if group != current_group:
            if current_group is not None:
                t.add_row("", "", "", "", "", "")
            sep = f"── {group} " + "─" * max(0, 34 - len(group))
            t.add_row(f"[dim]{sep}[/dim]", "", "", "", "", "")
            current_group = group

        end_str = s["last_ts"].astimezone().strftime("%H:%M")
        pct_str = s["pct_str"]
        pct_color = "dim"
        if pct_str != "—":
            try:
                v = float(pct_str.strip("+%"))
                pct_color = "red" if v > 10 else ("yellow" if v > 5 else "green")
            except Exception:
                pass
        out_k = s["output_tokens"]
        out_str = f"{out_k/1000:.1f}k" if out_k >= 1000 else str(out_k)
        directive = (s["directive"] or "—")[:32]
        mdl = _abbrev_model(s.get("model", ""))
        mdl_color = "magenta" if mdl == "opus" else ("cyan" if mdl == "sonnet" else "dim")
        time_dur = f"[dim]{end_str} {s['dur_str']:>6}[/dim]"
        src = s.get("source", "?")
        src_color = "yellow" if src == "paperclip" else ("green" if src == "cli" else ("cyan" if "atlas" in src else "dim"))
        t.add_row(
            time_dur, f"[{src_color}]{src}[/{src_color}]",
            f"[{mdl_color}]{mdl}[/{mdl_color}]", f"[{pct_color}]{pct_str}[/{pct_color}]",
            f"[dim]{out_str}[/dim]", directive,
        )
        shown += 1

    total = len(sessions)
    extra = f"  [dim](showing {MAX} of {total})[/dim]" if total > MAX else (
        "  [dim](indexing...)[/dim]" if _index_building else ""
    )
    return Panel(t, title=f"[bold]Session History[/bold]{extra}", border_style="blue")


def make_live_feed(last_n=18):
    rows = _compute_tool_feed_rows(last_n=last_n)

    t = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 1), expand=True)
    # Fixed order: Time, Session, Tool, Directive, Δ5h%
    t.add_column("Time", min_width=8, no_wrap=True)
    t.add_column("Session", min_width=9, no_wrap=True)
    t.add_column("Δ5h%", min_width=6, no_wrap=True)
    t.add_column("Tool", min_width=6, no_wrap=True)
    t.add_column("Directive", overflow="ellipsis", no_wrap=True, ratio=3)

    if not rows:
        t.add_row("[dim]—[/dim]", "", "", "[dim]no events yet[/dim]", "")
    else:
        for r in rows:
            t.add_row(
                f"[dim]{r['ts_str']}[/dim]",
                f"[cyan]{r['session']}[/cyan]",
                f"[{r['delta_style']}]{r['delta_str']}[/{r['delta_style']}]",
                r["tool"],
                f"[dim]{r['directive'][:30]}[/dim]",
            )

    return Panel(t, title="[bold]Tool Call Feed[/bold]  [dim](newest first)[/dim]", border_style="magenta")


def build_layout(five, seven, five_reset_ts, seven_reset_ts):
    layout = Layout()
    layout.split_column(
        Layout(make_header(five, seven, five_reset_ts, seven_reset_ts), size=5),
        Layout(make_sessions_panel(), size=6),
        Layout(make_session_history_panel(), ratio=3),
        Layout(name="feed", ratio=3),
        Layout(make_drain_panel(), ratio=2),
    )
    layout["feed"].split_row(
        Layout(make_live_feed(), ratio=3),
        Layout(make_tool_stats(), ratio=1),
    )
    return layout


def main():
    _ensure_index()
    console.print("[bold bright_blue]Token Window[/bold bright_blue] starting... (Ctrl+C to exit)\n")
    with Live(console=console, refresh_per_second=2, screen=True) as live:
        while True:
            five, seven, five_reset_ts, seven_reset_ts = _current_pct()
            live.update(build_layout(five, seven, five_reset_ts, seven_reset_ts))
            time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[dim]Token Window exited.[/dim]")
