---
name: review
description: "Manually invoked review — one verb, routed by target. A plan document routes to the review-plan skill; anything else is a code review of a branch/diff. Default code lens: design conformance against the architecture of record — drift and slop that `just validate` cannot catch. Deep audit ('thermo') adds correctness/security and code-quality lenses as parallel subagents. Use for review requests, pre-merge audits, or 'thermo' / 'thermonuclear' requests."
disable-model-invocation: true
---

# Review

## Route by target

If the review target is a **plan document** — a `docs/plans/*-plan.md` file, or any doc whose content is an implementation plan (task breakdown, steps, file lists) — defer to the `review-plan` skill and stop here: it owns the implementer's-seat lenses (completeness, buildability, interfaces, placement, coverage) and the revision loop that folds findings back into the plan file.
Everything below is the **code** review.
When the target is ambiguous (an operator says just `/review` with a plan file open but a dirty branch too), ask which one — one line, not a menu.

## Code review

Review the given diff (default: current branch vs `main`).
The area's tests and CI's `just validate` (every push to `main`) already cover tests, lints, and guards — do not re-run or re-litigate them; this skill exists for the judgment calls a guard can't make.
Output findings; the working agent loops on them.
Reviews are pulled, not gated.

Report each finding as **blocker** (violates an invariant or forks canon) / **drift** (works, but grows kruft) / **note**, with `file:line` and the canonical path the change should have taken.
Trace every finding end to end before reporting — never present unfinished research ("if the backend handles it, this is fine") when you can check the other side yourself.
A few high-conviction findings beat a list of nits; a clean review is one line.

When the working agent will act on **blocker** or **drift** findings that show implementation left the plan corridor (Owns/scope creep, forked canon, recurring miss): state in the report that correction should split and tighten the remaining units rather than re-run the same oversized one.

## Lens 1: Design conformance (always)

Read the diff and the touched files' surroundings; identify which architecture-of-record spec sections (linked from `AGENTS.md`) the change lives in, then walk this checklist.

- **Forked canon — the #1 slop pattern here.**
  Does the diff re-implement something that has one home: a query outside `long_data`, a fold/valuation/marks computation outside `long_ledger`, a verdict outside `long_gate`, a lifecycle loop outside `run_service`/`run_connector`, a provider call outside its gateway, a fetch/poll outside the scheduler, a UI data path outside `useChannel`/`useMutation`, a status list not derived from the registry?
  Near-duplicates count: a "slightly different" copy is a fork.
- **Missing deletion.**
  If the diff migrates or supersedes anything, is the old implementation deleted in the same change?
  A migration that leaves the loser alive is incomplete.
- **Numbers where classes belong.**
  Any new interval, sleep, TTL, staleness threshold, retry pacing, or timeout literal outside scheduler/config modules?
  Any cadence decision that should be a class reference or a plan-of-record change?
- **Declarations kept honest.**
  New dataset/table/topic/route/channel: is the registry/ownership/topics/channel declaration present and does it match what the code actually does (writer, class, inputs, `backfill` block or explicit `null`)?
- **Boundary and type discipline.**
  Validation only at boundaries; typed frozen rows with `as_of` on reads; branded ids not bare strings; sum-type matches exhaustive; no new `# type: ignore`/`any`/cast papering over a loose model; transaction boundaries with the caller.
- **Guard integrity.**
  Was any guard, lint, ratchet, or test weakened, skipped, or special-cased to make the diff pass?
  Always a blocker.
- **Scope and slop hygiene.**
  Speculative abstractions, one-caller wrappers, dead options, defensive guards for impossible states, comment noise, unrequested features?
  Recommend deletion, not polish.
  Conversely: does the change surface in the UI where the root `AGENTS.md` requires it?
- **Repeated-correction check.**
  A finding you've seen before in this repo → say so and propose the structural encoding (lint/type/validation) instead of expecting the next review to catch it again.

## Lens 2 + 3: Deep audit (on request — "thermo", "deep audit", pre-merge on risky branches)

Launch two parallel read-only subagents, each given the diff, the scope rules above, and one lens (run the lenses sequentially yourself if your harness has no subagents); synthesize findings-first, deduplicated, resolving disagreements with your own judgment.
Order the synthesis: correctness/security, then structural regressions and missed simplifications, then boundary/type problems, then size and legibility.

**Correctness and security.**
Trace the dependents of every changed interface, type, and behavior — subtle cross-module interactions are where breakage hides.
Devex breakage: renamed env vars, remapped ports, new required setup steps.
Security: injection, authz gaps, secret exposure, unsafe deserialization, SSRF — in the changed code and what it newly enables.
Intended, well-scoped breakage is not a finding unless the author seems unaware of the implications.

**Code quality.**
The core move is code judo: restructurings that keep behavior while whole branches, modes, or layers disappear.

| Flag | Remedy |
|---|---|
| Diff pushes a file past 1000 lines without strong reason | Decompose first: extract helpers, subcomponents, modules |
| Ad-hoc conditionals, one-off booleans/nullable modes bolted into unrelated flows | Reframe the state model so branches disappear |
| Generic mechanisms hiding simple data shapes; thin wrappers; identity abstractions | Delete the indirection; keep the direct flow |
| Silent fallbacks papering over unclear invariants | Explicit typed model; make the boundary explicit |
| Feature logic leaking into shared paths; near-duplicates of existing helpers | Move to the layer that owns the concept; reuse the canonical helper |

Rows are presumptive blockers unless the author justifies them.
Form your own findings before reading any PR discussion; evaluate bot and reviewer comments on merit and attribute what you incorporate.
