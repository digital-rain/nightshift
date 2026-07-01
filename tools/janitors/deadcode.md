# Janitor role: deadcode

Read and obey `tools/janitors/CHARTER.md` in full.

## Mission

Remove unreferenced code and build targets with high confidence.

## Targets

- Python: `vulture` or import-graph dead modules under `lib/python/` and `services/`
- Bazel: `bazel query` for unreferenced `py_library` / `py_binary` targets
- Unused deps in `pyproject.toml` / `requirements.lock` (only when removal is provably safe)

## Forbidden

- No test file changes.
- Pure deletions are exempt from diff cap; still respect forbidden paths.
- One deletion concern per PR (one package or one target graph).

## Role config

Read `roles.deadcode.draft` in `tools/janitors/config.json` on `origin/main`.

## Workflow

1. Fresh branch `janitor/deadcode/$(date +%Y-%m-%d)` from `origin/main`.
2. Remove one coherent dead cluster; verify with `just precheck` and Bazel build.
3. PR with labels `janitor`, `janitor:deadcode`; title `[janitor:deadcode] <concern>`.

## Stop conditions

- Dynamic import / reflection uncertainty — skip, do not delete.
- Empty night — exit cleanly.
