---
name: review-plan
description: "Pre-implementation review of a plan document from the implementer's seat: completeness, buildability, placement, requirement coverage. Findings loop back into the plan file. Use for '/review-plan <plan file>' or requests to review a plan before implementing it."
disable-model-invocation: true
---

# review-plan

Review a plan the way its implementer will hit it: could a skilled engineer with zero context for this codebase execute every task without getting stuck, guessing, or building the wrong thing?
This is the plan-side twin of the `review` skill (which reviews code); `/review` pointed at a plan document routes here.
It runs before implementation, typically in a fresh session of the model that will implement.

Read the plan file, the root `AGENTS.md` (placement table and invariants), and the area rules for every directory the plan touches.
If the plan names a source request or spec, read that too — coverage is judged against it.

## What to check

| Category | What to look for |
|---|---|
| Completeness | TODOs, placeholders, "similar to Task N", steps that describe without showing code, types or functions referenced but defined in no task |
| Buildability | Could you execute each step exactly as written? Missing commands, wrong paths, steps that assume unstated context |
| Interfaces | Do later tasks consume exactly what earlier tasks produce — same names, signatures, types? |
| Placement | Every file in its "Where things go" row; no forked canon, no parallel path beside a canonical mechanism |
| Coverage | Every requirement in the source request maps to a task; nothing in the plan that no requirement asked for |
| Right-sizing | Tasks independently verifiable; checks per unit, not batch-verified at the end |

## Calibration

Only flag what would cause real problems during implementation — an implementer getting stuck, building the wrong thing, or violating an invariant.
Wording, style, and nice-to-haves are not findings.
A few high-conviction findings beat a list of nits; a clean review is one line.

## Output

Report findings as **blocker** (implementer cannot proceed or will build the wrong thing) / **gap** (missing requirement or interface mismatch) / **note**, each with the task/step reference and what specifically to change.
End with: **Ready to implement** | **Needs revision**, one sentence of reasoning.

## Incorporate

When the operator asks for the feedback to be applied (same session or by handing findings to another agent): edit the plan file directly, re-run the `plan` skill's self-review checklist on the result, and report what changed.
Findings are verified against the plan text before acting — push back on wrong ones with evidence rather than blindly applying them.
