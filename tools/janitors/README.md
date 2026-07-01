# Janitor agents

Out-of-band agents that continuously improve the codebase via small, guarded PRs.

## Layout

| Path | Purpose |
|---|---|
| `config.json` | Tunables (throttle, caps, forbidden paths, roles) — **janitors cannot edit this** |
| `CHARTER.md` | Hard constraints for all roles |
| `regression-targets.md` | Human-owned regression inventory |
| `<role>.md` | Per-role prompts for the scheduler |

## Kill switch

```bash
just janitors-down   # emergency stop (no commit)
just janitors-up     # re-enable
```

Scheduled runs no-op when `JANITORS_ENABLED != 'true'`.

## Human setup (before first nightly run)

### 1. Repo secrets and variables

```bash
# Emergency kill switch — leave false until guard is verified on main
just janitors-down

# After guard verification + calibration plan ready:
just janitors-up

# In GitHub → Settings → Secrets and variables → Actions:
#   ANTHROPIC_API_KEY = <your key>
```

Set an Anthropic console **spend cap** before enabling scheduled runs.

### 2. Branch protection on `main`

Add required status check: **`janitor-guard / guard`**

Existing checks (Bazel, pytest, pyright, schema-compat) stay as-is.

### 3. Machine review (switchable via `config.json`)

`config.json` → `review`:

| Field | Current | Purpose |
|---|---|---|
| `mode` | `"single"` | `"single"` = one reviewer for all janitor PRs; `"tiered"` = per-role map |
| `single_reviewer` | `"cursor"` | Used when `mode` is `"single"` |
| `tiered.*` | copilot / cursor | Used when `mode` is `"tiered"` |

Configure the active reviewer tool to trigger on PR labels `janitor` (or `janitor:<role>` when tiered).
Flip `mode` to `"tiered"` in a commit when Copilot/Gemini label triggers are ready — no workflow change needed.

### 4. Config tuning

Edit **`tools/janitors/config.json`** (repo root relative path):

```
longitude/tools/janitors/config.json
```

Review `forbidden_paths`, `test_file_patterns`, role `enabled`/`draft`, and `throttle.max_prs_per_night`.

Janitors cannot modify this file (forbidden path + guard).

### 5. Verify guard (§8.1) before enabling janitors

On a throwaway branch, open PRs labeled `janitor` that deliberately violate each rule:

- diff > 500 lines (non-fixture)
- touch `migrations/` or `tools/janitors/`
- modify an existing test file

Confirm `janitor-guard / guard` fails each time.
Confirm a PR that only **adds** fixture lines under `tests/fixtures/` passes the diff cap.

### 6. Rollout sequence

1. Land guard + config (no janitors running) — verify guard rejections manually.
2. Seed regression exemplars under `tests/regression/` (done in-repo).
3. Enable `regression` with `draft: true`, throttle 1/night for calibration week.
4. Flip `draft: false` per role after review; enable next role.

## Local precheck

```bash
just precheck
```

Matches the janitor push gate: lint, config validate, Bazel py_test subset.

## Nightshift (task lane)

See [`../nightshift/README.md`](../nightshift/README.md) for kill switch, GitHub App setup
(required so CI runs automatically on nightshift PRs), and operator notes.
