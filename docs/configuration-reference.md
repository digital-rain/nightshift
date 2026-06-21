# Nightshift — Configuration Reference

The complete configuration surface for the Nightshift manager, workers, queues, and tasks.
For a guided bring-up, start with the [Setup Guide](setup-guide.md).

## Where configuration lives

| Source | Owner | Scope | Committed? |
|---|---|---|---|
| `--workspace` / `NIGHTSHIFT_WORKSPACE` | Both | The workspace dir bound at launch (see [The workspace](#the-workspace)) | n/a |
| `<workspace>/config.json` | Manager | Centralized task policy + `manager` block | Operator-managed |
| `<workspace>/config.json.local` | Worker | That worker's identity, backend, capabilities | No (gitignored) |
| `.env` (repo root) | Both | Environment overrides (`NIGHTSHIFT_*`, DB DSN, backend creds) | No |
| Process environment | Both | Same keys as `.env`, highest precedence | n/a |
| Per-queue `config.json` (content-store queue dir) | Manager | Queue order, sort mode, play-priorities, repo, validate, conflict policy | Yes (in `nightshift-tasks`) |
| `nightshift` Postgres schema | Manager | Runtime state: workers, leases, runs, events, queue dedication | n/a |
| `src/nightshift/assets/migrations/*.sql` | Manager | Nightshift's own schema migrations (separate from longitude's `migrations/`) | Yes |
| `justfile` | Operator | `migrate` / `rollback` / `manager` / `worker` recipes | Yes |
| Per-task frontmatter (`.tasks/*.md`) | Manager | Per-task overrides (model, mcp, caps, …) | Yes |

Precedence for a given setting, low to high: built-in default, then `config.json` / `config.json.local`, then `.env`, then the process environment. Environment variables always win.

## The workspace

The **workspace** is the directory passed as `--workspace` to the manager and each worker. It parents every git repo your workers operate on (each a direct child, e.g. `longitude/`), the `nightshift-tasks/` content-store repo (briefs + queue config), and the runtime dirs (`.worktrees/`, `.nightshift/`). The operator `config.json` is read from `<workspace>/config.json`.

It is **not** a `config.json` key — `config.json` lives inside the workspace, so the workspace must be selected before any config is read. Resolution order (high to low):

| Source | Notes |
|---|---|
| `--workspace <dir>` | Explicit CLI flag (highest). The manager/worker entry points default to the current directory when unset. |
| `NIGHTSHIFT_WORKSPACE` | Read by the `justfile`, which forwards it as `--workspace`. Falls back to the repo dir (`just`'s directory) when unset. Set it in `.env` (e.g. `NIGHTSHIFT_WORKSPACE=$HOME/workspaces`); use an absolute path or `$HOME/…` — a literal `~` is not expanded. |

The operator UI displays the bound workspace **read-only**: it is fixed for the life of the process, so change `NIGHTSHIFT_WORKSPACE` (or `--workspace`) and relaunch rather than editing it in the UI. A manager and a co-located worker on one box must share the same workspace.

## Manager configuration

The manager reads `<workspace>/config.json`. Service-level settings live under a `manager` block; task-policy settings are top-level. All of it is resolved in [`manager/config.py`](../src/nightshift/manager/config.py).

### `manager` block

```json
{
  "manager": {
    "host": "0.0.0.0",
    "port": 8800,
    "landing_mode": "none",
    "rendezvous_remote": "origin",
    "shared_secret": null,
    "dsn": null,
    "cadences": {
      "poll_seconds": 5.0,
      "heartbeat_seconds": 10.0,
      "lease_ttl_seconds": 120.0,
      "worker_stale_seconds": 45.0,
      "refresh_ms": 20000
    }
  }
}
```

| Key | Default | Meaning |
|---|---|---|
| `host` | `0.0.0.0` | Bind address for the manager HTTP server. |
| `port` | `8800` | Bind port (operator UI + worker/operator API). |
| `landing_mode` | `none` | Remote policy applied *after* the always-on local fast-forward of canonical `main`: `none` (local only), `push` (push `main` to origin), `pr` (open a PR). In `pr` mode `origin/main` is authoritative — the manager resyncs local `main` to `origin/main` at dispatch and at land, so its local `main` and `origin/main` never diverge after GitHub re-squashes the PR (see [cross-machine landing](spec/remote-landing.md)). |
| `rendezvous_remote` | `origin` | Git remote *name* (resolved inside each `repo_root`) the manager fetches a cross-machine worker's task branch from, and uses for the `pr`-mode `origin/main` resync. Set `null` to disable (cross-machine submits then fail closed). |
| `shared_secret` | `null` | If set, every worker call must send a matching `X-Nightshift-Secret` header. |
| `dsn` | `null` | Nightshift's own Postgres DSN. Set → durable `PgStore`; unset → in-memory store. Never inherited from longitude's `LONG_PG_DSN` (see [Database](#database--state-store)). |
| `cadences.poll_seconds` | `5.0` | Worker idle poll interval (sent to workers at checkin). |
| `cadences.heartbeat_seconds` | `10.0` | Worker→manager heartbeat interval that keeps a lease alive. |
| `cadences.lease_ttl_seconds` | `120.0` | Lease lifetime before the manager reclaims it. |
| `cadences.worker_stale_seconds` | `45.0` | Silence after which a worker is marked `offline`. |
| `cadences.refresh_ms` | `20000` | UI safety-poll fallback (SSE is the primary live channel). |

Cadences are config-driven, never hardcoded (invariant 13). The manager sends them to each worker at checkin, so changing them here changes worker behavior too.

### Manager environment overrides

| Variable | Overrides |
|---|---|
| `NIGHTSHIFT_WORKSPACE` | The `--workspace` launch dir (see [The workspace](#the-workspace)) |
| `NIGHTSHIFT_MANAGER_HOST` | `manager.host` |
| `NIGHTSHIFT_MANAGER_PORT` | `manager.port` |
| `NIGHTSHIFT_LANDING_MODE` | `manager.landing_mode` (`none` / `push` / `pr`) |
| `NIGHTSHIFT_RENDEZVOUS_REMOTE` | `manager.rendezvous_remote` (and the worker's `rendezvous_remote`) |
| `NIGHTSHIFT_SHARED_SECRET` | `manager.shared_secret` |
| `NIGHTSHIFT_DEFAULT_MODEL` | top-level `default_model` |
| `NIGHTSHIFT_TASKS_REPO` | top-level `tasks_repo` (the content-store repo's child name) |
| `NIGHTSHIFT_WIP_REF_PREFIX` | top-level `wip_ref_prefix` (the cross-machine WIP namespace) |
| `NIGHTSHIFT_PG_DSN` | `manager.dsn` — store selection (see [Database](#database--state-store)) |

### Top-level task-policy keys

These live at the root of `config.json` and are resolved into each work order or used by the dispatch/landing paths.

| Key | Default | Meaning |
|---|---|---|
| `tasks_repo` | `nightshift-tasks` | Name of the content-store repo (a workspace child) holding briefs + queue config; `tasks_root = <workspace>/<tasks_repo>`. |
| `default_model` | `auto` | Model a brief inherits when it sets no `model:`. |
| `wip_ref_prefix` | `nightshift-wip` | WIP namespace a cross-machine worker publishes its validated branch under (`refs/heads/<wip_ref_prefix>/<queue>/<task>`). Read at manager launch and handed to workers in the work order (the worker never reads this config). Editable in the Settings UI ("Branch prefix") — a change is saved to `config.json` and applies on the **next manager restart**. Scope worker push credentials to `<wip_ref_prefix>/*`; changing it after tasks exist orphans any in-flight WIP refs under the old prefix. |
| `scheduled_models` | (list) | Pin-only allow-set: an explicit `model:` must be in this list. |
| `diff_cap_lines` | `1500` | Default max changed lines for a task's result. |
| `diff_cap_exempt_paths` | (regex list) | Paths excluded from the diff cap (docs, fixtures, `.tasks/`, …). |
| `forbidden_paths` | (regex list) | Paths a worker may never modify (workflow files, Nightshift internals, agent docs). |
| `forbidden_template_paths` | (regex list) | Paths forbidden specifically in template/decomposition runs. |
| `automerge` | `false` | Default automerge for PR-mode landings. |
| `draft` | `false` | Default draft state for PR-mode landings. |
| `max_per_day` | `200` | Dispatch cap (GitHub Actions / daily-queue path). |
| `max_concurrent_queues` | `2` | Max queues served concurrently. |
| `auto_resolve` | `true` | Whether the manager hands out resolve work-orders on conflict/validation failure. |
| `max_resolve_attempts` | `2` | Resolve retries before parking. |
| `max_fix_attempts` | `6` | Fix retries (dispatch path). |
| `max_nights_before_parking` | `2` | Nights a failing task retries before being parked. |
| `resolve_model` / `resolve_backend` | `null` | Optional overrides for resolve runs. |
| `autostash_operator_work` | `true` | Stash uncommitted operator work before a local landing. |
| `model` / `cursor_model` / `worker_backend` | (see file) | Legacy defaults for the GitHub Actions and local-runner paths. |

> Note: several keys (`max_per_day`, `model`, `cursor_model`, `worker_backend`, `max_fix_attempts`, …) originate in the older GitHub Actions / local-runner flow. In the manager/worker architecture, model selection and backend are worker-owned (below); these remain for the compat paths.

## Worker configuration

A worker resolves its config in [`worker/config.py`](../src/nightshift/worker/config.py) from built-in defaults, then `<workspace>/config.json.local`, then the environment. The only strictly required setting is `manager_url`.

### `config.json.local` keys / environment variables

| `config.json.local` key | Environment variable | Default | Meaning |
|---|---|---|---|
| `worker_id` | `NIGHTSHIFT_WORKER_ID` | `<host>-<pid>` | Stable identity; must be unique per worker. |
| `backend` | `NIGHTSHIFT_WORKER_BACKEND` | `claude-code` | Which backend this worker runs (see [Backends](#backends)). |
| `manager_url` | `NIGHTSHIFT_MANAGER_URL` | `http://localhost:8800` | Manager location (required). |
| `shared_secret` | `NIGHTSHIFT_SHARED_SECRET` | `null` | Must match the manager's secret if one is set. |
| `queues` | `NIGHTSHIFT_WORKER_QUEUES` | any | Comma-separated queue labels this worker serves (`main` + playlist names). Unset = any queue. |
| `priorities` | `NIGHTSHIFT_WORKER_PRIORITIES` | any | Comma-separated 0–5 levels this worker accepts. Unset = any. |
| `models` | `NIGHTSHIFT_WORKER_MODELS` | `[]` | Request-facing model ids this worker advertises. A task pinning one of these routes here. |
| `mcps` | `NIGHTSHIFT_WORKER_MCPS` | `[]` | MCP connectors wired into this worker's harness. |
| `rendezvous_remote` | `NIGHTSHIFT_RENDEZVOUS_REMOTE` | `null` | Git remote *name* (resolved in each `repo_root`) this worker publishes its validated task branch to for cross-machine landing, as `refs/heads/<wip_ref_prefix>/<queue>/<task>` (the namespace comes from the manager's `wip_ref_prefix`, default `nightshift-wip`, delivered in the work order). Unset = co-located (publishes nothing; the manager squashes from the shared workspace). |
| `model_aliases` | — | `{}` | `{requested: actual}` remap applied at execution (identity by default). |
| `auto_model` | — | per-backend | Map overriding the model `auto` resolves to, per backend. |
| `max_model` | — | per-backend | Map overriding the model `max` resolves to, per backend. |
| `ui_host` | `NIGHTSHIFT_WORKER_UI_HOST` | `0.0.0.0` | Worker UI bind address. |
| `ui_port` | `NIGHTSHIFT_WORKER_UI_PORT` | `8810` | Worker UI bind port (must differ between co-located workers). |

Comma-separated env lists map to JSON arrays; e.g. `NIGHTSHIFT_WORKER_MODELS=gemini-3-pro,gemini-2.5-flash`.

### Capability advertisement and model resolution

The worker advertises `queues`, `priorities`, `models`, and `mcps` on every checkin and poll. The manager returns the first runnable task whose:

- queue is in the worker's `queues` (or worker is queue-agnostic), and is not dedicated to a different worker;
- priority is in the worker's `priorities` (or worker is priority-agnostic);
- pinned model is `auto`/`max`/unset, or one of the worker's advertised `models` (case-insensitive); and
- declared MCP connectors are a subset of the worker's advertised `mcps`.

At execution the worker resolves the work order's model:

- `auto` (or unset) → the worker's `auto_model` for its backend, else a built-in default.
- `max` → the worker's `max_model` for its backend, else its auto model.
- an explicit id → passed through `model_aliases` (identity unless remapped).

There is no vendor-mismatch failure: capability routing only ever hands a worker a model it advertised. Use `model_aliases` to absorb upgrades, sunsets, and cross-vendor naming (e.g. `{"gemini-3-pro": "gemini-3-pro-002"}`).

### Per-backend `auto` / `max` defaults

Overridable via the `auto_model` / `max_model` maps in `config.json.local`.

| Backend | `auto` default | `max` default |
|---|---|---|
| `claude-code` | `claude-sonnet-4-6` | `claude-opus-4-8` |
| `cursor` | `auto` (Cursor's own picker) | `claude-opus-4-8-high` |
| `gemini` | `gemini-2.5-flash` | `gemini-2.5-pro` |
| `anthropic` | `claude-sonnet-4-6` | `claude-opus-4-8` |
| `ollama` | `llama3.1` | `llama3.1:70b` |

## Backends

A worker runs exactly one backend, set by `backend`. Availability is checked at run time; the relevant tooling/credential must be present on the worker machine.

| Backend | Type | Requires | Telemetry |
|---|---|---|---|
| `claude-code` | Agentic CLI | `claude` on `PATH` (or `claude_bin`) | turns + tokens from `stream-json` |
| `cursor` | Agentic CLI | `cursor-agent` on `PATH` (or `cursor_bin`); use `cursor_model` to run a specific id (incl. Grok) | turns + tokens from `stream-json` |
| `gemini` | Agentic CLI | `gemini` on `PATH` (or `gemini_bin`) + authenticated account / `GEMINI_API_KEY` | turns + tokens from end-of-run JSON (no live stream, no cost) |
| `anthropic` | Single-shot API | `ANTHROPIC_API_KEY` | token counts with `turns=1` |
| `ollama` | Single-shot API | `ollama` on `PATH` (or `ollama_host`) | token counts with `turns=1`, no dollar cost |

Optional path overrides (`claude_bin`, `cursor_bin`, `gemini_bin`, `ollama_host`, `cursor_model`) go in `config.json.local`.

## Task frontmatter

Per-task overrides in a brief's YAML frontmatter (`.tasks/<NN>.<name>.md`), parsed in [`spawn_daily.py`](../src/nightshift/spawn_daily.py) and the [scheduler](../src/nightshift/manager/scheduler.py).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `title` | string | filename | PR/title for the task. |
| `model` | string | `default_model` (`auto`) | Worker-interpreted model or an explicit id (routes to a worker advertising it; must be in `scheduled_models`). |
| `mcp` | string | none | Comma-separated MCP connectors this task requires (e.g. `slack, github`). Routes only to a worker whose advertised connectors cover the set. |
| `priority` | int (0–5) | `5` | Lower number = higher priority; drives arbitration order. |
| `turns` | int | unlimited (`max_turns`) | Hard turn cap for this task. |
| `loc` | int | `diff_cap_lines` | Max changed lines for this task. |
| `draft` | bool | config `draft` | Open the PR as a draft (PR landing). |
| `automerge` | bool | config `automerge` | Enable automerge (PR landing). |
| `make_pr` | bool | `false` | Force PR landing for this task regardless of the manager's `landing_mode`. `true` wins over the manager default; absent/`false` defers to it (it is *not* the inverse and never forces a squash or push). |
| `split` | bool | `false` | Decomposition run: split into subtasks instead of implementing. |
| `evergreen` | bool | `false` | Reset from template instead of deleting on completion. |
| `disabled` | bool | `false` | Skip this task (never dispatched). |

Legacy plain headers are still honored: `after: <NN>.<name>.md` (blocked until that task's file leaves `main`) and `diff_cap: <n>` (superseded by `loc`).

## Queue configuration

A queue is either the default `.tasks/` queue (`main`) or a playlist (`.tasks/<name>/` with its own `config.json`). Per-queue settings, edited from the operator UI:

| Setting | Storage | API |
|---|---|---|
| Task order | playlist `config.json` | `PUT /api/queue/order` |
| Sort mode (`manual` / `priority`) | playlist `config.json` | `GET/PUT /api/queue/sort` |
| Play-priority filter (which 0–5 levels play) | playlist `config.json` | `GET/PUT /api/queue/play-priorities` |
| **Queue dedication** (bind queue → worker ids) | `nightshift.queue_routing` (manager DB) | `GET/PUT /api/queue/dedication` |

Queue dedication is manager-side: a dedicated queue's tasks are offered only to its bound worker(s); those workers still serve their other queues. For a dedicated queue to be served, the bound worker's own `queues` filter must include it (or be unset). This is the recommended way to fence an external system — dedicate the queue to a worker configured without the relevant MCP connectors.

## Database / state store

Nightshift owns its own DSN. The store is selected from `NIGHTSHIFT_PG_DSN` (env) or `manager.dsn` (config block); it deliberately does **not** fall back to longitude's `LONG_PG_DSN` or `DATABASE_URL`, so Nightshift never silently rides on the longitude database. To share one database, point `NIGHTSHIFT_PG_DSN` at the same DSN explicitly.

| Setting | Effect |
|---|---|
| `NIGHTSHIFT_PG_DSN` (or `manager.dsn`) | Use Postgres (`PgStore`) for durable state. Run `just migrate` to create/upgrade the `nightshift` schema in that DB. |
| (unset) | In-memory store (`MemoryStore`): no DB needed, but state is lost on restart and not shared across processes. |

The `nightshift` schema lives in its own migrations directory (`src/nightshift/assets/migrations/`), **separate from longitude's** root `migrations/`, so the two databases evolve independently. The files (`20260730000001_nightshift_schema.sql`, `…0002_nightshift_capability_routing.sql`, `…0003_nightshift_repo_column.sql`) create `workers` (incl. advertised `models`/`mcps`), `leases`, `tasks`, `runs` (incl. `required_mcps` + telemetry), `events` (the SSE source), the stats views, and `queue_routing` (dedication). Worker capabilities and queue dedication are runtime state, not committed config.

### Applying the schema

Run from the repo root:

| Recipe | DSN | Scope |
|---|---|---|
| `just migrate` | `NIGHTSHIFT_PG_DSN` (required) | Apply `src/nightshift/assets/migrations/*.sql` — a clean dedicated DB with just the `nightshift` schema. |
| `just rollback` | `NIGHTSHIFT_PG_DSN` (required) | Reverse them newest-first (drops the `nightshift` schema). |

Idempotent, tracking applied files in `_meta.schema_migrations` *in the target DB*. Plain-SQL fallback when `just` is unavailable: `psql "$NIGHTSHIFT_PG_DSN" -f src/nightshift/assets/migrations/<file>.sql`.

## HTTP surface (reference)

Worker-facing (`X-Nightshift-Secret` required when a secret is set):

- `POST /api/worker/checkin` — register + advertise capabilities; receive cadences.
- `POST /api/worker/poll` — capability filter in, leased work order out (or none).
- `POST /api/worker/heartbeat` — keep a lease/worker alive.
- `POST /api/worker/runs/{run_id}/events` — stream logs/phases.
- `POST /api/worker/runs/{run_id}/submit` — submit the result for landing.

Operator-facing: `/api/queue*`, `/api/tasks*`, `/api/runs`, `/api/workers`, `/api/stats`, `/api/blocked`, `/api/queue/dedication`, `/api/settings`, and the `/api/events` SSE stream (snapshot-on-connect + live deltas).
