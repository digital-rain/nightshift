# Arena synthesis note — workflow/stage statistics

Companion to `2026-07-17-workflow-stats.md`. Records how the recommendation was produced.

## Rubric (weighted, /35)
1. Metric substance (×1.5) · 2. Data-model fidelity (×1.5) · 3. Aggregation soundness (×1) · 4. Workflow-lifecycle correctness (×1) · 5. Operator surface & actionability (×1) · 6. Scope discipline & incrementality (×1).

## Candidates (5, parallel, distinct frames & model families)
| # | Frame | Model | Judge score | My call |
|---|---|---|---|---|
| 1 | SQL-view-first | Opus 4.8 | 30 | 30 — correct, disciplined, weaker on ROI/funnel |
| 2 | Operator-question / funnel-first | GPT-5.6 | 28 | 30 — best metric-model rigor + operator surface; needs a write path |
| 3 | Events / correlation-first | Gemini 3.1 Pro | 17.5 | 17.5 — over-built, parallel structures, violates "build on existing" |
| 4 | Minimal-diff / YAGNI | Sonnet 5 | 27 | 27 — cheapest real value; verified the key plumbing fact |
| 5 | Cost/ROI / FinOps-first | Grok 4.5 | 35 | 33 — best substance + fidelity; slight complexity dock |

Cross-judge: read-only Gemini 3.1 Pro. Judge and I agreed on base and ordering.

## Base
**Candidate 5 (FinOps).** Strongest metric substance (task-grain **cost-per-landed-task** north star, spend-bucket taxonomy, workflow-vs-single-shot ROI, worked example) and data-model fidelity (read-only, correct dual-dialect SQL, honest deferrals). Correctly saw that attempt-grain `costPerLanded` understates workflow cost.

## Grafts (with sources)
- **Candidate 2 → the three-level metric model** (attempt / visit / execution) and the four immutable jsonb facts (`execution_id`, `visit`, `emitted_signal`, `routed_to`) written *in-transaction*, not derived from `result_line`/editable defs. Became Layer 2 (§4) — the exact backbone the base only proxied. Also: funnel/route/path UI, p50/p90 wall-vs-active-vs-wait latency, coverage banner, "observational not causal" discipline.
- **Candidate 4 → the verified plumbing fact** (`workflow` in `RUN_VIEW_KEYS` but not `ANALYTICS_RUN_KEYS`; confirmed by reading `views.py`) and the **`completed` vs `landed` doc-step correctness fix** (the top read-only correctness graft), plus `DIM_KEYFNS` flatten-at-keyfn for client dimensions.
- **Candidate 1 → exact dual-dialect view SQL** and the **`wall_seconds` vs `compute_seconds`** rollup split, plus the honesty that `attempts_per_task` is *exact* for the 3 shipped non-looping workflows and only a proxy under loops.

## Rejections (highest-signal part of the record)
- **Candidate 3 wholesale:** parallel event model + `nightshift.tasks` schema changes violate the "build on the existing `workflow` column" constraint. Its valid instinct (funnel needs durable routing facts) is met far more cheaply by 4 jsonb keys on the existing table.
- **Write-time `workflow_stats` summary table:** hot-path aggregation + drift risk; read-time views are always consistent.
- **Normalized `workflow_executions/_visits/_edges` tables (for Layer 2):** the 4 jsonb facts suffice without a second write model.
- **Promoting `workflow_name/_step` to columns:** duplicates jsonb + backfill; a partial expression index is the escalation if load bites.
- **Redefining `landed` to include `no_change`:** would silently change every fleet KPI; a parallel `completed` flag is contained and honest.
- **Fully client-side funnel:** 2000-row cap truncates executions; per-client visit/cycle reconstruction. Hence `/api/analytics/workflows` is server-side; `/api/stats` views + Analytics economics panel stay in their existing lanes.

## Verification
- Confirmed `views.py`: `RUN_VIEW_KEYS` has `workflow` (line 36); `ANALYTICS_RUN_KEYS` does not (67–73); `analytics_run_view` sets `landed = state=='landed'` (line 86) with no `completed` branch → doc-step misread is real. Both fixes are ≤2 lines, additive.
- Two-layer split verified against the constraint that non-workflow paths stay byte-identical: Layer 1 is read-only + one additive projection change; Layer 2's only writes are additive jsonb keys inside the advancing transition.
- Dropouts: none (5/5 completed).
- Open dependency flagged in the spec, not resolved here: the budget-quarantine metric needs the transition to stamp `failure_kind='workflow_budget'` (1-line write-side change); degrades to 0 gracefully until then.
