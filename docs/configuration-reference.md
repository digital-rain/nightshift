# Nightshift — Configuration Reference

The complete configuration surface for the Nightshift manager, workers, queues, and tasks.
For a guided bring-up, start with the [Setup Guide](setup-guide.md).

## Where configuration lives

| Source | Owner | Scope | Committed? |
|---|---|---|---|
| `--workspace` / `NIGHTSHIFT_WORKSPACE` | Both | The workspace dir bound at launch (see [The workspace](#the-workspace)) | n/a |
| `<workspace>/.nightshift/manager.json` | Manager | Manager + task policy config (flat keys, `cadences` nested) | Yes |
| `<workspace>/.nightshift/worker.json` | Worker | That worker's identity, backend, capabilities | Yes |
| `<workspace>/.nightshift/player.json` | Operator | Operator UI/player preferences | Yes |
| `<repo-root>/.env` | Both | Secrets + environment overrides (`NIGHTSHIFT_*`, backend creds) | No (gitignored) |
| Process environment | Both | Same keys as `.env`, highest precedence | n/a |
| Per-queue `config.json` (content-store queue dir) | Manager | Queue order, sort mode, play-priorities, repo, validate, conflict policy | Yes (in `nightshift-tasks`) |
| `nightshift` Postgres schema | Manager | Runtime state: workers, leases, runs, events, queue dedication | n/a |
| `src/nightshift/assets/migrations/*.sql` | Manager | Nightshift's own schema migrations (separate from longitude's `migrations/`) | Yes |
| `justfile` | Operator | `migrate` / `rollback` / `manager` / `worker` / `init` recipes | Yes |
| Per-task frontmatter (`<tasks_repo>/<queue>/*.md`) | Manager | Per-task overrides (model, mcp, caps, …) | Yes (in `nightshift-tasks`) |

Precedence for a given setting, low to high: built-in default, then the `.nightshift/*.json` file, then `.env`, then the process environment.
Environment variables always win.

> **Note:** `.env` lives at the **repo root** (where the `justfile` is), not in the workspace.
> The `justfile` loads it before launching the manager or worker, making its variables available as process environment.

## The workspace

The **workspace** is the directory passed as `--workspace` to the manager and each worker.
It parents every git repo your workers operate on (each a direct child, e.g. `longitude/`), the `nightshift-tasks/` content-store repo (briefs + queue config), and the runtime dirs (`.worktrees/`, `.nightshift/`).

It is **not** a config key — the config files live inside the workspace, so the workspace must be selected before any config is read.
Resolution order (high to low):

| Source | Notes |
|---|---|
| `--workspace <dir>` | Explicit CLI flag (highest). The manager/worker entry points default to the current directory when unset. |
| `NIGHTSHIFT_WORKSPACE` | Read by the `justfile`, which forwards it as `--workspace`. Falls back to the repo dir (`just`'s directory) when unset. Set it in `.env` (e.g. `NIGHTSHIFT_WORKSPACE=$HOME/workspaces`); use an absolute path or `$HOME/…` — a literal `~` is not expanded. |

The operator UI displays the bound workspace **read-only**: it is fixed for the life of the process, so change `NIGHTSHIFT_WORKSPACE` (or `--workspace`) and relaunch rather than editing it in the UI.
A manager and a co-located worker on one box must share the same workspace.

## Templates & first-time setup

Shipped templates live in `src/nightshift/assets/config/` (resolved via `asset("config", …)`) alongside the UI, prompts, and migrations.
A committed `.env.example` at the repo root documents the secret/launch vars.

Run `just init` (or `python -m nightshift init --workspace <dir>`) to scaffold `<workspace>/.nightshift/` from the templates:

- Creates `<workspace>/.nightshift/` if absent.
- Copies each template (`manager.json`, `worker.json`, `player.json`) **only if it doesn't already exist** (never clobbers operator edits).
- Creates `<workspace>/.env` from `.env.example` if absent.

The command is idempotent and additive — safe to re-run, never destructive.

## `.env` reference

The `.env` file at the repo root holds secrets and launch variables.
The `justfile` sources it before running any recipe.
Only `NIGHTSHIFT_WORKSPACE` and `NIGHTSHIFT_MANAGER_URL` are strictly required for a local setup; the rest is optional or backend-specific.

| Variable | Required? | Default | Purpose |
|---|---|---|---|
| `NIGHTSHIFT_WORKSPACE` | Yes | repo dir | Absolute path to the workspace (parents target repos + `nightshift-tasks`). |
| `NIGHTSHIFT_MANAGER_URL` | Yes (worker) | `http://localhost:8800` | Where workers reach the manager. |
| `NIGHTSHIFT_PG_DSN` | Recommended | (in-memory) | Postgres DSN for durable state. Omit for ephemeral in-memory store. |
| `NIGHTSHIFT_SHARED_SECRET` | If remote | — | Shared secret protecting the manager API. Must match on both sides. |
| `ANTHROPIC_API_KEY` | If using `claude-code` or `anthropic` backend | — | Anthropic API key. |
| `GEMINI_API_KEY` | If using `gemini` backend | — | Gemini API key. |
| `CLAUDE_CLI_PATH` | No | auto-detect | Path to the `claude` CLI binary. |
| `CURSOR_CLI_PATH` | No | auto-detect | Path to the `cursor-agent` CLI binary. |
| `OLLAMA_BASE_URL` | If using `ollama` backend | `http://localhost:11434` | Ollama daemon address. |

**Minimal working `.env`** (single box, `claude-code` backend, Postgres):

```bash
NIGHTSHIFT_WORKSPACE=$HOME/workspaces
NIGHTSHIFT_MANAGER_URL=http://localhost:8800
NIGHTSHIFT_PG_DSN=postgresql://nightshift:nightshift@127.0.0.1:5432/nightshift
ANTHROPIC_API_KEY=sk-ant-api03-...
```

## Manager configuration

The manager reads `<workspace>/.nightshift/manager.json`.
Keys are top-level (no wrapper object); `cadences` is nested.
Secrets (`shared_secret`, `dsn`) are **not** in this file — they live in `.env`.

```json
{
  "host": "0.0.0.0",
  "port": 8800,
  "landing_mode": "none",
  "rendezvous_remote": "origin",
  "tasks_repo": "nightshift-tasks",
  "wip_ref_prefix": "nightshift-wip",
  "cadences": {
    "poll_seconds": 5.0,
    "heartbeat_seconds": 10.0,
    "lease_ttl_seconds": 120.0,
    "worker_stale_seconds": 45.0,
    "refresh_ms": 20000
  },
  "default_model": "auto",
  "max_per_day": 200,
  "automerge": false
}
```

| Key | Default | Meaning |
|---|---|---|
| `host` | `0.0.0.0` | Bind address for the manager HTTP server. |
| `port` | `8800` | Bind port (operator UI + worker/operator API). |
| `landing_mode` | `none` | Remote policy applied *after* the always-on local fast-forward of canonical `main`: `none` (local only), `push` (push `main` to origin), `pr` (open a PR). |
| `rendezvous_remote` | `origin` | Git remote *name* the manager fetches a cross-machine worker's task branch from. Set `null` to disable. |
| `tasks_repo` | `nightshift-tasks` | Name of the content-store repo. |
| `wip_ref_prefix` | `nightshift-wip` | WIP namespace for cross-machine worker branches. |
| `cadences.poll_seconds` | `5.0` | Worker idle poll interval (sent to workers at checkin). |
| `cadences.heartbeat_seconds` | `10.0` | Worker→manager heartbeat interval that keeps a lease alive. |
| `cadences.lease_ttl_seconds` | `120.0` | Lease lifetime before the manager reclaims it. |
| `cadences.worker_stale_seconds` | `45.0` | Silence after which a worker is marked `offline`. |
| `cadences.refresh_ms` | `20000` | UI safety-poll fallback (SSE is the primary live channel). |
| `default_model` | `auto` | Model a brief inherits when it sets no `model:`. |
| `scheduled_models_allow` | (list) | Filter: only auto-schedule tasks pinned to these models. UI model dropdown is populated from live worker registrations. |
| `max_per_day` | `200` | Dispatch cap (daily-queue path). |
| `max_concurrent_queues` | `2` | Max queues served concurrently. |
| `max_nights_before_parking` | `2` | Nights a failing task retries before being parked. |
| `diff_cap_lines` | `1500` | Default max changed lines for a task's result. |
| `diff_cap_exempt_paths` | (regex list) | Paths excluded from the diff cap. |
| `forbidden_paths` | (regex list) | Paths a worker may never modify. |
| `forbidden_template_paths` | (regex list) | Paths forbidden in template/decomposition runs. |
| `automerge` | `false` | Default automerge for PR-mode landings. |
| `draft` | `false` | Default draft state for PR-mode landings. |
| `autostash_operator_work` | `true` | Stash uncommitted operator work before a local landing. |
| `max_fix_attempts` | `6` | Fix retries (dispatch path). |
| `auto_resolve` | `true` | Hand out resolve work-orders on conflict/validation failure. |
| `max_resolve_attempts` | `2` | Resolve retries before parking. |
| `resolve_model` / `resolve_backend` | `null` | Optional overrides for resolve runs. |

Cadences are config-driven, never hardcoded.
The manager sends them to each worker at checkin, so changing them here changes worker behavior too.

### Manager environment overrides

| Variable | Overrides |
|---|---|
| `NIGHTSHIFT_WORKSPACE` | The `--workspace` launch dir (see [The workspace](#the-workspace)) |
| `NIGHTSHIFT_MANAGER_HOST` | `host` |
| `NIGHTSHIFT_MANAGER_PORT` | `port` |
| `NIGHTSHIFT_LANDING_MODE` | `landing_mode` (`none` / `push` / `pr`) |
| `NIGHTSHIFT_RENDEZVOUS_REMOTE` | `rendezvous_remote` |
| `NIGHTSHIFT_SHARED_SECRET` | Shared secret (stored in `.env`, not in `manager.json`) |
| `NIGHTSHIFT_DEFAULT_MODEL` | `default_model` |
| `NIGHTSHIFT_TASKS_REPO` | `tasks_repo` |
| `NIGHTSHIFT_WIP_REF_PREFIX` | `wip_ref_prefix` |
| `NIGHTSHIFT_PG_DSN` | Database DSN (stored in `.env`, not in `manager.json`) |

## Worker configuration

A worker resolves its config in [`config/worker.py`](../src/nightshift/config/worker.py) from built-in defaults, then `<workspace>/.nightshift/worker.json`, then the environment.
The only strictly required setting is `manager_url`.
The `backend` field is **the** backend selector (there is no manager-side `worker_backend`).

### `worker.json` keys / environment variables

| `worker.json` key | Environment variable | Default | Meaning |
|---|---|---|---|
| `worker_id` | `NIGHTSHIFT_WORKER_ID` | `<host>-<pid>` | Stable identity; must be unique per worker. |
| `backend` | `NIGHTSHIFT_WORKER_BACKEND` | `claude-code` | Which backend this worker runs (see [Backends](#backends)). |
| `manager_url` | `NIGHTSHIFT_MANAGER_URL` | `http://localhost:8800` | Manager location (required). |
| `shared_secret` | `NIGHTSHIFT_SHARED_SECRET` | `null` | Must match the manager's secret if one is set. Stored in `.env`, not `worker.json`. |
| `rendezvous_remote` | `NIGHTSHIFT_RENDEZVOUS_REMOTE` | `null` | Git remote for cross-machine landing. |
| `queues` | `NIGHTSHIFT_WORKER_QUEUES` | any | Comma-separated queue labels this worker serves. Unset = any queue. |
| `priorities` | `NIGHTSHIFT_WORKER_PRIORITIES` | any | Comma-separated 0–5 levels this worker accepts. Unset = any. |
| `models` | `NIGHTSHIFT_WORKER_MODELS` | `[]` | Request-facing model ids this worker advertises. |
| `mcps` | `NIGHTSHIFT_WORKER_MCPS` | `[]` | MCP connectors wired into this worker's harness. |
| `model_aliases` | — | `{}` | `{requested: actual}` remap applied at execution. |
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

Overridable via the `auto_model` / `max_model` maps in `worker.json`.

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

Optional path overrides (`claude_bin`, `cursor_bin`, `gemini_bin`, `ollama_host`, `cursor_model`) go in `worker.json`.

## Task frontmatter

Per-task overrides in a brief's YAML frontmatter (`<tasks_repo>/<queue>/<NN>.<name>.md`), parsed in [`spawn_daily.py`](../src/nightshift/spawn_daily.py) and the [scheduler](../src/nightshift/manager/scheduler.py).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `title` | string | filename | PR/title for the task. |
| `model` | string | `default_model` (`auto`) | Worker-interpreted model or an explicit id. |
| `mcp` | string | none | Comma-separated MCP connectors this task requires. |
| `priority` | int (0–5) | `5` | Lower number = higher priority. |
| `turns` | int | unlimited (`max_turns`) | Hard turn cap for this task. |
| `loc` | int | `diff_cap_lines` | Max changed lines for this task. |
| `draft` | bool | config `draft` | Open the PR as a draft (PR landing). |
| `automerge` | bool | config `automerge` | Enable automerge (PR landing). |
| `make_pr` | bool | `false` | Force PR landing for this task. |
| `split` | bool | `false` | Decomposition run: split into subtasks. |
| `evergreen` | bool | `false` | Reset from template on completion. |
| `disabled` | bool | `false` | Skip this task (never dispatched). |

## Queue configuration

A queue is a top-level directory of the `nightshift-tasks` content store. Per-queue settings, edited from the operator UI:

| Setting | Storage | API |
|---|---|---|
| Task order | playlist `config.json` | `PUT /api/queue/order` |
| Sort mode (`manual` / `priority`) | playlist `config.json` | `GET/PUT /api/queue/sort` |
| Play-priority filter | playlist `config.json` | `GET/PUT /api/queue/play-priorities` |
| **Queue dedication** | `nightshift.queue_routing` (manager DB) | `GET/PUT /api/queue/dedication` |

## Database / state store

Nightshift owns its own DSN. The store is selected from `NIGHTSHIFT_PG_DSN` (env/.env); it deliberately does **not** fall back to longitude's `LONG_PG_DSN`.

| Setting | Effect |
|---|---|
| `NIGHTSHIFT_PG_DSN` set | Use Postgres (`PgStore`). Run `just migrate` to create/upgrade the schema. |
| (unset) | In-memory store (`MemoryStore`): no DB needed, state lost on restart. |

### Applying the schema

| Recipe | DSN | Scope |
|---|---|---|
| `just migrate` | `NIGHTSHIFT_PG_DSN` (required) | Apply `src/nightshift/assets/migrations/*.sql`. |
| `just rollback` | `NIGHTSHIFT_PG_DSN` (required) | Reverse them newest-first. |

## Startup order

1. **Manager first** — `just manager` (or `just server` for single-host mode).
2. **Workers after** — `just worker [port]`. Each worker connects to `NIGHTSHIFT_MANAGER_URL` immediately on startup; if the manager isn't reachable, the worker exits with `httpx.ConnectError: Connection refused`.

The operator UI is served by the manager; open it in a browser once the manager logs `Uvicorn running on …`.

## HTTP surface (reference)

Worker-facing (`X-Nightshift-Secret` required when a secret is set):

- `POST /api/worker/checkin` — register + advertise capabilities; receive cadences.
- `POST /api/worker/poll` — capability filter in, leased work order out (or none).
- `POST /api/worker/heartbeat` — keep a lease/worker alive.
- `POST /api/worker/runs/{run_id}/events` — stream logs/phases.
- `POST /api/worker/runs/{run_id}/submit` — submit the result for landing.

Operator-facing: `/api/queue*`, `/api/tasks*`, `/api/runs`, `/api/workers`, `/api/models`, `/api/stats`, `/api/blocked`, `/api/queue/dedication`, `/api/settings`, and the `/api/events` SSE stream (snapshot-on-connect + live deltas).
