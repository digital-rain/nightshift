# Workflow & Workflow-Stage Statistics — Implementation Plan

> **For agentic workers:** Execute one phase per session, in order. Each phase is self-contained: its **Read first** block lists everything you need in context; do not read beyond it. Each phase ends with its tests passing, `just validate` green, and one commit. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `docs/spec/2026-07-17-workflow-stats.md` — statistics broken down *per workflow* and *per workflow-stage*, answering "are multi-step workflows worth the money/latency vs single-shot, and which stage is the problem?" Layer 1 (phases 1–4) is read-only and ships independently; Layer 2 (phases 5–7) adds a small write enrichment and the stage-flow funnel.

**Architecture:** Layer 1 reuses the existing `stats_by_*` SQL-view pattern (two new views over the `attempts.workflow` jsonb column, exposed through `/api/stats` + Workers-page tables) plus the client-side `analytics.js` pattern (two new group-by dimensions + a task-grain "Workflow economics" panel fed by `/api/analytics/runs`). No write path, no engine changes. Layer 2 stamps four immutable facts (`execution_id`, `visit`, `emitted_signal`, `routed_to`) onto the attempt's `workflow` jsonb at dispatch/advance, then adds a server-side `/api/analytics/workflows` aggregator and the funnel UI on top.

**Authority:** The spec (`docs/spec/2026-07-17-workflow-stats.md`) governs semantics and metric definitions; it in turn defers to `docs/spec/2026-07-16-workflows.md` for engine behavior. This plan governs sequencing, file boundaries, and interfaces. Where they disagree, the spec wins — and update whichever was wrong.

## Global constraints

- Python 3.12+, no new dependencies.
- Tests: `uv run pytest tests/<file> -x -q` per phase; `just validate` (ruff check + full pytest) before each phase's final commit.
- **Additive only.** Every change is a new view / new key / new panel. No existing view, `/api/stats` key, `run_view` shape, `landed` flag, or existing analytics KPI changes behavior. Non-workflow attempts (`workflow IS NULL`) must be excluded from every new workflow view and produce zero behavior change on existing surfaces. Run the full suite, not just your phase's file.
- **PG ↔ SQLite lockstep.** Every new SQL view exists in both a PG migration (`assets/migrations/`) and the SQLite `_SCHEMA` twin (`store_sqlite.py`), added in the same phase/commit — the standing `stats_by_enhanced` convention ("Keep in lockstep with the SQLite store's twin"). PG uses `extract(epoch FROM (finished_at - started_at))` and `workflow->>'x'` / `bool_or`; SQLite uses `(julianday(finished_at) - julianday(started_at)) * 86400.0`, `json_extract(workflow, '$.x')`, `max(state='landed')`, and unqualified `FROM attempts`.
- **Read-side stays read-side.** Aggregation is read-time (views) or client-side; never a write-time summary table (phases 1–4 touch zero write paths). Layer 2's only writes are *additive keys inside the existing `workflow` jsonb* — never new columns, never a new table.
- Imports at top of module, never inline. Commit messages: `task: workflow-stats phase N — <summary>`.

## Phase map and interface ledger

Sequential; each phase consumes only the **Produces** lines of earlier phases (restated in its own header, so you never need another phase's diff). **Layer 1 (phases 1–4) is independently shippable** — stop here for a complete, valuable v1. Layer 2 (5–7) is purely additive.

| Phase | Delivers | Layer | Session context risk |
|---|---|---|---|
| 1 | `stats_by_workflow` + `stats_by_workflow_step` (+ `stats_workflow_tasks`) views, both dialects | 1 | Low — copy `stats_by_enhanced` |
| 2 | Store methods + `/api/stats` keys | 1 | Low — additive |
| 3 | Workers-page tables (two new stat tables) | 1 | Low — reuse `renderStatTable` |
| 4 | Analytics: `workflow` in analytics view + `completed` flag + two dimensions + economics panel | 1 | Medium — client aggregation |
| 5 | Write enrichment: `execution_id`/`visit`/`emitted_signal`/`routed_to` on the `workflow` jsonb | 2 | Medium — touches dispatch + transition |
| 6 | `/api/analytics/workflows` + three-level aggregator | 2 | High — retry/visit/execution reconstruction |
| 7 | Analytics "Workflows" mode: funnel graph, bottleneck/route/path panels, coverage banner | 2 | Medium — new UI |

---

## LAYER 1 — read-only aggregation (v1, independently shippable)

### Phase 1 — the two workflow SQL views (both dialects)

**Read first:** spec §2 (vocabulary + metric set), §3.2 (the views); `src/nightshift/assets/migrations/20260801000001_nightshift_enhance_tracking.sql` (the whole file — `stats_by_enhanced` is the template, verbatim PG dialect + `-- migrate:up`/`-- migrate:down`); `src/nightshift/manager/store_sqlite.py:255-398` (the `_SCHEMA` stats-view block; `stats_by_enhanced` at :351-374 is the SQLite twin template; new views append before the closing `"""` at :398); `ls src/nightshift/assets/migrations/ | tail -1` (confirm the latest is `20260802000002`; bump the new timestamp past it). Nothing else.

**Files:**
- Create: `src/nightshift/assets/migrations/20260803000001_nightshift_workflow_stats.sql`
- Modify: `src/nightshift/manager/store_sqlite.py` (`_SCHEMA` — append three views after `stats_by_queue`)
- Test: `tests/test_nightshift_store.py` (extend — mirror `test_stats_by_enhanced_splits_outcomes_and_ratings` at :307-335 and `test_enhance_tracking_migration_shape` at :1021-1045)

**Produces (later phases consume exactly these view/column contracts):**

```
-- stats_by_workflow  — one row per workflow->>'name', WHERE workflow IS NOT NULL
--   columns: workflow, total_runs, completed, errored, landed, no_change,
--   blocked, rated_up, rated_down, total_loc, avg_seconds, total_turns,
--   avg_turns, total_input_tokens, total_output_tokens, total_tokens,
--   total_cache_read_tokens, total_cache_creation_tokens, total_cost_usd
--   (the stats_by_enhanced column core + landed/no_change/blocked split)

-- stats_by_workflow_step  — one row per (workflow->>'name', workflow->>'step')
--   the same core, plus passthrough: kind, role
--   plus: distinct_tasks (= count(DISTINCT task||'#'||queue)),
--         retry_waste_cost_usd (= sum(cost_usd) FILTER (WHERE state IN
--             ('failed','conflict','expired','aborted') OR failure_kind='validation_error')),
--         validation_errors (= count(*) FILTER (WHERE failure_kind='validation_error'))

-- stats_workflow_tasks  — end-to-end per-task rollup, GROUP BY queue, task
--   (created here, exposed in a later drill-down; §3.2 note):
--   queue, task, workflow (= max(workflow->>'name')), step_attempts,
--   task_cost_usd (= sum(cost_usd)), task_tokens, task_turns,
--   wall_seconds (= max(finished)-min(started)),
--   compute_seconds (= sum(finished-started)),
--   landed_task (= bool_or(state='landed')), started_at, finished_at
```

**Definitions content** — copy `stats_by_enhanced`'s column expressions verbatim; swap `GROUP BY enhanced` for the workflow grouping and add `WHERE workflow IS NOT NULL`. The `completed`/`errored`/`total_loc`/`avg_seconds`/token/`avg_turns` expressions are byte-identical to the template. `stats_by_workflow_step` adds the `kind`/`role` passthrough (`workflow->>'kind'`, `workflow->>'role'` in the GROUP BY; SQLite `json_extract`), `distinct_tasks`, `retry_waste_cost_usd`, `validation_errors`. `stats_workflow_tasks` groups on `(queue, task)` (no `WHERE workflow IS NOT NULL` on the group key — but only workflow tasks are interesting, so filter `WHERE workflow IS NOT NULL` there too so single-shot tasks don't appear).

**Steps:**
- [ ] Write the PG migration (three `CREATE VIEW`s under `-- migrate:up`; three `DROP VIEW IF EXISTS` in reverse order under `-- migrate:down`).
- [ ] Append the three SQLite twins to `_SCHEMA` (json_extract, julianday idiom, `FROM attempts`, `max(state='landed')` for `landed_task`).
- [ ] Write failing tests: insert workflow attempts with a `workflow` fixture dict across two steps + a retry pair on one step + a non-workflow attempt; assert `stats_by_workflow` groups only workflow rows (non-workflow excluded), `completed` counts `no_change` doc steps, `stats_by_workflow_step` splits by `(name,step)` with correct `kind`/`role`/`retry_waste_cost_usd`/`distinct_tasks`, and `stats_workflow_tasks` sums cost + spans wall time across a task's rows. Add a migration-shape test mirroring `test_enhance_tracking_migration_shape`.
- [ ] Run `uv run pytest tests/test_nightshift_store.py -x -q` — expect collection/assertion failures.
- [ ] Implement views; tests pass; `just validate`; commit `task: workflow-stats phase 1 — workflow SQL views (both dialects)`.

---

### Phase 2 — store methods + `/api/stats` keys

**Read first:** spec §3.2; `src/nightshift/manager/store.py:316-321` (stats-method protocol stubs), `:901-938` (`SqlStoreBase` stats implementations — `stats_by_model` at :916-919 is the simplest template; `stats_by_enhanced` at :926-938 shows the row-shaping pattern); `src/nightshift/manager/api_operator.py:1020-1035` (the `get_stats` handler). Phase 1's Produces. Do **not** read the UI or analytics modules.

**Files:**
- Modify: `src/nightshift/manager/store.py` (protocol stubs + `SqlStoreBase` methods; `PgStore`/`SqliteStore` inherit — no override)
- Modify: `src/nightshift/manager/api_operator.py` (`get_stats`)
- Test: `tests/test_nightshift_store.py` (method round-trip), `tests/test_nightshift_manager.py` (`/api/stats` keys — mirror the existing key assertions at :174-176 / :1154-1155)

**Produces:**

```python
# store.py — protocol (near :321) + SqlStoreBase (near :919, ORDER BY the group key):
async def stats_by_workflow(self) -> list[dict[str, Any]]: ...        # ORDER BY workflow
async def stats_by_workflow_step(self) -> list[dict[str, Any]]: ...   # ORDER BY workflow, step
# (stats_workflow_tasks is NOT exposed here — deferred to a drill-down; the view
#  exists from Phase 1 but no store method / API key yet, to keep /api/stats
#  O(#workflows + #steps), not O(#tasks).)

# api_operator.py — get_stats gains two additive keys:
#   "by_workflow":      [jsonable(r) for r in await store.stats_by_workflow()],
#   "by_workflow_step": [jsonable(r) for r in await store.stats_by_workflow_step()],
```

Both methods are plain `SELECT * FROM nightshift.<view> ORDER BY …` → `[dict(r) for r in rows]`, exactly like `stats_by_model` (no boolean-widening needed — grouping is on text keys, unlike `stats_by_enhanced`). Empty list when no workflow attempts exist.

**Steps:**
- [ ] Failing tests: `stats_by_workflow()` / `stats_by_workflow_step()` round-trip through `SqliteStore` (reuse Phase 1's fixtures); `/api/stats` response contains `by_workflow` + `by_workflow_step` (empty arrays with no workflow data) and all **existing** keys unchanged (snapshot-compare the existing key set).
- [ ] Implement; tests + full suite; `just validate`; commit `task: workflow-stats phase 2 — store methods + /api/stats keys`.

---

### Phase 3 — Workers-page tables

**Read first:** spec §3.2 (Workers surface), §6 item 4; `src/nightshift/assets/ui/workers.js:140-166` (`STAT_COLS`, `renderStatTable`), `:250-283` (`refreshWorkers` fetch + the `renderStatTable` call block at :274-278); `src/nightshift/assets/ui/index.html:278-297` (the by-backend/by-queue `<table>` skeletons + `<tbody id="by-…-body">` convention). Phase 2's Produces. Do **not** read analytics.js or the store.

**Files:**
- Modify: `src/nightshift/assets/ui/index.html` (two new `<h3>` + `<table>` blocks after :297)
- Modify: `src/nightshift/assets/ui/workers.js` (two `renderStatTable` calls; one small enriched renderer for the stage table)
- Test: none automated (JS/HTML); manual check with a live manager (note in the commit).

**Produces:**

- **"By workflow"** table (`<tbody id="by-workflow-body">`) rendered with the existing `renderStatTable("by-workflow-body", stats.by_workflow, "workflow")` — the 9-column schema matches verbatim.
- **"By stage"** table (`<tbody id="by-workflow-step-body">`) rendered with a small `renderWorkflowStepTable` variant that: prefixes the step key with a `kind` badge, groups rows under a workflow-name header row **in definition order** (fetch step order from `/api/workflows` — already present per the workflow-editor spec; fall back to alphabetical if unknown), and appends an `attempts/task` column (`total_runs / distinct_tasks`, formatted `1.0`–`2.3`) surfacing flaky stages. Reality (b): show `no_change` vs `landed` split in the completed cell (tooltip) so doc steps don't read as unproductive.

**Steps:**
- [ ] Add the two `<table>` skeletons in `index.html` (copy the by-queue block; the stage table gets one extra `<th>attempts/task</th>`).
- [ ] Add `renderStatTable("by-workflow-body", stats.by_workflow, "workflow")` and `renderWorkflowStepTable(...)` in `refreshWorkers`; both hide (skip render) when the array is empty.
- [ ] Manual check: run a manager with a workflow task's attempts in the store; confirm both tables render and non-workflow fleets show them empty/hidden. Commit `task: workflow-stats phase 3 — Workers-page workflow tables`.

---

### Phase 4 — Analytics: workflow dimensions + economics panel

**Read first:** spec §2.2 (metric set + spend buckets), §3.1 (the plumbing fix), §3.3 (client panel), §5 (realities a/b/f for v1); `src/nightshift/manager/views.py:66-89` (`ANALYTICS_RUN_KEYS` + `analytics_run_view`); `src/nightshift/manager/api_operator.py:955-967` (`get_analytics_runs`); `src/nightshift/assets/ui/analytics.js:104-154` (`aggregate`), `:366-374` (`groupBy`), `:416-458` (`renderEnhancement` panel pattern), `:919-925` (`queueKey`/`queueLabel` flatten precedent), `:1001-1021` (`dims` + dimension select), `:1048` (`r[view.dimension]` filter), `:1065-1076` (panel wiring in `renderBody`); `src/nightshift/price.py:35-71` (`_RATES`, `_CACHE_READ_MULT`=0.1, `_CACHE_WRITE_MULT`=1.25, `normalize_model`). Phase 1–3 Produces.

**Files:**
- Modify: `src/nightshift/manager/views.py` (`ANALYTICS_RUN_KEYS` + `analytics_run_view`)
- Modify: `src/nightshift/assets/ui/analytics.js` (dimensions + economics panel)
- Test: `tests/test_nightshift_manager.py` (extend `test_api_analytics_runs_landed_flag_and_shape` at :2256-2280 for the new fields)

**Produces:**

```python
# views.py — analytics_run_view() gains a branch and ANALYTICS_RUN_KEYS gains 3 keys:
#   add "workflow", "completed", "state" to ANALYTICS_RUN_KEYS
#   in the loop:
#     elif key == "completed":
#         out[key] = state in ("landed", "no_change")
#   ("workflow" and "state" fall through the generic attempt.get(key);
#    "workflow" is already a decoded dict/None. Does NOT touch run_view or
#    redefine the existing "landed" flag — "completed" is parallel.)
```

```js
// analytics.js
// - wfName(r) / wfStep(r): flatten the nested workflow at the keyFn (queueKey precedent).
//   wfStep returns "name:step" to disambiguate same-named steps across workflows.
// - DIM_KEYFNS map: { model, backend, queue, workflow: wfName, workflowStep: wfStep };
//   const keyFn = DIM_KEYFNS[view.dimension] || ((r) => r[view.dimension]);
//   (replaces the inline (r) => r[view.dimension] at :1048 and in the dim select)
// - dims array gains { id: "workflow", label: "Workflow" } and { id: "workflowStep", label: "Workflow step" }
// - renderWorkflowEconomics(container, runs): a renderEnhancement-style panel,
//   gated on presence of any r.workflow, wired in renderBody after renderEnhancement.
```

**`renderWorkflowEconomics` computes (spec §2.2, §3.3), all client-side over the returned rows:**
- **Task rollup** keyed by `(queue, task)` (reality f): `task_cost = Σcost`, `wall = max(finished)−min(started)`, `landed_task = any(state==='landed')`, `task_mode = 'workflow' if any r.workflow else 'enhanced' if any r.enhanced else 'single'`.
- **CPLT cohort deltas**: `CPLT(cohort) = Σ task_cost over landed tasks / #landed tasks`; show `workflow` vs `single` vs `enhanced` with p50/p90 (not just mean), cohort sizes, rating coverage, labeled **observational, not causal**.
- **Stage cost share** per workflow (stacked bar): each step's Σcost ÷ workflow total.
- **Spend buckets**: `productive_code` / `productive_ceremony` / `assurance`(→folded into ceremony until Layer 2) / `retry_waste` / `loop_waste`(proxy: Σcost of 2nd+ completion of a step per task) / `other`; `wasted_cost_ratio = Σ(retry+loop) / Σ`. Assurance shown beside waste, never inside.
- **Cache savings $**: `(cache_read·0.9 − cache_creation·0.25)·rate_in / 1e6` per priced attempt (mirror `price.py` rates in a small JS table or expose a rate endpoint — do **not** hard-code into SQL); report `coverage` = fraction of input tokens on priced models; skip unpriced (never invent $0).

**Steps:**
- [ ] Failing test: `/api/analytics/runs` records carry `workflow` (dict/None), `completed` (bool, true for `no_change`), and `state`; existing `landed` flag unchanged; non-workflow record has `workflow == None`.
- [ ] Implement `views.py` (2-line-ish change) + the analytics.js dimensions and panel; verify existing "By model"/"By backend"/"By queue" breakdowns and every existing KPI are byte-identical (the `DIM_KEYFNS` fallback preserves them; `completed` is only read by the new panel).
- [ ] Manual check: Analytics page shows the two new dimensions and the economics panel with real workflow data; a zero-workflow fleet sees no new panel and unchanged existing panels.
- [ ] Tests + full suite; `just validate`; commit `task: workflow-stats phase 4 — analytics workflow dimensions + economics panel`.

> **Layer 1 is complete and shippable here.** Phases 5–7 are additive; ship, dogfood, and gather operator feedback before starting Layer 2.

---

## LAYER 2 — exactness upgrade & stage-flow funnel (additive)

### Phase 5 — write enrichment: four immutable facts on the `workflow` jsonb

**Read first:** spec §4 (the four facts + when each is written), §5 (realities with §4); workflows-spec §6.3–§6.4 (transitions + entry-based visit counting); `src/nightshift/manager/api_worker.py:318-419` (`_lease_and_build` — `wf_persisted` built at :380-387, first-step engine_meta at :357-373, `create_attempt(workflow=...)` at :388-405), `:155-209` (`_build_workflow_step_policy`), `:735-738` (policy build in submit), `:832-835` (engine_meta applied post-commit); `src/nightshift/transitions.py:679-687` (`_advance_visits`), `:788-844` (`on_workflow_step`), `:925-931` (code-step land advance); `src/nightshift/manager/store.py:220-240,494-541` (`create_attempt` — already takes `workflow`). Phase 1–2 Produces.

**Files:**
- Modify: `src/nightshift/manager/api_worker.py` (stamp `execution_id`/`visit` into the persisted `workflow` dict at dispatch; stamp `emitted_signal`/`routed_to` into the engine_meta/attempt-fields write on advance)
- Modify: `src/nightshift/transitions.py` (thread `emitted_signal`/`routed_to` into the advance effect so the *submitting* attempt's `workflow` block is updated in the same transaction)
- Modify: engine-owned frontmatter: add `workflow_execution_id` alongside `workflow_step`/`workflow_visits` (hidden, engine-owned; generated at first cursor init, regenerated on evergreen reset / operator restart)
- Test: `tests/test_nightshift_store.py` (workflow-column round-trip with the four keys), `tests/test_transitions_workflow.py` (the advance effect writes signal/route), `tests/test_nightshift_manager.py` (dispatch stamps execution_id/visit; submit stamps emitted_signal/routed_to)

**Produces (the enriched `workflow` jsonb on each attempt row):**

```json
{ "name": "...", "step": "review", "kind": "doc", "role": "...", "signals": [...], "output": "...",
  "execution_id": "wf_01K...",     // fixed at attempt creation from workflow_execution_id frontmatter
  "visit": 1,                       // read from workflow_visits at dispatch; retries share it; back-edges increment
  "emitted_signal": "review-clear", // written in the advancing transition; null on failure/default route
  "routed_to": "implement" }        // resolved destination incl "$end"; code/split write it only on land/harvest success
```

**Semantics (spec §4, §5):**
- `execution_id` + `visit` set at `create_attempt` time (dispatch) — retries of the same visit reuse both (the cursor hasn't moved). Evergreen reset generates a fresh `workflow_execution_id`; do **not** regenerate on retry/hold/manager-restart/cursor-advance.
- `emitted_signal` + `routed_to` written in the **same store transaction** that advances the cursor — never derived from `result_line`, never from the (editable) definition. A failed/non-advancing attempt leaves both null. Code/split routes are final only after async land/harvest success.
- These are **additive keys inside existing jsonb** — no new column, no new table; `create_attempt` already serializes the `workflow` dict, so wire-compat is automatic (absent keys ⇒ None on old rows).

**Steps:**
- [ ] Failing tests: dispatch of a workflow task's first step stamps `execution_id` + `visit:1`; a retry of a failing step reuses the same `execution_id`/`visit`; a doc advance writes `emitted_signal`/`routed_to` on the submitting attempt in one transaction; a back-edge (looping-verify fixture) increments `visit`; evergreen reset regenerates `execution_id`.
- [ ] Implement; **non-workflow tasks and Layer-1 views must be byte-identical** (the new keys are ignored by Layer-1 aggregation); tests + full suite; `just validate`; commit `task: workflow-stats phase 5 — attempt workflow enrichment (execution_id, visit, signal, route)`.

---

### Phase 6 — `/api/analytics/workflows` + three-level aggregator

**Read first:** spec §4 (three-level model + endpoint shape), §5; `src/nightshift/manager/store.py:244-251` (`list_attempts` — the `since`/`queue`/`limit` reader; the aggregator reads normalized attempt rows), `:901-938` (stats method pattern for a new read method); `src/nightshift/manager/api_operator.py:955-967` (analytics handler pattern); Phase 5's Produces. Do **not** read the UI.

**Files:**
- Create: `src/nightshift/manager/workflow_analytics.py` (pure aggregator: rows → funnel/stage/edge/path/execution payload; no store/HTTP imports)
- Modify: `src/nightshift/manager/store.py` (one read method `workflow_analytics_rows(since, until, queues, workflow)` — selects executions whose min `started_at` is in-cohort, returns all their attempts through `until`)
- Modify: `src/nightshift/manager/api_operator.py` (`GET /api/analytics/workflows`)
- Test: `tests/test_workflow_analytics.py` (new — the aggregator against hand-built rows; both stores via the store method)

**Produces:**

```python
# workflow_analytics.py — pure, tested with fixtures:
def aggregate_workflows(rows: list[dict], *, workflow: str | None) -> dict:
    """Three-level model (spec §4):
      attempt  = one row; completed = state in (landed, no_change)
      visit    = (execution_id, step, visit); retries = attempts-1;
                 advancing attempt = the one with non-null routed_to;
                 wall = max(finished)-min(started); active = Σ(finished-started); wait = wall-active
      execution= trip to $end/abandon/live; retries = attempts-visits;
                 completed iff advancing attempt routed_to='$end' with state in (landed,no_change)
      Returns { period, coverage, summary, stages[], edges[], paths[], comparison, executions[] }.
      Legacy rows (no execution_id/visit): best-effort (queue,task) grouping,
      consecutive same-step = one visit, EXCLUDED from exact edge %s; coverage reports the fraction."""

# store.py:
async def workflow_analytics_rows(self, *, since, until, queues, workflow) -> list[dict]: ...

# api_operator.py:
# GET /api/analytics/workflows?since&until&queue(repeatable)&workflow
#   workflow omitted -> lightweight index (name, executions, completion, spend, last_seen)
```

**Steps:**
- [ ] Failing tests (the spec's acceptance list, §"acceptance"): two failures then success = one visit / three attempts / two retries; a loop back to `implement` = a second implement visit; evergreen task = one execution per reset cycle; doc `no_change` advances, adds 0 to LOC averages; code doesn't route until async land succeeds; split completes without inheriting child costs; aggregate totals invariant under execution-list pagination; legacy rows counted in spend but excluded from edge %s; coverage reported.
- [ ] Implement the pure aggregator, the store read, the endpoint; tests + full suite; `just validate`; commit `task: workflow-stats phase 6 — workflow analytics aggregator + endpoint`.

---

### Phase 7 — Analytics "Workflows" mode (funnel UI)

**Read first:** spec §4 (funnel/bottleneck/route/path/explorer + coverage banner); `src/nightshift/assets/ui/analytics.js:416-458` (panel pattern), `:1001-1021` (mode/dimension controls), `:1065-1076` (panel wiring); Phase 6's endpoint shape. Phase 4's dimension work.

**Files:**
- Modify: `src/nightshift/assets/ui/analytics.js` (a "Workflows" mode fetching `/api/analytics/workflows`)
- Modify: `src/nightshift/assets/ui/index.html` / `style.css` as needed for the funnel graph
- Test: none automated; manual check with a live manager.

**Produces (all from the Phase 6 payload; spec §4):** a "Workflows" mode with a KPI strip; the **stage-flow funnel graph** (nodes sized by visits, tinted by mean cost, red border = unresolved-error rate; signal-labeled edges with counts/shares; retry-loops visually distinct from workflow back-edges; `$end`/open/blocked/failed exits on the right); a **bottleneck table** (p50/p90 wall vs active vs wait per stage; LOC "n/a" for doc, "split completed" for split); a **route/path outcomes** panel (full path signatures ranked by cost/completion/retries); the **workflow-vs-single-shot distribution** comparison (p50/p90, cohort sizes, "observed differences" copy); an **execution explorer** reconciling aggregates to raw rows; and the **coverage banner** for pre-instrumentation rows.

**Steps:**
- [ ] Build the mode + panels; gate on presence of workflow data; existing Analytics modes untouched.
- [ ] Manual check: funnel renders for `plan-review-implement` with real data; loop back-edge and retry-loop are visually distinct; coverage banner shows for mixed old/new data. Commit `task: workflow-stats phase 7 — analytics workflows funnel UI`.

---

## Acceptance

**After Layer 1 (phase 4):** With a scratch DB of workflow + single-shot attempts, the Workers page shows "By workflow" and "By stage" tables, and the Analytics page offers "Workflow"/"Workflow step" dimensions plus a Workflow economics panel answering, in one screen: does `plan-review-implement` cost more per landed task than single-shot, how much of that is ceremony vs waste, and are planner-stage cache hits paying off. A zero-workflow fleet sees **no** behavior change on any existing surface, and `just validate` is green throughout.

**After Layer 2 (phase 7):** A `verify-refine` and a `plan-review-implement` task (including a looping-verify fixture) run end-to-end; `/api/analytics/workflows` reconstructs exact visits/retries/executions per the phase-6 acceptance list; the funnel renders routing/signal rates (`plan-trivial`, `review-clear`, `verify-clear`) that Layer 1 could not express; evergreen cycles appear as distinct executions; and pre-instrumentation history is included in spend but excluded from exact edge percentages behind a visible coverage banner. Then the reflexive dogfood: point the Analytics Workflows mode at the fleet that implemented this very plan.
