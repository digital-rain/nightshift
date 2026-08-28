---
name: arena
description: "Spawn N parallel candidates at the same task, pick a base, graft the strongest parts of the losers into it. Use for /arena, 'arena this', 'throw it in the arena', or when one attempt at a non-trivial artifact would lock in the wrong shape."
disable-model-invocation: true
---

# Arena

Fan out N parallel attempts at the same task, read every candidate end to end, pick the strongest as the base, graft the best ideas from the others into it, verify the result.
Open a todolist with one entry per phase before launching; the arena runs autonomously and the list keeps phases from silently disappearing.

## A. Frame

The N candidates receive the same prompt, so the prompt is the contract — get it right before spawning.

1. State the artifact each candidate produces.
2. Derive the rubric: 3–6 concrete, gradeable criteria for *this* task ("adds a `--dry-run` flag that skips writes", not "code is correct").
   The rubric is the picker's tool; candidates see only the task.
3. Pick the runners: 3 by default, more when the arena spans multiple design directions.
   Prefer different model families when the harness lets you choose; same model N times is fine when the work is generation-bound rather than judgment-sensitive.
   Never hardcode model ids — use what your harness offers; with no subagents at all, produce the candidates yourself sequentially from genuinely different starting frames.
4. Assign output paths: each candidate writes to its own directory under `/tmp/_candidates/<slug>/candidate-<n>/` — never into the repo.
   Use a git worktree per candidate only when a candidate must run the build or tests to produce its artifact.
   N candidates writing one shared path is shared mutable state; don't.

## B. Fan out

Spawn all N in one message, in the background, each with the task, the shared grounding, its own output path, and instructions to produce the artifact **plus a short rationale naming the alternatives it considered and rejected**.
The rationale is mandatory — without it you cannot tell whether a candidate's structure is principled or accidental.
If a candidate produces nothing, proceed with N−1 and note the dropout.

## C. Cross-judge

After all candidates complete, spawn one read-only judge on a different model family from your own.
It gets the rubric and the candidate paths, scores each criterion, and recommends a base with rationale.
Never spawn the judge while candidates are still writing.

## D. Pick a base

Read every candidate end to end — skimming surfaces only the candidate whose surface looks most familiar.
Score criterion by criterion, compare with the judge: agreement confirms the pick; disagreement means bias or an ambiguous rubric — read both rationales before deciding.
Pick for the future maintainer: the shape easiest to extend without breaking invariants; prefer the cleaner boundary when tied.

## E. Graft

Walk each losing candidate once more; the signal is usually one or two things per candidate.
Fold each graft in by hand so the result stays coherent under one mental model — never paste mechanically.
Record what was grafted, from where, and what was rejected and why; the rejection notes are the highest-signal part of the record.

- All candidates converge on one shape → strong agreement signal; ship the consensus, no graft needed.
- Candidates wildly diverge → Phase A was under-specified; reframe and re-run rather than averaging.

## F. Verify

The synthesized artifact faces the same scrutiny as any other output; the arena does not earn a pass.
If verification surfaces a miss, either the frame was wrong (re-frame, re-run) or a losing candidate caught it and the graft was missed (back to E).

## Outputs

One synthesized artifact plus one short synthesis note (base, grafts with sources, rejections, dropouts, verification result).
Only these two land in the repo; candidate directories stay in `/tmp/_candidates/` and are never committed — quote a losing candidate inline rather than linking its `/tmp` path.
