# Nightshift — Workflows (`workflow:` multi-step task execution)

**Subject:** A task frontmatter field that runs a brief through a named, declarative multi-step *workflow* — a small state machine of agent steps, each its own attempt with its own model, prompt, inputs, and budget — instead of the single-shot brief→run→land path. The first shipped workflow is `plan-review-implement`: a planning model writes an implementation plan, the implementing model reviews it, the planner reconciles the feedback, and a fresh instance of the implementing model implements the revised plan.
**Status:** Proposed — for implementation. Where this doc and the code disagree once implemented, the code governs and this doc should be updated.
**Default:** No workflow. Existing paths — single-shot, `enhance`, `split`, retry/resolve — are untouched; a task without `workflow:` frontmatter behaves byte-identically to today.
**Relationship:** `enhance` remains the light rewrite for small tasks (a workflow task skips create-time enhance — the plan step subsumes it). `split` remains brief decomposition. The goal-loop spec (`2026-07-04-loop-tasks.md`) is a *different* iteration axis (one goal, N runs, done-check); workflows may later express it, but this spec neither depends on nor changes it.

---

## 0. The one idea

Nightshift's sweet spot is bug-fix-sized briefs because one cold agent run is the whole pipeline. Larger features want the pattern operators already run by hand in desktop tools: **plan with a strong planning model, have the implementing model review the plan, reconcile, then implement with a fresh instance** — every handoff carried by documents, not by a live session.

A workflow makes that pattern (and others like it) engine-native:

- A **workflow definition** is declarative data — a named sequence of steps with signal-routed edges — shipped as an asset and interpretable without new engine code per workflow.
- Each **step** is one ordinary attempt: leased, dispatched through the existing capability-matched scheduler, telemetered, retried and quarantined by the existing ladders. The step — not the task — pins the model, so planning and implementation route to different models (and, if need be, different workers) with zero new routing machinery.
- **Document steps** (plan, review, revise) need no worktree and land nothing: they run read-only against the worker's base checkout and return one markdown artifact in the submit payload, which the manager commits into the tasks repo next to the brief. Every stage's full context is on disk at the manager; a crashed step re-dispatches from the last committed artifact. No sticky worker, no session as source of truth.
- **Code steps** (implement) are today's path unchanged: worktree, backend run, validate, submit, land.

The engine's job is the cursor (which step is next), routing (which model serves it), artifact custody, honest budgets, and termination. It never parses plan content; convergence and quality live in the step prompts.

## 1. Motivation

Nightshift is growing from an overnight batch runner into a development companion — an operator running many concurrent design/plan/implement threads. What the workflow engine adds over hand-driving those sessions in a desktop tool: durable artifacts, automatic handoffs, per-step model routing, turn/iteration budgets, and the existing land/validate/retry rails. Latency is therefore a first-class cost (not a batch afterthought): step **chaining** (§7.4) is core to v1, not an optimization.

## 2. Today's state (what this spec changes)

- One attempt = one `backend.run` (`worker/execute.py — execute_work_order`); no notion of stages.
- One `model:` per brief; the scheduler (`manager/scheduler.py — TaskCandidate.model`, `WorkerFilter._model_ok`) routes the whole task on it.
- Enhancement is a create-time, manager-side, tool-less completion (`enhance.py`) — it cannot read the repo and cannot be revisited after a review.
- A confirmed land drops a non-evergreen brief (`transitions._landed_transition — drop_brief=not policy.evergreen`); there is no retained multi-attempt task shape besides `evergreen` and the unimplemented goal loop.
- Sentinel plumbing exists for exactly one token (`prompts.extract_blocked_reason` — `NIGHTSHIFT_BLOCKED:`).

## 3. Workflow definitions

A workflow is a JSON document: shipped ones in `assets/workflows/<name>.json`, operator-defined ones in `.nightshift/workflows/<name>.json` (operator shadows shipped, resolved via the `_paths` asset-vs-state convention). Loaded and schema-validated at manager start; an invalid definition fails the load loudly.

### 3.1 Vocabulary

Deliberately small, so every workflow stays inspectable and a future editor stays tractable. Two step kinds; decisions are **signal-routed edges** on steps rather than a third node type; control is `next`, `signals`, and `max_visits`.

```json
{
  "name": "plan-review-implement",
  "steps": [
    {
      "id": "plan",
      "kind": "doc",
      "role": "planner",
      "prompt": "workflow-plan.md",
      "inputs": ["brief"],
      "output": "plan",
      "max_turns": 30,
      "signals": { "plan-trivial": "implement" }
    },
    { "id": "review", "kind": "doc", "role": "implementor", "prompt": "workflow-review.md",
      "inputs": ["brief", "plan"], "output": "review", "max_turns": 20,
      "signals": { "review-clear": "implement" } },
    { "id": "revise", "kind": "doc", "role": "planner", "prompt": "workflow-revise.md",
      "inputs": ["brief", "plan", "review"], "output": "plan", "max_turns": 30 },
    { "id": "implement", "kind": "code", "role": "implementor",
      "inputs": ["brief", "plan"], "max_turns": null }
  ]
}
```

| Field | Meaning |
|---|---|
| `kind` | `"doc"` — no worktree, read-only, produces one artifact; `"code"` — today's full worktree→validate→land path. |
| `role` | Model binding key resolved per §3.2. |
| `prompt` | Asset name under `assets/prompts/` (doc steps). Code steps use the standard `nightshift-local.md` charter with artifact injection (§7.2). |
| `inputs` | Artifact names materialized for the step. `"brief"` is always available; others must be a prior step's `output`. |
| `output` | Artifact name the step must produce (doc steps only). Re-using a name **overwrites** the artifact (revise's output *is* the plan; superseded versions live in the tasks repo's git history). |
| `max_turns` | Per-step turn budget handed to the backend; `null` = unbounded. Overrides the task/queue `turns` for this step. |
| `signals` | Map of sentinel token → step id (or `"$end"`). The routed-to step runs *instead of* `next`. |
| `next` | Explicit successor (defaults to the following step in the list; the last step's default is `"$end"`). |
| `max_visits` | Optional int: how many times this step may be *entered* before the task quarantines (§6.4). Default 1 for steps not on a cycle; required (validation error if absent) for any step reachable from itself. |

`$end` from a code step means land-and-consume as today. `$end` from a doc step terminates the task without a land: the brief is marked `completed` (retained for operator review, like the loop spec's completion shape) and the final artifact is kept with it — the shape a verify step needs when it finds no gaps (§10). A workflow whose *default* path never reaches a code step is a definition error (there would be nothing to implement).

### 3.2 Role → model resolution

Resolved manager-side at dispatch, per step, first match wins:

| Role | Resolution order |
|---|---|
| `implementor` | brief `model:` → queue `config.json` `"workflow_models": {"implementor": …}` → `cfg.default_model` |
| `planner` | brief `planner_model:` → queue `"workflow_models": {"planner": …}` → manager `planner_model` (new `OperatorConfig` field, default `""` = fall through) → `cfg.default_model` |
| any other key | queue `workflow_models` → manager `planner_model` fallback does **not** apply; unresolved → the task is marked blocked with an authoring-error reason |

The resolved id lands in the work order's existing `config.model` slot, so the worker's resolution path (`WorkerConfig.resolve_model`, provider split, `require_backend`) is unchanged.

## 4. Frontmatter contract

| Field | Type | Owner | Meaning |
|---|---|---|---|
| `workflow` | string (default unset) | operator | Workflow name; unset = today's single-shot path. |
| `planner_model` | string (optional) | operator | Per-task planner role override (§3.2). |
| `workflow_step` | string | **engine** | Current step id. Written as a transition effect through the tasks-repo executor (the `FrontmatterFlag` lane, extended to string fields). Absent until first dispatch. |
| `workflow_visits` | string | **engine** | Compact visit counters, e.g. `plan:1,review:1,implement:2`. Operator-visible progress; never hand-edited. |

Exclusions (UI-prevented, engine-ignored): `workflow` + `split`, `workflow` + `loop`, `workflow` + `evergreen`. `enhance` on create is skipped for workflow tasks (the plan step subsumes it); the create UI's segment becomes **Off | Enhance | Workflow** with a workflow picker.

## 5. Artifacts

**Custody: the manager, in the tasks repo.** A doc step's document rides the submit payload (new `Outcome.document: str | None`, capped at 256 KB — over-cap is a `worker_error`); the manager commits it to `<tasks_rel>/<task>.artifacts/<output>.md` through the same tasks-repo executor lane that writes frontmatter flags. Consequences, in order of why they were chosen:

- **Recoverable by construction.** Brief + committed artifacts are the *complete* context for every step. A failed/expired step re-dispatches against the same inputs; nothing lives only in an agent session. This is also why the planner does **not** need to be a sticky instance for revise: the revise prompt is brief + plan + review, fully reconstructable. (If a revision *needed* the planner's private session memory, the plan was underspecified — the implementor would have hit the same gap.)
- **No worktree churn.** Doc steps never touch the target repo's worktrees; the target repo sees nothing until implement.
- **Size-safe.** Plans can be large; they never ride the brief body or the work order twice — a step's work order embeds only that step's declared `inputs`.
- Superseded plan versions are ordinary git history on the tasks repo.

Materialization on the worker mirrors the brief: each input artifact's text is embedded in the work order and written to the run-scratch dir (`materialize_brief`'s sibling, `materialize_artifacts`) — read-only files outside any worktree, paths injected into the prompt header.

Artifacts are deleted with the brief on terminal consumption (land of the final step / operator delete), and retained alongside it on quarantine.

**Repo convention note:** operators who keep specs in-repo (e.g. `docs/specs/`) get that by *authoring it in the plan prompt's charter* ("include copying the plan to docs/specs/<task>.md in the implementation") — a convention, not engine machinery.

## 6. Engine semantics (manager)

### 6.1 Scheduling

`build_candidates` gains workflow awareness: for a workflow task, the candidate's `model` is the **current step's** resolved model (§3.2) and the candidate carries `(workflow, step)`. Everything downstream — `WorkerFilter._model_ok`, `unroutable`, dedication, priorities, `after:` — applies unchanged. A workflow whose current step's model no live worker advertises goes blocked with the existing `no live worker provides model '…'` reason.

### 6.2 Dispatch

`build_work_order` adds a `workflow` block to the config blob:

```json
"workflow": {
  "name": "plan-review-implement", "step": "plan", "kind": "doc",
  "prompt": "workflow-plan.md", "output": "plan",
  "artifacts": { "plan": "…full text…" },
  "signals": ["plan-trivial"]
}
```

plus the step's `max_turns` overriding the blob's. `kind: "code"` steps omit `prompt`/`output` and carry only `artifacts`.

### 6.3 Step transitions

New pure function `transitions.on_workflow_step` (fed by `on_submit`, which branches to it when the attempt's work order carried a workflow block):

- **Doc step, completed** with a document → attempt `NO_CHANGE`-analog terminal state (`AttemptState.NO_CHANGE`, `result_line` = the step + signal), effects: commit artifact, advance `workflow_step` per the extracted signal (§7.3) or `next`, increment `workflow_visits`, `Progress.RESET`. Brief retained. `queue_changed` + `task_result` events as usual.
- **Doc step, completed without a document** → `worker_error` through the normal retry ladder; the cursor does not move.
- **Doc step, blocked / error** → today's `_blocked_transition` / `_error_transition` verbatim; the cursor does not move; retry/hold/quarantine ladders apply per failure kind.
- **Code step, completed & landable** → `GitPhase.LAND` as today. On a confirmed land: if the step's route is `$end`, the standard `_landed_transition` with `drop_brief=True` (artifacts dropped with it); otherwise (a mid-workflow code step, e.g. inside a verify loop) the landed transition runs with `drop_brief=False` and the cursor advances.
- **Code step, validation failure** → today's `VALIDATION_ERROR` → HOLD, branch preserved, cursor unmoved.

### 6.4 Budgets and termination

- Entering a step past its `max_visits` quarantines the task with reason `workflow budget exhausted at '<step>' after N visits` — quarantine, not `failed`, for the same reason as the loop spec: budget exhaustion is an operator decision, not a retry.
- Step-level failures consume the *existing* ladders (`attempts_without_progress`, backoff, quarantine threshold) — `max_visits` counts successful entries, not retries of a failing step.
- Operator delete / quarantine-clear behave as today; clearing a quarantined workflow task resumes at its recorded `workflow_step`.

## 7. Worker semantics

### 7.1 Doc steps

`execute_work_order` branches on `workflow.kind == "doc"`:

- **No worktree, no preflight.** `cwd` is the target repo's base checkout (`workspace/<repo>`), synced as usual by the manager before dispatch; the prompt charter declares the run read-only.
- The header injects `$OUTPUT_FILE` — a run-scratch path outside the repo. The agent writes the document there; stdout is for logs/sentinels only. After the run the worker reads it into `Outcome.document` (missing/empty → `worker_error`).
- **Clean-checkout guard:** after the run, `git status --porcelain` on the base checkout must be empty. If an agent wrote to the repo anyway, the worker resets it (`git checkout -- . && git clean -fd` — the base checkout is nightshift-managed on worker boxes) and fails the step `worker_error` with the dirty file list as the reason. Never submit a doc alongside a dirtied checkout.
- Validation, publish, and landing are skipped; `landable=False` always.

### 7.2 Code steps

Unchanged path, two additions: input artifacts are materialized next to the brief scratch, and the prompt header names them (`The PLAN file is: <path>`). The `nightshift-local.md` charter gains a conditional paragraph: when a plan is present, it is the spec — follow it, and trust its context manifest (§8.1) before exploring.

### 7.3 Signals

One generic sentinel, scanned exactly like `NIGHTSHIFT_BLOCKED` (last match wins, whole captured log):

```
NIGHTSHIFT_SIGNAL: <token>
```

`prompts.extract_signal` mirrors `extract_blocked_reason`; the worker puts the token on `Outcome.signal`. The manager honors it only if the step's `signals` map declares it — undeclared tokens are logged and ignored (the step routes via `next`). Reference-workflow tokens: `plan-trivial` (the brief is small; skip review/revise — the ceremony-escape hatch for over-selected workflows) and `review-clear` (no questions; skip revise).

### 7.4 Chaining (latency)

When a submit advances the cursor, the manager checks the submitting worker's registered capabilities (`manager/registry.py` checkin data) against the next step's resolved model. On a match it builds the next work order **in the submit response** (`response.next_order`); the worker loop processes it immediately — no re-poll, no scheduler round-trip. This is *affinity, not stickiness*: if the worker doesn't match, or dies, the step enters normal scheduling on the next poll. Consecutive doc steps chain with near-zero setup (no worktree). A four-step flow typically costs one or two poll cycles instead of four.

Chaining chains the **lease, never the context**: every step's inputs are exactly its declared artifacts, identical whether chained or cold.

### 7.5 Session resume (opportunistic, worker-local)

When a chained step has the same `(task, role)` as the step the worker just ran, and the backend supports resumption (`claude --resume`, `cursor-agent --resume`), the worker *may* resume that session instead of cold-starting — the plan→…→revise planner hop being the payoff (prompt-cache hits instead of a cold context rebuild). Three hard rules:

1. Within one task only, matching role — never across tasks (that is where unbounded context accretion would come from; forbidden outright).
2. A hint, never a dependency: the step's prompt carries the full declared inputs regardless, so resumed and cold runs see identical inputs and recovery semantics are untouched.
3. Session ids live only in the worker's `LocalStore`, dropped on task end or process restart. Nothing crosses the wire; the manager is unaware.

## 8. Token discipline

### 8.1 The context manifest

The single biggest cost lever: the planner explores the codebase **once**, and the plan carries the result. The plan prompt's charter requires a `## Context manifest` section — files involved, key functions/classes with line references, conventions that apply, the shape of the change. Review and implement charters instruct: *read the manifest files first; trust the manifest before exploring.* A plan that cannot produce a manifest is not concrete enough to leave the plan step. (Forward note: a tighter what/where manifest schema is the substrate for future harness-side local edits — v1 keeps it prose-with-references.)

### 8.2 Prompt layout for cacheability

Step prompts follow the charter discipline `agent-charter.md` already models: byte-stable charter text first, task-varying header/artifacts last, so implicit prefix caching hits across runs of the same step. The nightshift harness backend may additionally pin explicit cache breakpoints (charter + manifest) across its turns. Cache-affinity scheduling (tiebreak toward the worker that most recently ran the same model) is noted as a follow-up, not v1.

### 8.3 Artifact discipline

A step receives **only** its declared `inputs` — implement gets brief + revised plan, not the review, not superseded plan versions. This is what keeps verify-loop iterations O(1) in context instead of accreting.

## 9. UI

- Create panel: the enhance segment becomes **Off | Enhance | Workflow**; choosing Workflow reveals a definition picker (shipped + operator definitions) and the optional planner-model override.
- Queue rows / detail: a workflow task shows a step badge — `plan → review → revise → implement` with the cursor highlighted and `workflow_visits` counts (e.g. `implement 2/4`); artifacts are viewable from the detail pane (rendered markdown, read-only).
- Quarantine reasons and blocked reasons surface as today.

## 10. The verify-loop extension (validates the abstraction; not v1)

Steps 6–9 of the operator pattern are **definition data only** — no engine change:

```json
{ "id": "verify",   "kind": "doc",  "role": "implementor", "prompt": "workflow-verify.md",
  "inputs": ["brief", "plan"], "output": "gaps",
  "signals": { "verify-clear": "$end" }, "next": "gap-plan" },
{ "id": "gap-plan", "kind": "doc",  "role": "planner", "prompt": "workflow-gap-plan.md",
  "inputs": ["brief", "plan", "gaps"], "output": "plan", "next": "implement" }
```

with `implement.next = "verify"` and `max_visits` on `implement`/`verify`. Mid-workflow implement steps land per §6.3 (brief retained until `$end`). Gap analysis is mechanical enough that `gap-plan` could bind a cheaper role — the vocabulary already allows it. If the engine cannot express this without code changes, the vocabulary is wrong; this section is the acceptance test for §3.

## 11. Non-goals

- **A workflow editor.** The JSON format is designed to make one tractable later; v1 ships definitions as files.
- **Free-form predicates.** Decisions branch on declared sentinel tokens and the engine's own outcomes (validation, land) — never on engine-evaluated expressions over artifacts.
- **Cross-task or manager-tracked sessions.** §7.5 is the entire session story.
- **Replacing enhance / split / loop / the single-shot path.** Converging defaults onto the workflow engine is a possible later cleanup, after the engine earns trust; nothing in v1 rewrites a working path.
- **Non-markdown artifacts.** Artifacts are named markdown documents, only.

## 12. Touch points (implementation checklist)

- New `src/nightshift/workflows.py` — definition load/validate/resolve (roles, steps, edges), pure; `assets/workflows/plan-review-implement.json`.
- `config/manager.py` — `planner_model` field (settings registry, env `NIGHTSHIFT_PLANNER_MODEL`); queue `config.json` keys `workflow`, `workflow_models` (documented, no schema change needed).
- `lifecycle.py` — `Outcome.document`, `Outcome.signal`; `SubmitPolicy` gains the step context (or a sibling `WorkflowPolicy`).
- `transitions.py` — `on_workflow_step` + the `drop_brief` generalization (`terminal and not evergreen`); artifact-commit and frontmatter-cursor effects on `TaskEffects`.
- `manager/scheduler.py` — per-step model on `TaskCandidate` for workflow tasks.
- `manager/work_orders.py` — the `workflow` config block, artifact embedding.
- `manager/api_worker.py` — submit: signal honoring, artifact commit dispatch, cursor advance, `next_order` chaining against the registry.
- `worker/execute.py` — doc-step branch (no worktree, `$OUTPUT_FILE`, clean-checkout guard); artifact materialization for code steps (`task_files.materialize_artifacts`).
- `worker/loop.py` + `local_store.py` — `next_order` processing; session-id memory for §7.5.
- `prompts.py` — `extract_signal`; header injection for artifacts/`$OUTPUT_FILE`; new assets `workflow-plan.md`, `workflow-review.md`, `workflow-revise.md`; the conditional plan paragraph in `nightshift-local.md`.
- `assets/ui/app.js` — create segment + picker, step badge, artifact viewer.
- `docs/user/configuration-reference.md` — frontmatter (`workflow`, `planner_model`, engine fields) + queue keys.
- Tests — definition validation (cycle requires `max_visits`; doc `$end` rejected); role resolution ladder; scheduler per-step routing + unroutable; doc-step execute (output file, clean-checkout guard, missing doc); signal extraction and undeclared-token ignore; cursor transitions incl. every failure shape leaving the cursor unmoved; chaining handoff + affinity miss; artifact commit/overwrite/drop; budget quarantine; verify-loop definition runs end-to-end on the engine unmodified.

## 13. Implementation order

1. `workflows.py` + definition asset + tests (pure, no wiring).
2. Lifecycle/transitions: outcome fields, `on_workflow_step`, retention generalization + tests.
3. Manager: scheduler per-step model, work-order block, submit wiring, artifact custody + tests.
4. Worker: doc-step branch, materialization, signals + tests.
5. Chaining (`next_order`) + tests; session resume last (pure optimization).
6. Config + UI + docs.
