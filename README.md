# <img src="src/nightshift/assets/ui/logos/winged-moon.png" alt="Nightshift logo" width="48" /> Nightshift

A pull-based overnight agent task runner.

You write task briefs — markdown files with a little frontmatter.
A **manager** owns the queues, the canonical briefs, the centralized config, Postgres-backed state, and the git landing authority.
One or more **workers** poll the manager, execute each task with a coding agent (Claude Code, Cursor, Gemini CLI, or a raw API model), validate the result, and squash-submit it back for the manager to land on `main`.
You wake up to validated, landed commits — or to a precise record of what blocked.

```mermaid
flowchart LR
  op["Operator (browser)"] -->|"HTTP :8800"| mgr["Manager\nqueues · state · git authority"]
  w1["Worker A\nbackend + capabilities"] -->|"poll / submit"| mgr
  w2["Worker B"] -->|"poll / submit"| mgr
  mgr -->|"squash to main"| repo["canonical main"]
```

## Features

- **Pull-based routing** — a worker advertises its capabilities (queues, priorities, models, MCP connectors) on every poll, and the manager hands back the first runnable task that fits.
- **Manager-owned git authority** — workers only ever produce commits on isolated task branches in git worktrees; the manager is the sole writer to `main`, with conflict detection, a landing lock, and an optional push/PR remote policy.
- **Multiple backends** — Claude Code, Cursor, and Gemini CLI as agentic backends; the Anthropic API and Ollama (local or cloud) as single-shot backends; plus an in-house agentic harness.
- **Validation as the trust boundary** — every run must pass your validate command (default `just validate`, per-queue override) before it can land.
- **Operator UI** — live queue / run / worker views over SSE, per-model / backend / queue analytics, and in-browser settings editing.
- **Cross-machine workers** — a remote worker pushes its validated branch to a rendezvous remote; the manager fetches, verifies the SHA fail-closed, and lands.
- **Durable state** — Postgres-backed; falls back to an in-memory store for a zero-dependency try-out.

## Requirements

- Linux or macOS with `git`, [`uv`](https://docs.astral.sh/uv/), and [`just`](https://github.com/casey/just).
- Python 3.12+ (installed into `.venv` by `uv sync`).
- Optional: PostgreSQL and `psql` for durable manager state.
- The CLI or credential for at least one backend on each worker box (see [Backends](#backends)).

To provision a fresh Ubuntu VM (toolchain, optional local Postgres, optional Claude CLI), see [`provision.sh`](provision.sh).

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

Declare the worker's identity and capabilities in `.nightshift/worker.json` (scaffolded by `just init`).
Models are **provider-qualified** (`provider/model`); the provider prefix selects the backend per task:

```json
{
  "worker_id": "vm-1",
  "manager_url": "http://localhost:8800",
  "models": ["claude-code/claude-sonnet-4-6", "claude-code/claude-opus-4-8"],
  "mcps": []
}
```

```bash
just worker             # polls the manager; worker UI on :8810
```

The same settings can come from `NIGHTSHIFT_*` environment variables (env wins over `worker.json`); see the [Configuration Reference](docs/user/configuration-reference.md).

## Common operations

| Goal | Command |
|---|---|
| Install deps | `just install` |
| Scaffold workspace config | `just init` |
| Apply DB schema | `NIGHTSHIFT_PG_DSN=… just migrate` |
| Roll back DB schema | `NIGHTSHIFT_PG_DSN=… just rollback` |
| Start the manager | `just manager [port]` |
| Restart the manager | `just restart [port]` |
| Start a worker | `just worker [ui-port]` |
| Worker, no UI (loop only) | `just worker-headless` |
| Slack capture daemon | `just slackd` (needs `uv sync --extra slack`) |
| Run tests | `just test` |
| Lint + tests | `just validate` |
| End-to-end smoke (manager + worker) | `just smoke` (see `docs/topics/smoke-test.md`) |
| Kill orphaned nightshift processes | `just expunge` |

## Backends

A worker can serve several providers at once; each task's resolved `provider/model` id picks the backend.
Install only the tooling for the providers you advertise:

- `claude-code` — the `claude` CLI on `PATH` (agentic).
- `cursor` — the `cursor-agent` CLI on `PATH` (agentic).
- `gemini` — the `gemini` CLI on `PATH`, with an authenticated account or `GEMINI_API_KEY` (agentic).
- `anthropic` — `ANTHROPIC_API_KEY` set (single-shot API, no CLI).
- `ollama` — the `ollama` CLI on `PATH` or a reachable Ollama daemon (single-shot API).
- `ollama-cloud` — `OLLAMA_API_KEY` for cloud-hosted models on `ollama.com` (single-shot API).
- `nightshift` — the in-house agentic harness over the Anthropic or Ollama APIs (see [`docs/topics/agentic-harness.md`](docs/topics/agentic-harness.md)).

## Documentation

- [Setup Guide](docs/user/setup-guide.md) — bring-up from nothing on one box, then adding co-located and remote workers.
- [Configuration Reference](docs/user/configuration-reference.md) — every config file, key, and environment variable.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — component map, data flow, state model, git model, and design rationale.
- [`docs/topics/`](docs/topics/) — focused topics: the agentic harness, the smoke test, task state handling.
- [`docs/spec/`](docs/spec/) — design documents behind the larger subsystems.

## Defaults worth reviewing

A few shipped defaults assume things about your target repos; check them before the first real run:

- The default validate command is `just validate` and the default environment preflight is `uv sync --frozen`; override either per queue (or globally) if your target repo is not `just`/`uv`-based.
- The conflict resolver's auto-repair pass (`resolve_runner.attempt_repair`) runs `.venv/bin/ruff check --fix` and `ruff format` in the worktree; it honours the target repo's own ruff config, but assumes ruff (from a `.venv`) is present.
- `.nightshift/manager.json` ships `forbidden_paths` / `forbidden_template_paths` protecting `.github/workflows/`, `CLAUDE.md`, and `AGENTS.md`; edit them to match your repo.

## License

MIT — see [`LICENSE`](LICENSE).
