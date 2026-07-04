# Repo design-conformance review — 2026-07-04

**Subject:** Whole-repo design conformance against the architecture of record (`AGENTS.md`, `ARCHITECTURE.md`, `docs/spec/*`).
**Scope:** `main` at `aad5936`, working tree clean — no branch diff, so the lens is the standing codebase, not a change set.
Lens 1 only (design conformance); no thermo/deep-audit was requested.
**Date:** 2026-07-04
**Verdict:** The core seams hold up well — git subprocesses stay behind `GitRunner`, SQL stays in the store layer, `pg.py` is the only asyncpg import, and the analytics module is genuinely shared between the two UIs.
The findings cluster around one theme: **migrations that never deleted the loser** — the retired config model still owns the architecture docs, a dead SSE client still ships in the operator UI, and compat shims still front the unified config package.

---

## Blockers

### 1. The operator UI hardcodes its refresh cadence — a direct violation of an every-run rule

`AGENTS.md` rule 2: "Refresh/polling cadence is config-driven (`manager.cadences.refresh_ms` in `config.json`), never hardcoded."
The operator UI's recurring data-refresh loop is a literal:

- `src/nightshift/assets/ui/app.js:5910` — `const REFRESH_MS = 20000;`, driving `setInterval(loadRuns/loadQueue/loadPlaylists)` at `app.js:5912`.

The value only coincidentally matches the config default; editing `cadences.refresh_ms` in `.nightshift/manager.json` changes worker behaviour but not the operator UI.
The worker side does this correctly and shows the canonical path: `worker/ui_app.py:75` exposes `refresh_ms` via `/api/info`, and `assets/ui-worker/app.js:52–53` consumes it with a fallback constant.
The manager's own `/api/info` (`manager/api_operator.py:1000`) returns only `brand_name` — extend it with `refresh_ms` (the manager already sends the same value to workers at checkin, `api_worker.py:237`) and have `startAutoRefresh` use it.
The justifying comment at `app.js:5908–5909` ("this also triggers the server's stale-run reconcile … every 20s") is itself stale: the reconciler runs server-side on its own `poll_seconds` loop (`manager/app.py:143–144`); no operator-API handler triggers reconcile.

### 2. The architecture of record still teaches the retired config model — canon is forked between docs and code

The code reads `.nightshift/manager.json` / `.nightshift/worker.json` / `.nightshift/player.json` exclusively (`config/io.py:73–82`, `config/manager.py:258–266`, `spawn_daily.py:58–66`).
The documents every agent run loads say otherwise:

- `AGENTS.md:34` — refresh cadence lives in "`manager.cadences.refresh_ms` in `config.json`".
- `AGENTS.md:53` — worker identity invariant names "`worker_id` + `config.json.local`".
- `ARCHITECTURE.md:40–41` — the workspace layout diagram shows `config.json` / `config.json.local` at the workspace root.
- `ARCHITECTURE.md:212–217` — the configuration table documents `config.json` ("Manager policy … Yes (example in repo)") and `config.json.local`.
- `ARCHITECTURE.md:225` and `README.md:75` — link `docs/configuration-reference.md`, a path that does not exist (the real file is `docs/user/configuration-reference.md`).
- The tracked root `config.json` is the old shape (a `manager` block plus top-level policy keys) that no live code reads, and its `forbidden_paths` (`config.json:46–56`) still guard `tools/nightshift/*` paths from the pre-migration layout.

`docs/spec/settings-1-config-model.md` already named these as required doc updates; the code half of that migration landed, the canon half didn't.
Canonical path: rewrite the config sections of `AGENTS.md` / `ARCHITECTURE.md` / `README.md` to the `.nightshift/*.json` model, point the reference links at `docs/user/configuration-reference.md`, and delete the stale root `config.json` (or regenerate it as the `nightshift init` template output — `__main__.py:33` — so the example matches what `init` actually writes).

### 3. A dead legacy SSE client ships in the operator UI — two EventSources per browser, and the live log tail is dead code

`app.js:4664` (`connectEvents`) opens an EventSource and dispatches on `data.kind === "state"` / `data.kind === "event"`.
The manager's hub emits `{type: "snapshot"}` and `{type: "event", kind: <event-kind>}` frames (`manager/hub.py:81,103`); no frame ever carries `kind: "state"` or `kind: "event"`, so this handler drops every frame.
`manager-events.js:8–9` admits it: "app.js opens its own EventSource too, but it speaks the legacy frame shape and silently ignores these frames; this module is the manager-aware path."
Consequences, both verified:

- Every operator browser holds **two** `/api/events` connections (`app.js:4664` and `manager-events.js:67`), doubling hub subscribers for zero benefit.
- The entire live-log-tail path (`app.js:4677–4703`: `task_log` frames → `state.logCache` → live Now-view tail) is unreachable on the manager — `task_log` events are emitted (`api_worker.py:700`) but neither client consumes them (`manager-events.js` has no `task_log` in either refresh set), so log updates arrive only via polled `fetchLog`.

The legacy frame shape belonged to the deleted single-process `server/` package (only a stale `src/nightshift/server/__pycache__/` remains on disk).
This is the #1 slop pattern: the migration to `manager-events.js` left the loser alive.
Canonical path: delete `connectEvents`/`handleEngineEvent` from `app.js`, and port the `task_log` live tail into `manager-events.js` (it is the one capability the legacy path had that the new path lacks) — one EventSource per page.

---

## Drift

### 4. `manager/config.py` duplicates the canonical config defaults instead of retiring

The config unification made `nightshift/config/` the authoritative model, but `manager/config.py:28–60` keeps a parallel flat `ManagerConfig` dataclass whose ~20 field defaults (`enhance_brief_model`, `retry_backoff_seconds`, `quarantine_threshold`, …) duplicate the defaults in `config/manager.py` (e.g. `enhance_brief_model` declared at both `manager/config.py:42` and `config/manager.py:79`).
All live callers (`manager/app.py:45`, `api_worker.py:71`, `reconciler.py:83`, `work_orders.py:14`, `run_local.py:40`) import the shim, not the canonical model, so the two default sets will drift silently — the dataclass defaults win whenever `ManagerConfig` is constructed directly (as tests do).
`worker/config.py` is the same pattern but a pure re-export, which is harmless by comparison.
Canonical path: fold the flat projection into `nightshift/config/manager.py` (or derive its defaults from `ManagerSettings` so they exist once), and migrate imports off the shim.

### 5. Shipped prompts tell the agent to read a charter that is never materialized, and the legacy doc twins survive

`assets/prompts/nightshift-local.md:7` and `nightshift-resolve.md:8` open with "**Charter:** `NIGHTSHIFT.md` — read it; every constraint applies."
Nothing materializes any `NIGHTSHIFT.md` into the target-repo worktree or the scratch dir (the only match in `src/` is those two prompt lines; the harness's charter is the separate `agent-charter.md`, consumed only by `agent/loop.py:98`).
Every CLI-backend run is instructed to read a file that does not exist where it runs — a silent no-op that costs a lookup and applies zero constraints.
Relatedly, `docs/nightshift.md` and `docs/NIGHTSHIFT.md` are the pre-migration prompt and charter from the `tools/nightshift` / PR-loop era (they reference `.tasks/$TASK.md`, `tools/nightshift/NIGHTSHIFT.md`, PR/CI iteration the current system doesn't do), and `docs/spec/configuration-reference.md` is the retired twin of `docs/user/configuration-reference.md`.
Canonical path: drop or fix the charter line in both prompts (either reference nothing, or materialize a charter beside the brief the way `materialize_brief` does), and delete or archive the three superseded docs.

### 6. Two migrations share serial `20260730000004`

`20260730000004_nightshift_remote_push.sql` and `20260730000004_nightshift_validate_cmd.sql` collide.
Harmless today — both are independent `ADD COLUMN`s on `nightshift.runs`, `just migrate` applies the glob in lexical order (`justfile:115`), and tracking is by filename — but the scheme's ordering guarantee is broken, and the next collision with a real dependency applies in name order rather than intent order.
Rename one file to a unique serial (a fresh migration-free rename is safe: update the `_meta.schema_migrations` row in the same change, or accept a one-time re-apply of an idempotent `ADD COLUMN IF NOT EXISTS`).

### 7. `renderStatsLegacy` is admitted dead code

`assets/ui-worker/app.js:504–506` says the legacy client-side stats renderer "is retained as renderStatsLegacy() but unused" after the shared analytics module took over; `renderStatsLegacy` (`app.js:528`) plus its `CHART_PALETTE` feeder (`app.js:408`) are ~130 lines nothing calls.
Delete them — the shared module (`renderStats`, `app.js:507`) is the one home.

---

## Notes

- **`app.js` is 5,974 lines** — the entire operator SPA in one file, ~6× the size where decomposition is usually overdue; `workers.js` and `analytics.js` prove extraction works in this no-build setup. `manager/store.py` (1,054) and `manager/api_operator.py` (1,005) sit just past the same threshold.
- **Repeated correction:** `manager/reconciler.py:67` imports the private `_wip_ref` from `git/transport.py`. The git-management review (docs/reviews/git-management-review.md §1.1) already flagged cross-module private imports as the sign of a fictional boundary; it has recurred post-rebuild. Propose the structural encoding this time: promote `_wip_ref` to a public name and add a lint (custom ruff rule or a grep guard in `just validate`) banning `from … import _name` across package boundaries.
- **Checked and clean:** git subprocesses appear only under `GitRunner` (`git/runner.py:74` is the sole spawn; `reconciler.py` goes through it); SQL text is confined to `manager/store.py` / `store_sqlite.py` / migrations (`views.py` matches are docstrings only); `pg.py` remains the only asyncpg import; the analytics module is genuinely shared (worker mounts the manager's copy at `/shared`, `worker/ui_app.py:123`); server-side cadences are all config-driven; every migration has both `migrate:up` and `migrate:down` sections; the `player` config surface is live (settings tiers, `api_operator.py:928`), not dead code.
