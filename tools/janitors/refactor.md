# Janitor role: refactor

Read and obey `tools/janitors/CHARTER.md` in full.

## Mission

Small structural improvements: extract duplication, simplify conditionals.
Lowest priority; strictest cap; only when regression/enforcer/deadcode are boringly clean.

## Targets

- Local duplication within a single module (not cross-package rewrites)
- Simplify nested conditionals without behavior change

## Forbidden

- No test file changes.
- No mixed refactor + behavior change.
- No forbidden paths.

## Role config

Read `roles.refactor.draft` in `tools/janitors/config.json` on `origin/main`.

## Workflow

1. Fresh branch `janitor/refactor/$(date +%Y-%m-%d)` from `origin/main`.
2. One structural concern; existing tests must pass unchanged.
3. `just precheck`
4. PR with labels `janitor`, `janitor:refactor`; title `[janitor:refactor] <concern>`.

## Stop conditions

- Requires behavior change to complete — stop, file issue instead.
- Diff cap — stop without PR.
