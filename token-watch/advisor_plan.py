#!/usr/bin/env python3
"""
Advisor Plan — helpers for reading/updating the active advisor execution plan.

Plan file: /tmp/advisor-plan-active.md

Usage from advisor context:
    from advisor_plan import update_plan_step
    update_plan_step(2, "Worker 1", "done")
    update_plan_step(3, "Hit Railway API", "in_progress")
"""

import re
from pathlib import Path

PLAN_PATH = Path("/tmp/advisor-plan-active.md")


def update_plan_step(phase: int, step: str, status: str):
    """Update a step in the active plan.

    Args:
        phase: Phase number (matches ## Phase N headers)
        step: Substring to match in the step text (e.g. "Worker 1" or "Hit Railway")
        status: One of 'done', 'in_progress', 'blocked'
    """
    if not PLAN_PATH.exists():
        raise FileNotFoundError(f"No plan file at {PLAN_PATH}")

    text = PLAN_PATH.read_text()
    lines = text.splitlines()

    status_map = {
        "done": ("[x]", " DONE ✅"),
        "in_progress": ("[ ]", " ⏳"),
        "blocked": ("[ ]", " 🚫 BLOCKED"),
    }

    if status not in status_map:
        raise ValueError(f"status must be one of: done, in_progress, blocked (got {status!r})")

    checkbox, suffix = status_map[status]
    current_phase = 0
    updated = False

    for i, line in enumerate(lines):
        # Track phase headers like "## Phase 1" or "## Phase 1: ..."
        phase_match = re.match(r"^##\s+Phase\s+(\d+)", line)
        if phase_match:
            current_phase = int(phase_match.group(1))
            continue

        if current_phase == phase and step in line:
            # Replace checkbox
            new_line = re.sub(r"\[[ xX]\]", checkbox, line)
            # Remove old status suffixes before adding new one
            new_line = re.sub(r"\s*(?:DONE\s*✅|⏳|🚫\s*BLOCKED)\s*$", "", new_line)
            # Add suffix for non-done statuses, or for done
            if status == "done":
                if "✅" not in new_line:
                    new_line = new_line.rstrip() + suffix
            else:
                new_line = new_line.rstrip() + suffix
            lines[i] = new_line
            updated = True
            break

    if not updated:
        raise ValueError(f"Could not find step matching {step!r} in Phase {phase}")

    PLAN_PATH.write_text("\n".join(lines) + "\n")
    return True


def get_plan_text() -> str:
    """Return the raw plan markdown, or empty string if no plan."""
    if PLAN_PATH.exists():
        return PLAN_PATH.read_text()
    return ""


def parse_phases(text: str):
    """Parse plan markdown into phases with checkbox counts.

    Returns list of dicts:
        [{"number": 1, "name": "Setup", "done": 3, "total": 5, "items": [...]}, ...]

    Each item is {"text": str, "done": bool, "in_progress": bool}.
    """
    phases = []
    current = None

    for line in text.splitlines():
        phase_match = re.match(r"^##\s+Phase\s+(\d+)\s*[:\-–—]?\s*(.*)", line)
        if phase_match:
            current = {
                "number": int(phase_match.group(1)),
                "name": phase_match.group(2).strip(),
                "done": 0,
                "total": 0,
                "items": [],
            }
            phases.append(current)
            continue

        if current is None:
            continue

        # Next top-level header ends the phase
        if re.match(r"^#\s+", line):
            current = None
            continue

        # Checkbox items
        done_match = re.match(r"\s*-\s*\[[xX]\]\s*(.*)", line)
        todo_match = re.match(r"\s*-\s*\[ \]\s*(.*)", line)
        if done_match:
            current["total"] += 1
            current["done"] += 1
            current["items"].append({"text": done_match.group(1), "done": True, "in_progress": False})
        elif todo_match:
            current["total"] += 1
            txt = todo_match.group(1)
            in_prog = "\u23f3" in txt  # hourglass emoji
            current["items"].append({"text": txt, "done": False, "in_progress": in_prog})
            # Track the first unchecked item as current_step
            if "current_step" not in current:
                current["current_step"] = txt.split("—")[0].split("✅")[0].strip()[:50]

    return phases


def compute_overall(phases):
    """Return (done, total) across all phases."""
    done = sum(p["done"] for p in phases)
    total = sum(p["total"] for p in phases)
    return done, total
