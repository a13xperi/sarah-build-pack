#!/usr/bin/env python3
"""
Token Matrix router — Pillar 3 task→engine classifier (deterministic).

Turns the routing table in docs/03-token-matrix.md into code. Given a task
description (and optional explicit type/risk), returns the cheapest capable
engine plus a confidence score and a human-readable reason.

The "Opus sandwich" invariant: Opus is the bread (design + validation),
cheap engines are the meat, Sonnet directs. So engines fall into two groups:
  - in-session : opus, sonnet  — handled by the main Claude session, NOT dispatched
  - external   : codex, gemini, minimax, kimi — runnable via bs-dispatch.sh

Routing table (from the doc):
  architecture / API / data-model / auth / billing / migration  -> opus
  pre-merge audit / security review                             -> opus (never delegated)
  isolated PR / multi-file refactor                            -> codex
  tests / boilerplate / lint / types / renames                 -> minimax
  single-file bug / research / docs / multimodal               -> gemini
  chat / decompose / route / integrate / ambiguous            -> sonnet (director)

Stdlib only. No network, no API calls — fully deterministic and unit-testable.

CLI:
  python3 lib/route.py "add unit tests for the parser"
  python3 lib/route.py --json --type refactor "split the god object"
  python3 lib/route.py --risk high "tweak the billing webhook"
"""
import argparse
import json
import re
import sys

# Engines that run in the main Claude session (never dispatched externally).
IN_SESSION = {"opus", "sonnet"}
# Engines runnable via bs-dispatch.sh (engine flag in parens where it differs).
EXTERNAL = {"codex", "gemini", "minimax", "kimi"}
ALL_ENGINES = IN_SESSION | EXTERNAL

# Map our engine label -> bs-dispatch.sh --engine value.
DISPATCH_ENGINE = {"minimax": "mm", "codex": "codex", "gemini": "gemini", "kimi": "kimi"}

# ── Explicit type → engine (highest authority when caller passes --type) ──
TYPE_TO_ENGINE = {
    "architecture": "opus", "design": "opus", "api": "opus",
    "data-model": "opus", "datamodel": "opus", "schema": "opus",
    "auth": "opus", "billing": "opus", "migration": "opus",
    "audit": "opus", "security": "opus",
    "refactor": "codex", "pr": "codex",
    "test": "minimax", "tests": "minimax", "lint": "minimax",
    "boilerplate": "minimax", "types": "minimax", "chore": "minimax",
    "bug": "gemini", "research": "gemini", "docs": "gemini",
    "chat": "sonnet", "plan": "sonnet", "route": "sonnet",
}

# ── Keyword signal sets for free-text classification ──
# Each category lists (regex, weight). Order of CATEGORIES sets tie-break
# priority (earlier = wins ties). "audit" is handled as a hard override below.
CATEGORIES = [
    ("opus", "audit", [
        (r"\baudit\b", 3), (r"security review", 3), (r"\bpentest\b", 3),
        (r"pre-?merge", 2), (r"validate before (ship|merge|deploy)", 3),
        (r"sign[- ]?off", 2), (r"vulnerabilit", 2), (r"\bthreat model", 2),
    ]),
    ("opus", "high blast-radius design", [
        (r"\barchitect", 3), (r"\bdesign\b", 2), (r"\bapi design\b", 3),
        (r"data ?model", 3), (r"schema design", 3), (r"\bschema\b", 1),
        (r"\bauth(enticat|oriz|n|z)?\b", 3), (r"\boauth\b", 2),
        (r"\bsession token", 2), (r"\blogin\b", 1),
        (r"\bbilling\b", 3), (r"\bpayment", 3), (r"\binvoic", 2),
        (r"\bmigration\b", 3), (r"breaking change", 3),
        (r"high[- ]risk", 3), (r"blast radius", 3),
    ]),
    ("minimax", "tests / boilerplate / lint", [
        (r"\btests?\b", 3), (r"unit test", 3), (r"test coverage", 3),
        (r"\bboilerplate\b", 3), (r"\blint", 3), (r"\bformat(ting)?\b", 2),
        (r"type annotation", 3), (r"\btyping\b", 2), (r"\bstub(s|bed)?\b", 2),
        (r"\bscaffold", 2), (r"rename (the )?(variable|symbol)", 2),
    ]),
    ("codex", "isolated PR / multi-file refactor", [
        (r"\brefactor", 3), (r"multi[- ]file", 3), (r"isolated pr", 3),
        (r"\bextract\b", 2), (r"\brestructure", 2), (r"\bcodemod\b", 3),
        (r"\bmove\b.*\bto\b", 1), (r"rename across", 2),
    ]),
    ("gemini", "single-file bug / research / docs", [
        (r"\bbug\b", 2), (r"single[- ]file", 2), (r"\bresearch\b", 3),
        (r"\binvestigat", 2), (r"look up", 2), (r"find out", 2),
        (r"\bdocs?\b", 2), (r"documentation", 3), (r"\breadme\b", 2),
        (r"multimodal", 3), (r"\bimage\b", 2), (r"screenshot", 2),
        (r"\bocr\b", 3), (r"real[- ]time", 2),
    ]),
    ("sonnet", "director / decompose", [
        (r"decompose", 2), (r"\bplan\b", 1), (r"\bdispatch\b", 1),
        (r"\bintegrate\b", 1), (r"figure out", 1), (r"\bchat\b", 1),
    ]),
]


def _score(text):
    """Return {engine: (score, reason)} from keyword matches in text."""
    scores = {}
    for engine, label, signals in CATEGORIES:
        total = 0
        for pattern, weight in signals:
            if re.search(pattern, text):
                total += weight
        if total > 0:
            # Keep the highest-scoring category per engine.
            prev = scores.get(engine, (0, ""))
            if total > prev[0]:
                scores[engine] = (total, label)
    return scores


def classify_task(description, task_type=None, risk=None):
    """
    Classify a task into a recommended engine.

    Returns a dict: {engine, dispatch, in_session, confidence, reason}.
      engine     — opus | sonnet | codex | gemini | minimax | kimi
      dispatch   — bs-dispatch.sh --engine value, or None for in-session engines
      in_session — True if handled by the main Claude session (no external dispatch)
      confidence — 0.0–1.0
      reason     — short human explanation
    """
    text = (description or "").lower().strip()

    # 1) Explicit type wins outright (caller knows the category).
    if task_type:
        key = task_type.lower().strip()
        engine = TYPE_TO_ENGINE.get(key)
        if engine:
            # Risk can still escalate to opus for safety.
            if risk and risk.lower() == "high" and engine in ("codex", "gemini", "minimax"):
                return _result("opus", 0.9, f"type={key} but risk=high → escalate to Opus")
            return _result(engine, 0.95, f"explicit type={key}")

    # 2) Empty / no description → director.
    if not text:
        return _result("sonnet", 0.3, "no task text → Sonnet directs")

    scores = _score(text)

    # 3) Hard override: any audit/security signal → Opus (never delegated).
    #    Detected via the 'opus' category whose label starts with 'audit'.
    if re.search(r"\baudit\b|security review|\bpentest\b|validate before (ship|merge|deploy)", text):
        return _result("opus", 0.95, "audit / security review → Opus (never delegated)")

    # 4) Explicit high risk with no clearly-cheap signal → Opus.
    if risk and risk.lower() == "high":
        # If it's clearly test/boilerplate work, cheap is still fine.
        if scores.get("minimax", (0, ""))[0] >= 3:
            return _result("minimax", 0.8, "risk=high but task is test/boilerplate → still cheap")
        return _result("opus", 0.9, "risk=high → Opus designs/validates")

    # 5) No signals at all → director.
    if not scores:
        return _result("sonnet", 0.4, "no routing signals → Sonnet directs")

    # 6) Pick highest score; tie-break by CATEGORIES order (priority).
    order = {eng: i for i, (eng, _, _) in enumerate(CATEGORIES)}
    best_engine = min(
        scores,
        key=lambda e: (-scores[e][0], order.get(e, 99)),
    )
    best_score, best_label = scores[best_engine]

    # Confidence from absolute score and margin over the runner-up.
    others = [s for e, (s, _) in scores.items() if e != best_engine]
    runner = max(others) if others else 0
    margin = best_score - runner
    confidence = min(0.95, 0.5 + 0.1 * best_score + 0.1 * margin)
    return _result(best_engine, round(confidence, 2), best_label)


def _result(engine, confidence, reason):
    return {
        "engine": engine,
        "dispatch": DISPATCH_ENGINE.get(engine),  # None for opus/sonnet
        "in_session": engine in IN_SESSION,
        "confidence": confidence,
        "reason": reason,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description="Token Matrix task→engine router")
    p.add_argument("description", nargs="*", help="task description")
    p.add_argument("--type", dest="task_type", default=None,
                   help="explicit task type (e.g. refactor, test, auth, audit)")
    p.add_argument("--risk", default=None, choices=["low", "medium", "high"],
                   help="explicit risk level")
    p.add_argument("--json", action="store_true", help="emit JSON")
    args = p.parse_args(argv)

    desc = " ".join(args.description)
    res = classify_task(desc, task_type=args.task_type, risk=args.risk)

    if args.json:
        print(json.dumps(res))
    else:
        where = "in-session" if res["in_session"] else f"dispatch:{res['dispatch']}"
        print(f"{res['engine']}  ({res['confidence']:.2f}, {where})  — {res['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
