---
name: do
description: "Quick, targeted implementation: worktree, do the small task directly yourself, then land (squash-merge to local main, gated on the touched area's tests via 'just test'). Use for '/do <task>' — a single-session change, concrete enough to need no planning, slicing, or subagents. Anything bigger, or anything borderline, belongs to implement."
---

# do

The fast path for a small, concrete task: worktree, build it, verify it, land it.
No architect pass, no subagents, no slicing — if the task turns out to need those, stop and switch to `implement`.

This skill's whole claim is that it is lighter than `implement`, so it only holds while the task is **one session, one unit, one check** and its shape is already settled.
The moment any of that stops being true — it wants slicing, it raises a shape question, it needs a second sitting — it is `implement`'s, borderline included.
Escalating early costs one handoff; escalating late costs an unsliced, unverified landing.

**Announce at start:** "Using do to <task>."

## 1. Worktree

`just worktree <branch>` (creates `.worktrees/<branch>` off `main` with `.venv`/`node_modules` symlinked in); work only inside it, never in the primary checkout.

## 2. Build

Do the task yourself, honoring the operator's wording verbatim and the touched area's rules.
Verify the real artifact — run the code path, look at the rendered UI — not just a green compile.
If the work grows past a couple of verifiable steps or raises shape questions, that's `implement` territory; hand it over and say so instead of pushing on.

## 3. Land

Finish with the `land` skill: the touched area's tests green (`just test <paths>`, quiet), squash-merge to local `main`, worktree removed, landing proof in the report.
