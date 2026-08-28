---
name: implement
description: "Build a change end to end: worktree, shape check, subagent development with verification between units, squash-merge to local main. The execution engine for any work needing more than one verifiable slice — a feature, a plan, a queued brief. Use for '/implement', 'implement X', 'build X', 'execute this plan', or 'dispatch this plan'. Work that fits one session or less hands off to `do`; anything borderline stays here."
---

# implement

The operator's stock instruction, encoded:

> work in a worktree; use subagents for development where units are genuinely parallel; verify their work against the plan; squash-merge to local main when done.

One idea drives the loop: fresh context per unit, verification between units, and nobody's claims — including your own — accepted without evidence.

**Announce once the size check below clears:** "Using implement to build <thing>."
**Open a todolist before any edit:** worktree · shape · slice · one todo per unit (added once sliced) · land.
A step that isn't a todo gets skipped under pressure; the todolist is the contract.

## Size check

Everything below this line — orchestration, delegate briefs, per-unit review, drift response — is machinery a one-session task never earns back.
Size the work before opening a worktree; read the brief first, and if the *intent* is unclear, resolve that (see **Intent**) before sizing, because you can't size what you can't state.

Hand it to the `do` skill when **all** of these hold:

- it is **one session or less**: a couple of verifiable steps, done in a single sitting;
- the shape is already concrete — no architect pass, no types, signatures, or module boundaries left to settle;
- it is **one unit** — nothing to slice, nothing independent to run in parallel;
- verifying the real artifact once settles it; there is no accumulated diff worth a separate review pass.

Then say so in one line — "This is a single-session change; using `do` instead." — and run `do`.
Do not open the orchestrator's machinery and then quietly do the work yourself; that is the worst of both, full ceremony and no delegation.

**Borderline goes to implement.** If the work is too big for `do` but doesn't obviously justify an orchestrator, it is this skill's, not `do`'s — the failure mode of the gap is a `do` session that grows three times and lands unsliced and unverified.
In that band the machinery scales down rather than disappearing: worktree, slice, and a check per unit stay; the architect pass runs only if there is a real shape question; and per **Dispatch** you execute the units yourself in order instead of dispatching subagents.
Delegation is what scales with size — the verify-between-units loop is not.

**The handoff runs one way in each direction.** Handing down to `do` happens here at the size check and nowhere else — once a worktree is open or the work is sliced, finish here rather than dropping ceremony mid-flight.
Handing up is always open: a `do` session that outgrows itself escalates to this skill at any point.

## Intent

The blank in "implement ______" is the operator's requirement.
Use their exact wording — requirements, copy, signatures, acceptance criteria — **verbatim** wherever they gave it; it is the brief units are dispatched against.
If the intent (not just the shape) is ambiguous, state your reading in one line and ask the single question that would change the plan before dispatching.

## Worktree

`just worktree <branch>` (creates `.worktrees/<branch>` off `main` with `.venv`/`node_modules` symlinked in), then work only inside it; confirm with `git rev-parse --show-toplevel` before the first edit.
Never edit the primary checkout; the rest of the root `AGENTS.md` loop applies unchanged.

## Shape

For anything non-trivial or shape-defining that is not already a planning document, run the `architect` skill first: sketch types, signatures, and module map before code so the plan is a real contract, not a vibe.
Skip only for genuinely mechanical work whose shape is already concrete.

## Slice

1. Re-read the brief, the touched area rules, and the architecture-of-record specs before the first edit.
2. State the deliverable and its validation gate in one sentence; if you can't, the brief is ambiguous — resolve that first.
3. Slice into the fewest units that each end in their own check (a failing-then-passing test, a rendered screen, a measurable output).
   Never batch-verify at the end; a unit isn't done until its check ran.
4. For each unit note: files touched, expected behavior, the check, and neighbors it must not change.

## Dispatch

**Before dispatching the first unit:** if this work executes a plan or spec in `docs/plans/`/`docs/specs/`, set its frontmatter to `status: building` and commit — the docs contract's lifecycle marker that implementation has started. The counterpart transition (`validating` at land time) is in the `land` skill.

Delegate units to subagents when they are available and the units are genuinely independent; otherwise execute them yourself in order — the loop is the same.
A subagent costs a fresh context plus your verification of it; with fewer than three independent units, executing them yourself is usually cheaper and no less verified.

- A delegate brief follows the root `AGENTS.md` contract and stands alone: worktree path, exact unit, the check, forbidden commands, spec pointers.
  Never paste session history into a dispatch; a fresh delegate needs its unit, the interfaces from prior units, and nothing else.
  Start from the templates in [`references/implementer-brief.md`](references/implementer-brief.md) and [`references/unit-reviewer-brief.md`](references/unit-reviewer-brief.md) rather than composing dispatches from scratch.
  Briefs, reports, and diffs move as files, not pasted text — everything pasted into a dispatch or printed back stays resident in your context for the rest of the session.
- Dispatch in one wave only units with disjoint writes.
  Two writers on the same files, migration, config, or UI primitive is a conflict, not parallelism.
- **Model selection:** name a model on every dispatch — `sonnet` for mechanical units, search, and unit review; `opus` only when a unit needs judgment the brief did not settle, or after a `sonnet` attempt produced bad work.
  An omitted model inherits the session's, which is the most expensive choice available.
  (`.cursor/rules/model-selection.mdc` is the operator's Cursor preference; Claude Code never loads it and its model names do not exist here.)
- **A delegate's report is a claim, not evidence.**
  Rerun the unit's cited check yourself (one command) and read the diff stat; read the full diff when the check disagrees with the report, the unit touched money-shaped code, or the stat shows files outside its Owns.
  A delegate that says "done" without evidence gets its unit re-verified, not re-trusted.
- Handle delegate statuses: DONE → rerun the check, read the stat; DONE_WITH_CONCERNS → read the concerns before proceeding; NEEDS_CONTEXT → supply it and re-dispatch; BLOCKED → more context, `opus`, a split unit, or escalate a plan problem to the operator — never force an unchanged retry. If the *run itself* failed (timeout, provider error), retry the same dispatch once; a second failure is an operator escalation.
  A reply with no status line ("I'll pause and wait for direction") counts as BLOCKED: read the worktree state yourself (uncommitted files, the check, the diff stat), then finish the unit inline or re-dispatch a tighter one. The delegate has already exited; only you are waiting.
- **A unit's owed tests are part of the unit.** Green code with its brief's tests unwritten is not DONE, and writing them is the next step — yours or a re-dispatch's — not a question for the operator. Commit what is green by path first, so the worktree never carries unstaged work across a wake.
- Between risky units, or after the last one, pull the `review` skill on the accumulated diff rather than reviewing your own work from memory.

### Drift response (orchestrator)

When verification or review finds **implementation drift** (diff outside the unit's Owns, fights the brief/Decisions, silent scope creep, or repeated correction of the same miss):

1. Stop continuing the oversized unit.
2. Correct on a smaller slice with a tighter brief; dispatch it on `opus` if the correction needs judgment.
3. Split remaining work into smaller units/sessions; tighten each brief so the slice is literal and checkable; update the plan if one exists.

Drift is a signal to change unit size — not only to patch the diff.

## Track

Keep the todo list current, and append one line per accepted unit to a progress ledger in the worktree: `Unit N: complete (commits <base>..<head>, review clean)`.
After any interruption, compaction, or restart: trust the ledger and `git log` over your memory — resumed sessions have re-done finished work and re-broken fixed things; a unit the ledger marks complete is never re-dispatched.

## Debug when a unit fights back

- Reproduce before fixing; read the whole error; trace the bad value to its source and fix there, not at the symptom.
- Find the nearest working example of the same pattern and list what differs; don't assume a difference "can't matter".
- One hypothesis, smallest change that tests it, one variable at a time.
- **Three failed fixes, each surfacing a new problem somewhere else, means the architecture is wrong — stop and raise the design question with the operator instead of attempting fix four.**
- When the fight is with the design rather than a bug — a unit needs a parameter, an ownership change, or a layer the plan didn't anticipate — return to the `architect` sketch and surface the deviation; don't absorb drift silently.
- If the fight is **implementation drift** (scope creep, Owns violation, brief fight), apply **Drift response** above: split and tighten.

## Receive findings

Review findings (from `review`, the operator, or a bot) are verified against the code before acting: confirm real ones and fix them; push back on wrong ones with evidence.
Never implement a finding you can't explain, and never thank your way past one.
**Drift-class findings** (blocker forks, Owns/scope creep, recurring correction): treat as **Drift response** — split and tighten the remaining units; do not burn another pass on the same oversized one.

## Land

Verify the real artifact — run the feature path, read the actual value, check the rendered UI — then finish with the `land` skill: gate green, squash-merge to local `main`, worktree removed, landing proof in the report.
