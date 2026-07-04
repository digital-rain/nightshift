# AGENTS.md

Guidance for AI coding agents in this repo.
Read by Cursor, Claude Code (`CLAUDE.md` → this file), and Gemini CLI (`GEMINI.md` → this file).
Keep this file small; it is loaded on every run.
Push detail into the linked docs below.

- Operator commands + runtime bring-up: [`README.md`](README.md) (`just --list`).
- System design, data flow, component map: [`ARCHITECTURE.md`](ARCHITECTURE.md).
- Markdown prose style: one sentence per line ([`.cursor/rules/sentence-per-line.mdc`](.cursor/rules/sentence-per-line.mdc)).

## Rules to follow every run

1. **Mandatory lifecycle: worktree → work → validate → squash-merge to `main`. ALWAYS. Both halves, every time.**
   These two are a single inseparable flow, not separate rules you can half-follow. "Work in a worktree" without "squash-merge to `main` when done" is a **failure**, not a partial success — it strands finished work on a branch the running system never sees, forcing the operator to re-verify on `main`, find nothing there, and ask you to merge. Do not create that cycle.
   This rule **overrides** any default agent behaviour of "only commit/merge when the operator explicitly asks." Landing on local `main` is the last step *of the task itself*. Never end your turn with finished, validated work sitting on a worktree branch — if you finished it, it is on `main`.
   **Why both halves are non-negotiable:** the operator's runtime (`just manager` / `just worker`) and the editable install serve the **primary checkout's `src/`**. Work that lives only in a worktree is *invisible* to the running system no matter how many times the operator restarts or hard-reloads.
   Do exactly this, start to finish:
   1. **Start in a worktree** — never edit the primary checkout directly:
      `git worktree add .worktrees/<branch> -b <branch>` (`.worktrees/` is gitignored). Do all work there.
   2. **Validate** in the worktree (`just validate`, or ruff + pytest) until clean. You are expected to fix any failures yourself.
   3. **Commit** on the worktree branch.
   4. **Squash-merge into local `main`** from the primary checkout: `git merge --squash <branch>` + `git commit`.
   5. **Tear down** — `git worktree remove .worktrees/<branch>` and `git branch -D <branch>`.
   6. **Tell the operator** to restart the manager/worker so the running runtime picks up your `src/` changes (you must not restart it yourself — see rule 3).
   **Never `git push`** to `main` or any remote — staging on local `main` is the boundary; the operator pushes when running locally.
   **The only exception** to landing on `main`: a validate failure that genuinely requires operator review (should be extremely rare). Then stop and say so **explicitly and loudly** — never silently leave work stranded on a branch.

2. **UI-first — the manager UI is the product surface.**
   The operator drives everything through the manager UI (`just manager`, default `:8800`); only a few `just` recipes (`migrate`, `manager`, `worker`, `validate`, …) are used from the shell.
   Any feature or capability you add MUST be surfaced in the UI, whether or not the request says so explicitly.
   If how it should be surfaced is unclear, stop and clarify — and recommend an approach.
   The operator UI is served by the manager (`src/nightshift/manager/app.py`): static assets live in `src/nightshift/assets/ui/` and talk only to the manager's HTTP API — no SQL and no third-party REST from the frontend JS.
   Refresh/polling cadence is config-driven (`cadences.refresh_ms` in `.nightshift/manager.json`), never hardcoded.

3. **Parallel lanes — the operator owns the runtime.**
   Several agents share one VM; worktrees isolate the source, but the runtime is global.
   The operator alone runs `just manager`, `just worker`, and `just stop` from the console.
   These are not worktree-scoped: `just stop` frees ports (`:8800`, `:8810`) globally, so an agent running it kills the operator's manager and workers.
   Never run them from an agent; point at the already-running `:8800` instead.
   `just migrate` is idempotent (tracked in `_meta.schema_migrations`): apply it when your change adds a migration to `src/nightshift/assets/migrations/`, but don't run two migrates at once — the operator's console run is the failsafe.
   `just validate` is parallel-safe (ruff + pytest, no live DB required).
   `.venv` is shared, so serialize dependency changes (`uv sync`).

4. **Don't violate the architectural invariants below without an explicit decision.**

## Architectural invariants

- **Manager is the sole git authority.** Only the manager writes to `main`; workers submit via the API (or push to the WIP ref namespace for cross-machine landing) and the manager squash-lands under its lock.
- **Pull-based routing.** Workers poll the manager and advertise capabilities; the manager never pushes work. Task-to-worker matching is entirely capability-driven (queues, models, MCPs).
- **One DSN, one schema.** Nightshift owns `NIGHTSHIFT_PG_DSN`; it never reuses another project's database connection. Migrations live in `src/nightshift/assets/migrations/` and are applied by `just migrate`.
- **Assets are package-relative.** Shipped UI, templates, prompts, and migrations resolve from the installed `nightshift` package (`src/nightshift/assets/`). Operator state (`.nightshift/*.json`, `.worktrees/`) resolves from the workspace root.
- **Workers are stateless between tasks.** A worker's only durable identity is its `worker_id` + `.nightshift/worker.json`; all task state lives in the manager's Postgres (or in-memory store).

## Working norms

- Default loop is Python + `just` + `uv`.
- Prefer focused diffs; there is no repo-wide LOC cap.
- Tests verify behaviour, not implementation; default to scoped `pytest` via `.venv` (`just test` / `just validate`).
  `tests/_workspace.py` (`build_workspace()`) is the canonical fixture builder for multi-repo workspace tests.
- After changes to `src/nightshift/manager/` or `src/nightshift/worker/`, the running process needs a restart — leave that to the operator (see rule 3).
  Static asset changes (`assets/ui/`) are served fresh on next browser reload (no build step).
- Adding a dependency: add it to `pyproject.toml` and run `uv sync` from a shared terminal (serialize — `.venv` is shared).
- Touching `src/nightshift/assets/migrations/`: write both `-- migrate:up` and `-- migrate:down` sections; test with `just migrate` + `just rollback`.
