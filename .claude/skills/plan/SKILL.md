---
name: plan
description: "Write an implementable plan for a multi-step task: exact files, complete code, bite-sized verifiable steps. 'Plan', 'make a plan', 'write a plan', 'build a plan', and plan mode all mean this artifact — never a spec or design doc first unless the operator explicitly asks for one. Use for '/plan' or any request to plan work."
---

# plan

Write the plan for an engineer with zero context for this codebase and questionable taste: which files to touch for each task, the actual code, how to test it, bite-sized tasks throughout.
Assume a skilled developer who knows almost nothing about our toolset, problem domain, or good test design.

**Announce at start:** "Using plan to write the implementation plan."

**"Plan" means this artifact — plans only.**
Do not write a spec or design doc first; a spec is a separate deliverable the operator requests by name (`/spec`, the `spec` skill — saved to `docs/specs/`; `architect` for shape sketches).
Neither artifact is a prerequisite for the other.
If the request is genuinely too ambiguous to plan, ask the single question that unblocks you — don't manufacture a design phase.

**Save to:** `docs/plans/YYYY-MM-DD-<feature-name>-plan.md` (plans never go in `docs/specs/`) with the docs-contract frontmatter:

```yaml
---
status: draft
date: YYYY-MM-DD
---
```

Lifecycle (docs contract, `docs/AGENTS.md` §Lifecycle — the implementer's job, not the plan's): `building` when implementation dispatches its first unit, `validating` when the work lands and is handed to the operator for manual validation, then `status: implemented` + move to `docs/implemented/` once the operator confirms.
If this plan implements an existing spec, set that spec's frontmatter to `status: approved` in the same landing — a spec with a plan against it is approved.

## Scope check

If the work covers multiple independent subsystems, split it — one plan per subsystem, each producing working, testable software on its own.

## File structure first

Before defining tasks, map which files will be created or modified and what each is responsible for — this is where decomposition gets locked in.

- Each file has one clear responsibility; prefer smaller, focused files over large ones that do too much.
- Files that change together live together; split by responsibility, not technical layer.
- In this codebase, placement is not a choice: check the root `AGENTS.md` "Where things go" table for every file; a row mismatch is a design question for the operator, not a license to improvise.

## Task right-sizing

A task is the smallest unit that carries its own check and is worth a fresh reviewer's gate.
Fold setup, configuration, and docs steps into the task whose deliverable needs them; split only where a reviewer could reject one task while approving its neighbor.
Each task ends with an independently verifiable deliverable.

**Each step is one action (2–5 minutes):** write the failing test — run it to confirm it fails — implement the minimal code — run to green — commit.

## Plan document header

```markdown
# [Feature Name] Implementation Plan

> **For agentic workers:** execute with the `implement` skill, task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** [One sentence describing what this builds]

**Architecture:** [2-3 sentences about approach]

## Global constraints

[Project-wide requirements that bind every task — version floors, naming and copy rules, invariants touched — one line each, exact values verbatim from the request or spec. Every task implicitly includes this section.]
```

## Task structure

````markdown
### Task N: [Component Name]

**Files:**
- Create: `exact/path/to/file.py`
- Modify: `exact/path/to/existing.py:123-145`
- Test: `tests/exact/path/to/test.py`

**Interfaces:**
- Consumes: [what this task uses from earlier tasks — exact signatures]
- Produces: [what later tasks rely on — exact function names, parameter and return types.
  A task's implementer sees only their own task; this block is how they learn the names and types neighboring tasks use.]

- [ ] **Step 1: Write the failing test**

```python
def test_specific_behavior():
    result = function(input)
    assert result == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/path/test.py::test_name -v`
Expected: FAIL with "function not defined"

- [ ] **Step 3: Write minimal implementation**

```python
def function(input):
    return expected
```

- [ ] **Step 4: Run test to verify it passes**

- [ ] **Step 5: Commit**
````

## No placeholders

Every step contains the actual content an engineer needs. These are **plan failures** — never write them:

- "TBD", "TODO", "implement later", "fill in details"
- "Add appropriate error handling" / "add validation" / "handle edge cases"
- "Write tests for the above" (without actual test code)
- "Similar to Task N" (repeat the code — tasks may be read out of order)
- Steps that describe what to do without showing how (code blocks required for code steps)
- References to types, functions, or methods not defined in any task

## Remember

- Exact file paths always; complete code in every step; exact commands with expected output.
- DRY. YAGNI. TDD. Frequent commits.
- Use the operator's exact wording — requirements, copy, signatures, acceptance criteria — verbatim wherever they gave it.

## Self-review

After writing the complete plan, check it with fresh eyes — inline, not a subagent dispatch:

1. **Requirement coverage:** can you point to a task for each requirement in the request? List any gaps.
2. **Placeholder scan:** search for the red-flag patterns above; fix them.
3. **Type consistency:** do signatures and names in later tasks match what earlier tasks defined? `clearLayers()` in Task 3 but `clearFullLayers()` in Task 7 is a bug.
4. **Placement:** every file in the plan sits in its "Where things go" row.
5. **Workaround scan (reverse-entropy, `engineering-principles` §2):** find every task that adds a wrapper, adapter, shim, alias, compat flag, or parallel path around landed code — including any "X is off-limits" constraint the plan invents to shrink its own blast radius.
   Each one either carries its deletion step inside this plan (time-boxed, `engineering-principles` §5) or gets redrawn as the direct refactor of what it routes around.
   "Too many call sites" is never the reason: count them, write the mechanical recipe, batch the migration tasks.
   A workaround that survives this scan needs a cited operator instruction in its task or decision row — "be surgical" in the request is exactly that instruction: honor it, plan the minimal diff, and record the deferred refactor as entropy debt in the decisions table.
   The scan runs in reverse too: a task that stands a new solution beside a canonical one is the same error from the other side — a redesign deepens the existing shape or migrates it to deletion within this plan, never siblings it.

Fix issues inline and move on.
For an independent pre-implementation review, the operator runs the `review-plan` skill — don't self-dispatch it.
