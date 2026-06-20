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

```
src/nightshift/            the package (run as `python -m nightshift.<entry>`)
  manager/                 operator + worker HTTP API, operator UI, store, landing, scheduler
  worker/                  poll loop, per-task execution, worker UI, manager client
  server/                  legacy single-box UI server (viewer + player)
  slack/                   optional inbound capture daemon + outbound notifications
  engine.py                git worktrees, backend dispatch, validate/repair, landing primitives
  pg.py                    the only asyncpg seam (structural pool type + open_pool)
  _paths.py                shipped-asset vs. operator-state path resolution
  assets/                  shipped, package-relative: ui/, ui-worker/, templates/, prompts/, migrations/
config.json                operator-editable runner config (repo root)
config.json.local          worker-owned local config (gitignored)
docs/                      setup guide + configuration reference + specs
tests/                     the scoped test suite
```

Shipped assets are resolved relative to the installed package.
Operator state — `config.json`, `config.json.local`, and runtime dirs (`.tasks/`, `.worktrees/`, `.nightshift/`) — lives under the **root** passed to each entry point (defaults to the current working directory; `just` passes the repo root).

## Quickstart

```bash
# 1. Install (creates .venv via uv, installs the package editable).
just install            # == uv sync

# 2. Point at a database in .env (an in-memory store is used if omitted):
#    NIGHTSHIFT_PG_DSN=postgresql://nightshift:nightshift@127.0.0.1:5432/nightshift

# 3. Create the schema (Postgres only; idempotent).
just migrate

# 4. Start the manager — operator UI + API on :8800.
just manager            # override port: just manager 8801
```

Open <http://localhost:8800> for the operator UI.

### Run a worker

Declare the worker's backend and capabilities in `config.json.local` (gitignored):

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

The same settings can come from `NIGHTSHIFT_*` environment variables (env wins over `config.json.local`); see `docs/configuration-reference.md`.

## Common operations

| Goal | Command |
|---|---|
| Install deps | `just install` |
| Apply DB schema | `NIGHTSHIFT_PG_DSN=… just migrate` |
| Roll back DB schema | `NIGHTSHIFT_PG_DSN=… just rollback` |
| Start the manager | `just manager [port]` |
| Start a worker | `just worker [ui-port]` |
| Worker, no UI (loop only) | `just worker-headless` |
| Legacy single-box UI server | `just server [port]` |
| Slack capture daemon | `just slackd` (needs `uv sync --extra slack`) |
| Run tests | `just test` |
| Lint + tests | `just validate` |

## Backends

A worker uses exactly one backend; install the tooling for the ones you intend to use:

- `claude-code` — the `claude` CLI on `PATH`.
- `cursor` — the `cursor-agent` CLI on `PATH`.
- `gemini` — the `gemini` CLI on `PATH`, with an authenticated account or `GEMINI_API_KEY`.
- `anthropic` — `ANTHROPIC_API_KEY` set (single-shot API backend, no CLI).
- `ollama` — the `ollama` CLI on `PATH` (or an `ollama_host`).

## Caveats inherited from the original deployment

Nightshift was extracted from a larger monorepo. A few defaults still assume that repo's layout and are worth tuning for your target project:

- `engine._attempt_repair` runs `ruff` against `lib/python/long_*`, `services/`, `tools/long_cli` during a post-failure repair pass. Adjust these for the repository your workers operate on.
- The default validate command is `just validate`; the queue's `config.json` (`validate`) overrides it per queue.
- `config.json` `forbidden_paths` / `forbidden_template_paths` ship with the original project's protected paths; edit them to match your repo.
