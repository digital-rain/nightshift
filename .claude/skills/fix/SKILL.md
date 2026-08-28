---
name: fix
description: "Something is broken — find the root cause with evidence, implement the recommended fix, verify it on the real artifact, land it, then report root cause and fix together. Not always a test failure, not always code: config, data, environment, and design faults all qualify. Use for '/fix <what's wrong>', 'X is broken', 'why is X doing Y', 'figure out X', 'sort out X'. Only '/fix --ask' (or 'diagnose only', 'ask before fixing') stops for approval between diagnosis and fix."
---

# fix

Diagnose first, fix second, land, then report both — in one run.
The diagnosis is still a finding — root cause with evidence — but it is delivered with the fix, not instead of it; a session that stops to ask "may I fix it?" is a session the operator has to come back to.

**Announce at start:** "Using fix on <symptom>."
**Todos:** reproduce · root cause with evidence · implement · verify · land · report.

Two carve-outs still stop for the operator, always:

- **Destructive or stateful data operations** — hard rule 3: confirm scope and make the checkpoint (dump, snapshot, reversible migration) before any repair that rewrites operator data.
- **Architectural faults** — when the fix is a design decision (which implementation becomes canonical, a schema/API change, an ownership move), report and recommend; that decision is the operator's.

Invoked as **`/fix --ask`** (or "diagnose only", "ask before fixing"): stop after §2 with the report and proposal, and implement only on approval — end-of-turn question, never a mid-task prompt.

## 1. Diagnose

No fixes without a root cause first; a symptom that pattern-matches a known failure may have a different cause.

- **Reproduce before theorizing.** Trigger the failure yourself; if you can't reproduce it, gather more evidence — don't guess.
  Not everything broken is a test failure: a wrong value on a dashboard, a stale dataset, a config knob that doesn't stick, a service that won't start are all in scope; reproduce in whatever medium the fault lives (UI, DB query, logs, process table).
- **Read the whole error**, then trace the bad value to its source — fix belongs at the origin, not the symptom.
- **Check recent changes**: `git log` on the touched area, recent migrations, config or dependency changes.
- **In multi-component paths** (UI → API → store → worker), instrument the boundaries and find *which* hop breaks before descending into one.
- **Compare against the nearest working example** of the same pattern and list what differs; don't assume a difference "can't matter".
- **Your first plausible explanation is a hypothesis, not a finding.** A nearby error is usually a downstream symptom; correlation with a recent change is not causation. Before running with it, name one rival cause and what evidence would disprove your favourite — then go look for that evidence, not for confirmation.
- One hypothesis at a time, smallest probe that tests it, one variable at a time — and it graduates to root cause only when a probe confirms it (failure reproduced through the cause, or symptom gone with the cause neutralized), never on plausibility alone.
- **The cause must explain every symptom** — including why the unaffected cases are unaffected. An unexplained symptom means the wrong cause or a second fault; don't ignore the inconvenient ones.
- **Three failed probes each surfacing a new problem somewhere else means the architecture is wrong** — stop and raise the design question instead of probing further.
- A fault that survives two tested-and-rejected hypotheses, or spans multiple components or incidents, has outgrown this loop — switch to the full `rca` protocol rather than probing on.

## 2. Root cause and proposal

Settle these before touching anything; under `--ask` they are the report you stop on, otherwise they are the first half of the closing report:

- **Root cause:** what is actually wrong, with evidence (file:line, query output, log lines) — not "probably". Name the rival hypothesis you ruled out and what ruled it out; if nothing was ruled out, the diagnosis isn't done.
- **Blast radius:** what else this affects; whether data is corrupted and needs the undo-path treatment (hard rule 3: checkpoint before any destructive repair).
- **Proposed fix:** the change you recommend, where it goes per "Where things go", and what verifies it.
- **Alternatives** when the fix involves a real choice (patch vs. re-shape), each in a line with the trade-off; lead with your recommendation.
- **If drift:** name it; recommend correcting on a smaller, tighter unit, not another pass on the same oversized one.

## 3. Implement

- A regression check comes first where one is possible: a failing test that reproduces the fault, or a recorded before-value for non-code faults; then the fix turns it green.
- Scope to the approved fix — no bundled cleanup, no "while I'm here".
- Follow the root `AGENTS.md` loop (worktree for code changes); land with the `land` skill.
- Verify on the real artifact — the original symptom gone in the medium it was reported (the page renders, the value is right, the service stays up), not just tests green.
- **If the fault is implementation drift** (scope creep vs plan/Owns, forked canon from an oversized session, recurring correction of the same miss): split the remaining work into smaller units and tighten the brief before continuing. Say so in the report.
- While diagnosing and iterating, run targeted checks (`just lint`, `just typecheck`, `just ui-typecheck`, the touched tests) — the full gate runs once, backgrounded, at land time.

## 4. Close out

Report: root cause, the fix, the verification evidence, and the landing proof.
A correction you'd write twice becomes a guard — propose the lint/type/validation that makes this fault impossible, per the root working norms.
If the fault should be queued instead of fixed now (out of scope, blocked on a carve-out, bigger than the session), say so and hand it to `sniffer-fix`.
