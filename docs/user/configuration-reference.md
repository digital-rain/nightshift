# Nightshift — Configuration Reference

The complete configuration surface for the Nightshift manager, workers, queues, and tasks.
For a guided bring-up, start with the [Setup Guide](setup-guide.md).

## Where configuration lives

| Source | Owner | Scope | Committed? |
|---|---|---|---|
| `--workspace` / `NIGHTSHIFT_WORKSPACE` | Both | The workspace dir bound at launch (see [The workspace](#the-workspace)) | n/a |
| `<workspace>/.nightshift/manager.json` | Manager | Manager + task-policy config (flat keys, `cadences` nested) | Yes |
| `<workspace>/.nightshift/worker.json` | Worker | That worker's identity and capabilities | Yes |
| `<workspace>/.nightshift/player.json` | Operator | Operator UI/player preferences | Yes |
| `.env` | Both | Secrets + environment overrides (`NIGHTSHIFT_*`, backend creds) | No (gitignored) |
| Process environment | Both | Same keys as `.env`, highest precedence | n/a |
| Per-queue `config.json` (content-store queue dir) | Manager | Queue order, sort mode, play-priorities, repo binding, validate/preflight commands | Yes (in `nightshift-tasks`) |
| Per-task frontmatter (`<tasks_repo>/<queue>/*.md`) | Manager | Per-task overrides (model, mcp, priority, …) | Yes (in `nightshift-tasks`) |
| `nightshift` Postgres schema | Manager | Runtime state: workers, leases, runs, events, queue dedication | n/a |
| `src/nightshift/assets/migrations/*.sql` | Manager | Nightshift's own schema migrations | Yes |
| `justfile` | Operator | `init` / `manager` / `worker` / `migrate` / `rollback` recipes | Yes |

Precedence for a given setting, low to high: built-in default, then the `.nightshift/*.json` file, then `.env`, then the process environment.
Environment variables always win.

> **Where `.env` is read from.** The `justfile` loads the `.env` at the **repo root** before running any recipe (this is where `NIGHTSHIFT_WORKSPACE` must live, since it selects the workspace).
> Each entry point additionally loads `<workspace>/.env` at startup with setdefault semantics (a real environment variable always wins).
> When the workspace is the repo dir — the default — they are the same file.

## The workspace

The **workspace** is the directory passed as `--workspace` to the manager and each worker.
It parents every git repo your workers operate on (each a direct child, e.g. `my-project/`), the `nightshift-tasks/` content-store repo (briefs + queue config), and the runtime dirs (`.worktrees/`, `.nightshift/`).

It is **not** a config key — the config files live inside the workspace, so the workspace must be selected before any config is read.
Resolution order (high to low):

| Source | Notes |
|---|---|
| `--workspace <dir>` | Explicit CLI flag (highest). The manager/worker entry points default to the current directory when unset. |
| `NIGHTSHIFT_WORKSPACE` | Read by the `justfile`, which forwards it as `--workspace`. Falls back to the repo dir (`just`'s directory) when unset. Set it in the repo-root `.env` (e.g. `NIGHTSHIFT_WORKSPACE=$HOME/workspaces`); use an absolute path or `$HOME/…` — a literal `~` is not expanded. |

The operator UI displays the bound workspace **read-only**: it is fixed for the life of the process, so change `NIGHTSHIFT_WORKSPACE` (or `--workspace`) and relaunch rather than editing it in the UI.
A manager and a co-located worker on one box must share the same workspace.

## Templates & first-time setup

Shipped templates live in `src/nightshift/assets/config/` alongside the UI, prompts, and migrations.
A committed `.env.example` at the repo root documents the secret/launch vars.

Run `just init` (or `python -m nightshift init --workspace <dir>`) to scaffold `<workspace>/.nightshift/` from the templates:

- Creates `<workspace>/.nightshift/` if absent.
- Copies each template (`manager.json`, `worker.json`, `player.json`) **only if it doesn't already exist** (never clobbers operator edits).
- Creates `<workspace>/.env` from `.env.example` if absent.

The command is idempotent and additive — safe to re-run, never destructive.

## `.env` reference

Only `NIGHTSHIFT_WORKSPACE` is strictly required for a local setup (and only when the workspace is not the repo dir); the rest is optional or backend-specific.

| Variable | Required? | Default | Purpose |
|---|---|---|---|
| `NIGHTSHIFT_WORKSPACE` | If not the repo dir | repo dir | Absolute path to the workspace (parents target repos + `nightshift-tasks`). |
| `NIGHTSHIFT_MANAGER_URL` | Worker | `http://localhost:8800` | Where workers reach the manager. |
| `NIGHTSHIFT_PG_DSN` | Recommended | (in-memory) | Postgres DSN for durable state. Omit for the ephemeral in-memory store. |
| `NIGHTSHIFT_SHARED_SECRET` | If remote | — | Shared secret protecting the manager's worker API. Must match on both sides. |
| `ANTHROPIC_API_KEY` | `anthropic` backend, harness, or enhance-on-create | — | Anthropic API key. |
| `GEMINI_API_KEY` | `gemini` backend (unless the CLI account is authenticated) | — | Gemini API key. |
| `OLLAMA_API_KEY` | `ollama-cloud` backend | — | Ollama Cloud API key (create at `https://ollama.com/settings/keys`). Sent as a Bearer token to `https://ollama.com`. |
| `OLLAMA_HOST` | No | `http://localhost:11434` | Local Ollama daemon address (harness `ollama` vendor). |

Backend CLIs (`claude`, `cursor-agent`, `gemini`, `ollama`) are resolved from `PATH`, with a fallback scan of the common install dirs a non-login shell misses (`~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`).

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
Every key is editable in the manager Settings UI, which writes back to this file.

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
    "refresh_ms": 20000,
    "git_refresh_seconds": 15.0
  },
  "default_model": "auto",
  "max_per_day": 200,
  "automerge": false
}
```

### Identity, landing & git

| Key | Default | Meaning |
|---|---|---|
| `host` | `0.0.0.0` | Bind address for the manager HTTP server. |
| `port` | `8800` | Bind port (operator UI + worker/operator API). |
| `landing_mode` | `none` | Remote policy applied *after* the always-on local squash to canonical `main`: `none` (local only), `push` (push `main` to origin), `pr` (open a PR). |
| `rendezvous_remote` | `origin` | Git remote *name* the manager fetches a cross-machine worker's task branch from. Set `null` to disable. |
| `tasks_repo` | `nightshift-tasks` | Name of the content-store repo. |
| `wip_ref_prefix` | `nightshift-wip` | WIP namespace for cross-machine worker branches. |
| `automerge` | `false` | Default automerge for PR-mode landings. |
| `draft` | `false` | Default draft state for PR-mode landings. |
| `max_push_retries` | `3` | How many times a land re-syncs origin/main and re-squashes when the push is rejected because origin advanced (optimistic concurrency). |
| `validate_on_integrate` | `false` | Re-run the validate command on the integrated tree before pushing when origin drifted but the squash was textually clean (guards against semantic conflicts). |

### Cadences

| Key | Default | Meaning |
|---|---|---|
| `cadences.poll_seconds` | `5.0` | Worker idle poll interval (sent to workers at checkin). |
| `cadences.heartbeat_seconds` | `10.0` | Worker→manager heartbeat interval that keeps a lease alive. |
| `cadences.lease_ttl_seconds` | `120.0` | Lease lifetime before the manager reclaims it. |
| `cadences.worker_stale_seconds` | `45.0` | Silence after which a worker is marked `offline`. |
| `cadences.refresh_ms` | `20000` | UI safety-poll fallback (SSE is the primary live channel). |
| `cadences.git_refresh_seconds` | `15.0` | Minimum seconds between origin/main fetch checks per target repo. Each check fetches and fast-forwards local `main` **only when it is strictly behind** `origin/main`; unpushed or divergent local commits (e.g. a direct cherry-pick on `main`) are left alone. `0` disables throttling. Legacy key `origin_sync_seconds` is still read as a fallback. |

Cadences are config-driven, never hardcoded.
The manager sends them to each worker at checkin, so changing them here changes worker behavior too.

### Scheduling

| Key | Default | Meaning |
|---|---|---|
| `default_model` | `auto` | Model a brief inherits when it sets no `model:`. |
| `enhance_brief_model` | `anthropic/claude-sonnet-4-6` | Model for the enhance-on-create brief rewrite (a one-shot manager-side completion). |
| `planner_model` | `""` | Default model for a workflow's `planner` role (spec §3.2). Empty falls through to `default_model`. A brief's `planner_model:` and the queue's `workflow_models.planner` override it. |
| `scheduled_models_allow` | (list) | Filter: only auto-schedule tasks pinned to these provider-qualified model ids (e.g. `claude-code/claude-sonnet-4-6`). The UI model dropdown is populated from live worker registrations, not this list. |
| `max_per_day` | `200` | Dispatch cap (daily-queue path). |
| `max_concurrent_queues` | `2` | Max queues served concurrently. |
| `max_nights_before_parking` | `2` | Nights a failing task retries before being parked. |

### Worker execution policy

| Key | Default | Meaning |
|---|---|---|
| `validate` | `just validate` | System-wide default validate command run after each task. Per-queue `validate` in queue `config.json` overrides it. Empty string disables validation globally. |
| `diff_cap_lines` | `1500` | Default max changed lines for a task's result. |
| `diff_cap_exempt_paths` | (regex list) | Paths excluded from the diff cap. |
| `forbidden_paths` | (regex list) | Paths a worker may never modify. Ships protecting `.github/workflows/`, `CLAUDE.md`, `AGENTS.md`. |
| `forbidden_template_paths` | (regex list) | Paths forbidden specifically in template/decomposition runs. |
| `max_fix_attempts` | `6` | Fix retries (dispatch path). |
| `quarantine_threshold` | `2` | Consecutive no-progress runs of one task before it is quarantined: held in the queue but skipped by every worker until an operator releases it. `0` disables. |
| `retry_backoff_seconds` | `60.0` | Base of the exponential retry backoff: the n-th consecutive no-progress attempt delays the task's next dispatch by `base * 2**(n-1)` seconds (capped at an hour). |

### Task documents

Governs the by-reference delivery of `docs:` / `attachments:` (see [Task documents](#task-documents-by-reference-delivery)). All three are ceiling-clamped hard limits — Nightshift never delivers more than the operator explicitly permits.

| Key | Default | Meaning |
|---|---|---|
| `document_cap_bytes` | `262_144` (256 KiB) | Per-document byte cap. Applies to each `docs:` blob and each `attachments:` file individually; over-cap attach requests are rejected with `400 document_too_large`. The `NIGHTSHIFT_DOCUMENT_CAP_BYTES` environment variable overrides this at load time; both are clamped to a hard ceiling of `5 MiB` — a larger configured value is silently reduced. |
| `document_budget_bytes` | `4_194_304` (4 MiB) | Total document footprint per task (paths + attachments). Attach requests that would push the sum over the budget are rejected with `400 document_budget_exceeded`. |
| `allowed_doc_media_types` | `["text/*", "application/json", "application/yaml", "image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"]` | Allow-list of media types (glob patterns supported). Anything outside is rejected with `400 unsupported_document_type` at attach time and never resolved into `docs_pin`. |

### Conflict resolution

| Key | Default | Meaning |
|---|---|---|
| `auto_resolve` | `true` | Hand out resolve work-orders on conflict/validation failure. |
| `max_resolve_attempts` | `2` | Resolve retries before parking. |
| `resolve_model` / `resolve_backend` | `null` | Optional overrides for resolve runs. |
| `max_concurrent_resolves` | `1` | Cap on simultaneous out-of-process resolve jobs per repo. |

### Manager environment overrides

| Variable | Overrides |
|---|---|
| `NIGHTSHIFT_WORKSPACE` | The `--workspace` launch dir (see [The workspace](#the-workspace)) |
| `NIGHTSHIFT_MANAGER_HOST` | `host` |
| `NIGHTSHIFT_MANAGER_PORT` | `port` |
| `NIGHTSHIFT_LANDING_MODE` | `landing_mode` (`none` / `push` / `pr`) |
| `NIGHTSHIFT_RENDEZVOUS_REMOTE` | `rendezvous_remote` |
| `NIGHTSHIFT_DEFAULT_MODEL` | `default_model` |
| `NIGHTSHIFT_ENHANCE_MODEL` | `enhance_brief_model` |
| `NIGHTSHIFT_PLANNER_MODEL` | `planner_model` |
| `NIGHTSHIFT_TASKS_REPO` | `tasks_repo` |
| `NIGHTSHIFT_WIP_REF_PREFIX` | `wip_ref_prefix` |
| `NIGHTSHIFT_QUARANTINE_THRESHOLD` | `quarantine_threshold` |
| `NIGHTSHIFT_DOCUMENT_CAP_BYTES` | `document_cap_bytes` (clamped to the 5 MiB ceiling) |
| `NIGHTSHIFT_SHARED_SECRET` | Shared secret (stored in `.env`, never in `manager.json`) |
| `NIGHTSHIFT_PG_DSN` | Database DSN (stored in `.env`, never in `manager.json`) |

## Worker configuration

A worker resolves its config in [`config/worker.py`](../../src/nightshift/config/worker.py) from built-in defaults, then `<workspace>/.nightshift/worker.json`, then the environment.
There is no `backend` key: providers are derived automatically from the qualified model ids in `models` / `auto_model` / `max_model`, and the provider half of each resolved id picks the backend per task.

### `worker.json` keys / environment variables

| `worker.json` key | Environment variable | Default | Meaning |
|---|---|---|---|
| `worker_id` | `NIGHTSHIFT_WORKER_ID` | `<host>-<pid>` | Stable identity; must be unique per worker. |
| `manager_url` | `NIGHTSHIFT_MANAGER_URL` | `http://localhost:8800` | Manager location (required). |
| — | `NIGHTSHIFT_SHARED_SECRET` | `null` | Must match the manager's secret if one is set. Lives in `.env`, never in `worker.json`. |
| `rendezvous_remote` | `NIGHTSHIFT_RENDEZVOUS_REMOTE` | `null` | Git remote for cross-machine landing; `null` = co-located. |
| `queues` | `NIGHTSHIFT_WORKER_QUEUES` | any | Comma-separated queue labels this worker serves. Unset = any queue. |
| `priorities` | `NIGHTSHIFT_WORKER_PRIORITIES` | any | Comma-separated 0–5 levels this worker accepts. Unset = any. |
| `models` | `NIGHTSHIFT_WORKER_MODELS` | `[]` | Provider-qualified model ids (`provider/model`) this worker advertises. |
| `mcps` | `NIGHTSHIFT_WORKER_MCPS` | `[]` | MCP connectors wired into this worker's harness. |
| `model_aliases` | — | `{}` | `{requested: actual}` remap applied at execution. |
| `auto_model` | — | `claude-code/claude-sonnet-4-6` | Single qualified id that `auto` resolves to. |
| `max_model` | — | `claude-code/claude-opus-4-8` | Single qualified id that `max` resolves to. |
| `model_timeout_seconds` | `NIGHTSHIFT_MODEL_TIMEOUT_SECONDS` | `0` (disabled) | Global wall-clock timeout applied to every backend run. `0` means no limit. |
| `quarantine` | `NIGHTSHIFT_WORKER_QUARANTINE` | `false` | When enabled, any task that fails on this worker is immediately quarantined (held in the queue, skipped by every worker) instead of retried. |
| `worker_url` | `NIGHTSHIFT_WORKER_URL` | `null` | Externally reachable URL for this worker's UI, sent at checkin so the operator UI can link through. |
| `ui_host` | `NIGHTSHIFT_WORKER_UI_HOST` | `0.0.0.0` | Worker UI bind address. |
| `ui_port` | `NIGHTSHIFT_WORKER_UI_PORT` | `8810` | Worker UI bind port (must differ between co-located workers). |
| `nightshift` | — | (disabled) | Nested in-house agentic harness settings — see [the harness topic](../topics/agentic-harness.md). |

Comma-separated env lists map to JSON arrays; e.g. `NIGHTSHIFT_WORKER_MODELS=claude-code/claude-sonnet-4-6,ollama-cloud/gpt-oss:120b`.

### Capability advertisement and model resolution

The worker advertises `queues`, `priorities`, `models`, and `mcps` on every checkin and poll.
The `models` list contains provider-qualified ids (e.g. `claude-code/claude-sonnet-4-6`); a model is only advertised when its provider's backend is actually available on that box (CLI on `PATH`, or credential set).
The manager returns the first runnable task whose:

- queue is in the worker's `queues` (or worker is queue-agnostic), and is not dedicated to a different worker;
- priority is in the worker's `priorities` (or worker is priority-agnostic);
- pinned model is `auto`/`max`/unset, or one of the worker's advertised `models` (case-insensitive); and
- declared MCP connectors are a subset of the worker's advertised `mcps`.

At execution the worker resolves the work order's model:

- `auto` (or unset) → the worker's `auto_model` (a single qualified id, default `claude-code/claude-sonnet-4-6`).
- `max` → the worker's `max_model` (a single qualified id, default `claude-code/claude-opus-4-8`).
- an explicit qualified id → passed through `model_aliases` (identity unless remapped), then the provider prefix selects the backend.

The provider portion of the resolved model id determines which backend executes the task. A single worker can serve multiple providers simultaneously (e.g. `claude-code` and `ollama-cloud`) by advertising models from each.

There is no vendor-mismatch failure: capability routing only ever hands a worker a model it advertised. Use `model_aliases` to absorb upgrades, sunsets, and cross-vendor naming (e.g. `{"gemini-3-pro": "gemini/gemini-3-pro-002"}`).

Model keywords `auto`, `max`, and `default` are **agnostic** — any worker may serve them; each worker resolves them to its own configured `auto_model` / `max_model`.

## Backends

A worker can serve multiple providers concurrently. The provider is chosen per task from the resolved model's qualified id (the part before the `/`). Availability is checked at run time; the relevant tooling/credential must be present on the worker machine.

| Backend | Type | Requires | Telemetry |
|---|---|---|---|
| `claude-code` | Agentic CLI | `claude` on `PATH` | turns + tokens + cost from `stream-json` |
| `cursor` | Agentic CLI | `cursor-agent` on `PATH` | turns + tokens from `stream-json` |
| `gemini` | Agentic CLI | `gemini` on `PATH` + authenticated account / `GEMINI_API_KEY` | turns + tokens from end-of-run JSON (no live stream, no cost) |
| `anthropic` | Single-shot API | `ANTHROPIC_API_KEY` | token counts with `turns=1` |
| `ollama` | Single-shot API | `ollama` on `PATH` / a local daemon | token counts with `turns=1`, no dollar cost |
| `ollama-cloud` | Single-shot API | `OLLAMA_API_KEY` (cloud-hosted on `ollama.com`) | token counts with `turns=1`, no dollar cost |
| `nightshift` | Agentic harness (in-house) | The chosen vendor's API credential; enabled via `worker.json`'s `nightshift.enabled` | turns + per-turn tokens + cost from the owned price table |

The single-shot API backends stream a model response but do not edit files, so their runs finish as "no changes"; they exist to measure raw model latency/throughput against the agent CLIs.
The `nightshift` harness is the in-house agentic loop over the Anthropic or Ollama APIs — see [`docs/topics/agentic-harness.md`](../topics/agentic-harness.md).

## Task frontmatter

Per-task overrides in a brief's YAML frontmatter (`<tasks_repo>/<queue>/<NN>.<name>.md`).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `title` | string | filename | Display/PR title for the task. |
| `model` | string | `default_model` (`auto`) | `auto`, `max`, or an explicit provider-qualified id. |
| `mcp` | string | none | Comma-separated MCP connectors this task requires (routes to workers advertising them). |
| `priority` | int (0–5) | `5` | Lower number = higher priority. |
| `turns` | int | unlimited | Hard turn cap for this task. |
| `repo` | string | queue's repo | Target-repo override (bare child name in the workspace). |
| `after` | string | none | Dependency: task stem(s) that must complete first. |
| `draft` | bool | config `draft` | Open the PR as a draft (PR landing). |
| `automerge` | bool | config `automerge` | Enable automerge (PR landing). |
| `make_pr` | bool | `false` | Force PR landing for this task regardless of the manager's `landing_mode`. |
| `split` | bool | `false` | Decomposition run: write subtask briefs instead of implementing. |
| `loop` | bool | `false` | Ralph-loop mode: iterative multi-pass prompt instead of the single-pass prompt. |
| `loop_max_iterations` | int | `0` (unbounded) | Iteration cap for `loop` tasks. |
| `workflow` | string | none | Run this task under a named workflow definition (spec §3). Mutually exclusive with `loop`. |
| `planner_model` | string | config `planner_model` | Model for this task's workflow `planner` role, overriding the manager default. |
| `autosplit` | bool | `false` | Accumulation task that periodically splits into subtasks. |
| `evergreen` | bool | `false` | Reset from template on completion instead of being consumed. |
| `disabled` | bool | `false` | Skip this task (never dispatched). |
| `docs` | list | none | Repo paths (or `{path, range?, as?, steps?}` objects) delivered to the worker by reference. Pinned to their blob sha on first dispatch (see `docs_pin`). |
| `attachments` | list | none | Task-local filenames under `<task>.docs/` in the tasks repo (bytes committed alongside the brief); delivered by reference to the worker. |
| `docs_pin` | JSON object | (engine-owned) | Written by the engine on first dispatch — a `{key: {sha, media, bytes}}` map that locks each `docs:` path and `attach:<name>` to a specific blob so later runs materialize identical bytes even if the source drifts. Never hand-edited; the operator uses the detail pane's **Re-pin** button. |

The system also writes state flags into frontmatter as a task progresses (`quarantined` / `quarantine_reason`, `failed` / `failed_reason`, `completed`, `enhanced`); these are managed from the UI, not hand-edited. Workflow tasks additionally carry the engine-owned cursor (`workflow_step`, `workflow_visits`), which the engine writes and operators must not edit.

### Task documents (by-reference delivery)

Task documents attach real content to a brief — repo paths (`docs:`) and task-local attachments (`attachments:`) — without embedding bytes in the work order. On first dispatch the engine resolves every entry to a Git blob sha and writes the `docs_pin` map into the brief's frontmatter; every later run materializes the exact same bytes from the target-repo (`docs:`) or tasks-repo (`attachments:`) object store, so a source edit at the base_ref never silently changes what a running task sees. The manager UI surfaces drift ("source drifted — pinned to older version") with a **Re-pin** action that refreshes the pin against the current base_ref.

## Queue configuration

A queue is a top-level directory of the `nightshift-tasks` content store; the default queue is `main`.
Per-queue `config.json` keys layer over `<tasks_root>/config.json` (a store-wide layer) and `manager.json` — a queue inherits every setting it does not override:

| Key | Meaning |
|---|---|
| `repo` | Target repo (bare child name) this queue's tasks run against. |
| `validate` | Validate command for this queue (overrides the manager-wide `validate`; empty string disables). |
| `preflight` | Environment preflight command run in the worktree before the agent (default `uv sync --frozen`; empty string disables). |
| `max_turns` | Default turn cap for the queue's tasks. |
| `workflow_models` | Map of workflow *role* → model pin for this queue (e.g. `{"planner": "anthropic/claude-opus-4-8", "implementor": "claude-code/claude-sonnet-4-6"}`). Resolved per the §3.2 ladder: a brief's own `model`/`planner_model` wins, then this map, then the manager defaults. |

Queue presentation settings, edited from the operator UI:

| Setting | Storage | API |
|---|---|---|
| Task order | queue `config.json` | `PUT /api/queue/order` |
| Sort mode (`manual` / `priority`) | queue `config.json` | `GET/PUT /api/queue/sort` |
| Play-priority filter | queue `config.json` | `GET/PUT /api/queue/play-priorities` |
| **Queue dedication** | `nightshift.queue_routing` (manager DB) | `GET/PUT /api/queue/dedication` |

## Database / state store

The store is selected from `NIGHTSHIFT_PG_DSN` (env/`.env`).

| Setting | Effect |
|---|---|
| `NIGHTSHIFT_PG_DSN` set | Use Postgres (`PgStore`). Run `just migrate` to create/upgrade the schema. |
| (unset) | In-memory SQLite store (`SqliteStore`): no DB needed, state lost on restart. |

### Applying the schema

| Recipe | DSN | Scope |
|---|---|---|
| `just migrate` | `NIGHTSHIFT_PG_DSN` (required) | Apply `src/nightshift/assets/migrations/*.sql` in filename order (idempotent; tracked in `_meta.schema_migrations`). |
| `just rollback` | `NIGHTSHIFT_PG_DSN` (required) | Reverse them newest-first (drops the `nightshift` schema). |

## Startup order

1. **Manager first** — `just manager`.
2. **Workers after** — `just worker [ui-port]`. Each worker checks in with `NIGHTSHIFT_MANAGER_URL` immediately on startup; if the manager isn't reachable, the worker exits with `httpx.ConnectError: Connection refused`.

The operator UI is served by the manager; open it in a browser once the manager logs `Uvicorn running on …`.

## HTTP surface (reference)

Worker-facing (`X-Nightshift-Secret` required when a secret is set):

- `POST /api/worker/checkin` — register + advertise capabilities; receive cadences.
- `POST /api/worker/poll` — capability filter in, leased work order out (or none).
- `POST /api/worker/heartbeat` — keep a lease/worker alive.
- `POST /api/worker/runs/{run_id}/events` — stream logs/phases.
- `POST /api/worker/runs/{run_id}/submit` — submit the result for landing.

Operator-facing: `/api/queue*`, `/api/tasks*`, `/api/runs`, `/api/workers`, `/api/models`, `/api/stats`, `/api/blocked`, `/api/playlists*`, `/api/repos*`, `/api/queue/dedication`, `/api/settings`, and the `/api/events` SSE stream (snapshot-on-connect + live deltas).
