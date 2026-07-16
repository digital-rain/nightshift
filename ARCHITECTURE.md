# Architecture

Nightshift is a pull-based overnight agent task runner built around a **manager / worker** split.
The manager owns state, queues, and git authority; workers own execution.

## High-level topology

```mermaid
flowchart LR
  op["Operator (browser)"] -->|"HTTP :8800"| mgr["Manager"]
  w1["Worker A"] -->|"poll / submit"| mgr
  w2["Worker B"] -->|"poll / submit"| mgr
  mgr -->|"squash to main"| repo["canonical main"]

  subgraph mgr["Manager :8800"]
    direction TB
    api["FastAPI app"]
    store["Store (Pg / Memory)"]
    sched["Scheduler"]
    land["Landing lock"]
    hub["SSE Hub"]
    ui["Operator UI (static)"]
  end

  subgraph w1["Worker"]
    direction TB
    loop["Poll loop"]
    exec["Execute"]
    backend["Backend (claude / cursor / …)"]
  end
```

## Repository layout

The package is run as `python -m nightshift.<entry>` (the `just` recipes wrap this):

```
src/nightshift/            the package
  manager/                 operator + worker HTTP API, operator UI, store, landing, scheduler
  worker/                  poll loop, per-task execution, worker UI, manager client
  agent/                   the in-house agentic harness (loop, tools, transport)
  slack/                   optional inbound capture daemon + outbound notifications
  git/                     the git seam: runner, worktrees, squash, landing, sync, transport
  config/                  typed config models + .nightshift/*.json and .env I/O
  pg.py                    the only asyncpg seam (structural pool type + open_pool)
  _paths.py                shipped-asset vs. operator-state path resolution
  assets/                  shipped, package-relative: ui/, ui-worker/, templates/, prompts/, config/, migrations/
docs/                      setup guide + configuration reference + topics + specs
tests/                     the scoped test suite
tools/                     the end-to-end smoke driver + maintenance janitors
```

Shipped assets are resolved relative to the installed package (`_paths.py`).
Operator state lives under the **workspace** (below), never inside the package.

## The workspace

Everything Nightshift touches at run time lives under a single **workspace** directory (the `--workspace` arg, defaulting to `NIGHTSHIFT_WORKSPACE` or the current directory; the `just` recipes forward `NIGHTSHIFT_WORKSPACE`, falling back to the repo dir).
The workspace parents:

```
<workspace>/
  .nightshift/
    manager.json           manager + task-policy config (committed)
    worker.json            this box's worker identity + capabilities (committed)
    player.json            operator UI/player preferences (committed)
  .env                     secrets only (gitignored): NIGHTSHIFT_PG_DSN, …
  nightshift-tasks/        content-store repo: queues → briefs + per-queue config.json
  <repo>/                  one or more target repos (direct children)
  .worktrees/<repo>/       git worktrees for in-progress tasks
```

Nightshift resolves repos as bare child slugs of the workspace — never absolute paths.
See `src/nightshift/repos.py` for the path-traversal guard and availability lifecycle.

## Task lifecycle

```mermaid
sequenceDiagram
  participant W as Worker
  participant M as Manager
  participant G as Git (main)

  W->>M: POST /api/worker/checkin (capabilities)
  M-->>W: cadences + ack

  loop every poll_seconds
    W->>M: POST /api/worker/poll (filter)
    M->>M: Scheduler: match capabilities → next task
    M-->>W: work order (brief, repo, base_ref, model, …)
  end

  W->>W: setup worktree, build prompt, run backend
  W->>M: POST /api/worker/heartbeat (keep lease alive)
  W->>W: validate (just validate or per-queue cmd)

  alt validated
    W->>M: POST /api/worker/runs/{run_id}/submit (branch, sha, outcome)
    M->>M: Landing lock: squash to main
    M->>G: fast-forward main (+ optional push/PR)
  else blocked / error
    W->>M: POST /api/worker/runs/{run_id}/submit (outcome=blocked|error)
  end
```

### Execution outcomes

| Outcome | Meaning | Landing |
|---------|---------|---------|
| `completed` + `landable=True` | Validated commit(s) on the task branch | Manager squashes to main |
| `completed` + `landable=False` | No commits produced (no changes needed) | Nothing to land |
| `blocked` | Agent emitted `NIGHTSHIFT_BLOCKED:` | Held for resolve |
| `error` | Launch, backend, or validation failure | Branch kept for retry/resolve |

## Component map

### Manager (`src/nightshift/manager/`)

| Module | Responsibility |
|--------|---------------|
| `app.py` | FastAPI app assembly — mounts the APIs, SSE, static UI, background loops |
| `api_worker.py` | Worker-facing API (`/api/worker/*`): checkin, poll, heartbeat, events, submit |
| `api_operator.py` | Operator-facing API (`/api/*`): queues, tasks, runs, workers, stats, settings, SSE |
| `api_playlists.py` | Playlist + repo endpoints (`/api/playlists*`, `/api/repos*`) |
| `api_repo_tasks.py` | Repo-task import endpoints (`/api/queue/repo-tasks*`) |
| `store.py` | State protocol (`NightshiftStore`) + the shared SQL query layer + `PgStore` |
| `store_sqlite.py` | `SqliteStore` — the same query layer on in-memory SQLite (tests / no-DB fallback) |
| `scheduler.py` | Cross-queue next-task arbitration: capability matching, priority, round-robin tiebreak |
| `work_orders.py` | Work-order assembly — the JSON contract handed to a polling worker |
| `landing.py` | Git authority — conflict detection, squash, remote policy (none/push/pr) |
| `reconciler.py` | Periodic recovery/hygiene loop: stale leases, orphan runs, retry state |
| `failure_policy.py` | Per-queue failure/retry state (quarantine, backoff) — pure, no I/O |
| `resolve_job.py` | Out-of-process conflict resolver the manager spawns as a separate process |
| `views.py` | API-compat projections over `attempts` rows (`/api/runs*`, `/api/leases`, SSE) |
| `wire.py` | Shared wire shapes for the worker- and operator-facing APIs |
| `hub.py` | SSE broadcast hub for live operator UI updates |
| `registry.py` | Worker registration and liveness tracking |

The manager serves the operator UI as static files from `src/nightshift/assets/ui/`.

### Worker (`src/nightshift/worker/`)

| Module | Responsibility |
|--------|---------------|
| `loop.py` | `WorkerLoop`: checkin → poll → execute → submit cycle |
| `execute.py` | Per-task execution: worktree, prompt, backend, validate — stops before landing |
| `client.py` | HTTP client for the manager API (checkin, poll, heartbeat, submit) |
| `config.py` | Re-export of `config/worker.py`: `.nightshift/worker.json` + `NIGHTSHIFT_*` env → `WorkerConfig` |
| `local_store.py` | Worker-local run history (for the worker UI) |
| `ui_app.py` | Minimal worker UI (Now + History) on `:8810` |

### Shared core

| Module | Responsibility |
|--------|---------------|
| `config/` | The config models: `ManagerSettings`/`ManagerConfig`, `WorkerConfig`, `PlayerConfig`, field metadata, `.nightshift/*.json` + `.env` I/O |
| `git/` | The git seam: `GitRunner` subprocess boundary, worktrees, squash landing, sync, transport |
| `task_files.py`, `queue_config.py` | Task lists, brief round-trips, queue order/priorities |
| `repo_tasks.py` | Repo task import: drain a target repo's `.tasks/` publishing inbox into its queue (scan the `main` tree in both legacy layouts, copy to the content store, remove from repo `main` via the landing pipeline) |
| `preflight.py`, `prompts.py` | Run preconditions, env sync, prompt building |
| `resolve_runner.py` | Conflict-resolve driver run by the manager's out-of-process resolve job |
| `backends.py` | Pluggable backend shims: `claude-code`, `cursor`, `gemini`, `anthropic`, `ollama`, `ollama-cloud`, `nightshift` |
| `agent/` | The in-house `nightshift` agentic harness: tool loop, tools, API transport |
| `model_id.py` | Provider-qualified model id parsing (`provider/model`) |
| `price.py` | Owned price table — per-run `cost_usd` for harness/Anthropic runs |
| `enhance.py` | Enhance-on-create brief rewrite (one-shot manager-side completion) |
| `lifecycle.py`, `transitions.py` | Run/task state enums and legal state transitions |
| `events.py` | Observable event types (`RUN_STARTED`, `TASK_RESULT`, etc.) |
| `repos.py` | Workspace repo addressing, slug validation, availability checks |
| `playlists.py` | Queue/playlist management |
| `spawn_daily.py` | Brief parsing (frontmatter), priority, daily spawn, autosplit |
| `render_task.py` | Brief template rendering |
| `pg.py` | The only asyncpg seam — structural pool type + `open_pool` |
| `_paths.py` | Shipped-asset vs operator-state path resolution |

### Optional

| Module | Responsibility |
|--------|---------------|
| `slack/` | Socket Mode capture daemon + outbound notifications |
| `run_local.py` | One-shot CLI runner — ephemeral in-process manager + one worker loop |

## State model

```
┌──────────────────────────────────────────────┐
│ Manager                                      │
│                                              │
│  NightshiftStore (Protocol)                  │
│  ┌────────────┐   ┌────────────────────────┐ │
│  │ SqliteStore│   │ PgStore                │ │
│  │ (fallback) │   │ nightshift.* schema    │ │
│  └────────────┘   │ workers, attempts,     │ │
│                    │ events, queue_routing  │ │
│                    └────────────────────────┘ │
│                                              │
│  Briefs: on-disk in nightshift-tasks/ repo   │
│  Landing: git ops on <workspace>/<repo>      │
└──────────────────────────────────────────────┘
```

State is split:
- **Durable coordination** (workers, leases, runs, events, routing) → Postgres via `NIGHTSHIFT_PG_DSN`.
  Falls back to an in-memory store when unset — fine for dev, state lost on restart.
- **Canonical briefs** → on-disk in the `nightshift-tasks/` git repo (committed, version-controlled).
- **Git state** → target repo clones under the workspace, with worktrees for in-progress tasks.

## Git model

The manager is the **sole writer to `main`**.
Workers never touch `main`; they produce commits on isolated task branches.

### Co-located workers

Share the workspace's repo clones.
The manager squashes the worker's branch directly through the landing pipeline (`git/landing.py`, `git/squash.py`).

### Remote workers (cross-machine)

Push validated branches to a **rendezvous remote** as `refs/heads/<wip_ref_prefix>/<queue>/<task>`.
The manager fetches, verifies the tip SHA matches the submitted `head_sha` (fail-closed), then lands.
See `docs/spec/remote-landing.md`.

### Landing lock

Every mutation of a canonical repo — landing, origin sync, transport fetch/prune — serializes on one `RepoLock` per `(workspace, repo)` (`git/locks.py`), held by that repo's executor thread (`git/executor.py`).
Locks are keyed per repo, so lands on different repos never serialize against each other; a cross-process `flock` guards against a separate process touching the same repo.
Under the lock the manager:
1. Checks for base-ref drift / content conflict (`git merge-tree`).
2. Squashes the task branch onto `main`.
3. Applies the remote policy: `none` (local only), `push`, or `pr`.

Conflicts refuse the land and preserve the branch for a resolve pass.

## Scheduling & routing

Routing is pull-based.
On every poll, the worker advertises: `queues`, `priorities`, `models`, `mcps`.
The scheduler (`manager/scheduler.py`) does:

1. Per queue: compute the ordered runnable tasks (excluding leased, blocked, after-blocked).
2. Filter by the worker's capabilities (queue membership, priority range, model set superset, MCP superset).
3. Apply manager-side queue dedication (bind a queue to specific worker ids).
4. Arbitrate across surviving queue heads: ascending priority, round-robin tiebreak.

Model keywords `auto`, `max`, `default` are **agnostic** — any worker may serve them.
A pinned explicit model routes only to workers advertising it.

## Configuration

Precedence (highest wins): environment → `.nightshift/*.json` file → built-in dataclass defaults.

| File | Scope | Committed |
|------|-------|-----------|
| `.nightshift/manager.json` | Manager + task policy: models, cadences, forbidden paths, diff caps, landing mode | Yes |
| `.nightshift/worker.json` | Worker identity: `worker_id`, `models`, `mcps`, `manager_url` | Yes |
| `.nightshift/player.json` | Operator UI/player preferences: theme, transport mode | Yes |
| `.env` | Secrets only: `NIGHTSHIFT_PG_DSN`, `NIGHTSHIFT_SHARED_SECRET`, API keys | No (gitignored) |
| Per-queue `config.json` | Queue order, repo binding, validate cmd, priority overrides | Yes (in `nightshift-tasks/`) |

Key `manager.json` blocks:
- `cadences` — `poll_seconds`, `heartbeat_seconds`, `lease_ttl_seconds`, `refresh_ms`
- `scheduled_models_allow` — filter for auto-scheduled recurring tasks (not the UI dropdown source)
- `forbidden_paths` / `forbidden_template_paths` — paths workers may not modify
- `default_model` — the model a brief inherits when it pins none (there is no manager-side backend selector: the provider half of each resolved `provider/model` id picks the backend on the worker)

Scaffold a fresh workspace with `just init` (copies the shipped templates from `src/nightshift/assets/config/`).
Full reference: [`docs/user/configuration-reference.md`](docs/user/configuration-reference.md).

## Frontend

The operator UI and worker UI are **vanilla HTML / CSS / JS** — no React, no Vite, no build step.
Assets are shipped in `src/nightshift/assets/ui/` (operator) and `assets/ui-worker/` (worker).
The manager mounts them via FastAPI `StaticFiles`.

The operator UI:
- Connects to `/api/events` (SSE) for live state convergence.
- Calls `/api/*` for mutations (add task, reorder, settings, etc.).
- Refresh cadence is driven by `cadences.refresh_ms` in `.nightshift/manager.json` (served via `/api/info`).

**Shared analytics module.** The Statistics page in both UIs is one shared,
self-contained module (`assets/ui/analytics.js` + `analytics.css`) — the manager
serves it at `/` and the worker mounts the same dir at `/shared`, so there is a
single implementation. It renders the measure-forward tuning views (KPI header
with prior-window deltas, daily trends, per-model/backend/queue breakdowns, a
waste panel, and harness run-shape attribution) over normalized run records.
Each host supplies a tiny `fetchRuns(sinceIso)` adapter: the manager reads
`/api/analytics/runs` (which exposes an explicit `landed` flag derived from the
raw attempt state, so cost-per-landed-change can separate a real change from a
no-change completion — a distinction the frozen `/api/runs` shape collapses);
the worker reads its local `/api/history`. Per-run `cost_usd` comes from the
owned price table (`src/nightshift/price.py`) for the harness and Anthropic
backends; CLI-reported cost (Claude Code) keeps precedence.

Changes to asset files take effect on the next browser reload (no HMR, no build).

## Testing

```bash
just test       # pytest only
just validate   # ruff + pytest
```

Tests use `tests/_workspace.py` (`build_workspace()`) to construct fake multi-repo workspaces with real git repos.
No live Postgres required — tests exercise the same SQL query layer through `SqliteStore` (in-memory SQLite).

Key test scopes:
- Manager API and worker protocol (`test_nightshift_manager.py`, `test_nightshift_worker.py`)
- Landing and conflict detection (`test_nightshift_landing.py`, `test_remote_landing.py`, `test_land_recovery.py`)
- Scheduler and capability routing (`test_nightshift_scheduler.py`)
- End-to-end workflow (`test_nightshift_workflow.py`)
- The git seam (`test_git_seam.py`, `test_git_executor.py`, `test_local_git_ops.py`)
- The in-house harness (`test_agent_loop.py`, `test_agent_tools.py`, `test_agent_transport.py`)
- Config models and the settings API (`test_config_model.py`, `test_settings_api.py`)

An end-to-end smoke driver (`tools/smoke.py`, `just smoke`) runs a real manager + worker as subprocesses in an isolated temp workspace; see `docs/topics/smoke-test.md`.

## Migrations

SQL migrations live in `src/nightshift/assets/migrations/`.
Applied via `just migrate` (requires `NIGHTSHIFT_PG_DSN` + `psql`); rolled back via `just rollback`.
Tracked in `_meta.schema_migrations` — idempotent on re-run.

Each migration must contain both `-- migrate:up` (applied top-down) and `-- migrate:down` (rollback) sections.
