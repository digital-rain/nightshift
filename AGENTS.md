# AGENTS.md

Guidance for AI coding agents in this repo.
Read by Cursor, Claude Code (`CLAUDE.md` → this file), and Gemini CLI (`GEMINI.md` → this file).
Keep this file small; it is loaded on every run.
Push detail into the linked docs below.

- Operator commands + runtime bring-up: [`README.md`](README.md) (`just --list`).
- System design, data flow, component map: [`ARCHITECTURE.md`](ARCHITECTURE.md).
- Markdown prose style: one sentence per line ([`.cursor/rules/sentence-per-line.mdc`](.cursor/rules/sentence-per-line.mdc)).

## Rules to follow every run

1. **Work in a worktree — always.**
   Never edit the primary checkout directly.
   Create one under `.worktrees/<branch>` (gitignored) and do all work there:
   `git worktree add .worktrees/<branch> -b <branch>`.

2. **Commit policy.**
   Preferred: squash and commit to local `main` when finished in the worktree.
   Allowed when multiple agents run concurrently: commit to your own branch and open a PR *from the worktree*.
   Never push to `main` directly, but stage your commits and allow the operator to do this when running locally.
   When finished with your worktree and changes have been committed, delete your worktree.

3. **UI-first — the manager UI is the product surface.**
   The operator drives everything through the manager UI (`just manager`, default `:8800`); only a few `just` recipes (`migrate`, `manager`, `worker`, `validate`, …) are used from the shell.
   Any feature or capability you add MUST be surfaced in the UI, whether or not the request says so explicitly.
   If how it should be surfaced is unclear, stop and clarify — and recommend an approach.
   The operator UI is served by the manager (`src/nightshift/manager/app.py`): static assets live in `src/nightshift/assets/ui/` and talk only to the manager's HTTP API — no SQL and no third-party REST from the frontend JS.
   Refresh/polling cadence is config-driven (`manager.cadences.refresh_ms` in `config.json`), never hardcoded.

4. **Parallel lanes — the operator owns the runtime.**
   Several agents share one VM; worktrees isolate the source, but the runtime is global.
   The operator alone runs `just manager`, `just worker`, and `just stop` from the console.
   These are not worktree-scoped: `just stop` frees ports (`:8800`, `:8810`) globally, so an agent running it kills the operator's manager and workers.
   Never run them from an agent; point at the already-running `:8800` instead.
   `just migrate` is idempotent (tracked in `_meta.schema_migrations`): apply it when your change adds a migration to `src/nightshift/assets/migrations/`, but don't run two migrates at once — the operator's console run is the failsafe.
   `just validate` is parallel-safe (ruff + pytest, no live DB required).
   `.venv` is shared, so serialize dependency changes (`uv sync`).

5. **Don't violate the architectural invariants below without an explicit decision.**

## Architectural invariants

- **Manager is the sole git authority.** Only the manager writes to `main`; workers submit via the API (or push to the WIP ref namespace for cross-machine landing) and the manager squash-lands under its lock.
- **Pull-based routing.** Workers poll the manager and advertise capabilities; the manager never pushes work. Task-to-worker matching is entirely capability-driven (queues, models, MCPs).
- **One DSN, one schema.** Nightshift owns `NIGHTSHIFT_PG_DSN`; it never reuses another project's database connection. Migrations live in `src/nightshift/assets/migrations/` and are applied by `just migrate`.
- **Assets are package-relative.** Shipped UI, templates, prompts, and migrations resolve from the installed `nightshift` package (`src/nightshift/assets/`). Operator state (`config.json`, `.nightshift/`, `.worktrees/`) resolves from the workspace root.
- **Workers are stateless between tasks.** A worker's only durable identity is its `worker_id` + `config.json.local`; all task state lives in the manager's Postgres (or in-memory store).

## Working norms

- Default loop is Python + `just` + `uv`.
- Prefer focused diffs; there is no repo-wide LOC cap.
- Tests verify behaviour, not implementation; default to scoped `pytest` via `.venv` (`just test` / `just validate`).
  `tests/_workspace.py` (`build_workspace()`) is the canonical fixture builder for multi-repo workspace tests.
- After changes to `src/nightshift/manager/` or `src/nightshift/worker/`, the running process needs a restart — leave that to the operator (see rule 4).
  Static asset changes (`assets/ui/`) are served fresh on next browser reload (no build step).
- Adding a dependency: add it to `pyproject.toml` and run `uv sync` from a shared terminal (serialize — `.venv` is shared).
- Touching `src/nightshift/assets/migrations/`: write both `-- migrate:up` and `-- migrate:down` sections; test with `just migrate` + `just rollback`.
