# Janitor role: regression

Read and obey `tools/janitors/CHARTER.md` in full.

## Mission

Add functional regression tests that pin observable behavior.
**Not line coverage** — the unit of progress is a behavior pinned.

## Target sources (priority order)

1. `tools/janitors/regression-targets.md` — work top-down; check completed items in the PR body.
2. Spec corpus: `docs/plans/longitude/` — derive expectations when the inventory entry includes `spec:`.

## Exemplar patterns (extend only)

| Category | Directory | Recording |
|---|---|---|
| API | `tests/regression/api/` | Frozen JSON in `tests/fixtures/api/` |
| Workflow | `tests/regression/workflow/` | Canned ticket/context JSON in `tests/fixtures/workflow/` |
| PIT | `tests/regression/pit/` | Frozen panel rows in `tests/fixtures/pit/` |

Copy the seed tests; do not invent new harnesses.

## Role config (this run)

Read `tools/janitors/config.json` on `origin/main` for limits.
Read `roles.regression.draft` in `tools/janitors/config.json` on `origin/main`.
Open PR as draft when true (no automerge).

## Workflow

1. `git fetch origin main && git worktree add ../janitor-regression-$(date +%Y%m%d) origin/main`
2. Branch: `janitor/regression/$(date +%Y-%m-%d)`
3. Pick the next unchecked inventory item with clear, testable scope within diff cap.
4. Add **new** test + fixture files only under `tests/regression/` and `tests/fixtures/`.
5. Cite spec sections in test docstrings where applicable.
6. `just precheck` — all new tests must pass.
7. Push and open PR:
   - Labels: `janitor`, `janitor:regression`
   - Title: `[janitor:regression] <single concern>`
   - Body template:

```markdown
## Concern
<one sentence>

## Inventory
- [ ] <item from regression-targets.md>

## Spec sections pinned
- <file §section>

## Verification
- `just precheck`
- bazel test //tests/regression/...

## Notes
<empty night explanation if exiting without PR>
```

8. If spec vs implementation diverges: file a GitHub issue (both sides + repro), do not add the test.

## Stop conditions

- Approaching diff cap (`diff_cap_lines` minus fixture exempt paths) — stop, no partial PR.
- Nothing meets the bar — exit cleanly with no PR.
- Any new test fails on HEAD — file issue, drop test, no PR unless other completed work remains.
