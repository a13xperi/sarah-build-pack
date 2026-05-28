"""
Token Window Advisor Agent — synthesizes all data sources into ranked actionable insights.

Usage:
    from token_watch_advisor import run_advisor, get_top_insights

    report = run_advisor()
    for insight in report.insights:
        print(f"[{insight.severity}] {insight.category}: {insight.message}")

    # For session briefings:
    top = get_top_insights(max_count=3)
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_log = logging.getLogger("token_watch.advisor")

# ── Data structures ─────────────────────────────────────────────────────────

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2, "positive": 3}


@dataclass
class Insight:
    category: str
    severity: str
    title: str
    message: str
    action: str
    source: str
    data: dict = field(default_factory=dict)
    auto_fixable: bool = False
    remediation_key: str = ""

    def sort_key(self):
        return (SEVERITY_ORDER.get(self.severity, 9), self.category, self.title)


@dataclass
class AdvisorReport:
    insights: List[Insight]
    timestamp: str
    duration_ms: int
    checks_run: int
    summary: Dict[str, int]


# ── Check registry ──────────────────────────────────────────────────────────

_CHECKS: List[Callable] = []


def advisor_check(name: str, sources: List[str]):
    """Decorator to register a heuristic check function."""
    def decorator(fn: Callable):
        fn._check_name = name  # type: ignore[attr-defined]
        fn._check_sources = sources  # type: ignore[attr-defined]
        _CHECKS.append(fn)
        return fn
    return decorator


# ── Helpers ─────────────────────────────────────────────────────────────────

def _sf(val, default=0.0) -> float:
    """Safe float conversion."""
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _read_context_md() -> str:
    """Read ~/CONTEXT.md, return text or empty string."""
    try:
        return (Path.home() / "CONTEXT.md").read_text()
    except Exception:
        return ""


def _read_directives_md() -> str:
    """Read ~/DIRECTIVES.md, return text or empty string."""
    try:
        return (Path.home() / "DIRECTIVES.md").read_text()
    except Exception:
        return ""


def _reset_seconds(reset_ts: str) -> Optional[int]:
    """Parse an ISO reset timestamp and return seconds until reset, or None."""
    if not reset_ts:
        return None
    try:
        reset = datetime.fromisoformat(reset_ts.replace("Z", "+00:00"))
        diff = int((reset - datetime.now(timezone.utc)).total_seconds())
        return diff
    except Exception:
        return None


# ── Context builder ─────────────────────────────────────────────────────────

def _build_context() -> Dict[str, Any]:
    """Pre-fetch all data sources. Supabase calls run in parallel."""
    from token_watch_data import (
        _current_pct,
        _active_sessions,
        _get_peer_sessions,
        _load_index,
        _get_build_ledger,
        _get_project_tasks,
        _get_session_tasks,
        _get_window_scores,
        _get_current_cycle_id,
        _get_cycle_items,
        _get_system_health,
        _get_wire_messages,
        get_account_capacity_display,
        _get_test_queue,
        _get_utilization_analytics,
    )

    ctx: Dict[str, Any] = {}

    # Fast local reads (inline)
    ctx["rate_limits"] = _current_pct()
    ctx["active_sessions"] = _active_sessions()
    ctx["session_index"] = _load_index()
    ctx["window_scores"] = _get_window_scores(limit=20)
    ctx["current_cycle_id"] = _get_current_cycle_id()
    ctx["context_md"] = _read_context_md()
    ctx["directives_md"] = _read_directives_md()

    # Supabase calls in parallel
    supabase_tasks = {
        "capacities": lambda: get_account_capacity_display(),
        "peer_sessions": lambda: _get_peer_sessions(),
        "build_ledger": lambda: _get_build_ledger(days=7, limit=200),
        "project_tasks": lambda: _get_project_tasks(),
        "session_tasks": lambda: _get_session_tasks(today_only=True),
        "wire_messages": lambda: _get_wire_messages(limit=50),
        "system_health": lambda: _get_system_health(),
        "test_queue": lambda: _get_test_queue(),
        "utilization_analytics": lambda: _get_utilization_analytics("24h"),
    }

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn): key for key, fn in supabase_tasks.items()}
        for future in as_completed(futures):
            key = futures[future]
            try:
                ctx[key] = future.result()
            except Exception as e:
                _log.warning("Advisor: failed to fetch %s: %s", key, e)
                # Provide safe defaults
                if key == "build_ledger":
                    ctx[key] = {"items": [], "by_company": {}, "stats": {"total": 0, "untested": 0, "decisions": 0, "sessions": 0, "projects": 0}}
                elif key == "wire_messages":
                    ctx[key] = {"messages": [], "total": 0, "unread": 0, "sessions": 0}
                elif key == "system_health":
                    ctx[key] = {"claude_sessions": [], "infrastructure": [], "totals": {"cpu": 0, "mem_mb": 0, "mem_pct": 0}, "alerts": []}
                elif key == "test_queue":
                    ctx[key] = []
                elif key == "utilization_analytics":
                    ctx[key] = {"waste": {"waste_pct": 0}, "efficiency": {}, "fleet": {"utilization_pct": 0}, "suggestions": []}
                else:
                    ctx[key] = []

    # Cycle items (needs cycle_id)
    cycle_id = ctx.get("current_cycle_id")
    if cycle_id:
        try:
            ctx["cycle_items"] = _get_cycle_items(cycle_id, all_windows=False)
        except Exception:
            ctx["cycle_items"] = []
    else:
        ctx["cycle_items"] = []

    return ctx


# ── Heuristic checks ───────────────────────────────────────────────────────

# --- Capacity checks ---

@advisor_check("five_hour_critical", ["rate_limits"])
def check_five_hour_critical(ctx: Dict) -> List[Insight]:
    five = _sf(ctx["rate_limits"][0])
    if five == 0:
        return []
    if five > 90:
        return [Insight(
            category="CAPACITY", severity="critical",
            title="5h window near cap",
            message=f"5-hour window at {five:.0f}%.",
            action="Switch accounts or pause sessions.",
            source="five_hour_critical",
            data={"five_pct": five},
        )]
    if five > 75:
        return [Insight(
            category="CAPACITY", severity="warning",
            title="5h window elevated",
            message=f"5-hour window at {five:.0f}%.",
            action="Monitor burn rate.",
            source="five_hour_critical",
            data={"five_pct": five},
        )]
    return []


@advisor_check("seven_day_critical", ["rate_limits"])
def check_seven_day_critical(ctx: Dict) -> List[Insight]:
    seven = _sf(ctx["rate_limits"][1])
    if seven == 0:
        return []
    if seven > 85:
        return [Insight(
            category="CAPACITY", severity="critical",
            title="Weekly limit near cap",
            message=f"7-day limit at {seven:.0f}%.",
            action="Switch to another account.",
            source="seven_day_critical",
            data={"seven_pct": seven},
        )]
    if seven > 70:
        return [Insight(
            category="CAPACITY", severity="warning",
            title="Weekly limit elevated",
            message=f"7-day limit at {seven:.0f}%.",
            action="Consider switching accounts.",
            source="seven_day_critical",
            data={"seven_pct": seven},
        )]
    return []


@advisor_check("account_opportunity", ["capacities"])
def check_account_opportunity(ctx: Dict) -> List[Insight]:
    insights = []
    for acct in ctx.get("capacities", []):
        if acct.get("is_active"):
            continue
        five = _sf(acct.get("five_pct", "—"))
        if five == 0 and str(acct.get("five_pct")) == "—":
            continue  # no data
        if five < 20:
            insights.append(Insight(
                category="OPPORTUNITY", severity="info",
                title=f"Account {acct['label']} available",
                message=f"Account {acct['label']} ({acct.get('name', '?')}) at {five:.0f}% 5h usage.",
                action=f"Switch to {acct['label']} for fresh capacity.",
                source="account_opportunity",
                data={"label": acct["label"], "five_pct": five},
            ))
    return insights


@advisor_check("tokens_expiring", ["rate_limits"])
def check_tokens_expiring(ctx: Dict) -> List[Insight]:
    five = _sf(ctx["rate_limits"][0])
    five_reset_ts = ctx["rate_limits"][2]
    secs = _reset_seconds(five_reset_ts)
    if secs is None or secs > 1800 or secs < 0:
        return []
    remaining = 100 - five
    if remaining < 15:
        return []  # not much to lose
    minutes = secs // 60
    return [Insight(
        category="CAPACITY", severity="warning",
        title="Capacity expiring soon",
        message=f"~{remaining:.0f}% unused capacity expires in {minutes}m.",
        action="Use remaining tokens or lose them at reset.",
        source="tokens_expiring",
        data={"remaining_pct": remaining, "reset_minutes": minutes},
    )]


@advisor_check("burnout_hours", ["session_index"])
def check_burnout_hours(ctx: Dict) -> List[Insight]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    total_secs = 0
    index = ctx.get("session_index", {})
    for sid, entry in index.items():
        try:
            last_ts = entry.get("last_ts", "")
            if not last_ts:
                continue
            ts = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            if ts > cutoff:
                total_secs += entry.get("duration_sec", 0)
        except Exception:
            continue
    total_hours = total_secs / 3600
    if total_hours > 16:
        return [Insight(
            category="BURNOUT", severity="warning",
            title="Heavy usage",
            message=f"{total_hours:.0f}h of sessions in last 24h.",
            action="Consider taking a break.",
            source="burnout_hours",
            data={"hours_24h": round(total_hours, 1)},
        )]
    if total_hours > 10:
        return [Insight(
            category="VELOCITY", severity="info",
            title="Active day",
            message=f"{total_hours:.0f}h of sessions in last 24h.",
            action="",
            source="burnout_hours",
            data={"hours_24h": round(total_hours, 1)},
        )]
    return []


# --- Test & Quality checks ---

@advisor_check("test_debt", ["build_ledger"])
def check_test_debt(ctx: Dict) -> List[Insight]:
    insights = []
    ledger = ctx.get("build_ledger", {})
    total_untested = ledger.get("stats", {}).get("untested", 0)

    # Per-project breakdown
    for company, projects in ledger.get("by_company", {}).items():
        for project, items in projects.items():
            untested = sum(1 for i in items if i.get("test_status") == "untested")
            if untested > 10:
                insights.append(Insight(
                    category="TEST_DEBT", severity="critical",
                    title=f"Test debt: {project}",
                    message=f"{untested} untested items in {project} ({company}).",
                    action="Run test verification.",
                    source="test_debt",
                    data={"project": project, "untested": untested},
                ))
            elif untested > 5:
                insights.append(Insight(
                    category="TEST_DEBT", severity="warning",
                    title=f"Test debt: {project}",
                    message=f"{untested} untested items in {project} ({company}).",
                    action="Run test verification.",
                    source="test_debt",
                    data={"project": project, "untested": untested},
                ))

    # Overall check if no per-project hit but total is high
    if not insights and total_untested > 15:
        insights.append(Insight(
            category="TEST_DEBT", severity="critical",
            title="High test debt overall",
            message=f"{total_untested} untested items across all projects.",
            action="Run test verification sweep.",
            source="test_debt",
            data={"total_untested": total_untested},
        ))
    return insights


@advisor_check("shipping_velocity", ["build_ledger"])
def check_shipping_velocity(ctx: Dict) -> List[Insight]:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    items = ctx.get("build_ledger", {}).get("items", [])
    today_count = sum(1 for i in items if (i.get("created_at") or "").startswith(today_str))

    if today_count > 5:
        return [Insight(
            category="VELOCITY", severity="positive",
            title="Strong shipping day",
            message=f"Shipped {today_count} items today.",
            action="",
            source="shipping_velocity",
            data={"today_count": today_count},
        )]
    hour = datetime.now().hour
    if today_count == 0 and hour > 14:
        return [Insight(
            category="VELOCITY", severity="info",
            title="Nothing shipped yet",
            message="No items shipped today (afternoon).",
            action="Focus on completing and committing work.",
            source="shipping_velocity",
            data={"today_count": 0, "hour": hour},
        )]
    return []


@advisor_check("decisions_captured", ["build_ledger"])
def check_decisions_captured(ctx: Dict) -> List[Insight]:
    decisions = ctx.get("build_ledger", {}).get("stats", {}).get("decisions", 0)
    if decisions > 5:
        return [Insight(
            category="QUALITY", severity="positive",
            title="Good decision capture",
            message=f"{decisions} decisions logged this week.",
            action="",
            source="decisions_captured",
            data={"decisions": decisions},
        )]
    if decisions == 0:
        return [Insight(
            category="QUALITY", severity="info",
            title="No decisions logged",
            message="No architectural decisions logged this week.",
            action="Use [DECISION] prefix on commits for non-obvious choices.",
            source="decisions_captured",
        )]
    return []


# --- Task Pipeline checks ---

@advisor_check("stale_tasks", ["project_tasks"])
def check_stale_tasks(ctx: Dict) -> List[Insight]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=3)
    tasks = ctx.get("project_tasks", [])
    stale = []
    for t in tasks:
        if t.get("status") != "ready":
            continue
        created = t.get("created_at", "")
        try:
            ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if ts < cutoff:
                stale.append(t)
        except Exception:
            continue
    if len(stale) > 3:
        names = ", ".join(t.get("task_name", "?")[:30] for t in stale[:3])
        return [Insight(
            category="STALE_TASKS", severity="warning",
            title=f"{len(stale)} stale tasks",
            message=f"{len(stale)} tasks in Ready state for >3 days: {names}...",
            action="Review and prioritize or archive.",
            source="stale_tasks",
            data={"count": len(stale)},
            auto_fixable=True,
            remediation_key="task_archive",
        )]
    return []


@advisor_check("empty_pipeline", ["project_tasks"])
def check_empty_pipeline(ctx: Dict) -> List[Insight]:
    tasks = ctx.get("project_tasks", [])
    ready = [t for t in tasks if t.get("status") == "ready"]
    if len(ready) == 0:
        return [Insight(
            category="PIPELINE", severity="info",
            title="Empty pipeline",
            message="No tasks in Ready state to claim.",
            action="Plan next cycle and add tasks.",
            source="empty_pipeline",
        )]
    return []


@advisor_check("blocked_tasks", ["project_tasks"])
def check_blocked_tasks(ctx: Dict) -> List[Insight]:
    tasks = ctx.get("project_tasks", [])
    blocked = [t for t in tasks if t.get("status") == "blocked"]
    if blocked:
        first_name = blocked[0].get("task_name", "?")[:40]
        return [Insight(
            category="PIPELINE", severity="warning",
            title=f"{len(blocked)} blocked task(s)",
            message=f"{len(blocked)} tasks are blocked. First: {first_name}.",
            action="Unblock or reassign.",
            source="blocked_tasks",
            data={"count": len(blocked), "first": first_name},
        )]
    return []


# --- Session Health checks ---

@advisor_check("stuck_sessions", ["active_sessions"])
def check_stuck_sessions(ctx: Dict) -> List[Insight]:
    from token_watch_data import _etime_to_secs
    insights = []
    for session in ctx.get("active_sessions", []):
        pid, etime, directive, delta, source = session
        secs = _etime_to_secs(etime)
        if secs is None:
            continue
        if secs > 10800 and delta in ("?", "new"):  # 3h+, no progress
            hours = secs // 3600
            return [Insight(
                category="SESSION_HEALTH", severity="warning",
                title=f"Session cc-{pid} possibly stuck",
                message=f"Session cc-{pid} active {hours}h with no measurable progress.",
                action="Check if stuck or kill.",
                source="stuck_sessions",
                data={"pid": pid, "age_secs": secs},
                auto_fixable=True,
                remediation_key="zombie_kill",
            )]
    return insights


@advisor_check("file_conflicts", ["peer_sessions"])
def check_file_conflicts(ctx: Dict) -> List[Insight]:
    peers = ctx.get("peer_sessions", [])
    active_peers = [p for p in peers if p.get("status") == "active"]
    repo_sessions: Dict[str, List[str]] = {}
    for p in active_peers:
        repo = p.get("repo", "")
        if repo:
            repo_sessions.setdefault(repo, []).append(p.get("session_id", "?"))
    insights = []
    for repo, sessions in repo_sessions.items():
        if len(sessions) >= 2:
            insights.append(Insight(
                category="COORDINATION", severity="warning",
                title=f"Concurrent sessions in {repo}",
                message=f"{len(sessions)} active sessions in {repo}: {', '.join(sessions[:3])}.",
                action="Check file conflicts via coordination dashboard.",
                source="file_conflicts",
                data={"repo": repo, "sessions": sessions},
            ))
    return insights


@advisor_check("system_health", ["system_health"])
def check_system_health(ctx: Dict) -> List[Insight]:
    health = ctx.get("system_health", {})
    insights = []

    # Memory check
    mem_pct = health.get("totals", {}).get("mem_pct", 0)
    if mem_pct > 60:
        insights.append(Insight(
            category="SYSTEM", severity="warning",
            title="High memory usage",
            message=f"Claude ecosystem using {mem_pct:.0f}% of system memory.",
            action="Close idle sessions.",
            source="system_health",
            data={"mem_pct": mem_pct},
        ))

    # Propagate runaway alerts
    for alert_msg in health.get("alerts", []):
        if "runaway" in alert_msg.lower():
            insights.append(Insight(
                category="SYSTEM", severity="critical",
                title="Runaway session detected",
                message=alert_msg,
                action="Kill the runaway process.",
                source="system_health",
            ))
        elif "CPU" in alert_msg and "%" in alert_msg:
            insights.append(Insight(
                category="SYSTEM", severity="warning",
                title="High CPU process",
                message=alert_msg,
                action="Investigate.",
                source="system_health",
            ))

    return insights


# --- Velocity & Cycles checks ---

@advisor_check("cycle_utilization", ["window_scores"])
def check_cycle_utilization(ctx: Dict) -> List[Insight]:
    scores = ctx.get("window_scores", [])
    if len(scores) < 2:
        return []
    recent = scores[:3]
    avg_burn = sum(s.get("burn_pct", 0) for s in recent) / len(recent)
    if avg_burn < 50 and avg_burn > 0:
        return [Insight(
            category="VELOCITY", severity="info",
            title="Under-utilizing capacity",
            message=f"Last {len(recent)} cycles averaged {avg_burn:.0f}% burn.",
            action="Use more of available capacity or switch to lower-tier model.",
            source="cycle_utilization",
            data={"avg_burn": round(avg_burn, 1)},
        )]
    if avg_burn > 85:
        return [Insight(
            category="VELOCITY", severity="positive",
            title="Strong utilization",
            message=f"Averaging {avg_burn:.0f}% burn across last {len(recent)} cycles.",
            action="",
            source="cycle_utilization",
            data={"avg_burn": round(avg_burn, 1)},
        )]
    return []


@advisor_check("cycle_progress", ["cycle_items", "rate_limits"])
def check_cycle_progress(ctx: Dict) -> List[Insight]:
    items = ctx.get("cycle_items", [])
    if not items:
        return []
    total = len(items)
    done = sum(1 for i in items if (i.get("status") or "").lower() in ("done", "built", "shipped", "tested"))
    five_pct = _sf(ctx["rate_limits"][0])

    if total > 0 and five_pct > 70 and done / total < 0.25:
        return [Insight(
            category="CYCLE", severity="warning",
            title="Cycle falling behind",
            message=f"Window {five_pct:.0f}% through but only {done}/{total} planned items done.",
            action="Focus on completing planned items.",
            source="cycle_progress",
            data={"done": done, "total": total, "five_pct": five_pct},
        )]
    return []


@advisor_check("unread_wire", ["wire_messages"])
def check_unread_wire(ctx: Dict) -> List[Insight]:
    wire = ctx.get("wire_messages", {})
    unread = wire.get("unread", 0)
    if unread > 3:
        return [Insight(
            category="COORDINATION", severity="info",
            title=f"{unread} unread Wire messages",
            message=f"{unread} unread inter-session messages.",
            action="Check Wire tab (w).",
            source="unread_wire",
            data={"unread": unread},
            auto_fixable=True,
            remediation_key="wire_cleanup",
        )]
    return []


# --- Context checks ---

@advisor_check("context_blockers", ["context_md"])
def check_context_blockers(ctx: Dict) -> List[Insight]:
    text = ctx.get("context_md", "")
    if not text:
        return []

    insights = []
    keywords = ["down", "blocked", "urgent", "broken", "failing"]
    in_urgent_section = False

    for line in text.splitlines():
        stripped = line.strip().lower()
        # Track if we're in an urgent/blockers section
        if any(h in stripped for h in ("## urgent", "## blocker", "## critical")):
            in_urgent_section = True
            continue
        if stripped.startswith("## ") and in_urgent_section:
            in_urgent_section = False
            continue

        if in_urgent_section and stripped and not stripped.startswith("#"):
            insights.append(Insight(
                category="CONTEXT", severity="warning",
                title="CONTEXT.md blocker",
                message=line.strip()[:120],
                action="Address this blocker.",
                source="context_blockers",
            ))
            if len(insights) >= 3:
                break

    # Fallback: if no section-based matches, scan for keywords in any line
    if not insights:
        for line in text.splitlines():
            stripped = line.strip().lower()
            if any(f" {kw}" in f" {stripped} " for kw in keywords):
                # Skip section headers and empty lines
                if stripped.startswith("#") or not stripped:
                    continue
                insights.append(Insight(
                    category="CONTEXT", severity="info",
                    title="CONTEXT.md flag",
                    message=line.strip()[:120],
                    action="Review.",
                    source="context_blockers",
                ))
                if len(insights) >= 2:
                    break

    return insights


# --- Directive Alignment checks ---

def _parse_directives(text: str) -> Dict[str, List[str]]:
    """Parse DIRECTIVES.md into {section: [directive_lines]}."""
    sections: Dict[str, List[str]] = {}
    current_section = "global"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            current_section = stripped[3:].strip().lower()
            continue
        if stripped.startswith("- ") and current_section:
            sections.setdefault(current_section, []).append(stripped[2:])
    return sections


# Map directive section names to keywords that match repos/projects
_DIRECTIVE_PROJECT_MAP = {
    "delphi os": ["atlas", "delphi"],
    "kaa": ["kaa", "openclaw", "landscape"],
    "paperclip": ["paperclip"],
    "token watch": ["token-watch", "token_watch", "tokenwatch"],
}


@advisor_check("directive_alignment", ["directives_md", "peer_sessions", "build_ledger"])
def check_directive_alignment(ctx: Dict) -> List[Insight]:
    text = ctx.get("directives_md", "")
    if not text:
        return []

    sections = _parse_directives(text)
    peers = ctx.get("peer_sessions", [])
    ledger = ctx.get("build_ledger", {})

    active_peers = [p for p in peers if p.get("status") == "active"]
    active_repos = [p.get("repo", "").lower() for p in active_peers]
    active_work = [p.get("work_unit", "").lower() for p in active_peers]
    active_text = " ".join(active_repos + active_work)

    # Collect recent build projects
    build_projects = set()
    for item in ledger.get("items", []):
        proj = (item.get("project") or "").lower()
        if proj:
            build_projects.add(proj)

    insights: List[Insight] = []

    # Check each directive section for alignment
    for section, directives in sections.items():
        if section == "global":
            continue

        # Find keywords for this section
        keywords = _DIRECTIVE_PROJECT_MAP.get(section, [section.split()[0].lower()])

        has_active_work = any(kw in active_text for kw in keywords)
        has_recent_builds = any(kw in bp for kw in keywords for bp in build_projects)

        # Check for "non-negotiable" or "must" directives with no work
        for directive in directives:
            dl = directive.lower()
            is_critical = any(w in dl for w in ["non-negotiable", "must", "critical", "required"])
            if is_critical and not has_active_work and not has_recent_builds:
                insights.append(Insight(
                    category="ALIGNMENT", severity="warning",
                    title=f"Directive gap: {section.title()}",
                    message=f'"{directive[:80]}" — no active sessions or recent builds for {section.title()}.',
                    action=f"Start work on {section.title()} or review directive.",
                    source="directive_alignment",
                    data={"section": section, "directive": directive[:120]},
                ))

    # Focus distribution: if directives have a clear priority but work is elsewhere
    # Count active sessions per directive area
    area_session_count: Dict[str, int] = {}
    for section in sections:
        if section == "global":
            continue
        keywords = _DIRECTIVE_PROJECT_MAP.get(section, [section.split()[0].lower()])
        count = sum(1 for repo in active_repos if any(kw in repo for kw in keywords))
        area_session_count[section] = count

    total_active = len(active_peers)
    if total_active >= 3:
        # If one area has most sessions but directives emphasize another
        dominant_area = max(area_session_count, key=area_session_count.get, default=None) if area_session_count else None
        if dominant_area and area_session_count.get(dominant_area, 0) >= total_active * 0.7:
            # Check if other areas have unmet critical directives
            for section, directives in sections.items():
                if section == "global" or section == dominant_area:
                    continue
                has_critical = any(
                    any(w in d.lower() for w in ["non-negotiable", "must", "priority", "flagship"])
                    for d in directives
                )
                if has_critical and area_session_count.get(section, 0) == 0:
                    insights.append(Insight(
                        category="ALIGNMENT", severity="info",
                        title=f"Focus skew: {dominant_area.title()} dominant",
                        message=f"{area_session_count[dominant_area]}/{total_active} sessions on {dominant_area.title()}, none on {section.title()} (has priority directives).",
                        action=f"Consider allocating a session to {section.title()}.",
                        source="directive_alignment",
                        data={"dominant": dominant_area, "neglected": section, "total_active": total_active},
                    ))

    return insights


# --- Pipeline checks ---

@advisor_check("unclaimed_continuations", ["project_tasks"])
def check_unclaimed_continuations(ctx: Dict) -> List[Insight]:
    """Flag next-session prompts sitting unclaimed in the dispatch queue."""
    tasks = ctx.get("project_tasks", [])
    continuations = [
        t for t in tasks
        if t.get("status") == "ready"
        and t.get("source") in ("close-session", "expired-session")
        and t.get("priority") in ("high", "critical")
    ]

    if not continuations:
        return []

    now = datetime.now(timezone.utc)
    old_continuations = []
    for c in continuations:
        created = c.get("created_at", "")
        try:
            ct = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age_hours = (now - ct).total_seconds() / 3600
            if age_hours > 2:
                old_continuations.append((c, age_hours))
        except Exception:
            old_continuations.append((c, 99))

    insights: List[Insight] = []

    if len(continuations) >= 3:
        names = ", ".join(c.get("task_name", "?")[:30] for c in continuations[:3])
        insights.append(Insight(
            category="PIPELINE", severity="warning",
            title=f"{len(continuations)} unclaimed continuations",
            message=f"Session continuations sitting unclaimed: {names}.",
            action="Run /dispatch to pick one up, or archive stale ones.",
            source="unclaimed_continuations",
            data={"count": len(continuations)},
        ))
    elif old_continuations:
        c, hours = old_continuations[0]
        insights.append(Insight(
            category="PIPELINE", severity="info",
            title="Continuation aging",
            message=f'"{c.get("task_name", "?")[:40]}" unclaimed for {hours:.0f}h.',
            action="Claim via /dispatch or review if still relevant.",
            source="unclaimed_continuations",
            data={"task_name": c.get("task_name"), "age_hours": round(hours, 1)},
        ))

    return insights


# --- Test Queue checks ---

@advisor_check("test_queue_backlog", ["test_queue"])
def check_test_queue_backlog(ctx: Dict) -> List[Insight]:
    queue = ctx.get("test_queue", [])
    pending = [item for item in queue if item.get("status") == "pending"]
    count = len(pending)
    if count > 50:
        return [Insight(
            category="QA", severity="critical",
            title="QA bottleneck",
            message=f"{count} pending test items — QA is bottlenecked.",
            action="Run /push-to-test or triage in Test tab (x).",
            source="test_queue_backlog",
            data={"pending": count},
        )]
    if count > 20:
        return [Insight(
            category="QA", severity="warning",
            title="QA backlog growing",
            message=f"{count} pending test items — QA backlog growing.",
            action="Run /push-to-test or triage in Test tab (x).",
            source="test_queue_backlog",
            data={"pending": count},
        )]
    return []


# --- Stale Sessions checks ---

@advisor_check("stale_sessions_auto_cleanup", ["peer_sessions"])
def check_stale_sessions_auto_cleanup(ctx: Dict) -> List[Insight]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=2)
    insights = []
    for peer in ctx.get("peer_sessions", []):
        hb = peer.get("heartbeat_at", "")
        if not hb:
            continue
        try:
            hb_dt = datetime.fromisoformat(hb.replace("Z", "+00:00"))
        except Exception:
            continue
        if hb_dt < cutoff:
            age_hours = (now - hb_dt).total_seconds() / 3600
            sid = peer.get("session_id", "?")
            insights.append(Insight(
                category="SESSION_HEALTH", severity="warning",
                title=f"Stale session {sid[:12]}",
                message=f"Session {sid} has stale heartbeat ({age_hours:.0f}h ago) — likely dead.",
                action="Auto-cleanup: mark as done.",
                source="stale_sessions_auto_cleanup",
                data={"session_id": sid, "age_hours": round(age_hours, 1)},
                auto_fixable=True,
                remediation_key=f"cleanup_session:{sid}",
            ))
    return insights


# --- Capacity Waste checks ---

@advisor_check("capacity_waste", ["utilization_analytics"])
def check_capacity_waste(ctx: Dict) -> List[Insight]:
    analytics = ctx.get("utilization_analytics", {})
    waste = analytics.get("waste", {})
    fleet = analytics.get("fleet", {})
    suggestions = analytics.get("suggestions", [])

    insights = []
    waste_pct = _sf(waste.get("waste_pct", 0))
    utilization_pct = _sf(fleet.get("utilization_pct", 0))

    if waste_pct > 30:
        wasted_hours = waste.get("total_wasted_hours", 0)
        reason = f"{wasted_hours:.0f}h wasted" if wasted_hours else "idle gaps between sessions"
        insights.append(Insight(
            category="EFFICIENCY", severity="warning",
            title="High capacity waste",
            message=f"{waste_pct:.0f}% capacity wasted in last 24h — {reason}.",
            action=suggestions[0]["message"] if suggestions else "Consolidate sessions and reduce idle gaps.",
            source="capacity_waste",
            data={"waste_pct": waste_pct, "wasted_hours": wasted_hours},
        ))

    if utilization_pct > 0 and utilization_pct < 50:
        insights.append(Insight(
            category="EFFICIENCY", severity="info",
            title="Low fleet efficiency",
            message=f"Fleet efficiency at {utilization_pct:.0f}% — consider consolidating sessions.",
            action=suggestions[0]["message"] if suggestions else "Consolidate sessions or switch to fewer active accounts.",
            source="capacity_waste",
            data={"utilization_pct": utilization_pct},
        ))

    return insights


# ── GitHub / cross-session coordination checks ──────────────────────────────

@advisor_check("github_stale_prs", ["project_tasks"])
def check_github_stale_prs(ctx: Dict) -> List[Insight]:
    """Surface open GitHub PRs with no activity in 3+ days and no active claimer."""
    from datetime import datetime, timezone, timedelta
    tasks = ctx.get("project_tasks") or []
    active_sessions = ctx.get("peer_sessions") or []
    claimed_tasks = {s.get("task_ref", "") for s in active_sessions}

    stale_threshold = datetime.now(timezone.utc) - timedelta(days=3)
    stale = []
    for t in tasks:
        if t.get("source") != "github":
            continue
        github_id = t.get("github_issue_id", "")
        if "#pr-" not in github_id:
            continue
        if t.get("status") in ("done", "blocked"):
            continue
        # Check notes for updated date
        notes = t.get("notes", "")
        updated_str = ""
        for part in notes.split():
            if part.startswith("updated:"):
                updated_str = part[8:]
        if updated_str:
            try:
                updated = datetime.fromisoformat(updated_str + "T00:00:00+00:00")
                if updated < stale_threshold:
                    task_id = str(t.get("id", ""))
                    is_claimed = any(task_id in c for c in claimed_tasks)
                    stale.append((t, is_claimed))
            except Exception:
                pass

    insights = []
    unclaimed_stale = [t for t, claimed in stale if not claimed]
    if len(unclaimed_stale) >= 2:
        repos = list({t.get("project", "?") for t in unclaimed_stale})
        insights.append(Insight(
            category="PIPELINE", severity="warning",
            title=f"{len(unclaimed_stale)} stale PRs unclaimed",
            message=f"{len(unclaimed_stale)} GitHub PRs open 3+ days with no active session in {', '.join(repos[:3])}.",
            action="Claim in Dispatch (d) or close stale branches.",
            source="github_stale_prs",
            data={"count": len(unclaimed_stale), "repos": repos},
        ))
    elif len(unclaimed_stale) == 1:
        t = unclaimed_stale[0]
        insights.append(Insight(
            category="PIPELINE", severity="info",
            title="Stale PR needs attention",
            message=f"'{t.get('task_name','?')[:50]}' open 3+ days, no active session.",
            action="Claim or close.",
            source="github_stale_prs",
            data={"task_id": t.get("id")},
        ))
    return insights


@advisor_check("cross_session_work_gap", ["project_tasks", "peer_sessions"])
def check_cross_session_work_gap(ctx: Dict) -> List[Insight]:
    """Flag repos with ready work in Dispatch but no active session working in them."""
    tasks = ctx.get("project_tasks") or []
    peer_sessions = ctx.get("peer_sessions") or []

    # Active repos from session_locks
    active_repos = {s.get("repo", "") for s in peer_sessions if s.get("repo")}

    # Ready tasks grouped by project, excluding projects with active sessions
    gap: dict = {}
    for t in tasks:
        if t.get("status") != "ready":
            continue
        if t.get("claimed_by"):
            continue
        proj = t.get("project", "?")
        # Map project to repo name for comparison
        repo_equiv = proj.replace("-backend", "").replace("-portal", "")
        if any(repo_equiv in r or r in repo_equiv for r in active_repos):
            continue
        gap[proj] = gap.get(proj, 0) + 1

    if not gap:
        return []

    top = sorted(gap.items(), key=lambda x: -x[1])[:3]
    summary = ", ".join(f"{proj}:{n}" for proj, n in top)
    total = sum(gap.values())

    return [Insight(
        category="PIPELINE", severity="info",
        title=f"{total} ready tasks, no session active",
        message=f"Ready work with no active session: {summary}.",
        action="Open Dispatch (d) and claim a task.",
        source="cross_session_work_gap",
        data={"gap": gap},
    )]


# ── Main entry point ────────────────────────────────────────────────────────

_advisor_cache: Optional[AdvisorReport] = None
_advisor_cache_ts: float = 0.0
_ADVISOR_CACHE_TTL = 30


def run_advisor(force_refresh: bool = False) -> AdvisorReport:
    """Run all advisor checks and return ranked insights."""
    global _advisor_cache, _advisor_cache_ts

    now = time.time()
    if not force_refresh and _advisor_cache and (now - _advisor_cache_ts) < _ADVISOR_CACHE_TTL:
        return _advisor_cache

    start = time.monotonic()
    ctx = _build_context()

    all_insights: List[Insight] = []
    for check_fn in _CHECKS:
        try:
            results = check_fn(ctx)
            all_insights.extend(results)
        except Exception as e:
            _log.warning("Advisor check %s failed: %s", getattr(check_fn, '_check_name', '?'), e)

    # Deduplicate by (category, title)
    seen: set = set()
    deduped: List[Insight] = []
    for ins in all_insights:
        key = (ins.category, ins.title)
        if key not in seen:
            seen.add(key)
            deduped.append(ins)

    # Sort by severity then category
    deduped.sort(key=lambda i: i.sort_key())

    duration_ms = int((time.monotonic() - start) * 1000)
    summary = dict(Counter(i.severity for i in deduped))

    report = AdvisorReport(
        insights=deduped,
        timestamp=datetime.now(timezone.utc).isoformat(),
        duration_ms=duration_ms,
        checks_run=len(_CHECKS),
        summary=summary,
    )

    _advisor_cache = report
    _advisor_cache_ts = time.time()
    return report


def get_top_insights(max_count: int = 3, min_severity: str = "warning") -> List[Insight]:
    """Quick wrapper returning only critical/warning insights for session briefings."""
    report = run_advisor()
    if min_severity == "warning":
        severity_filter = {"critical", "warning"}
    else:
        severity_filter = {"critical"}
    return [i for i in report.insights if i.severity in severity_filter][:max_count]



def diff_reports(current, previous_json):
    # type: (AdvisorReport, dict) -> List[Insight]
    """Return insights in current that are new (not in previous run).

    Used by advisor-watch.sh cron to detect changes and send proactive alerts.
    """
    prev_keys = {(i['category'], i['title']) for i in previous_json.get('insights', [])}
    return [i for i in current.insights
            if i.severity in ('critical', 'warning')
            and (i.category, i.title) not in prev_keys]



def get_inbox_items():
    """Aggregate all items needing user attention across all systems.

    Returns list of dicts sorted by priority:
        priority (1=urgent, 2=attention, 3=fyi), category, source, summary, action
    """
    from token_watch_data import (
        _active_sessions, _get_wire_messages, _get_project_tasks,
        _get_system_health, _get_build_ledger, _etime_to_secs,
    )

    wire = _get_wire_messages(limit=50)
    advisor = run_advisor()
    tasks = _get_project_tasks()
    sessions = _active_sessions()
    health = _get_system_health()
    build = _get_build_ledger(days=3, limit=100)

    items = []

    # Wire messages needing response
    for m in wire.get("messages", []):
        if m.get("read"):
            continue
        if m.get("type") in ("question", "file_release", "patch"):
            items.append({
                "priority": 1, "category": "WIRE",
                "source": m.get("from", "?"),
                "summary": "[{}] {}".format(m["type"], m["message"][:80]),
                "action": "Reply to {}".format(m.get("from", "?")),
            })
        elif m.get("type") in ("info", "status"):
            items.append({
                "priority": 3, "category": "WIRE_FYI",
                "source": m.get("from", "?"),
                "summary": m["message"][:80],
                "action": "Acknowledge",
            })

    # Advisor critical/warning insights
    for ins in advisor.insights:
        if ins.severity in ("critical", "warning"):
            items.append({
                "priority": 1 if ins.severity == "critical" else 2,
                "category": "ADVISOR",
                "source": ins.category,
                "summary": ins.message,
                "action": ins.action,
            })

    # Blocked tasks
    for t in tasks:
        if t.get("status") == "blocked":
            items.append({
                "priority": 2, "category": "BLOCKED",
                "source": t.get("project", "?"),
                "summary": "{} ({})".format(t.get("task_name", "?"), t.get("project", "?")),
                "action": "Unblock or reassign",
            })

    # Runaway sessions
    for s in health.get("claude_sessions", []):
        if s.get("status") == "runaway":
            items.append({
                "priority": 1, "category": "RUNAWAY",
                "source": "cc-{}".format(s["pid"]),
                "summary": "cc-{} at {:.0f}% CPU while idle".format(s["pid"], s["cpu"]),
                "action": "Kill process",
            })

    # Stuck sessions (>3h, no progress)
    for sess in sessions:
        pid, etime, directive, delta, source = sess
        secs = _etime_to_secs(etime)
        if secs and secs > 10800 and delta in ("?", "new"):
            items.append({
                "priority": 2, "category": "STUCK",
                "source": "cc-{}".format(pid),
                "summary": "cc-{} active {}h ({})".format(pid, secs // 3600, directive),
                "action": "Check or kill",
            })

    # Untested builds (>=3 per project)
    untested_by_project = {}
    for item in build.get("items", []):
        if item.get("test_status") == "untested":
            proj = item.get("project", "?")
            untested_by_project[proj] = untested_by_project.get(proj, 0) + 1
    for proj, count in untested_by_project.items():
        if count >= 3:
            items.append({
                "priority": 3, "category": "UNTESTED",
                "source": proj,
                "summary": "{} untested in {}".format(count, proj),
                "action": "Run verification",
            })

    # Sort and deduplicate
    items.sort(key=lambda x: (x["priority"], x["category"]))
    seen = set()
    deduped = []
    for item in items:
        key = (item["category"], item["summary"][:40])
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    return deduped
