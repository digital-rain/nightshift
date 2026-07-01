# Janitor charter

Hard constraints for all janitor roles.
Role prompts include this document by reference.

## Branching and isolation

- Branch fresh from `origin/main` each run, in an isolated worktree.
- Never reuse a branch.
- Branch name: `janitor/<role>/<yyyy-mm-dd>`.

## Scope

- **One concern per PR.**
  Mixed refactor + behavior diffs are rejected.
- Diff cap and forbidden paths per `tools/janitors/config.json`
  (`diff_cap_lines`, default 500; fixture paths exempt; pure deletions exempt for `deadcode`).
- Read guard limits from **`origin/main`**, never from the PR head.

## Tests and fixtures

- **Existing tests are immutable.**
  Non-`regression` janitors may not touch any existing test file.
- The `regression` janitor is **additions-only** — new test files and new fixture files only;
  never edit or delete an existing test, assertion, or fixture.

## Regression-specific

- **Hermetic by default.**
  Tests run in the Bazel sandbox against recorded fixtures or mocks — no network.
  A test genuinely requiring the live backend is tagged `external` + `manual`
  (excluded from PR checks; runs in the nightly suite only), added only when recording is impractical,
  and flagged in the PR body.
- **Extend, don't invent.**
  Add cases following the exemplar test pattern and recording mechanism per category
  under `tests/regression/{api,workflow,pit}/` and `tests/fixtures/`.
  Do not build new harnesses or fixture infrastructure — that is human/pipeline work.
- **Spec-derived tests are first preference.**
  When a behavior is covered by a spec document under `docs/plans/longitude/`,
  derive expected values from the spec.
  The test docstring or comment **must** cite the spec file and section
  (e.g. `# verifies docs/plans/longitude/10-trade-evaluator.md §gates: liquidity hard_fail → REJECT`).
- **Spec/implementation divergence is an issue, never a test.**
  If code contradicts the spec, file an issue citing both sides and drop the test from the PR.
- A new test must **pass against HEAD**.
  If writing it exposes a defect: file an issue with reproduction and drop the test.
  Never merge a failing or skipped test.
- Only where **no spec covers the behavior** and it is merely pinned as-is,
  name the test `test_characterization_*`.
- For numeric paths (greeks, P&L, PIT queries): prefer property-based tests or
  spec/independently-derived expected values over blind pinning.
  Suspicious current behavior is an issue, never a test.
- PR body lists which inventory items (`tools/janitors/regression-targets.md`)
  and/or spec sections the PR pins, checkbox-style.

## PR format

- Labels: `janitor` + `janitor:<role>`.
- Title prefix: `[janitor:<role>]`.
- Body states the single concern and how it was verified.
- Open as **draft** when `roles.<role>.draft` is `true` in `config.json` (no automerge).

## Before push

- Run `just precheck` before pushing.
- Stop cleanly at budget cap rather than pushing partial work.
- **An empty night is a success** — if nothing meets the bar, exit with no PR; never force output.
