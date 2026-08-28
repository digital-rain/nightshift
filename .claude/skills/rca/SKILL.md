---
name: rca
description: >-
  Run a disciplined root-cause analysis for a bug, failure, incident, or
  unexpected behavior. Counteracts first-find anchoring by requiring evidence
  collection, competing hypotheses, and falsification before any cause is
  declared. Use when the user invokes /rca or asks for a root-cause analysis.
disable-model-invocation: true
---

# Root-Cause Analysis

You are prone to a specific failure mode: treating the first plausible explanation you find as the root cause. This skill exists to stop that. Follow the phases in order. Do not skip ahead to a fix.

## Prime directives

1. **The first plausible explanation is a hypothesis, not a finding.** Nearby errors are usually downstream symptoms. Correlation in logs or timing is not causation.
2. **You have not investigated until you have rejected at least one hypothesis with evidence.** If every hypothesis you generated survived, you were confirming, not testing.
3. **A root cause must explain 100% of the observed symptoms.** One unexplained symptom means you have the wrong cause, an incomplete cause, or two problems.
4. **Distinguish the trigger from the cause.** "What made it break now" (a deploy, an input, a timeout) is often not "why the system was vulnerable to breaking."
5. **No fixes until the cause is confirmed.** Proposing a patch mid-investigation anchors you to it.

## Phase 0 — Freeze the symptoms

Before forming any explanation, record verbatim:

- Exact error messages, stack traces, log lines, wrong outputs.
- Expected vs. actual behavior.
- Scope: which environments, users, inputs, or code paths are affected — and, just as important, which are NOT.
- Reproducibility: always, intermittent, or one-off.

Write these down as a numbered symptom list (S1, S2, ...). Every later claim must trace back to these. If you catch yourself explaining before this list exists, stop and finish the list.

## Phase 1 — Reconstruct the timeline

Answer with evidence, not memory:

- When did it last demonstrably work? (passing CI run, working deploy, user report)
- What changed between then and now? Check all of: code (git log/diff across the window), dependencies (lockfiles), configuration, infrastructure/environment, data shape or volume, external services.
- If nothing changed, say so explicitly — that points toward latent bugs surfaced by new inputs, resource exhaustion, or time-dependent behavior, and away from "the last commit did it."

## Phase 2 — Generate competing hypotheses

Write down **at least 3 distinct hypotheses before investigating any of them**. They must come from different categories — do not write three variants of the same idea. Categories to draw from:

- Code defect (logic, edge case, off-by-one, wrong assumption)
- Configuration or environment difference
- Data/input: unexpected shape, encoding, volume, nulls
- Timing/concurrency: race, ordering, timeout, retry
- External dependency: API change, outage, version bump
- Resource limits: memory, disk, connections, rate limits
- The report itself: is the observed "failure" a misread, a flaky test, a stale artifact?

For each hypothesis record: what it predicts you should observe, and **what evidence would disprove it**. If you cannot name disproving evidence for a hypothesis, it is not testable — rewrite it.

## Phase 3 — Test by falsification

Investigate the hypotheses, cheapest test first. For each one, actively look for the disproving evidence you named — not for confirmation.

- Read the actual failing code path end to end; do not stop at the first suspicious file.
- Prefer direct evidence: reproduce it, add a log, run the failing case in isolation, bisect.
- Explicitly mark each hypothesis: **REJECTED** (with the evidence), **SURVIVING**, or **UNTESTED**.
- If your leading hypothesis survives but you tested nothing else, test at least one alternative before proceeding. (See directive 2.)

## Phase 4 — Five Whys, with the evidence rule

Only now, take the surviving hypothesis and drill down. Ask "why" repeatedly (typically ~5 levels), with one hard rule:

**Each "why" must be answered by observed evidence — a log line, a code path you read, a reproduced behavior, a diff. An answer justified only by plausibility must be marked `[UNVERIFIED]`, and an unverified link invalidates every deeper link in the chain until you verify it.**

Example shape:

```
S1: Checkout requests return 500 since Tuesday.
Why? → OrderService throws NullPointerException at line 142. [stack trace]
Why? → `customer.address` is null for guest checkouts. [reproduced with guest account]
Why? → Migration 0087 stopped backfilling addresses for guests. [diff]
Why? → The migration assumed all carts have a registered user. [code + PR discussion]
Why? → Guest checkout launched after this code path was written; no test covers it. [git history, test grep]
```

Stop descending when the answer becomes a process/design decision (missing test, wrong assumption, absent validation) or leaves your ability to verify. Note branch points: if a "why" has two evidenced answers, follow both — real incidents often have a defect *and* a missing safeguard.

## Phase 5 — Verify coverage and confirm

Before declaring the root cause:

1. **Symptom coverage check**: walk the symptom list S1..Sn and confirm the cause explains each one, including the scope boundaries (why the unaffected cases are unaffected). List any symptom it does not explain.
2. **Confirmation test** (when feasible): reproduce the failure via the cause, or show the failure disappears when the cause is neutralized (revert, flag off, fixed input). If confirmation is not feasible, say so and cap your confidence.

## Phase 6 — Report

Deliver in this structure:

```markdown
## Root cause
[One or two sentences. Confidence: CONFIRMED (reproduced/verified) | PROBABLE (evidence-backed, not reproduced) | SPECULATIVE.]

## Symptoms
S1..Sn, verbatim.

## Cause chain
The Five Whys chain, each link with its evidence. Trigger vs. root cause distinguished.

## Hypotheses considered
Each hypothesis with its status (REJECTED + evidence / SURVIVING / UNTESTED). This section is mandatory — it is the proof that alternatives were examined.

## Unexplained
Any symptom or observation the root cause does not account for. "None" only if genuinely none.

## Recommended fix
Fix for the root cause, plus (if distinct) mitigation for the trigger and the missing safeguard that let the defect reach this point. Do not implement unless asked.
```

## Anti-patterns (self-check before reporting)

- Did I settle on the cause within the first few tool calls? If yes, which alternative did I reject, and with what evidence?
- Is my "root cause" actually the proximate cause (the throwing line) rather than why that state arose?
- Did I stop at the first suspicious diff without confirming it produces these exact symptoms?
- Does my chain contain an `[UNVERIFIED]` link that everything below depends on?
- Am I explaining the symptoms I found first and ignoring the inconvenient ones?
