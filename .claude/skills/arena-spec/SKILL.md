---
name: arena-spec
description: "Build a spec through the arena: generate N candidate design ideas with subagents, develop and state the scoring criteria, run the arena, and integrate the best portions of the top ideas into one recommendation written up as a spec. Use for '/arena-spec' or any request to arena a spec."
disable-model-invocation: true
---

# arena-spec

Build a spec whose design earned its place: fan out candidate ideas, score them against stated criteria, and integrate the best portions of the top ideas into one recommendation — then write that recommendation up as a spec.
This skill supersedes any brainstorming skill; it *is* the idea-generation step, and it ends in an artifact.

**Announce at start:** "Using arena-spec to build the spec."

## How it composes

The `arena` skill owns the mechanics (framing, fan-out, cross-judge, base pick, graft, verify); the `spec` skill owns the artifact (contents, self-review, save path).
This skill sets the arena's parameters for spec work:

- **The task each candidate gets:** the operator's problem statement, verbatim, plus any grounding the operator attached (files, repo-relative paths). Candidates propose a design idea — the shape of a solution with its rationale and rejected alternatives — not a full spec and not an implementation.
- **N = 5 candidates** by default; the operator's request overrides.
- **Scoring criteria:** develop them for *this* problem before fan-out, and state them in the final output — the operator grades the arena partly by whether the criteria were right.
- **Synthesis:** pick the strongest idea as the base and integrate the best portions of the other top ideas into it; the synthesis note records what was taken from where and what was rejected.

## Output

One spec at `docs/specs/YYYY-MM-DD-<name>-spec.md`, written per the `spec` skill (frontmatter, contents, self-review), whose Design and Decisions-and-alternatives sections carry the synthesized recommendation — the stated scoring criteria and the losing ideas become the alternatives record, which is the highest-signal part.
Candidate material stays in `/tmp/_candidates/` and is never committed.
