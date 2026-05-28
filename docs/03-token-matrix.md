# Pillar 3 — The Token Matrix (Opus Sandwich)

**Opus is the bread; cheap engines are the meat.** Spend the expensive model on judgment (design + validation), not on typing. Sonnet directs.

```mermaid
flowchart TD
    Task(["Task"]) --> Classify{"type + risk"}
    Classify -->|"architecture / API / data model /<br>auth-billing-migration"| Top[["OPUS — Top Bread<br>design"]]
    Classify -->|"chat / decompose / route"| Son[["SONNET — director"]]
    Classify -->|"spec-clear execution"| Cheap{"cheapest capable"}
    Top --> Son --> Cheap
    Cheap -->|"isolated PR / refactor"| Codex["Codex"]
    Cheap -->|"tests / boilerplate / lint"| MM["MiniMax / cheap-fast"]
    Cheap -->|"single-file bug / research / multimodal"| Gem["Gemini"]
    Codex --> Coll[["SONNET — integrate"]]
    MM --> Coll
    Gem --> Coll
    Coll --> Bot[["OPUS — Bottom Bread<br>validate before ship"]]
    Bot --> Gate{"PASS?"}
    Gate -->|yes| Ship(["merge / deploy"])
    Gate -->|no| Cheap
```

## Layer stack
1. **Opus (top)** — architecture, API shape, data model, high-blast-radius (auth/billing/migrations). Gated.
2. **Sonnet** — think, decompose, route, dispatch, integrate. The default session.
3. **Cheap engines** — execute spec-clear work in parallel.
4. **Sonnet** — collect + integrate results.
5. **Opus (bottom)** — validate before any merge/deploy. Never delegated to cheap engines.

## Routing table

| Work | Route to |
|---|---|
| Isolated PR · multi-file refactor | Codex |
| Tests · boilerplate · types · lint · renames | cheap-fast model |
| Single-file bug w/ clear spec · polish | Gemini |
| Research / docs (no code) · real-time / multimodal | Gemini |
| Architecture · API · data model · auth/billing/migration | **Opus** |
| Pre-merge audit / security review | **Opus** (never delegated) |

## Rules
1. **Default to cheap.** Spec-clear → cheapest capable engine.
2. **Sandwich anything risky.** Opus designs → cheap executes → Opus validates.
3. **Audit always on Opus.** The bottom bread is non-negotiable.
4. **Parallel where possible.** Fire independent sub-tasks across engines; Sonnet integrates.
5. **Escalate freely.** Sonnet bumps to Opus whenever judgment demands — be cheap on typing, not on thinking.

> Swap engine names for whatever you actually have API access to. The *shape* (Opus bread / Sonnet director / cheap meat) is the invariant, not the specific vendors.
