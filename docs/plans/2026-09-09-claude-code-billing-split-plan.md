---
status: validating
date: 2026-09-09
---

# Claude Code Billing Split Implementation Plan

> **For agentic workers:** execute with the `implement` skill, task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `claude-code/` runs bill the operator's claude.ai subscription, `anthropic/` runs bill the API key, every run record says which, and the spend rollups sum actual (API) and notional (subscription) dollars separately.

**Architecture:** One new module, `src/nightshift/billing.py`, owns the decision: a declared `claude_billing` setting (`auto | subscription | api`, one shared default constant on both the worker and the manager config dataclasses), a probe of `claude auth status` run against an already-scrubbed environment, and the scrub itself.
`ClaudeCodeBackend` is the only place that spawns the `claude` CLI, so it applies the decision to its subprocess env in both `run()` and `complete_text()`, which covers the worker run, workflow doc steps, the conflict resolver, and the manager's enhance pass without touching the call sites.
The decision is stamped on `WorkerResult.billing` at spawn time and flows unchanged through `Telemetry` → `Outcome` → the submit body → the attempt row and the worker's local JSONL, where the three rollups (analytics.js, `local_store.stats`, the SQL stats views) split cost by it.

## Re-verified findings (2026-09-09, CLI v2.1.183, this box)

Everything in the scoping prompt held up under re-verification, with the corrections and additions below stated loudly.

**Confirmed, with current file:line cites.**

- Routing is correct: `model_id.py` splits on the first `/`; `backends.py:987` declares `DEFAULT_BACKEND = "claude-code"`; the registry at `backends.py:976-985` holds the seven providers.
- The leak: `prompts.py:262` (`worker_env`) starts from `os.environ.copy()` and scrubs nothing; `config/io.py:29` (`load_dotenv`) loads `<workspace>/.env` into every entrypoint's `os.environ`; `backends.py:511-525` (`ClaudeCodeBackend.run`) and `backends.py:456-509` (`complete_text`) hand that env to the `claude` subprocess unchanged.
- `preflight.py:334-338` hard-exits without `ANTHROPIC_API_KEY`.
- The `anthropic` backend legitimately needs the key (`backends.py:629-631`), as does the harness's anthropic vendor (`agent/transport.py:171-173`).
- `nightshift.enabled` (`config/worker.py:42`) is off in every deployed `worker.json`; not the bill.
- Spend attribution: 406 records in `~/workspaces/.nightshift-worker/runs.jsonl`, every September dollar under `backend: "claude-code"`. Not redone.
- `claude auth status` on this box reports `loggedIn: true`, `authMethod: "claude.ai"`, `subscriptionType: "max"`, with no `apiKeySource` in a shell that has no key exported. Consistent with the prompt's measurement.
- `--bare` is not used anywhere in `src/`; its help text confirms it reads only `ANTHROPIC_API_KEY`.

**Corrections and additions.**

1. **The preflight key demand only fires on the legacy local runner.** `check_preconditions` is called from `run_local.py:328` only; the worker daemon (`worker/loop.py`) never calls it. The worker leaks the key purely because `.env` is loaded into its process and `worker_env` copies it. Fixing preflight is still right (it is what told the operator to add the key), but it is not the worker's gate.
2. **There is a second leak path on the manager.** `manager/api_operator.py:483` runs `enhance_brief` with `env=dict(os.environ)`, and the deployed `~/workspaces/.nightshift/manager.json` sets `enhance_brief_model: claude-code/claude-opus-5`. Every enhance-on-create rewrite has been a CLI print-mode call with the key in its env. The fix inside `ClaudeCodeBackend.complete_text` covers it, and the manager needs its own `claude_billing` declaration because it is a separate process with its own auth context.
3. **`.nightshift/settings.json` is dead config.** No module under `src/` reads it (`worker_backend`, `transport_mode`, `theme`, `port` in it are unread). The only `worker_backend` reader is `resolve_runner.py:432`, which reads the *merged queue config* (`spawn_daily.resolve_config`, layered from `manager.json` + content-store config). The plan treats `manager.json` / `worker.json` as the declared surfaces and leaves `settings.json` alone.
4. **Stats are three rollups, not one.** `assets/ui/analytics.js:145` (`aggregate`, shared by the manager and worker UIs), `worker/local_store.py:149` (`stats`, which today has no cost field at all), and the five SQL `stats_by_*` views (`manager/store_sqlite.py:268-410`, mirrored by the Postgres migration `20260731000005`) that feed the Workers tab tables via `total_cost_usd`.
5. **Deployment hazard.** `Telemetry` fields flow straight into `store.update_attempt(**fields)` (`manager/store.py:47` builds the allowlist from `Outcome.model_fields`). A manager restarted on the new code before `just migrate` would fail every submit with an unknown-column error. The runbook orders `just migrate` before the restart.
6. **Bare-model legacy qualification is already safe.** A bare model with no legacy `backend` key stays bare and fails at `worker/execute.py:432` with `MODEL_UNAVAILABLE` naming the model; no silent literal there. The deployed `~/workspaces/.nightshift/worker.json` carries the legacy `"backend": "claude-code"` key but every model in it is qualified, so `_qualify` is a no-op. Left alone.
7. **The one silent fallback that matters is `select_run_backend`** (`resolve_runner.py:63-80`): an unknown provider falls through to `get_backend(fallback)`, and `get_backend` (`backends.py:998`) returns the default for any unknown name. Two tests pin that behaviour (`tests/test_backends_dispatch.py:52-56, 85-89`) and flip in Session 5.
8. **Scrubbing changes what the agent's own shell children see.** With subscription billing the `claude` process and everything it spawns no longer has nightshift's `ANTHROPIC_API_KEY`. Target repos' own `.env` is symlinked into task worktrees, so repo tooling is unaffected. This is intentional and is stated in the runbook.

**Only the operator can verify:** the Anthropic console (the single ground truth for "not billed"), the auth state on any other worker box, and whether any target repo's own `.env` carries an `ANTHROPIC_API_KEY` that the agent's Bash children would still see.

## Design decisions

- **Setting:** `claude_billing: "auto" | "subscription" | "api"`, default `auto`, declared once as `billing.DEFAULT_CLAUDE_BILLING` and used as the dataclass default on both `WorkerConfig` (category "Models") and `OperatorConfig` (category "Models"). Both render in the Settings UI automatically through the registry.
- **Where the backend reads it:** `spec.config["claude_billing"]` for `run()`, the `config` kwarg for `complete_text()`. The worker stamps `cfg.claude_billing` into the order's config blob before building the `WorkerSpec` (the worker box owns its own auth state, so its declaration wins over anything a queue config says). The resolve path reads the merged queue config, which already carries `manager.json` keys. Enhance passes `{"claude_billing": cfg.claude_billing}`. A missing key resolves to the declared default; an invalid value raises naming `claude_billing`.
- **Decision semantics** (`billing.decide_claude_billing`):
  - `subscription` → scrub unconditionally; if the CLI is not logged in it fails honestly at run time.
  - `api` → keep the key; if `ANTHROPIC_API_KEY` is absent, error naming `claude_billing=api`.
  - `auto` → run `claude auth status` with the scrubbed env (per-process cache keyed on the binary path, subprocess timeout); `loggedIn` and `authMethod == "claude.ai"` → subscription; otherwise api with a loud log line when a key exists, else an error that names the setting.
- **Scrub set:** `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`.
- **`billing` field:** `"api" | "subscription" | None` on `WorkerResult`, `Telemetry`, the attempt row, and the local record. `claude-code` sets it from the decision; `anthropic` and the harness's anthropic vendor set `"api"`; every other backend leaves `None`.
- **Rollup rule:** `subscription` → notional; `api` → actual; `None` (every pre-existing record, and backends that cannot say) → actual. Conservative: an unattributed dollar is counted as real money, so the actual figure can only overstate. September stays readable as the API spend it was.
- **Never `--bare`.** A test asserts it is absent from both argv builders.
- **Tests never spawn the real CLI.** A fake `claude` script (`tests/_fake_claude.py` writes it into a tmp dir) answers `auth status` (logged-in / logged-out / hang variants), print-mode `-p ... --output-format json`, and `stream-json`, and dumps its environment to a file so tests can assert the scrub.

## Sessions

Each session is independently landable and ends with `just validate` green in the worktree (plus `node --test tests/ui/` when analytics.js changes, since validate skips it).
Sessions 1, 3, and 5 have disjoint writes and can run in parallel; 2 depends on 1 and 3; 4 depends on 1; 6 is docs.

### Session 1 — `billing.py` + the declared setting

Files: `src/nightshift/billing.py` (new), `src/nightshift/config/worker.py`, `src/nightshift/config/manager.py`, `src/nightshift/assets/config/worker.json`, `src/nightshift/assets/config/manager.json`, `tests/_fake_claude.py` (new), `tests/test_billing.py` (new), `tests/test_config_model.py`.

- [x] `billing.py`: `CLAUDE_BILLING_MODES`, `DEFAULT_CLAUDE_BILLING = "auto"`, `CLI_AUTH_ENV_KEYS`, `scrub_cli_auth(env) -> dict`, `billing_setting(config) -> str` (missing → default, invalid → `ValueError` naming `claude_billing`), `claude_auth_status(claude_bin, env, *, timeout=15.0) -> dict | None` (cached per binary), `BillingDecision(mode, reason, env)`, `decide_claude_billing(setting, *, env, claude_bin, log) -> BillingDecision`.
- [x] `WorkerConfig.claude_billing` and `OperatorConfig.claude_billing` with `options=list(CLAUDE_BILLING_MODES)`, loaded/saved like the neighbouring string fields.
- [x] Shipped config templates gain the key with the default.
- [x] Tests: scrub removes exactly the five keys and nothing else; each mode's decision against the fake CLI's logged-in / logged-out / hang / missing-binary variants; the setting round-trips through both config loaders; an invalid value names the setting.

Check: `just test tests/test_billing.py tests/test_config_model.py tests/test_nightshift_config.py`.

### Session 2 — the backend applies the decision (the money-shaped unit)

Files: `src/nightshift/backends.py`, `src/nightshift/worker/execute.py`, `src/nightshift/enhance.py`, `src/nightshift/manager/api_operator.py`, `src/nightshift/worker/loop.py`, `tests/test_backends_dispatch.py`, `tests/test_enhance.py`.

- [x] `WorkerResult.billing: str | None`.
- [x] `ClaudeCodeBackend.run`: decide from `spec.config`, spawn with `decision.env`, emit one log line (`[claude-code] billing: subscription (claude.ai max)`), set `result.billing`. A decision error becomes a `WorkerResult(returncode=2, error=...)` naming the setting.
- [x] `ClaudeCodeBackend.complete_text`: same decision from the `config` kwarg; a decision error raises `TransportError`.
- [x] `AnthropicBackend.run` and the harness anthropic vendor set `billing="api"`.
- [x] `execute_work_order` and `_execute_doc_step` stamp `cfg.claude_billing` into the config blob and copy `result.billing` into `tele`.
- [x] `enhance_brief(..., config=None)` forwards to `complete_text`; `api_operator` passes `{"claude_billing": cfg.claude_billing}`.
- [x] Worker checkin logs the resolved mode once at startup (the loud surface for a fallback to api).
- [x] Tests against the fake CLI: env dumped by the fake has no auth keys under subscription and has the key under api, for both `run` and `complete_text`; `--bare` absent from both argv builders; `result.billing` matches the decision; enhance forwards the config.

Check: `just test tests/test_backends_dispatch.py tests/test_enhance.py tests/test_nightshift_worker.py`; full diff read by the orchestrator.

### Session 3 — `billing` on the record, and the actual/notional split

Files: `src/nightshift/lifecycle.py`, `src/nightshift/assets/migrations/20260909000001_nightshift_attempt_billing.sql` (new), `src/nightshift/manager/store_sqlite.py`, `src/nightshift/manager/views.py`, `src/nightshift/manager/wire.py`, `src/nightshift/manager/api_worker.py`, `src/nightshift/worker/local_store.py`, `src/nightshift/assets/ui/analytics.js`, `src/nightshift/assets/ui/workers.js`, `src/nightshift/assets/ui/index.html`, `tests/test_nightshift_store.py`, `tests/test_nightshift_worker.py`, `tests/ui/analytics_billing.test.mjs` (new).

- [x] `Telemetry.billing: str | None = None` (flows into `Outcome`, `SubmitBody`, `ATTEMPT_UPDATABLE_FIELDS`, the local record).
- [x] Migration: `ADD COLUMN IF NOT EXISTS billing text`; recreate the five stats views with `actual_cost_usd` (`billing IS DISTINCT FROM 'subscription'`) and `notional_cost_usd` (`billing = 'subscription'`) alongside the existing `total_cost_usd`; `-- migrate:down` restores the prior views and drops the column. SQLite `_SCHEMA` kept in lockstep.
- [x] `RUN_VIEW_KEYS`, `ANALYTICS_RUN_KEYS`, `ResolveResultBody`, and the resolve telemetry dict at `api_worker.py:1154` carry `billing`.
- [x] `LocalStore.stats` gains `actual_cost_usd`, `notional_cost_usd`, `unattributed_runs`.
- [x] `analytics.js` `aggregate` splits `cost` into `actualCost` and `notionalCost` (rule in the header comment); the KPI header shows "Actual spend" with a "notional (subscription) $X" sub-line; the Workers tab stat tables show actual and notional columns.
- [x] Tests: store round-trips `billing` and the views report both sums (SQLite); local stats split; an mjs test renders records with `api`, `subscription`, and no `billing` and asserts the two totals.

Check: `just test tests/test_nightshift_store.py tests/test_nightshift_worker.py tests/test_settings_api.py` and `node --test tests/ui/`.

### Session 4 — preflight asks for what is configured

Files: `src/nightshift/preflight.py`, `src/nightshift/run_local.py`, `tests/test_local_git_ops.py`, `tests/test_env_preflight.py`.

- [x] `check_preconditions(workspace, repo, *, claude_billing, providers)`: `claude` on `PATH` is required only when `claude-code` is among the providers; `ANTHROPIC_API_KEY` is required only when `claude_billing == "api"` or an API-keyed provider (`anthropic`, harness anthropic vendor) is configured; under `subscription`/`auto` with `claude-code`, `claude auth status` must report `loggedIn` (via `billing.claude_auth_status`, cached) or the exit message says to run `claude login`.
- [x] `run_local.py` passes the worker config's providers and the manager config's `claude_billing`.
- [x] Tests flip `_prep_preconditions` to the fake CLI and cover: subscription without a key passes; api without a key exits naming the setting; logged-out under subscription exits naming `claude login`.

Check: `just test tests/test_local_git_ops.py tests/test_env_preflight.py tests/test_run_local.py`.

### Session 5 — declared fallbacks only

Files: `src/nightshift/backends.py` (registry tail), `src/nightshift/resolve_runner.py`, `src/nightshift/spawn_daily.py`, `tests/test_backends_dispatch.py`, `tests/test_resolve_runner.py`, `tests/test_spawn_daily.py`.

- [x] `get_backend(name)`: `None` still means the declared default; an unknown name raises `KeyError` instead of returning the default.
- [x] `select_run_backend(model, fallback_backend)`: an unknown provider raises `BackendSelectionError` naming the model and the settings that could declare it (`resolve_model` / the brief's `model:`); an agnostic id with no declared `resolve_backend` / `worker_backend` raises naming both. `resolve_runner.run_task` turns the error into a typed `TaskResult` failure rather than a traceback.
- [x] `spawn_daily.py:325` reads the `default_model` default from `OperatorConfig` rather than a literal `"auto"`.
- [x] Tests flip the two silent-fallback assertions and add the error-naming cases.

Check: `just test tests/test_backends_dispatch.py tests/test_resolve_runner.py tests/test_spawn_daily.py`.

### Session 6 — runbook and operator smoke

Files: `docs/user/configuration-reference.md`, `docs/user/setup-guide.md`, `README.md`, `.env.example`, `tools/billing_smoke.py` (new), this plan (`status: validating`).

- [x] Configuration reference: a "Claude billing" section documenting `claude_billing`, the scrub, the `.env` recommendation (remove `ANTHROPIC_API_KEY` unless an API path is configured), the migrate-before-restart order, and how to read actual vs notional.
- [x] Setup guide and README stop presenting `ANTHROPIC_API_KEY` as the `claude-code` credential.
- [x] `tools/billing_smoke.py` (operator-run, never in tests): prints the decision for the current config, runs one tiny print-mode completion with the decided env, and prints the `billing` the run would record, so the operator can then check the console.

Check: docs render; `just validate` green; plan frontmatter `status: validating`.

## Review corrections (landed with the sessions)

A high-effort review of the accumulated diff confirmed thirteen findings; these were fixed before landing.

- A fifth `claude` spawn existed outside the seam: the Slack intake normaliser (`slack/intake.py`) now goes through `ClaudeCodeBackend.complete_text` with the runner config.
- A refused spawn (an unsatisfiable `claude_billing`, a missing key) is `backends.CONFIG_FAILED` and maps to `BACKEND_UNAVAILABLE` in both the worker executor and the conflict resolver, so a box fault is retried elsewhere instead of quarantining the task or being reported as "conflicts remain".
- The resolver now carries the agent's telemetry (`billing`, `cost_usd`, turns, tokens, usage) onto its result and the resolve-result payload.
- `complete_text` logs its decision and the auto fallback through the module logger, and the manager prints its enhance-pass billing line at startup.
- The real CLI prints the logged-out answer and exits 1; the probe now parses stdout before judging the exit code and caches every answer, and an unreadable probe is logged as such rather than as "not logged in".
- The preflight reuses `decide_claude_billing` and `resolve_claude_bin` instead of re-implementing them, and `run_local` asks for the key when any declared model routes to the harness's anthropic vendor.
- `select_run_backend`'s remediation names `resolve_model`, the shipped manager template declares one, and the runbook says so.
- The SQL split uses `coalesce(billing, '') <> 'subscription'` in both dialects.
- The docs attribute conflict resolves and Slack intake to `manager.json`, not `worker.json`.

Left as follow-ups (documented, not blocking): the other Stats KPIs remain list price by design; `resolve_backend` / `resolve_model` are not validated against known providers at settings-save time; the worker keeps advertising `claude-code` after a startup billing error (bounded by the environment-failure mapping); three money formatters exist across the UI surfaces.

## Landing

One worktree branch (`claude-billing-split`), one commit per session, `just validate` green after each, a review pass on the accumulated diff, then a single squash-merge to local `main`.
After landing: `just migrate` from the primary checkout, then the operator restarts the manager and worker.
