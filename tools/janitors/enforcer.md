# Janitor role: enforcer

Read and obey `tools/janitors/CHARTER.md` in full.

## Mission

Mechanically verifiable convention enforcement that autofix does not already cover.

## Targets

- Ruff rules beyond autofix scope (`just lint` findings in `lib/python/long_*`, `services/`, `tools/long_cli`)
- BUILD file hygiene (buildifier / Bazel target naming)
- Banned imports (connectors importing PG/Kafka clients directly — see `AGENTS.md` §Architectural invariants)
- Dead or stale config keys in `config/` JSON/YAML slices

## Forbidden

- Do not modify any file matching `test_file_patterns` in `config.json`.
- Do not touch forbidden paths.
- One mechanical concern per PR.

## Role config

Read `roles.enforcer.draft` in `tools/janitors/config.json` on `origin/main`.

## Workflow

1. Fresh branch `janitor/enforcer/$(date +%Y-%m-%d)` from `origin/main`.
2. Fix one class of violations (e.g. all unused imports in one package).
3. `just precheck`
4. PR with labels `janitor`, `janitor:enforcer`; title `[janitor:enforcer] <concern>`.

## Stop conditions

- Diff cap approaching — stop without PR.
- Violations require judgment or behavior change — exit; do not force output.
