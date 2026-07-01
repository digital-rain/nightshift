# Regression target inventory

Human-owned steering list for the `regression` janitor.
Work top-down; check items off in PR bodies.
Entries with `spec:` may derive expectations from that spec section.
Entries without `spec:` yield `test_characterization_*` tests only.

**Spec corpus (trusted for derivation when cited):** `docs/plans/longitude/`
Load-bearing: `00-context-handoff.md`, `01-project-overview.md`, `02-detailed-design.md`.
Behavioral authority for janitor work: `10-trade-evaluator.md`, `11-trade-idea-generators-spec.md`,
`07-pre-trade-screening.md`, `12-trade-decision-spine.md`.

Recording mechanisms (seed exemplars under `tests/regression/`):

| Category | Mechanism | Fixture location |
|---|---|---|
| API | Hand-frozen JSON request/expected fields | `tests/fixtures/api/` |
| Workflow | Canned ticket/context JSON → evaluator or generator output | `tests/fixtures/workflow/` |
| PIT | Frozen vintage panel rows → `long_regime.pit` helpers | `tests/fixtures/pit/` |

---

## API regression

- [ ] `GET /api/calendar` timeframe bounds — week is Monday–Sunday (ET)
  - spec: `docs/plans/longitude/10-trade-evaluator.md` (calendar bounds helper; seed exemplar)
  - seed: `tests/regression/api/test_calendar_bounds_regression.py`
- [ ] `POST /api/trade/evaluate` — empty book gate G04 passes
  - spec: `docs/plans/longitude/10-trade-evaluator.md` §library
- [ ] `POST /api/trade/evaluate/batch` — item parity with single evaluate
  - spec: `docs/plans/longitude/10-trade-evaluator.md` §library

## Workflow tests

- [ ] Trade evaluator — liquidity hard_fail forces REJECT regardless of score
  - spec: `docs/plans/longitude/10-trade-evaluator.md` §checks
  - seed: `tests/regression/workflow/test_trade_eval_verdict_regression.py`
- [ ] Idea generators — cross-domain dedupe merges `(symbol, side)` for same day
  - spec: `docs/plans/longitude/11-trade-idea-generators-spec.md` §4
- [ ] Fills reconciliation — Schwab JSON fixture parses expected trade count
  - characterization (broker export format)

## PIT / historic replay

- [ ] Regime PIT — `as_of` picks latest vintage ≤ as-of date (no lookahead)
  - spec: `docs/plans/longitude/08-new-macro-state.md` (PIT invariants)
  - seed: `tests/regression/pit/test_pit_as_of_regression.py`
- [ ] Vintage walk — revisions sorted ascending by vintage_date
  - characterization

## Deferred

- UI screenshot diffs — do not implement until API/workflow/PIT categories are mature.
