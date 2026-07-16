# Workflows Implementation Plan

> **For agentic workers:** Execute one phase per session, in order. Each phase is self-contained: its **Read first** block lists everything you need in context; do not read beyond it. Each phase ends with its tests passing, `just validate` green, and one commit. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `docs/spec/2026-07-16-workflows.md` — declarative multi-step, multi-model task execution (`workflow:` frontmatter), shipping the `plan-review-implement`, `verify-refine`, and `plan-split` workflows.

**Architecture:** A pure definition layer (`workflows.py`) is interpreted by the manager: the scheduler routes each *step* on its own resolved model, `build_work_order` embeds a `workflow` block + input artifacts, the submit path advances a cursor stored in brief frontmatter and commits doc-step artifacts into the tasks repo. The worker adds one branch: doc steps run in a throwaway read-only worktree and return a document instead of commits. Chaining hands the next doc/split step back in the submit response.

**Authority:** The spec (`docs/spec/2026-07-16-workflows.md`) governs semantics. This plan governs sequencing, file boundaries, and interfaces. Where they disagree, the spec wins — and update whichever was wrong.

## Global constraints

- Python 3.12+, no new dependencies.
- Tests: `uv run pytest tests/<file> -x -q` per phase; `just validate` before each phase's final commit.
- Pure modules stay pure: `workflows.py` and `transitions.py` never import the store, git, or HTTP. This is convention, not test-enforced (`tests/test_import_boundaries.py` guards a different axis — cross-package private-name imports); hold the line by review discipline.
- Exhaustive `match` over enums/unions with `assert_never` in the default case (house rule; see `lifecycle.py` for the pattern).
- Imports at top of module, never inline.
- A task without `workflow:` frontmatter must behave **byte-identically** to today, every phase. Run the full suite, not just your phase's file.
- Commit messages: `task: workflows phase N — <summary>`.

## Phase map and interface ledger

Sequential; each phase consumes only the **Produces** lines of earlier phases (restated in its own header, so you never need another phase's diff).

| Phase | Delivers | Session context risk |
|---|---|---|
| 1 | `workflows.py` + 3 shipped definitions + prompt assets | Low — greenfield |
| 2 | `task_files.py` seams + `lifecycle.py` fields | Low — additive |
| 3 | `transitions.py` cursor machine | Medium — hairiest logic, but pure |
| 4 | Worker: doc-step execution + signals | Medium |
| 5 | Manager: scheduler + work orders | Medium |
| 6 | Manager: submit wiring + artifact custody + land-completion advance | **High — read budget is tight; follow the Read-first block strictly** |
| 7 | Chaining (`next_order`) | Medium |
| 8 | Config, UI, docs, session resume | Low |

---

### Phase 1 — the definition layer (pure) + shipped assets

**Read first:** spec §3 (vocabulary, role resolution), §10 (the two verify definitions); `src/nightshift/_paths.py` (the `asset()` helper); `src/nightshift/manager/scheduler.py:40-78` (agnostic-model + `queue_label` at :74, for role-resolution parity). Nothing else.

**Files:**
- Create: `src/nightshift/workflows.py`
- Create: `src/nightshift/assets/workflows/plan-review-implement.json`, `verify-refine.json`, `plan-split.json`
- Create: `src/nightshift/assets/prompts/workflow-plan.md`, `workflow-review.md`, `workflow-revise.md`, `workflow-verify.md`, `workflow-gap-plan.md`
- Test: `tests/test_workflows.py` (new — `tests/test_nightshift_workflow.py` is unrelated justfile checks; do not touch it)

**Produces (later phases consume exactly these):**

```python
class StepKind(StrEnum):
    DOC = "doc"; CODE = "code"; SPLIT = "split"

END = "$end"

@dataclass(frozen=True)
class WorkflowStep:
    id: str
    kind: StepKind
    role: str
    inputs: tuple[str, ...]            # "brief" | prior step outputs
    prompt: str | None = None          # doc steps only
    output: str | None = None          # doc steps only
    max_turns: int | None | object = _INHERIT   # sentinel: absent = inherit
    signals: dict[str, str] = ...      # token -> step id | END
    next: str = END
    max_visits: int = 1

@dataclass(frozen=True)
class WorkflowDef:
    name: str
    steps: tuple[WorkflowStep, ...]
    def step(self, step_id: str) -> WorkflowStep: ...
    @property
    def first(self) -> WorkflowStep: ...

class WorkflowError(ValueError): ...   # load/validation failures, loud

def load_workflows(workspace: Path) -> dict[str, WorkflowDef]:
    """Shipped assets/workflows/*.json, shadowed by <workspace>/.nightshift/workflows/*.json."""

def resolve_role_model(
    role: str, *,
    brief_meta: dict,                  # model:, planner_model:
    queue_config: dict,                # workflow_models: {role: model}
    planner_model: str,                # manager cfg (may be "")
    default_model: str,
) -> str | None:
    """Spec §3.2 ladder. None => unresolvable (caller marks the task blocked)."""

def route(step: WorkflowStep, signal: str | None) -> str:
    """Destination step id or END: declared signal wins, else step.next.
    Undeclared signals are ignored (route via next)."""

def parse_visits(raw: str | None) -> dict[str, int]     # "plan:1,implement:2"
def format_visits(visits: dict[str, int]) -> str
```

**Validation rules (`WorkflowError` on violation), each with a test:**
- `max_turns` absent → inherit sentinel; explicit `null` → unbounded (`None`); int → override. (JSON: distinguish key-absent from key-null when parsing.)
- Doc steps require `prompt` + `output`; code/split steps must not set them.
- `inputs` may only name `"brief"` or an output produced by some earlier-listed step.
- Split steps must route to `$end` (both `next` and every signal destination).
- The default path (following `next` from the first step) must reach a code or split step.
- Any step reachable from itself must declare `max_visits` explicitly.
- Signal destinations and `next` must name existing steps or `$end`.

**Definitions content** — per spec §3.1's JSON for `plan-review-implement` (verbatim), §10 + §12 for `verify-refine` (verify: `role: implementor`, prompt `workflow-verify.md`, inputs `[brief]`, output `gaps`, signals `{verify-clear: $end}`, next `gap-plan`; gap-plan: `role: planner`, prompt `workflow-gap-plan.md`, inputs `[brief, gaps]`, output `plan`, next `implement`; implement: code, `role: implementor`, inputs `[brief, plan]`), and `plan-split` = plan/review/revise as in the reference workflow with the terminal step swapped for `{id: split, kind: split, role: planner, inputs: [brief, plan]}`.

**Prompt assets** — each is a byte-stable charter (task-varying content is injected by the *header* in Phase 4, never interpolated into these files; see `assets/prompts/agent-charter.md` for the discipline). Required content per spec §8.1: `workflow-plan.md` demands an **implementable plan** (ordered work items, exact files, function-level changes, signatures for anything a later item consumes, per-item tests) with a `## Context manifest` section, and documents the `NIGHTSHIFT_SIGNAL: plan-trivial` escape. `workflow-review.md`: reviewer role, read manifest files first, output questions/clarifications or emit `NIGHTSHIFT_SIGNAL: review-clear`. `workflow-revise.md`: integrate the review into a revised plan (full plan re-emitted, not a delta). `workflow-verify.md`: audit the landed implementation against the brief's named spec; gaps document or `NIGHTSHIFT_SIGNAL: verify-clear`. `workflow-gap-plan.md`: implementable fix plan from the gaps document. All doc prompts: write the document to `$OUTPUT_FILE`; the run is read-only; never modify the repo.

**Steps:**
- [ ] Write `tests/test_workflows.py` covering every validation rule above, the role ladder (each rung, both roles, unknown role → `None`), `route` (declared signal / undeclared signal / no signal / `$end`), visits round-trip, and the three shipped definitions loading + validating.
- [ ] Add the **looping verify definition** (spec §10's JSON: verify/gap-plan with `implement.next = "verify"`, `max_visits` on `implement` and `verify`) as a test fixture (`tests/fixtures/workflow-verify-loop.json` or inline) that must validate — it is the spec's acceptance test for the vocabulary (cycle detection must *require* its `max_visits`, and the definition must pass). Not a shipped asset.
- [ ] Run: `uv run pytest tests/test_workflows.py -x -q` — expect failures/collection errors.
- [ ] Implement `workflows.py`, the three JSON definitions, the five prompt assets.
- [ ] Tests pass; `just validate`; commit `task: workflows phase 1 — definition layer + shipped workflows`.

---

### Phase 2 — task_files + lifecycle groundwork

**Read first:** spec §4 (frontmatter contract), §5 (artifacts), §6.5 (reset); `src/nightshift/task_files.py:296-330` (`drop_completed_task`), `:456-520` (`set_task_meta`, `_EDITABLE_META_KEYS`), `:596-700` (`materialize_brief`, `harvest_split_output`); `src/nightshift/lifecycle.py:228-295` (`Telemetry`/`Outcome`), `:392-412` (`SubmitPolicy`), `:423-470` (`FrontmatterFlag`, `TaskEffects`). Phase 1's Produces block above.

**Files:**
- Modify: `src/nightshift/task_files.py`
- Modify: `src/nightshift/lifecycle.py`
- Test: extend `tests/test_lifecycle.py` and add artifact/harvest cases to the test file that already covers `task_files` helpers (locate with `rg -l "drop_completed_task" tests/`)

**Produces:**

```python
# task_files.py
def artifacts_dir(tasks_root: Path, task: str, tasks_rel: str = "main") -> Path
    # <tasks_root>/<tasks_rel>/<task>.artifacts/
def write_artifact(tasks_root: Path, task: str, name: str, text: str,
                   tasks_rel: str = "main") -> Path
    # write <name>.md + commit_tasks (one commit per artifact write)
def read_artifacts(tasks_root: Path, task: str, names: Sequence[str],
                   tasks_rel: str = "main") -> dict[str, str]
def delete_artifacts(tasks_root: Path, task: str, tasks_rel: str = "main",
                     *, commit: bool = True) -> bool
def set_engine_meta(tasks_root: Path, task: str, changes: dict[str, object | None],
                    tasks_rel: str = "main") -> dict
    # engine-owned lane: same rewrite mechanics as set_task_meta but keyed to
    # ENGINE_META_KEYS = {"workflow_step", "workflow_visits"}; these keys are
    # NOT added to _EDITABLE_META_KEYS (spec §4)
def materialize_artifacts(workspace: Path, repo: str, task: str,
                          artifacts: dict[str, str], *, queue: str | None = None) -> dict[str, Path]
    # sibling scratch files: task-local-<queue>-<task>.artifact-<name>.md
# harvest_split_output(..., retain_parent: bool = False)  — new kwarg; when True,
#   skip drop_completed_task. drop_completed_task also removes <task>.artifacts/
#   (add to the same commit's pathspecs).
# delete_task (task_files.py:275 — the operator-delete path, called from
#   api_operator.py:591) also removes <task>.artifacts/: spec §5 requires
#   artifact deletion on operator delete, and this endpoint does NOT go
#   through drop_completed_task.

# lifecycle.py
class Outcome(Telemetry):
    ...  # existing fields unchanged, plus:
    document: str | None = None
    signal: str | None = None

@dataclass(frozen=True)
class WorkflowStepPolicy:
    """Step context for on_submit — computed by the caller from the attempt's
    work-order workflow block (never from frontmatter)."""
    workflow: str
    step_id: str
    kind: StepKind                 # import from nightshift.workflows
    output: str | None
    route_to: str                  # precomputed: workflows.route(step, outcome.signal)
    dest_kind: StepKind | None     # kind of route_to's step (None for $end)
    dest_visits_exhausted: bool    # would entering route_to exceed its max_visits?
    evergreen: bool

class SubmitPolicy:
    ...  # existing fields unchanged, plus:
    workflow_step: WorkflowStepPolicy | None = None
```

Design note (why `route_to`/`dest_visits_exhausted` are precomputed): `transitions.py` must stay pure and free of definition-loading; the api layer resolves the graph, the transition core only acts on the verdict. Keep it that way.

**Steps:**
- [ ] Failing tests: artifact write/read/delete round-trip with commits (use the existing tmp tasks-root fixtures — find them via `rg -n "tasks_root" tests/ | head`); `set_engine_meta` writes/clears the two keys and rejects others; `set_task_meta` still rejects `workflow_step` (not editable); `harvest_split_output(retain_parent=True)` keeps the parent brief; `drop_completed_task` removes the artifacts dir; `delete_task` removes the artifacts dir (the operator-delete path); `materialize_artifacts` paths; `Outcome` accepts/defaults the two new fields (wire-compat: absent in JSON ⇒ None).
- [ ] Implement. The 256 KB document cap (spec §5) is *not* enforced here — it belongs to the submit endpoint (Phase 6).
- [ ] Tests + full suite pass; `just validate`; commit `task: workflows phase 2 — artifact custody, engine meta lane, outcome fields`.

---

### Phase 3 — the cursor machine (`transitions.py`)

**Read first:** spec §6.3, §6.4, §6.5 in full — this phase *is* those sections; `src/nightshift/transitions.py` (whole file — it's the module you're extending; note `_landed_transition`, `on_split_result`, `_no_change_transition`, `_failure_ladder`); `lifecycle.py:436-470` (`TaskEffects`) plus Phase 2's Produces. Do **not** read manager or worker modules.

**Files:**
- Modify: `src/nightshift/transitions.py`
- Modify: `src/nightshift/lifecycle.py` (new `TaskEffects` fields only)
- Test: `tests/test_transitions_workflow.py` (new file — keeps the existing transition tests untouched and this phase's session focused)

**Produces:**

```python
# lifecycle.py — TaskEffects gains:
    write_artifact: tuple[str, str] | None = None      # (name, text) — post-commit effect
    engine_meta: dict[str, object | None] | None = None  # workflow_step / workflow_visits writes
    workflow_reset: bool = False                       # evergreen $end: clear meta + delete artifacts

# transitions.py
def on_workflow_step(ref: AttemptRef, outcome: Outcome, policy: SubmitPolicy) -> Transition | GitPhase:
    """Handles every submit whose policy.workflow_step is not None.
    on_submit delegates: `if policy.workflow_step is not None: return on_workflow_step(...)`."""

def on_workflow_land(ref: AttemptRef, outcome: Outcome, land: LandOutcome,
                     policy: SubmitPolicy) -> Transition:
    """Land-result counterpart for workflow code steps (mirrors on_land_result)."""

def on_workflow_split(ref: AttemptRef, outcome: Outcome, created: list[str],
                      policy: SubmitPolicy) -> Transition:
    """Split-harvest counterpart for workflow split steps."""
```

**Behavior table (each row = at least one test):**

| Input | Result |
|---|---|
| doc step, `COMPLETED`, `document` set, route to step S, S not exhausted | state `NO_CHANGE`; effects: `write_artifact=(output, document)`, `engine_meta` advancing `workflow_step`→S + visits, `Progress.RESET`; brief retained |
| doc step, `COMPLETED`, route destination exhausted (`dest_visits_exhausted`) | quarantine transition (reuse the `_failure_ladder` quarantine shape) reason `workflow budget exhausted at '<S>' after N visits`; artifact still committed (the work was good) |
| doc step, `COMPLETED`, route `$end` | state `NO_CHANGE`; `completed: true` frontmatter flag (the loop-spec completion shape); brief + artifacts retained; evergreen ⇒ `workflow_reset=True` instead of `completed` |
| doc step, `COMPLETED`, `document is None` | `_error_transition` with `failure_kind=WORKER_ERROR`, reason `doc step produced no document`; cursor untouched |
| doc step, `BLOCKED` / `ERROR` | delegate to existing `_blocked_transition` / `_error_transition` verbatim; cursor untouched |
| code step, `COMPLETED` + landable | `GitPhase.LAND` (caller later feeds `on_workflow_land`) |
| `on_workflow_land`, land success, route `$end`, non-evergreen | `_landed_transition` shape with `drop_brief=True` (artifacts dropped via Phase 2's `drop_completed_task`) |
| `on_workflow_land`, land success, route `$end`, evergreen | `drop_brief=False`, `workflow_reset=True` |
| `on_workflow_land`, land success, mid-workflow route to S | `drop_brief=False`, `engine_meta` advance + visit S (or quarantine if exhausted) |
| `on_workflow_land`, land failure | existing `_land_failed_transition` verbatim; cursor untouched |
| split step (`GitPhase.HARVEST_SPLIT` routed by `policy.workflow_step.kind is SPLIT`, **not** `policy.split`) → `on_workflow_split`, children created, non-evergreen | `on_split_result` shape (parent consumed — caller passes `retain_parent=False` to harvest) |
| `on_workflow_split`, children, evergreen | parent retained (`retain_parent=True` at harvest), `workflow_reset=True` |
| `on_workflow_split`, zero children | parent retained, cursor **stays on the split step** (no cursor entry ⇒ no visit burned), `Progress.INCREMENT` — the existing no-change ladder bounds repeated empty splits. (The spec's §12 test list was amended 2026-07-17 to this semantics; it previously said quarantine-on-visit-check, which contradicted §6.4 entry counting.) |
| undeclared signal on any completed step | identical to no signal (`route_to` already reflects this — assert the policy precompute contract with a `route()` test in Phase 1, and here that `on_workflow_step` never inspects `outcome.signal` directly) |

Visit counting is entry-based (spec §6.4): the *destination* step's counter increments in the same `engine_meta` write that moves the cursor. The first step's visit is counted at first dispatch (Phase 6's dispatch wiring, not here). Failures never move the cursor, so retries burn no visits by construction — add one test asserting a doc-step `ERROR` transition carries no `engine_meta`.

**Steps:**
- [ ] Write the test file: one test per table row, plus the two invariants (no `engine_meta` on failure; `on_workflow_step` ignores `outcome.signal`). Build `SubmitPolicy`/`WorkflowStepPolicy` fixtures by hand — no store, no files.
- [ ] Run to fail; implement; `on_submit` gains only the one delegation line.
- [ ] Tests + full suite; `just validate`; commit `task: workflows phase 3 — workflow step transitions`.

---

### Phase 4 — worker: doc-step execution + signal extraction

**Read first:** spec §7.1, §7.2, §7.3; `src/nightshift/worker/execute.py` (whole file); `src/nightshift/prompts.py:194-215` (blocked-sentinel pattern), `:18-71` (`build_prompt`); `src/nightshift/git/worktrees.py:49-130`; `src/nightshift/git/transport.py:125-160` (`prepare_worktree_base`); `src/nightshift/backends.py:60-100` (`WorkerSpec`) and the class-level `available()` pattern around lines 490-560. Phase 1–2 Produces.

**Files:**
- Modify: `src/nightshift/worker/execute.py`
- Modify: `src/nightshift/prompts.py`
- Modify: `src/nightshift/assets/prompts/nightshift-local.md` (the plan paragraph — see below)
- Modify: `src/nightshift/backends.py` (capability flag only)
- Test: `tests/test_prompts.py` (extend), `tests/test_nightshift_worker.py` (extend)

**Produces:**

```python
# prompts.py
def extract_signal(text: str) -> str | None
    # NIGHTSHIFT_SIGNAL: <token> — same last-match-wins mechanics as extract_blocked_reason
def build_doc_prompt(task: str, *, prompt_asset: str, task_file: str,
                     artifact_files: dict[str, str], output_file: str) -> str
    # header (task file, artifact paths, $OUTPUT_FILE) + the asset body, byte-stable body
# build_prompt gains artifact_files: dict[str, str] | None — code steps name their
#   materialized artifacts in the header ("The PLAN file is: <path>")

# backends.py — each backend class gains a class attr:
    tool_capable: bool   # True: claude-code, cursor, gemini, nightshift; False: anthropic, ollama, ollama-cloud
```

**Worker branch in `execute_work_order`** (after backend resolution, before the existing worktree setup): when `order["config"]["workflow"]["kind"] == "doc"`:
1. `if not backend.tool_capable:` → `fail(FailureKind.BACKEND_UNAVAILABLE, "doc step requires a tool-capable backend")` (environment kind ⇒ RETRY_ELSEWHERE, per spec §7.1).
2. Skip preflight entirely. Cut the worktree exactly as the code path does (`prepare_worktree_base` when rendezvous, `setup_worktree`), but wrap the whole run so `teardown_worktree` executes **unconditionally** (no `preserve` flag on this path).
3. Materialize brief + artifacts; `output_file` = a `.taskfile.md`-style sibling scratch path (`task-local-<queue>-<task>.output.md`); build the doc prompt; run the backend with the step's `max_turns`.
4. After the run: blocked sentinel → existing BLOCKED outcome; nonzero exit → WORKER_ERROR; else read `output_file` — missing/empty → `fail(WORKER_ERROR, "doc step produced no document")`; else `Outcome(status=COMPLETED, landable=False, document=<text>, signal=<sig>, result_line=...)` where `sig = extract_signal(captured)` and `result_line` is `f"doc step '{step}' produced '{output}'"` plus `f" (signal: {sig})"` when a signal was emitted — spec §6.3's "the step + signal".
5. Never validate, never publish.

Split steps on the worker are **unchanged** (the workflow block's `kind == "split"` arrives with `config.split` already true from Phase 5 — the existing split path runs; artifacts materialization for the decomposing agent comes free from the code-step change). Code steps: materialize artifacts, pass `artifact_files` into `build_prompt`, and attach `signal=extract_signal(...)` to every completed outcome (verify-loop code steps may signal in the future; harmless otherwise).

**`nightshift-local.md` gains the plan paragraph (spec §7.2)** — and here is how it squares with the byte-stable-charter rule: the paragraph is *unconditionally present* in the charter body ("If the header above names a PLAN file, that plan is the spec — follow it, and trust its context manifest before exploring the codebase"), and only *applies* when the header names one. Charter stays byte-identical across all runs (cacheable); the header remains the only task-varying region. Do not interpolate the paragraph conditionally.

**Steps:**
- [ ] Failing tests: `extract_signal` (present/absent/bare/last-wins); `build_doc_prompt` header shape; doc-step run with a stub backend writing the output file (assert Outcome fields + worktree gone afterward); doc-step with stub *not* writing it (WORKER_ERROR, worktree still gone); tool-less backend (BACKEND_UNAVAILABLE before any worktree exists); code step's prompt names the plan file. The worker tests already stub backends — copy the fixture pattern from the nearest existing `execute_work_order` test.
- [ ] Implement; tests + full suite; `just validate`; commit `task: workflows phase 4 — doc-step execution, signals, artifact materialization`.

---

### Phase 5 — manager: scheduling + work orders + the attempt's workflow column

**Read first:** spec §3.2, §6.1, §6.2; `src/nightshift/manager/scheduler.py` (whole — it's small; note `queue_label` sits at line 74); `src/nightshift/manager/work_orders.py` (whole); `src/nightshift/spawn_daily.py` — only `resolve_config` and `load_queue_config` signatures (`rg -n "def resolve_config|def load_queue_config" src/nightshift/spawn_daily.py`); for the store column: `create_attempt` in `src/nightshift/manager/store.py` (protocol at :217, implementation at :490 — read both, ±20 lines) and the `CREATE TABLE nightshift.attempts` DDL at `src/nightshift/manager/store_sqlite.py:150`. Phase 1–2 Produces.

**Files:**
- Modify: `src/nightshift/manager/scheduler.py`, `src/nightshift/manager/work_orders.py`
- Modify: `src/nightshift/manager/store.py`, `src/nightshift/manager/store_sqlite.py`
- Create: `src/nightshift/assets/migrations/20260802000002_nightshift_attempt_workflow.sql` (numbering must sort after the latest existing migration — check `ls src/nightshift/assets/migrations/ | tail -1` and bump if the repo moved)
- Test: `tests/test_nightshift_scheduler.py` (extend), `tests/test_nightshift_store.py` (extend), plus work-order assertions where `build_work_order` is already tested (`rg -l "build_work_order" tests/`)

**Produces:**

```python
# scheduler.py
@dataclass(frozen=True)
class TaskCandidate:
    ...  # plus:
    workflow: str | None = None
    workflow_step: str | None = None
    workflow_error: str | None = None   # unresolved role / unknown definition / unknown step

# build_candidates gains a keyword:
def build_candidates(tasks_root, queue, *, default_model="auto",
                     workflow_resolver: WorkflowResolver | None = None) -> list[TaskCandidate]

class WorkflowResolver(Protocol):
    def __call__(self, meta: dict, queue_config: dict) -> tuple[str, str, str] | tuple[None, None, str]:
        """(workflow, step_id, resolved_model) or (None, None, error)."""

# work_orders.py — build_work_order emits, for workflow tasks:
config_blob["workflow"] = {
    "name": ..., "step": ..., "kind": ...,          # step kind value ("doc"/"code"/"split")
    "prompt": ...,  "output": ...,                   # doc steps only
    "artifacts": {name: text, ...},                  # via task_files.read_artifacts
    "signals": [...],                                # declared tokens (informational)
}
# and for split steps additionally config_blob["split"] = True (worker path reuse);
# step max_turns overrides per the absent/int/null rule; the resolved step model
# replaces config_blob["model"].

# store — the attempt row persists the step context for the submit path:
#   migration: ALTER TABLE nightshift.attempts ADD COLUMN IF NOT EXISTS workflow jsonb;
#   mirrored in store_sqlite.py's CREATE TABLE (TEXT, json-serialized);
#   create_attempt(..., workflow: dict | None = None) on the protocol and BOTH
#   implementations; attempt reads surface it as attempt["workflow"].
# The stored dict is the work order's workflow block MINUS "artifacts" (context
# lives in the tasks repo; the row stores routing metadata only — name, step,
# kind, output, signals).
```

The concrete resolver lives in `api_worker`'s wiring (Phase 6) — this phase implements it as a small factory in `workflows.py` (`make_resolver(defs, manager_cfg)`) so the scheduler change is testable with a fake. Candidate semantics: a workflow task's `model` is the step's resolved model; `workflow_error` candidates are surfaced exactly like `repo_error` ones (manager marks blocked — wire that in Phase 6; here just carry the field). A workflow brief whose `workflow_step` is absent resolves to the definition's first step (the first visit is *counted* in Phase 6's dispatch wiring, not here).

**Steps:**
- [ ] Failing tests: candidate carries step-resolved model (fake resolver); `WorkerFilter` routes on it; `unroutable` reports the step's model; absent `workflow_step` → first step; unknown definition/step/role → `workflow_error`; work order embeds the workflow block + artifacts, sets `split: true` for split steps, applies the three-way `max_turns` rule, and non-workflow orders are byte-identical to before (snapshot-compare one existing expected blob); store: `create_attempt(workflow=...)` round-trips through both stores (None for non-workflow attempts).
- [ ] Implement (including `make_resolver` in `workflows.py`); tests + full suite; `just validate`; commit `task: workflows phase 5 — per-step routing, work orders, attempt workflow column`.

---

### Phase 6 — manager: submit wiring, artifact custody, cursor advance

This is the highest-context phase. **Read only:** spec §6.3–§6.5; `src/nightshift/manager/api_worker.py` — the poll handler's dispatch section (`rg -n "pick_next|create_attempt|build_work_order" src/nightshift/manager/api_worker.py` and read ±40 lines around each) and `worker_submit` from its `def` through `_complete_land` (≈ lines 469–720); `rg -n "def apply_transition" src/nightshift/manager/store.py` + the transition-effects applier it delegates to (read that function only). Phases 1–3, 5 Produces. **Do not read** the whole store, reconciler, or landing modules — the transition core already encapsulates them.

**Files:**
- Modify: `src/nightshift/manager/api_worker.py`, `src/nightshift/manager/store.py` (+ `store_sqlite.py` only if the effects applier is duplicated there — check with `rg -n "drop_brief" src/nightshift/manager/store*.py`)
- Test: `tests/test_nightshift_manager.py` (extend)

**Wiring, in dependency order:**
1. **App state:** load definitions once at startup (`app.state.workflows = load_workflows(workspace)`), fail-loud on `WorkflowError`.
2. **Dispatch:** thread `make_resolver(app.state.workflows, cfg)` into `build_candidates`; on picking a workflow candidate, if its brief has no `workflow_step`, write the first step + `<step>:1` visit through `set_engine_meta` (the tasks-repo executor job) *before* `build_work_order`; `workflow_error` candidates get the existing blocked-marking path with the error as reason.
3. **Submit:** compute `WorkflowStepPolicy` from the attempt row's `workflow` column (persisted at dispatch by Phase 5's `create_attempt(workflow=...)` — this phase only *reads* it): `route_to = workflows.route(step, body.signal)`, `dest_visits_exhausted` from `parse_visits(meta)`, etc. When `body.signal` is set but undeclared for the step, log it (`log.warning("task %s step %s emitted undeclared signal %r — ignored", ...)`) — the spec's "logged and ignored" — and route via `next` (which `route()` already does). Enforce the 256 KB `document` cap here (over-cap → coerce the outcome to WORKER_ERROR before transitioning).
4. **Effects application:** the store's transition applier gains the three new `TaskEffects` fields — `write_artifact` → `task_files.write_artifact`, `engine_meta` → `set_engine_meta`, `workflow_reset` → clear both keys + `delete_artifacts` — all through the existing tasks-repo executor lane that `FrontmatterFlag` uses today (find it: `rg -n "FrontmatterFlag" src/nightshift/manager/`).
5. **Land completion:** in `_complete_land`, workflow attempts route to `on_workflow_land` instead of `on_land_result`; split harvests route to `on_workflow_split` and pass `retain_parent=policy.workflow_step.evergreen` into `harvest_split_output`.

**Steps:**
- [ ] Failing tests (in-memory store + tmp tasks root, mirroring the existing manager-test fixtures): full doc-step round trip (dispatch stamps step 1; submit with document commits the artifact, advances cursor, next poll dispatches step 2 with the artifact embedded); signal skip (review-clear jumps revise); `$end` doc completion (completed flag, artifacts retained); budget quarantine on entry; code-step land advances cursor in `_complete_land` (mid-workflow) and consumes brief+artifacts at `$end`; **one looping-verify round trip** (Phase 1's fixture definition: implement lands → cursor enters verify → verify's gaps doc routes to gap-plan → second implement entry; assert the visit counts — this is spec §12's "runs on the engine unmodified" test); quarantine-clear resumes at the recorded `workflow_step`; evergreen `$end` resets (meta cleared, artifacts gone, next dispatch starts at step 1); evergreen split retains parent; zero-children split increments no-progress (no visit burned); oversized document → WORKER_ERROR; non-workflow submits untouched (run the whole existing manager suite).
- [ ] Implement; tests + full suite; `just validate`; commit `task: workflows phase 6 — submit wiring, artifact custody, cursor advance`.

---

### Phase 7 — chaining

**Read first:** spec §7.4; the poll handler's guard sequence in `api_worker.py` (pause filter, dedication, backoff/`next_eligible_at`, repo availability — locate each via `rg -n "queue_pauses|next_eligible|repo_available|dedication" src/nightshift/manager/api_worker.py`); `worker_submit`'s response paths (Phase 6 shape); `src/nightshift/worker/loop.py:87-200`.

**Files:**
- Modify: `src/nightshift/manager/api_worker.py` (factor a `dispatch_guards_ok(...)` helper callable from poll + submit), `src/nightshift/worker/loop.py`
- Test: `tests/test_nightshift_manager.py`, `tests/test_nightshift_worker.py` (extend)

**Contract:** only doc/split submits that advanced the cursor may chain. After the transition applies, if the submitting worker's registered capabilities (from `_registry()` checkin data) accept the next step's resolved model *and* `dispatch_guards_ok` passes (queue not paused, dedication honored, task not backed off, repo available), create the next attempt + work order and attach as `response["next_order"]`; otherwise omit it — the step reaches workers via normal polling. Code-step submits return `{"queued": true}` and never chain (assert this). Worker side: `run_once` processes `next_order` from a submit response before polling again; a worker mid-`next_order` still heartbeats normally (it holds a real lease — nothing special).

**Steps:**
- [ ] Failing tests: capable worker chains doc→doc (no second poll needed — assert the response carries a valid order and the attempt row exists); each guard independently suppresses `next_order` (paused queue, dedicated-elsewhere, backed-off task, missing repo); incapable worker → no chain, next poll dispatches; code-step submit response has no `next_order`; worker loop executes a chained order end-to-end.
- [ ] Implement; tests + full suite; `just validate`; commit `task: workflows phase 7 — doc-step chaining`.

---

### Phase 8 — config, UI, docs, session resume

**Read first:** spec §3.2 (planner_model), §7.5, §9; `src/nightshift/config/manager.py:74-100` (field-metadata pattern) and the `load/save_manager_settings` + `ManagerConfig` projection sites (`rg -n "enhance_brief_model" src/nightshift/config/manager.py`); `assets/ui/app.js` — only `enhanceSegment` and the loop-settings panel (`rg -n "enhanceSegment|loopSettings" src/nightshift/assets/ui/app.js`); for resume: the claude/cursor backend `run` methods in `backends.py` and `worker/local_store.py`.

**Files:**
- Modify: `src/nightshift/config/manager.py`, `src/nightshift/assets/ui/app.js` (+ `style.css` as needed), `src/nightshift/backends.py`, `src/nightshift/worker/local_store.py`, `src/nightshift/worker/loop.py`, `docs/user/configuration-reference.md`, spec Status line
- Test: `tests/test_config_model.py` / `tests/test_settings_api.py` (follow where `enhance_brief_model` is tested), `tests/test_nightshift_worker.py`

**Work items:**
- `planner_model` on `OperatorConfig` (default `""`, env `NIGHTSHIFT_PLANNER_MODEL`, category next to `default_model`, `apply="restart"`), threaded through load/save/projection — copy the `enhance_brief_model` plumbing exactly.
- UI: create-panel segment **Off | Enhance | Workflow** + definition picker + optional planner-model input; selecting Workflow **disables the Loop toggle** and an active Loop toggle disables the Workflow segment (spec §4's UI-prevented exclusion); Split and Evergreen toggles stay enabled — both compose with workflows. New `GET /api/workflows` returns `{name: [ordered step ids]}` — the step *lists*, not just names, because the queue-row badge renders the full path (`plan → review → revise → implement`) with the cursor highlighted from `workflow_step` and counts from `workflow_visits` (spec §9). Read-only artifact viewer in the detail pane (fetch via a `GET /api/tasks/<id>/artifacts` endpoint reading `read_artifacts`). Manual check with a live manager; keep JS consistent with the existing segmented-control pattern.
- Session resume (spec §7.5, all three rules): `LocalStore` remembers `(task, role) -> session_id` for the last completed step; claude/cursor backends accept an optional resume id in `WorkerSpec.config` and emit their session id in `WorkerResult` when parseable; the worker offers resume only when a chained order matches task+role; drop memory on task end/restart. If a backend's session id is not cheaply extractable from its stream JSON, ship without it and note that in the spec — resume is an optimization, not a contract.
- Docs: configuration-reference rows for `workflow`, `planner_model`, `workflow_step`, `workflow_visits`, queue `workflow_models`; flip the spec's Status to Implemented and correct any drift found during phases 1–7.
- Final: `just validate` + full suite; commit `task: workflows phase 8 — config, UI, session resume, docs`.

---

## Acceptance (after Phase 8)

Run the three shipped workflows end-to-end against a scratch repo (the smoke-test harness in `docs/topics/smoke-test.md` is the template): `plan-review-implement` lands a change through all four steps; `verify-refine` on a brief naming an existing spec completes via `verify-clear` or lands a fix; `plan-split` enqueues children carrying `after:` chains. Then the reflexive dogfood: a `verify-refine` task whose brief points at `docs/spec/2026-07-16-workflows.md`.
