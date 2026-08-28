---
name: spec
description: "Write a spec: the intent and design of something not yet built — problem, goals, design decisions, acceptance criteria. A spec is never a plan (no task breakdowns, no step-by-step code); the operator asks for a plan separately via the plan skill. Use for '/spec' or any request to write a spec or design doc."
---

# spec

Write the design of record for something not yet built: what it is, why, the decisions that shape it, and how we'll know it's done.
The reader is the operator deciding whether to build it, and the plan-writer who will later turn it into tasks — give both what they need and nothing procedural.

**Announce at start:** "Using spec to write the spec."

**"Spec" means this artifact — specs only.**
A spec carries no task breakdown, no step-by-step code, no checkbox tracking; that is a plan, a separate deliverable the operator requests by name (`/plan`, the `plan` skill — saved to `docs/plans/`).
Neither artifact is a prerequisite for the other — do not follow a spec with an unrequested plan.

**Save to:** `docs/specs/YYYY-MM-DD-<name>-spec.md` (specs never go in `docs/plans/`) with the docs-contract frontmatter:

```yaml
---
status: draft
date: YYYY-MM-DD
---
```

Lifecycle (docs contract, `docs/AGENTS.md` §Lifecycle — the implementer's job, not the spec's): `building` when implementation starts, `validating` when the work lands and awaits operator validation, then `status: implemented` + move to `docs/implemented/` once the operator confirms. When a plan drives the implementation, the plan carries `building`/`validating` and the spec is closed out alongside it.

## Before writing

Search `docs/` for the topic first — extend or supersede an existing spec (`supersedes:` frontmatter) rather than adding a competing one.
Read the architecture-of-record specs linked from the root `AGENTS.md` for any layer the design touches; a spec that contradicts the architecture of record without saying so is a defect.
Check every component against the "Where things go" table — a spec that places something with no row is raising a design question, and must say so explicitly rather than improvise.

## What a spec contains

- **Problem and goals** — what hurts today, what this makes possible; non-goals stated just as plainly.
- **Design** — the shape of the solution: data model, interfaces, module boundaries, UI surfacing (the dashboard is the product surface — say where this appears).
- **Decisions and alternatives** — each contested choice with the option taken, the options rejected, and why; one-way doors flagged.
- **Invariants touched** — which architectural invariants this brushes against and how it stays conformant.
- **Acceptance criteria** — observable statements an operator can check, not implementation steps.
- **Open questions** — what still needs an operator decision, stated as questions, not silently defaulted.

Use the operator's exact wording — requirements, copy, constraints — verbatim wherever they gave it.

## Self-review

After writing, check with fresh eyes — inline, not a subagent dispatch:

1. **No plan leakage:** any task lists, file-by-file edit steps, or test code belongs in a future plan; cut it.
2. **Decision completeness:** every contested choice records its alternatives, or it lands in Open questions.
3. **Placement:** every named component sits in a "Where things go" row, or the spec flags the gap.
4. **Testability:** each acceptance criterion is observable by the operator on the real artifact.

Fix issues inline and move on.
