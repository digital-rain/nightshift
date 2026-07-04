# Nightshift

A pull-based overnight agent task runner.
A **manager** owns the queues, the canonical task briefs, the centralized config, Postgres-backed state, and the git landing authority.
One or more **workers** poll the manager, run and validate work with their configured backend (`claude-code` / `cursor` / `gemini` / `anthropic` / `ollama`), then squash-submit the result for the manager to land.

Routing is pull-based: a worker advertises its capabilities (queues, priorities, models, MCP connectors) on every poll, and the manager hands back the first runnable task that fits.

```mermaid
flowchart LR
  op["Operator (browser)"] -->|"HTTP :8800"| mgr["Manager\nqueues · state · git authority"]
  w1["Worker A\nbackend + capabilities"] -->|"poll / submit"| mgr
  w2["Worker B"] -->|"poll / submit"| mgr
  mgr -->|"squash to main"| repo["canonical main"]
```

## Layout

For the full component map, data flow, state model, and design rationale see [`ARCHITECTURE.md`](ARCHITECTURE.md).

```
src/nightshift/            the package (run as `python -m nightshift.<entry>`)
  manager/                 operator + worker HTTP API, operator UI, store, landing, scheduler
  worker/                  poll loop, per-task execution, worker UI, manager client
  slack/                   optional inbound capture daemon + outbound notifications
  git/                     the git seam: runner, worktrees, squash, landing, sync, transport
  pg.py                    the only asyncpg seam (structural pool type + open_pool)
  _paths.py                shipped-asset vs. operator-state path resolution
  assets/                  shipped, package-relative: ui/, ui-worker/, templates/, prompts/, config/, migrations/
.nightshift/manager.json   manager + task-policy config (committed)
.nightshift/worker.json    this box's worker identity + capabilities (committed)
.nightshift/player.json    operator UI/player preferences (committed)
docs/                      setup guide + configuration reference + specs
tests/                     the scoped test suite
```

Shipped assets are resolved relative to the installed package.
Operator state — `.nightshift/*.json` and runtime dirs (`.tasks/`, `.worktrees/`) — lives under the **workspace** passed to each entry point (defaults to the current working directory; `just` passes the repo root).
Secrets live only in `.env` (gitignored); scaffold a fresh workspace with `just init`.

## Quickstart

```bash
# 1. Install (creates .venv via uv, installs the package editable).
just install            # == uv sync

# 2. Scaffold .nightshift/{manager,worker,player}.json + .env (idempotent).
just init

# 3. Point at a database in .env (an in-memory store is used if omitted):
#    NIGHTSHIFT_PG_DSN=postgresql://nightshift:nightshift@127.0.0.1:5432/nightshift

# 4. Create the schema (Postgres only; idempotent).
just migrate

# 5. Start the manager — operator UI + API on :8800.
just manager            # override port: just manager 8801
```

Open <http://localhost:8800> for the operator UI.

### Run a worker

Declare the worker's backend and capabilities in `.nightshift/worker.json` (scaffolded by `just init`):

```json
{
  "worker_id": "vm-1",
  "backend": "claude-code",
  "manager_url": "http://localhost:8800",
  "models": ["claude-opus-4-8", "claude-sonnet-4-6"],
  "mcps": []
}
```

```bash
just worker             # polls the manager; worker UI on :8810
```

The same settings can come from `NIGHTSHIFT_*` environment variables (env wins over `worker.json`); see [`docs/user/configuration-reference.md`](docs/user/configuration-reference.md).

## Common operations

| Goal | Command |
|---|---|
| Install deps | `just install` |
| Scaffold workspace config | `just init` |
| Apply DB schema | `NIGHTSHIFT_PG_DSN=… just migrate` |
| Roll back DB schema | `NIGHTSHIFT_PG_DSN=… just rollback` |
| Start the manager | `just manager [port]` |
| Start a worker | `just worker [ui-port]` |
| Worker, no UI (loop only) | `just worker-headless` |
| Slack capture daemon | `just slackd` (needs `uv sync --extra slack`) |
| Run tests | `just test` |
| Lint + tests | `just validate` |
| End-to-end smoke (manager + worker) | `just smoke` (see `docs/topics/smoke-test.md`) |

## Backends

A worker uses exactly one backend; install the tooling for the ones you intend to use:

- `claude-code` — the `claude` CLI on `PATH`.
- `cursor` — the `cursor-agent` CLI on `PATH`.
- `gemini` — the `gemini` CLI on `PATH`, with an authenticated account or `GEMINI_API_KEY`.
- `anthropic` — `ANTHROPIC_API_KEY` set (single-shot API backend, no CLI).
- `ollama` — the `ollama` CLI on `PATH` (or an `ollama_host`).

## Caveats inherited from the original deployment

Nightshift was extracted from a larger monorepo. A few defaults still assume that repo's layout and are worth tuning for your target project:

- `engine._attempt_repair` runs `.venv/bin/ruff check --fix` and `ruff format` over the worktree during a post-failure repair pass. This assumes the target repo uses `ruff` (from a `.venv`) and carries its own `ruff` config; drop or adjust the repair pass if that doesn't match your project.
- The default validate command is `just validate`; the queue's `config.json` (`validate`) overrides it per queue.
- `.nightshift/manager.json` `forbidden_paths` / `forbidden_template_paths` ship with the original project's protected paths; edit them to match your repo.
