"""DispatchGrid — 5-terminal visual grid widget for the token-watch Dispatch tab.

Renders a wrapping grid of cards (one per active peer session) showing:
terminal id, directive, project, account, 5h session % + mini activity bar,
and a status glyph (working / live / idle / dead).

Data source:
    * Supabase ``session_locks`` via ``token_watch_data._get_peer_sessions``
    * Live directives from ``/tmp/claude-directive-{pid}`` (fresher than
      the ``task_name`` column, which only updates on ``/dispatch``).

Intentionally standalone: the widget imports ``_get_peer_sessions`` lazily
inside ``update_content`` so ``token_watch_tui`` only needs a 1-line
``yield DispatchGrid(...)`` edit inside ``DispatchView.compose``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.columns import Columns
from rich.console import Group
from rich.panel import Panel
from rich.text import Text
from textual.widgets import Static


# Card inner text width — kept narrow so 4-5 cards fit on a ~130-col terminal.
_CARD_INNER_WIDTH = 22
_BAR_WIDTH = 12


def _mini_bar(pct: float, width: int = _BAR_WIDTH) -> Text:
    """Coloured block-character activity bar showing a 0-100 percentage."""
    try:
        f = max(0.0, min(100.0, float(pct)))
    except (TypeError, ValueError):
        f = 0.0
    filled = int(round(f * width / 100))
    filled = max(0, min(width, filled))
    if f < 50:
        color = "green"
    elif f < 75:
        color = "yellow"
    elif f < 90:
        color = "orange3"
    else:
        color = "red"
    bar = Text()
    bar.append("█" * filled, style=color)
    bar.append("░" * (width - filled), style="grey37")
    return bar


def _heartbeat_age_seconds(heartbeat_at: Optional[str]) -> float:
    """Seconds since a heartbeat ISO timestamp. Returns a large sentinel on error."""
    if not heartbeat_at:
        return 9_999_999.0
    try:
        ts = datetime.fromisoformat(str(heartbeat_at).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return 9_999_999.0


def _status_label(age_secs: float, files_touched: Optional[List[str]]) -> Tuple[str, str, str]:
    """Return ``(glyph, label, color)`` for a session's liveness."""
    if age_secs > 300:
        return "●", "dead", "red"
    if age_secs > 120:
        return "◐", "idle", "yellow"
    if files_touched:
        return "◉", "work", "green"
    return "○", "live", "cyan"


def _short_session_id(sid: str) -> str:
    """``cc-12345`` → ``cc12345`` so the card title stays compact."""
    if not sid:
        return "?"
    return sid.replace("cc-", "cc")


def _read_directive_for_pid(sid: str) -> str:
    """Return the live directive written by the owning session, or ""."""
    if not sid or not sid.startswith("cc-"):
        return ""
    pid = sid.split("cc-", 1)[1]
    if not pid:
        return ""
    path = Path(f"/tmp/claude-directive-{pid}")
    try:
        return path.read_text().strip()
    except Exception:
        return ""


def _truncate(s: Optional[str], width: int) -> str:
    """Plain truncate with a single-char ellipsis."""
    if not s:
        return ""
    if len(s) <= width:
        return s
    return s[: max(0, width - 1)] + "…"


_ACCOUNT_COLORS = {"A": "cyan", "B": "magenta", "C": "yellow"}


def _account_color(account: Optional[str]) -> str:
    return _ACCOUNT_COLORS.get((account or "").upper(), "grey50")


def _peer_five_pct(peer: Dict[str, Any]) -> float:
    raw = peer.get("five_pct")
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _build_card(peer: Dict[str, Any]) -> Panel:
    """Render one session card."""
    sid = str(peer.get("session_id") or "?")
    label_short = _short_session_id(sid)
    account = (peer.get("account") or "").upper() or "?"
    acct_color = _account_color(account)
    repo = str(peer.get("repo") or "?")
    five_pct = _peer_five_pct(peer)

    # Directive: prefer live /tmp file (updated every command) over the
    # Supabase snapshot which only refreshes on claim.
    live_dir = _read_directive_for_pid(sid)
    directive = live_dir or str(peer.get("task_name") or "—")

    age_secs = _heartbeat_age_seconds(peer.get("heartbeat_at"))
    glyph, status_text, status_color = _status_label(age_secs, peer.get("files_touched"))

    # Title: "◉ cc1234  B  work"
    title = Text()
    title.append(f"{glyph} ", style=status_color)
    title.append(label_short, style="bold white")
    title.append("  ")
    title.append(account, style=f"bold {acct_color}")
    title.append("  ")
    title.append(status_text, style=status_color)

    dir_line = Text(_truncate(directive, _CARD_INNER_WIDTH), style="bold")
    proj_line = Text(_truncate(repo, _CARD_INNER_WIDTH), style="cyan")

    bar_line = Text()
    bar_line.append(_mini_bar(five_pct))
    bar_line.append(f" {int(five_pct):>3}%", style="dim")

    body = Group(dir_line, proj_line, bar_line)
    border = status_color if status_text != "dead" else "grey35"

    return Panel(
        body,
        title=title,
        title_align="left",
        border_style=border,
        padding=(0, 1),
        expand=False,
    )


def _sort_peers(peers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Working sessions first, then live, idle, dead; each sorted by 5h % desc."""

    def key(peer: Dict[str, Any]):
        age = _heartbeat_age_seconds(peer.get("heartbeat_at"))
        _, label, _ = _status_label(age, peer.get("files_touched"))
        rank = {"work": 0, "live": 1, "idle": 2, "dead": 3}.get(label, 4)
        return (rank, -_peer_five_pct(peer))

    return sorted(peers, key=key)


def _partition_live_dead(
    peers: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int]:
    """Split peers into ``(live_or_idle, dead_count)``.

    "Dead" is heartbeat older than 5 minutes — almost always a stale
    openclaw/heartbeat ghost, not a real claude session. Keeping them in
    the grid drowns out the actual terminals Alex wants to see.
    """
    live: List[Dict[str, Any]] = []
    dead = 0
    for peer in peers:
        age = _heartbeat_age_seconds(peer.get("heartbeat_at"))
        if age > 300:
            dead += 1
        else:
            live.append(peer)
    return live, dead


def render_dispatch_grid(peers: List[Dict[str, Any]]) -> Group:
    """Top-level render used by tests and the widget."""
    live_peers, dead_count = _partition_live_dead(peers)

    header = Text()
    header.append("5-Terminal Grid  ", style="bold")
    header.append(f"{len(live_peers)} active", style="dim")
    if dead_count:
        header.append(f"  +{dead_count} stale", style="grey37")
    header.append("   ")
    header.append("◉work ○live ◐idle", style="dim italic")

    if not live_peers:
        empty = Text("  (no active peer sessions)", style="dim italic")
        return Group(header, Text(""), empty)

    cards = [_build_card(p) for p in _sort_peers(live_peers)]
    cols = Columns(cards, equal=True, expand=True, padding=(0, 1))
    return Group(header, Text(""), cols)


class DispatchGrid(Static):
    """Textual widget that renders the 5-terminal grid for the Dispatch tab.

    Mount with ``yield DispatchGrid(id="dispatch-grid")`` inside a view's
    ``compose`` method and call ``update_content()`` on refresh.
    """

    DEFAULT_CSS = """
    DispatchGrid {
        height: auto;
        padding: 0 1;
        margin-bottom: 1;
    }
    """

    def on_mount(self) -> None:  # pragma: no cover — Textual lifecycle
        self.update_content()

    def update_content(self) -> None:
        try:
            from token_watch_data import _get_peer_sessions  # lazy
        except Exception as exc:  # pragma: no cover — import guard
            self.update(Text(f"[DispatchGrid] import error: {exc}", style="red"))
            return

        try:
            peers = _get_peer_sessions() or []
        except Exception as exc:
            self.update(Text(f"[DispatchGrid] load error: {exc}", style="red"))
            return

        try:
            self.update(render_dispatch_grid(peers))
        except Exception as exc:
            self.update(Text(f"[DispatchGrid] render error: {exc}", style="red"))


__all__ = ["DispatchGrid", "render_dispatch_grid"]
