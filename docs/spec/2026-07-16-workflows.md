# Nightshift — Workflows (`workflow:` multi-step task execution)

**Subject:** A task frontmatter field that runs a brief through a named, declarative multi-step *workflow* — a small state machine of agent steps, each its own attempt with its own model, prompt, inputs, and budget — instead of the single-shot brief→run→land path. The first shipped workflow is `plan-review-implement`: a planning model writes an implementation plan, the implementing model reviews it, the planner reconciles the feedback, and a fresh instance of the implementing model implements the revised plan.
**Status:** Proposed — for implementation. Where this doc and the code disagree once implemented, the code governs and this doc should be updated.
**Default:** No workflow. Existing paths — single-shot, `enhance`, `split`, retry/resolve — are untouched; a task without `workflow:` frontmatter behaves byte-identically to today.
**Relationship:** `enhance` remains the light rewrite for small tasks (a workflow task skips create-time enhance — the plan step subsumes it). Standalone `split: true` keeps its meaning; workflows can also *end* in a decomposition (`kind: "split"` step, §3.1) so children are cut from a reviewed plan. `evergreen` composes: the brief survives completion and the workflow resets each cycle (§6.5) — the janitor shape. The goal-loop spec (`2026-07-04-loop-tasks.md`) is a *different* iteration axis (one goal, N runs, done-check); composition with workflows is deferred past v1.

---

## 0. The one idea

Nightshift's sweet spot is bug-fix-sized briefs because one cold agent run is the whole pipeline. Larger features want the pattern operators already run by hand in desktop tools: **plan with a strong planning model, have the implementing model review the plan, reconcile, then implement with a fresh instance** — every handoff carried by documents, not by a live session.

A workflow makes that pattern (and others like it) engine-native:

- A **workflow definition** is declarative data — a named sequence of steps with signal-routed edges — shipped as an asset and interpretable without new engine code per workflow.
- Each **step** is one ordinary attempt: leased, dispatched through the existing capability-matched scheduler, telemetered, retried and quarantined by the existing ladders. The step — not the task — pins the model, so planning and implementation route to different models (and, if need be, different workers) with zero new routing machinery.
- **Document steps** (plan, review, revise) land nothing: they run read-only in a throwaway worktree and return one markdown artifact in the submit payload, which the manager commits into the tasks repo next to the brief. Every stage's full context is on disk at the manager; a crashed step re-dispatches from the last committed artifact. No sticky worker, no session as source of truth.
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

Deliberately small, so every workflow stays inspectable and a future editor stays tractable. Three step kinds (`doc`, `code`, `split`); decisions are **signal-routed edges** on steps rather than a node type; control is `next`, `signals`, and `max_visits`.

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
| `kind` | `"doc"` — read-only throwaway worktree, lands nothing, produces one artifact (§7.1); `"code"` — today's full worktree→validate→land path; `"split"` — today's decomposition run (the worker writes child briefs into the split output dir; the manager harvests them) with the step's declared `inputs` materialized, so the children are cut from the plan rather than from the raw brief. |
| `role` | Model binding key resolved per §3.2. |
| `prompt` | Asset name under `assets/prompts/` (doc steps). Code steps use the standard `nightshift-local.md` charter with artifact injection (§7.2). |
| `inputs` | Artifact names materialized for the step. `"brief"` is always available; others must be a prior step's `output`. |
| `output` | Artifact name the step must produce (doc steps only). Re-using a name **overwrites** the artifact (revise's output *is* the plan; superseded versions live in the tasks repo's git history). |
| `max_turns` | Per-step turn budget handed to the backend. An int overrides the task/queue `turns` for this step; explicit `null` = unbounded; **absent** = inherit the task/queue `turns` as today. |
| `signals` | Map of sentinel token → step id (or `"$end"`). The routed-to step runs *instead of* `next`. |
| `next` | Explicit successor (defaults to the following step in the list; the last step's default is `"$end"`). |
| `max_visits` | Optional int: how many times the cursor may *enter* this step (§6.4 defines entry precisely). Default 1 for steps not on a cycle; required (validation error if absent) for any step reachable from itself. |

`$end` from a code step means land-and-consume as today. `$end` from a split step consumes the parent exactly like today's decomposition (`on_split_result`) — the children carry the work forward. `$end` from a doc step terminates the task without a land: the brief is marked `completed` (retained for operator review, like the loop spec's completion shape) and the final artifact is kept with it — the shape a verify step needs when it finds no gaps (§10). A workflow whose *default* path never reaches a code or split step is a definition error (there would be nothing to implement). Split steps must route to `$end` (a decomposition mid-workflow has no meaningful successor — the parent's work now lives in the children).

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

The engine fields are written through the tasks-repo executor but are **not** added to the detail editor's `_EDITABLE_META_KEYS` whitelist — the operator UI renders them read-only, and the engine write lane sets them directly (a deliberate split: operator-writable and engine-owned frontmatter are different key sets).

Compositions:

- `workflow` + `evergreen` — **supported**: the brief survives workflow completion and the workflow *resets* for the next cycle (§6.5). This is the janitor shape: a recurring diagnose→plan→execute workflow whose plan differs every cycle because the diagnosis does (errors in the logs, drifted docs, flaky tests).
- `workflow` + decomposition — **supported as a step kind**, not a frontmatter combination: a `kind: "split"` terminal step (§3.1) harvests child briefs from the plan. The `split: true` frontmatter flag keeps its existing single-shot meaning and is ignored on a workflow task (the definition, not the flag, says where decomposition happens).
- `workflow` + `loop` — **not in v1** (UI-prevented, engine-ignored). The goal-loop's across-run iteration axis and the workflow cursor need a deliberate composition design first.

`enhance` on create is skipped for workflow tasks (the plan step subsumes it); the create UI's segment becomes **Off | Enhance | Workflow** with a workflow picker.

## 5. Artifacts

**Custody: the manager, in the tasks repo.** A doc step's document rides the submit payload (new `Outcome.document: str | None`, capped at 256 KB — over-cap is a `worker_error`); the manager commits it to `<tasks_rel>/<task>.artifacts/<output>.md` through the same tasks-repo executor lane that writes frontmatter flags. Consequences, in order of why they were chosen:

- **Recoverable by construction.** Brief + committed artifacts are the *complete* context for every step. A failed/expired step re-dispatches against the same inputs; nothing lives only in an agent session. This is also why the planner does **not** need to be a sticky instance for revise: the revise prompt is brief + plan + review, fully reconstructable. (If a revision *needed* the planner's private session memory, the plan was underspecified — the implementor would have hit the same gap.)
- **No repo side effects.** A doc step's worktree is throwaway (§7.1); the target repo's history sees nothing until implement.
- **Size-safe.** Plans can be large; they never ride the brief body or the work order twice — a step's work order embeds only that step's declared `inputs`.
- Superseded plan versions are ordinary git history on the tasks repo.

Materialization on the worker mirrors the brief: each input artifact's text is embedded in the work order and written to the run-scratch dir (`materialize_brief`'s sibling, `materialize_artifacts`) — read-only files outside any worktree, paths injected into the prompt header.

Artifacts are deleted with the brief on terminal consumption (land or split-harvest of the final step / operator delete), deleted-and-reset on an evergreen cycle's completion (§6.5), and retained alongside the brief on quarantine.

**Repo convention note:** operators who keep specs in-repo (e.g. `docs/specs/`) get that by *authoring it in the plan prompt's charter* ("include copying the plan to docs/specs/<task>.md in the implementation") — a convention, not engine machinery.

## 6. Engine semantics (manager)

### 6.1 Scheduling

`build_candidates` gains workflow awareness: for a workflow task, the candidate's `model` is the **current step's** resolved model (§3.2) and the candidate carries `(workflow, step)`. This is a real signature/seam change, not a tweak — resolution needs the loaded workflow definitions, the brief's `workflow_step`, the queue's `workflow_models`, and the manager's `planner_model`, none of which today's `(tasks_root, queue, default_model)` signature carries; the definitions and manager config thread in as an explicit resolver argument. Everything downstream — `WorkerFilter._model_ok`, `unroutable`, dedication, priorities, `after:` — applies unchanged. A workflow whose current step's model no live worker advertises goes blocked with the existing `no live worker provides model '…'` reason.

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

plus the step's `max_turns` overriding the blob's (per the absent/int/null rule in §3.1). `kind: "code"` steps omit `prompt`/`output` and carry only `artifacts`. `kind: "split"` steps set the blob's existing `split: true` (the worker's decomposition path is reused verbatim) and carry `artifacts` so the plan is materialized for the decomposing agent. Note for the submit path: `SubmitPolicy.split` is sourced from the brief's frontmatter today (`worker_submit` reads `meta.get("split")`); for a workflow task it must come from the **step** (the workflow block on the attempt), since §4 ignores the frontmatter flag — `on_submit`'s `HARVEST_SPLIT` routing hangs on this exact field.

### 6.3 Step transitions

New pure function `transitions.on_workflow_step` (fed by `on_submit`, which branches to it when the attempt's work order carried a workflow block):

- **Doc step, completed** with a document → attempt `NO_CHANGE`-analog terminal state (`AttemptState.NO_CHANGE`, `result_line` = the step + signal), effects: commit artifact, advance `workflow_step` per the extracted signal (§7.3) or `next` (counting the destination step's visit, §6.4), `Progress.RESET`. Brief retained. `queue_changed` + `task_result` events as usual.
- **Doc step, completed without a document** → `worker_error` through the normal retry ladder; the cursor does not move.
- **Doc step, blocked / error** → today's `_blocked_transition` / `_error_transition` verbatim; the cursor does not move; retry/hold/quarantine ladders apply per failure kind.
- **Code step, completed & landable** → `GitPhase.LAND` as today. On a confirmed land: if the step's route is `$end`, the standard `_landed_transition` (brief consumption per §6.5); otherwise (a mid-workflow code step, e.g. inside a verify loop) the landed transition runs with `drop_brief=False` and the cursor advances.
- **Code step, validation failure** → today's `VALIDATION_ERROR` → HOLD, branch preserved, cursor unmoved.
- **Split step, completed** → `GitPhase.HARVEST_SPLIT` → `on_split_result` as today: children enqueued, parent consumed (evergreen: retained and reset per §6.5). Note the parent drop does **not** live in the transition today — `task_files.harvest_split_output` calls `drop_completed_task` unconditionally after committing the children; it gains a retention parameter so an evergreen workflow parent survives its own decomposition (§12). Children are ordinary tasks — each may carry its own `model:`, `after:` dependencies for sequencing, or even its own `workflow:`.

### 6.4 Budgets and termination

**Visit accounting — one rule for every step kind.** A *visit* to step S is counted when the manager moves the cursor onto S: at first dispatch for the workflow's first step, and in the transition that advances the cursor onto S (a doc/split submit, or a code step's land completion) for every other entry. The check runs at the same moment: if moving the cursor onto S would make its count exceed `max_visits`, the task quarantines instead of entering, with reason `workflow budget exhausted at '<step>' after N visits`. Consequences of counting on cursor-entry:

- Retries of a *failing* step never burn visits — the cursor doesn't move on failure (§6.3), so the step's count was taken once on entry, however many attempts it takes. Failing steps are bounded by the existing ladders (`attempts_without_progress`, backoff, quarantine threshold), not by `max_visits`.
- A code step's visit is counted when the cursor enters it — not per dispatch, not on land. In the looping variant, `implement: max_visits: 3` means at most three trips *into* implement, regardless of validation retries inside each trip.

Quarantine, not `failed`, for the same reason as the loop spec: the retry machinery lifts `failed` for a re-pick, which would silently grant an exhausted workflow another entry — budget exhaustion is an operator decision.

- Operator delete / quarantine-clear behave as today; clearing a quarantined workflow task resumes at its recorded `workflow_step`.

### 6.5 Completion — consume or reset

When the cursor routes to `$end`:

- **Non-evergreen** (the default): the brief is consumed exactly as today's land/split consumes it (`drop_brief=True` on land; `on_split_result`'s parent consumption); artifacts are deleted with it.
- **Evergreen**: the brief is retained and the workflow **resets** — a new tasks-repo commit (today's evergreen land writes nothing to the brief; the reset is new machinery, dispatched through the tasks-repo executor like other frontmatter effects) clears `workflow_step` and `workflow_visits` and deletes the cycle's artifacts. The next dispatch starts a fresh cycle at the first step with a clean slate; the previous cycle's artifacts remain recoverable in the tasks repo's git history, and its attempts/telemetry remain in the store.

The full-reset rule is deliberate: a janitor workflow's plan is a function of *this cycle's* diagnosis (the errors in the logs today), so stale artifacts must never leak into the next cycle as inputs. Anything a cycle should remember, it lands (in the target repo) — memory travels through the repo, not through artifacts, the same principle the goal-loop spec uses for its state note.

Evergreen composes with either terminal kind: an evergreen workflow ending in a code step is the recurring janitor (diagnose → plan → fix); ending in a split step it is a recurring *dispatcher* (diagnose → plan → cut child tasks each cycle, e.g. one child per subsystem found drifting).

## 7. Worker semantics

### 7.1 Doc steps

`execute_work_order` branches on `workflow.kind == "doc"`:

- **A throwaway read-only worktree at `base_ref`** — the same `setup_worktree`/`prepare_worktree_base` path code steps use, torn down **unconditionally** after the run (never preserved, never published). Two hazards force this over running in the shared base checkout: co-located workers share the manager's workspace, where `workspace/<repo>` is the very tree the land pipeline mutates under the per-repo executor — agent reads (let alone a cleanup `git clean`) racing a mid-land merge from outside that executor are unsafe; and cross-machine, the base clone can be stale, so a plan (and its context manifest line references) must pin to the manager's `base_ref` exactly as code steps do. `worktree add` is cheap; the doc-step speed comes from skipping preflight, validate, publish, and land — all of which remain skipped.
- **No preflight.** The agent reads code; it does not run it. The prompt charter declares the run read-only; any commits or writes in the worktree are discarded by the unconditional teardown (no clean-checkout guard needed — disposal is the guard).
- The header injects `$OUTPUT_FILE` — a run-scratch path outside the worktree. The agent writes the document there; stdout is for logs/sentinels only. After the run the worker reads it into `Outcome.document` (missing/empty → `worker_error`).
- **Tool-capable backends only.** A doc step needs an agent that can explore the worktree and write a file; single-shot completion backends (`anthropic`, `ollama` in completion mode) can do neither. The worker fails fast with `backend_unavailable` ("doc step requires a tool-capable backend") — a typed environment failure that routes RETRY_ELSEWHERE instead of burning attempts on missing-output `worker_error`s until quarantine.
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

**Scope: doc- and split-step submits only.** Those submits advance the cursor synchronously in the submit handler, so the handler can hand back the next step. A code step cannot chain: its submit CASes the attempt to `LANDING` and returns `{"queued": true}` immediately (the Phase-7 async land), and the cursor only advances in the land job's completion — by which time the submit response is long gone. A code step's successor (e.g. implement → verify in the looping variant) reaches the worker through its normal next poll, which follows within one poll interval of the land completing. That is the accepted cost; the doc-step hops — plan → review → revise, the bulk of a workflow's handoffs — are where chaining wins.

When a doc/split submit advances the cursor, the manager checks the submitting worker's registered capabilities (`manager/registry.py` checkin data) against the next step's resolved model. On a match it builds the next work order **in the submit response** (`response.next_order`); the worker loop processes it immediately — no re-poll, no scheduler round-trip. This is *affinity, not stickiness*: if the worker doesn't match, or dies, the step enters normal scheduling on the next poll.

**Chaining must re-run the dispatch guards** it bypasses by skipping `worker_poll`: queue pause state, queue dedication, task backoff (`next_eligible_at`), and repo availability. The `next_order` construction runs the same checks the poll path runs (factored to be callable from both); any guard refusing simply omits `next_order` and the step falls back to normal scheduling. Capability matching alone is not sufficient.

Chaining chains the **lease, never the context**: every step's inputs are exactly its declared artifacts, identical whether chained or cold.

### 7.5 Session resume (opportunistic, worker-local)

When a chained step has the same `(task, role)` as the step the worker just ran, and the backend supports resumption (`claude --resume`, `cursor-agent --resume`), the worker *may* resume that session instead of cold-starting — the plan→…→revise planner hop being the payoff (prompt-cache hits instead of a cold context rebuild). Three hard rules:

1. Within one task only, matching role — never across tasks (that is where unbounded context accretion would come from; forbidden outright).
2. A hint, never a dependency: the step's prompt carries the full declared inputs regardless, so resumed and cold runs see identical inputs and recovery semantics are untouched.
3. Session ids live only in the worker's `LocalStore`, dropped on task end or process restart. Nothing crosses the wire; the manager is unaware.

## 8. Token discipline

### 8.1 The context manifest

The single biggest cost lever: the planner explores the codebase **once**, and the plan carries the result. The plan prompt's charter requires a `## Context manifest` section — files involved, key functions/classes with line references, conventions that apply, the shape of the change. Review and implement charters instruct: *read the manifest files first; trust the manifest before exploring.* A plan that cannot produce a manifest is not concrete enough to leave the plan step. (Forward note: a tighter what/where manifest schema is the substrate for future harness-side local edits — v1 keeps it prose-with-references.)

**The plan is an implementable plan, not a spec.** The workflow goes brief → implementable plan in one step — there is no intermediate design document that itself needs a planning pass. The `workflow-plan.md` charter demands executable shape: ordered work items with exact files and function-level changes, signatures for anything a later item consumes, test requirements per item, and the context manifest. "Design rationale" appears only where a reviewer would otherwise have to reverse-engineer intent. (An operator who wants a human-approved spec first writes the spec conversationally, then feeds it to the workflow *as* the brief — the meta-level is the operator's choice, not the engine's.)

### 8.2 Prompt layout for cacheability

Step prompts follow the charter discipline `agent-charter.md` already models: byte-stable charter text first, task-varying header/artifacts last, so implicit prefix caching hits across runs of the same step. The nightshift harness backend may additionally pin explicit cache breakpoints (charter + manifest) across its turns. Cache-affinity scheduling (tiebreak toward the worker that most recently ran the same model) is noted as a follow-up, not v1.

### 8.3 Artifact discipline

A step receives **only** its declared `inputs` — implement gets brief + revised plan, not the review, not superseded plan versions. This is what keeps verify-loop iterations O(1) in context instead of accreting.

## 9. UI

- Create panel: the enhance segment becomes **Off | Enhance | Workflow**; choosing Workflow reveals a definition picker (shipped + operator definitions) and the optional planner-model override.
- Queue rows / detail: a workflow task shows a step badge — `plan → review → revise → implement` with the cursor highlighted and `workflow_visits` counts (e.g. `implement 2/4`); artifacts are viewable from the detail pane (rendered markdown, read-only).
- Quarantine reasons and blocked reasons surface as today.

## 10. Verify and gap-plan steps — `verify-refine` (v1) and the verify loop (post-v1)

Steps 6–9 of the operator pattern are **definition data only** — no engine change. The looping form:

```json
{ "id": "verify",   "kind": "doc",  "role": "implementor", "prompt": "workflow-verify.md",
  "inputs": ["brief", "plan"], "output": "gaps",
  "signals": { "verify-clear": "$end" }, "next": "gap-plan" },
{ "id": "gap-plan", "kind": "doc",  "role": "planner", "prompt": "workflow-gap-plan.md",
  "inputs": ["brief", "plan", "gaps"], "output": "plan", "next": "implement" }
```

with `implement.next = "verify"` and `max_visits` on `implement`/`verify`. Mid-workflow implement steps land per §6.3 (brief retained until `$end`). Gap analysis is mechanical enough that `gap-plan` could bind a cheaper role — the vocabulary already allows it. If the engine cannot express this without code changes, the vocabulary is wrong; this section is the acceptance test for §3.

**Non-looping cousin — `verify-refine` (ships in v1, §12):** the same two steps *without* the back-edge make a standalone audit workflow: verify a landed implementation against its brief/spec → plan the gaps → implement once → `$end` (`verify-clear` short-circuits to `$end` when nothing is missing). Its brief points at the spec to audit against; "plan" here is the gap-fix plan. This is the natural acceptance pass for any large landed feature — including, reflexively, this spec's own implementation, whose first real dogfood should be a `verify-refine` run against this document. Only the *looping* variant above (the back-edge and its `max_visits`) stays post-v1.

## 11. Non-goals

- **A workflow editor.** The JSON format is designed to make one tractable later; v1 ships definitions as files.
- **Free-form predicates.** Decisions branch on declared sentinel tokens and the engine's own outcomes (validation, land) — never on engine-evaluated expressions over artifacts.
- **Cross-task or manager-tracked sessions.** §7.5 is the entire session story.
- **Replacing enhance / single-shot split / loop / the single-shot path.** The `kind: "split"` step reuses the decomposition machinery; it does not replace the standalone `split: true` task shape. Converging defaults onto the workflow engine is a possible later cleanup, after the engine earns trust; nothing in v1 rewrites a working path.
- **Workflow + goal-loop composition.** Deferred past v1 (§4); evergreen cycling covers the recurring case.
- **Non-markdown artifacts.** Artifacts are named markdown documents, only.

## 12. Touch points (implementation checklist)

- New `src/nightshift/workflows.py` — definition load/validate/resolve (roles, steps, edges), pure. Shipped definitions, all v1:
  - `assets/workflows/plan-review-implement.json` — the reference workflow (§3.1).
  - `assets/workflows/verify-refine.json` — verify → gap-plan → implement, non-looping (§10): audit landed work against the brief/spec it names, plan the gaps, fix once. `verify` is `role: implementor`, signals `verify-clear → $end`; `gap-plan` is `role: planner`.
  - `assets/workflows/plan-split.json` — plan → review → revise → split: the reviewed plan becomes child briefs executed as ordinary tasks (the large-feature decomposition shape).
- `config/manager.py` — `planner_model` field (settings registry, env `NIGHTSHIFT_PLANNER_MODEL`); queue `config.json` keys `workflow`, `workflow_models` (documented, no schema change needed).
- `lifecycle.py` — `Outcome.document`, `Outcome.signal`; `SubmitPolicy` gains the step context (or a sibling `WorkflowPolicy`).
- `transitions.py` — `on_workflow_step` + the `drop_brief` generalization (`terminal and not evergreen`); the evergreen workflow-reset effect (§6.5 — a new tasks-repo commit); artifact-commit and frontmatter-cursor effects on `TaskEffects`; split-step routing to `HARVEST_SPLIT`/`on_split_result` with evergreen retention; `SubmitPolicy.split` sourced from the step for workflow tasks (§6.2).
- `task_files.py` — `harvest_split_output` gains a parent-retention parameter (§6.3); `drop_completed_task` (or a sibling) also removes `<task>.artifacts/`; `materialize_artifacts`.
- `manager/scheduler.py` — per-step model on `TaskCandidate`; `build_candidates` threads a step-model resolver (definitions + manager config + queue `workflow_models`), a real seam change (§6.1).
- `manager/work_orders.py` — the `workflow` config block, artifact embedding.
- `manager/api_worker.py` — submit: signal honoring, artifact commit dispatch, cursor advance + visit counting (§6.4); `next_order` chaining for doc/split submits only, re-running the poll-path dispatch guards (§7.4); the code-step cursor advance in the land-completion path.
- `worker/execute.py` — doc-step branch (throwaway read-only worktree at `base_ref`, unconditional teardown, `$OUTPUT_FILE`, tool-capable-backend guard → `backend_unavailable`); artifact materialization for code steps.
- `worker/loop.py` + `local_store.py` — `next_order` processing; session-id memory for §7.5.
- `prompts.py` — `extract_signal`; header injection for artifacts/`$OUTPUT_FILE`; new assets `workflow-plan.md`, `workflow-review.md`, `workflow-revise.md`, `workflow-verify.md`, `workflow-gap-plan.md`; the conditional plan paragraph in `nightshift-local.md`.
- `assets/ui/app.js` — create segment + picker, step badge, artifact viewer.
- `docs/user/configuration-reference.md` — frontmatter (`workflow`, `planner_model`, engine fields) + queue keys.
- Tests — definition validation (cycle requires `max_visits`; default path must reach code/split; split step must route `$end`); role resolution ladder; scheduler per-step routing + unroutable; doc-step execute (output file, unconditional worktree teardown even on failure, missing doc, tool-less backend → `backend_unavailable`); split-step execute with plan materialized; empty split output mid-workflow (parent retained, cursor stays on the split step — no cursor entry, so no visit burned per §6.4 — and the completed-with-nothing run takes `Progress.INCREMENT`, so the existing no-change ladder bounds repeated empty splits); signal extraction and undeclared-token ignore; cursor transitions incl. every failure shape leaving the cursor unmoved (and visits not burned by retries); code-step visit counted on cursor entry, not per dispatch or land; chaining handoff + affinity miss + each dispatch guard (pause, dedication, backoff, repo availability) suppressing `next_order`; code-step submits never chain; artifact commit/overwrite/drop incl. `<task>.artifacts/` removal with the brief; evergreen completion resets cursor/visits/artifacts and the next dispatch starts at step one; evergreen split parent survives harvest; budget quarantine; all three shipped definitions validate and run end-to-end; the looping verify definition runs on the engine unmodified.

## 13. Implementation order

1. `workflows.py` + definition asset + tests (pure, no wiring).
2. Lifecycle/transitions: outcome fields, `on_workflow_step`, retention generalization + tests.
3. Manager: scheduler per-step model, work-order block, submit wiring, artifact custody + tests.
4. Worker: doc-step branch, materialization, signals + tests.
5. Chaining (`next_order`) + tests; session resume last (pure optimization).
6. Config + UI + docs.
