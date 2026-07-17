# Nightshift — Workflow & workflow-stage statistics

**Subject:** Track statistics broken down *per workflow* and *per workflow-stage*, answering the operator's real question: **are these multi-step workflows worth the money and latency versus single-shot, and if not, which stage is the problem?**
**Status:** Design proposal.
**Grounding:** `docs/spec/2026-07-16-workflows.md` (esp. §6.3–§6.5 transitions, §6.4 visit accounting, §7.4 chaining, §8 token discipline); the `nightshift.attempts.workflow` jsonb column (migration `20260802000002`); the existing `stats_by_*` SQL views + `GET /api/stats`; `GET /api/analytics/runs` + client-side `analytics.js`; `stats_by_enhanced` as the "was this ceremony worth it?" precedent.
**Default:** No changes to dispatch, submit, or workflow-engine semantics. Non-workflow paths behave byte-identically. This is an additive, read-side-first feature with one small, optional write-side enrichment (§4) gated behind a phase.

---

## 0. The one idea

Every existing stats cut is a `GROUP BY <dimension>` over `attempts`. Workflow name and `(name, step)` are just two more dimensions sitting inside a column the store already writes. But two things make workflows genuinely different from every prior dimension, and the design must respect both:

1. **A workflow task is N attempt rows, not one.** The unit an operator cares about — "what did this whole `plan-review-implement` task cost and how long did it take" — is a *rollup across rows keyed by `(queue, task)`*, not a single row. The north-star metric is therefore **task-grain**, not attempt-grain: **cost per landed task**.
2. **A stage is entered as *visits*, each visit is one-or-more *attempts*.** A failing step retries without advancing the cursor (spec §6.4), so several attempt rows share one visit; a loop re-enters the same step as a new visit. Conflating attempt / visit / execution corrupts every retry, loop, and evergreen number. The metric model is explicitly **three-level** (§5).

The design ships in two layers that mirror the two existing stats surfaces:

- **Read-only v1 (no write path):** SQL views (`stats_by_workflow`, `stats_by_workflow_step`) on the Workers page for the operational rollup, plus a **Workflow economics** panel on the Analytics page (client-side, time-windowed) for the FinOps story. Handles retries correctly by summing; approximates loops/visits with an honest, labeled proxy that is *exact for all three shipped (non-looping) workflows*.
- **Exactness upgrade (small write enrichment, phased):** stamp four immutable facts onto the attempt's `workflow` jsonb — `execution_id`, `visit`, `emitted_signal`, `routed_to` — which unlock exact visit/loop/evergreen accounting and the **stage-flow funnel** (routing/signal analysis) that read-only aggregation fundamentally cannot reconstruct.

The v1 layer answers "is it worth it and where's the money going"; the funnel layer answers "which decisions does the workflow actually make and where does it get stuck." Ship v1 first; it is independently valuable and the funnel is purely additive on top.

---

## 1. Verified facts about the current code

Read from `src/nightshift/manager/views.py`:

- `RUN_VIEW_KEYS` **includes** `"workflow"` (line 36) — `GET /api/runs` history already carries the block.
- `ANALYTICS_RUN_KEYS` **does not** include `"workflow"` (lines 67–73) — `GET /api/analytics/runs`, the endpoint `analytics.js` consumes, drops it.
- `analytics_run_view()` computes `landed = (state == "landed")` (line 86) and has **no** `completed`/`no_change` distinction. A doc step ends in `no_change`, so under today's projection every doc-step attempt reads as "never landed" → `landRate 0%`, `costPerLanded null` — which misreads a *successful* plan/review as a failure.

Both are one- to two-line additive fixes (§3.1), exactly the lane `enhanced`/`rating` were added through. This is the load-bearing plumbing fact the whole read-only layer rests on.

`attempts.workflow` (per migration `20260802000002` + spec §6.2) stores the work-order block **minus artifacts**:

```json
{"name": "plan-review-implement", "step": "plan", "kind": "doc",
 "role": "planner", "signals": ["plan-trivial"], "output": "plan"}
```

`NULL` for every non-workflow attempt. Every field is a top-level scalar, so extraction is one `->>` (PG) / `json_extract(...,'$.x')` (SQLite). The **emitted signal and the routed-to destination are NOT on the row today** — they drive routing through the transition and appear only in `result_line` text (not a stable contract). This is the single fact that forces the write enrichment for funnel analysis (§4).

---

## 2. Metrics

### 2.1 Vocabulary (codebase's existing definitions, verbatim)

- **completed** = `state IN ('landed','no_change')` — doc steps that end `no_change` count as completed.
- **errored** = `state IN ('failed','conflict')`.
- **landed task** = a `(queue, task)` with ≥1 attempt in `state = 'landed'`.
- **doc ceremony** = a workflow attempt with `kind = 'doc'` (lands nothing; cost is real and expected — *not* waste).
- **avg_seconds** = `avg(finished_at - started_at) FILTER (WHERE finished_at IS NOT NULL)` — derived; no duration column, ever.

### 2.2 The metric set (why each one earns its place)

| Metric | Grain | Answers |
|---|---|---|
| **Cost per landed task (CPLT)** by workflow | task | "Is `plan-review-implement` worth its money?" — the north star. |
| **Task land rate** (landed tasks / tasks) by workflow | task | "Does the ceremony actually raise the success rate?" |
| **End-to-end wall-clock** = `max(finished) − min(started)` by task | task | "How long does a whole workflow task take?" (what the operator *feels*, includes queue/chaining gaps.) |
| **Stage cost share %** | stage | "Which stage is the money sink — cheap plan, expensive implement?" |
| **Spend-bucket taxonomy** (below) | attempt→stage | "How much spend is *productive* vs *wasted*?" |
| **Signal / route rates** (`plan-trivial`, `review-clear`, `verify-clear`) | visit | "How often does the escape hatch fire; is review/verify earning its keep?" — funnel layer (§4). |
| **Retries-per-visit** & **retry cost** | visit | "Which stage is flaky and burning re-attempts?" |
| **Cache hit rate** (`cache_read / input_tokens`) & **cache savings $** | stage | "Is chaining/session-resume (spec §7.4–7.5) actually cutting token cost?" |
| **Workflow vs single-shot delta** (CPLT / land-rate / wall-clock) | cohort | "The ROI verdict, head to head." |

**Spend-bucket taxonomy** — every workflow attempt lands in exactly one bucket (grafted from the FinOps base, refined with the visit-aware definitions from the metric-model graft):

| Bucket | Predicate |
|---|---|
| `productive_code` | `kind='code'` ∧ `state='landed'` |
| `productive_ceremony` | `kind IN ('doc','split')` ∧ `state='no_change'` ∧ first completion of that step-visit |
| `assurance` | a `verify` step completing `no_change` via `verify-clear` (found nothing, but bought confidence) — **exact only with §4**; until then folded into `productive_ceremony` |
| `retry_waste` | `state IN ('failed','conflict','expired','aborted')` ∨ `failure_kind='validation_error'` |
| `loop_waste` | a 2nd-or-later completion of the same step for the task (exact with §4's `visit`; proxied in v1, §5.3) |
| `other` | `blocked`/`skipped`/quarantine leftovers |

`wasted_cost_ratio(W) = Σ cost(retry_waste ∪ loop_waste) / Σ cost(name = W)`. **Assurance is not waste** — a verify that finds nothing still bought confidence; it sits *beside* waste, never inside it.

> LOC caution: never make `cost_per_loc` a primary workflow KPI — doc-heavy workflows look "expensive per LOC" by construction. Show it only on the landed/code subset.

---

## 3. Layer 1 — read-only aggregation (v1)

### 3.1 The plumbing fix (2 lines server-side)

Add `"workflow"` and a `"completed"` flag to the analytics projection so the client can group by workflow and read doc-step success correctly (§1):

```python
# views.py — analytics_run_view()
elif key == "completed":
    out[key] = state in ("landed", "no_change")
# ...and add "workflow", "completed", "state" to ANALYTICS_RUN_KEYS
```

`workflow` falls through the generic `attempt.get(key)` (already a decoded dict/None); `state` is exposed on the analytics projection only (waste classification needs the raw state). This does **not** touch `run_view` (frozen) and does **not** redefine the existing `landed` flag — `completed` is a *parallel* flag so every existing KPI (`costPerLanded`, model/backend/queue breakdowns) stays byte-identical.

### 3.2 SQL views (Workers page / `/api/stats`)

New migration `20260803000001_nightshift_workflow_stats.sql` (`-- migrate:up`/`-- migrate:down`; timestamp after `20260802000002`), with the SQLite twin appended to `store_sqlite._SCHEMA` in lockstep (the `stats_by_enhanced` convention). Two views, mirroring `stats_by_model` / `stats_by_enhanced` column-for-column so the existing `renderStatTable` renderer works unchanged, plus the workflow-specific additions.

**`stats_by_workflow`** — one row per `workflow->>'name'`, `WHERE workflow IS NOT NULL`, with the standard column core (`total_runs`, `completed`, `errored`, `landed`, `no_change`, `blocked`, `rated_up`/`rated_down`, `total_loc` filtered to completed, `avg_seconds`, `total_turns`, `avg_turns`, token totals, cache token totals, `total_cost_usd`).

**`stats_by_workflow_step`** — one row per `(name, step)`, same core plus passthrough `kind` and `role`, plus:
- `distinct_tasks = count(DISTINCT task || '#' || queue)` — the denominator for the retry proxy.
- `retry_waste_cost_usd = sum(cost_usd) FILTER (WHERE state IN ('failed','conflict','expired','aborted') OR failure_kind='validation_error')`.
- `validation_errors = count(*) FILTER (WHERE failure_kind='validation_error')`.

PG uses `workflow->>'name'` / `workflow->>'step'` / `workflow->>'kind'` / `workflow->>'role'`; the SQLite twin uses `json_extract(workflow,'$.name')` etc., the `(julianday(finished_at)-julianday(started_at))*86400.0` duration idiom, `FROM attempts` (unqualified), and `max(state='landed')` where PG uses `bool_or`. `FILTER (WHERE …)` is supported by SQLite ≥ 3.30 (existing views already rely on it).

Two store methods (`stats_by_workflow`, `stats_by_workflow_step`) mirror the existing `stats_*` methods on both PG and SQLite stores. `/api/stats` gains two additive keys `by_workflow`, `by_workflow_step` (empty arrays when no workflow attempts exist → UI hides the tables).

> **Optional `stats_workflow_tasks` rollup view** (created in the same migration, exposed later via `GET /api/stats/workflow/{name}/tasks` to keep the default `/api/stats` O(#workflows+#steps), not O(#tasks)): `GROUP BY queue, task` with `sum(cost_usd) AS task_cost_usd`, `max(finished)-min(started) AS wall_seconds`, `sum(finished-started) AS compute_seconds` (the wall-vs-compute distinction — grafted from the SQL-view candidate — separates latency-optimizers from spend-optimizers), `bool_or(state='landed') AS landed_task`.

### 3.3 Client-side (Analytics page) — the FinOps panel

The task-grain ROI numbers (`CPLT`, cohort deltas, `wasted_cost_ratio`, `cache_savings_usd`) need `GROUP BY (queue,task)` then a second aggregation by cohort — cheaply done client-side over the ≤2000 rows `/api/analytics/runs` already returns, matching the existing `analytics.js` pattern. Add two **dimension** options ("Workflow", "Workflow step") via a small `DIM_KEYFNS` map (grafted from the minimal-diff candidate — flatten the nested `workflow` at the `keyFn`, don't rewrite records) so the existing breakdown table renders per-workflow / per-stage rollups with zero new renderer.

**Cache savings estimate** (USD) — compute in JS/Python against `price.py` rates, never hard-coded into SQL:
`cache_savings_usd = (cache_read·0.9 − cache_creation·0.25)·rate_in / 1e6` summed per priced attempt; report `coverage` = fraction of input tokens on priced models (skip unpriced, never invent $0). This directly measures whether chaining/session-resume (spec §8) is paying off — expect higher hit rate on planner-role steps.

**Cohorts** for the ROI comparison use a **task-mode** key so a task that *ever* ran as a workflow is counted (with its full spend, including any later single-shot cleanup):
`task_mode = 'workflow' if any attempt.workflow else 'enhanced' if any attempt.enhanced else 'single'`.
`ΔCPLT = CPLT(workflow) − CPLT(single)`, likewise `Δland_rate`, `Δwall`. Show p50/p90, not just means (retry-heavy tasks skew means), with cohort sizes and rating coverage prominent, labeled **observational, not causal** (workflow and single-shot briefs may differ in difficulty; the queue filter is the v1 "same kind of work" control).

---

## 4. Layer 2 — the exactness upgrade & stage-flow funnel (phased, small write)

Read-only aggregation cannot reconstruct four facts (§1): which signal fired, which edge was taken, whether two same-step rows are retries or loop visits, and which evergreen cycle a row belongs to. Rather than a parallel events table or `workflow_runs`/`workflow_visits`/`workflow_edges` tables (rejected — see §7), stamp **four immutable facts onto the existing `workflow` jsonb**, keeping `attempts` the sole telemetry table:

```json
{ "name": "...", "step": "review", "kind": "doc", "role": "...",
  "execution_id": "wf_01K...",   // fixed at first cursor init; new one on evergreen reset / operator restart
  "visit": 1,                     // read from workflow_visits at dispatch; retries share it, back-edges increment it
  "emitted_signal": "review-clear",  // appended atomically in the advancing transition (null on failure/default)
  "routed_to": "implement" }         // resolved destination incl "$end"; for code/split, written only on land/harvest success
```

`execution_id` + `visit` are set at attempt creation (dispatch); `emitted_signal` + `routed_to` are written in the **same store transaction** that advances the cursor (spec §6.3) — never derived from `result_line`, never from the current (editable) definition. This is the correct home for the exact **three-level metric model** (grafted wholesale from the funnel candidate, which had the rigorous version):

- **Attempt** = one row. `attempt_completed = state IN ('landed','no_change')`.
- **Visit** = `(execution_id, step, visit)`; `retries = attempts − 1`; the *advancing* attempt is the one with non-null `routed_to`; cost/tokens sum across the visit; `wall = max(finished)−min(started)`, `active = Σ(finished−started)`, `wait = wall − active`.
- **Execution** = one trip to `$end` / abandonment / live cursor; `retries = attempts − visits`; `completed` iff an advancing attempt has `routed_to='$end'` with terminal `state IN ('landed','no_change')` (**not** merely an intermediate land/no_change).

With these, the **`/api/analytics/workflows`** endpoint (server-side aggregation — route semantics + visit/cycle reconstruction shouldn't be reimplemented per-client, and the 2000-row client cap can truncate executions) returns the funnel: KPI strip, **stage-flow funnel graph** (nodes = stages sized by visits, tinted by mean cost, red border = unresolved-error rate; edges = signal-labeled routes with counts/shares; retry-loops visually distinct from workflow back-edges), a bottleneck table (p50/p90 wall vs active vs wait per stage), a **route/path outcomes** panel (rank full path signatures like `plan → review → revise → implement` by cost/completion/retries), the workflow-vs-single-shot distribution comparison, and an execution explorer that reconciles every aggregate down to raw rows.

**Historical compatibility:** pre-instrumentation rows lack the four facts. Do not fabricate exact loops/edges — group legacy `(queue,task)` rows as best-effort executions, treat consecutive same-step rows as one visit, exclude legacy from exact edge percentages, and **surface a coverage banner** ("exact flow telemetry for 98% of attempts; legacy included in spend, excluded from route rates"). No destructive backfill.

---

## 5. Workflow realities (a)–(f) — how each is handled

| # | Reality | v1 (read-only) | With §4 |
|---|---|---|---|
| (a) | failed step → multiple rows per visit | all rows sum into cost/`errored`; `retry_waste_cost_usd` isolates the burn; `attempts_per_task = total_runs/distinct_tasks` surfaces flakiness (**exact for all 3 shipped non-looping workflows**; labeled "per task" not "per visit") | exact retries = `attempts − 1` per `(execution_id, step, visit)` |
| (b) | doc steps `no_change`, no `loc` | `completed` counts them; `completed`-flag fix (§3.1) stops the `landRate 0%` misread; `total_loc` filters to landed/`no_change` → naturally 0 | unchanged; `assurance` bucket becomes exact |
| (c) | loops / `max_visits`, step entered N times | `loop_waste_proxy = Σcost(completed on step) − cost(first completion)` per task; budget-quarantine surfaced via a `failure_kind='workflow_budget'` count (**depends on the transition stamping that kind — flagged as a 1-line write-side dependency**; degrades to 0 if absent) | exact: each cursor entry increments `visit`; back-edges render as loops; quarantine is an unresolved exit, never a fabricated visit |
| (d) | split steps create children | parent's split attempt rolls into the parent `(queue,task)`; children are ordinary tasks in their own cohort — **no child→parent attribution** (no parent pointer exists) | same; label parent "split completed" not "landed"; lineage rollup still deferred |
| (e) | evergreen resets each cycle | v1 rolls **all cycles** into one task cost (honest *lifetime* cost); per-cycle deferred | `execution_id` regenerated on reset ⇒ each cycle is one execution; grouping evergreen by `(queue,task)` alone is **forbidden** |
| (f) | end-to-end rollup across N rows keyed by `(queue,task)` | `stats_workflow_tasks` view / client rollup: `sum(cost)`, `max(finished)−min(started)`, `bool_or(landed)` | `execution_id` is the exact key; `(queue,task)` remains the human-facing label |

---

## 6. Scope

**v1 (read-only, ship first):**
1. `views.py`: add `workflow`, `completed`, `state` to `ANALYTICS_RUN_KEYS` + the `completed` branch (§3.1).
2. Migration `20260803000001` + SQLite twin: `stats_by_workflow`, `stats_by_workflow_step` (+ `stats_workflow_tasks`, created but exposed later).
3. Two store methods; two additive `/api/stats` keys.
4. Workers page: two tables (reused `renderStatTable`; stage table grouped under workflow-name headers in definition order via the loaded defs).
5. Analytics page: "Workflow"/"Workflow step" dimensions + a **Workflow economics** panel (stage cost share, spend-bucket waste vs assurance, cache savings, CPLT cohort deltas with p50/p90, observational-copy block).
6. Docs + attribution-rule tests (retry pair on one step; doc `no_change` in ceremony not waste; NULL excluded; split parent excludes children; CPLT math).

**Phase 2 (exactness + funnel, additive):**
7. Write enrichment: `execution_id`/`visit` at dispatch, `emitted_signal`/`routed_to` in the advancing transition (one store transaction).
8. `/api/analytics/workflows` + the three-level aggregator (shared, tested against both stores).
9. Analytics "Workflows" mode: funnel graph, bottleneck table (p50/p90 wall/active/wait), route/path panel, cohort distributions, execution explorer, coverage banner.
10. Partial PG index `((workflow->>'name'), started_at DESC) WHERE workflow IS NOT NULL` if volume warrants.

**Deferred (both phases):** split parent→child lineage rollup; per-cycle evergreen partitioning UI beyond `execution_id`; causal/quality claims about review/verify artifacts; generic execution ids for non-workflow tasks; materialized daily cubes; cache-affinity scheduler metrics; the `failure_kind='workflow_budget'` write-side change (small, orthogonal) to make the quarantine count non-zero.

---

## 7. Rationale — grafts, base, and rejections

**Base: the FinOps / economics-first candidate.** It had the strongest *metric substance* (task-grain cost-per-landed-task as the north star, the spend-bucket taxonomy separating productive ceremony from waste, the workflow-vs-single-shot ROI answer with a worked example) and *data-model fidelity* (read-only, correct dual-dialect SQL, honest deferral of evergreen/split), and it correctly identified that attempt-grain `costPerLanded` systematically *understates* workflow cost. It is the shape easiest to extend.

**Grafted in:**
- **From the stage-flow-funnel candidate** (its highest-signal contribution): the rigorous **three-level metric model** (attempt / visit / execution) and its consequence — the four immutable jsonb facts (`execution_id`, `visit`, `emitted_signal`, `routed_to`) written in-transaction rather than derived from `result_line` or the editable definition. This is the *correct* backbone that the base's `loop_waste_proxy` only approximated; it becomes Layer 2 (§4), which the base lacked. Also grafted: the funnel/route/path UI, p50/p90 latency with wall-vs-active-vs-wait decomposition, the coverage banner for historical rows, and the discipline of labeling cohort comparisons "observational, not causal."
- **From the minimal-diff candidate:** the *verified* plumbing fact (`workflow` in `RUN_VIEW_KEYS` but not `ANALYTICS_RUN_KEYS` — a one-line fix, confirmed by reading `views.py`) and the **`completed` vs `landed` doc-step correctness fix** — the single most important read-only correctness graft, and the `DIM_KEYFNS` flatten-at-keyfn technique for the client dimensions.
- **From the SQL-view candidate:** the exact dual-dialect view SQL and the **`wall_seconds` vs `compute_seconds`** distinction in the task rollup (latency-optimizers want wall, spend-optimizers want compute), plus the honest framing that `attempts_per_task` is *exact* for the three shipped non-looping workflows and only a proxy under loops.

**Rejected:**
- **The events / correlation-first candidate wholesale** (weakest, 17.5/35): it invented a parallel event-emission model and modified the `nightshift.tasks` table, violating the "build on the existing `workflow` column" constraint and adding a second write model. Its one good instinct — that funnel analysis needs *durable* routing facts — is captured far more cheaply by §4's four jsonb keys on the existing table.
- **A denormalized `workflow_stats` summary table updated on each transition:** puts aggregation on the hot path and risks drift from the raw rows; read-time views are always consistent.
- **Promoting `workflow_name`/`workflow_step` to top-level columns:** duplicates jsonb data and needs a backfill; a partial expression index gets the same speed if load ever bites.
- **Redefining the existing `landed` flag to include `no_change`:** would silently change every fleet-wide KPI; a parallel `completed` flag is the honest, contained fix.
- **Normalized `workflow_executions`/`workflow_visits`/`workflow_edges` tables for Layer 2:** the four immutable jsonb facts make exact server rollups possible without a second write model.
- **Doing everything client-side (no server aggregation for the funnel):** the 2000-row cap can truncate executions and each client would re-implement visit/cycle reconstruction — hence `/api/analytics/workflows` is server-side, while the Workers `/api/stats` views and the Analytics economics panel stay in their existing (view / client) lanes.
