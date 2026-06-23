# Nightshift — In-Process Agentic Backend Specification

**Subject:** Turn the existing single-shot `anthropic` backend into a true **agentic** backend that runs an in-process tool loop, edits files in the task worktree through a **deterministic Python SEARCH/REPLACE applier** (no apply-model round-trip), and — in a later phase — reads code spans through a nightshift-owned retrieval index.
The goal is **latency**: a backend that is noticeably faster than vanilla `claude-code` while still able to drive any Anthropic model.
**Status:** Proposed — design for unimplemented work.
This supersedes the "single-shot completion, NOT agentic" framing of the `anthropic` backend.
Where this doc and the code disagree once implemented, the code governs and this doc should be updated.
**Primary sources (to change):** `src/nightshift/backends.py` (the backend registry + `AnthropicBackend`), a new `src/nightshift/agent/` subpackage (`loop.py`, `tools.py`, `apply.py`), `src/nightshift/engine.py` (worktree/squash primitives, reused unchanged), `src/nightshift/worker/execute.py` (backend dispatch, reused unchanged), and — Phase 1 only — `src/nightshift/assets/migrations/` + a new retrieval module.

---

## 0. The one idea

Nightshift already has agentic backends (`claude-code`, `cursor`, `gemini`) that shell out to a vendor CLI, and two non-agentic API backends (`anthropic`, `ollama`) that stream a single completion and edit nothing.
This spec adds a third kind: an **in-process agentic backend** that owns its own tool loop, talks to the Anthropic Messages API directly (the way `AnthropicBackend` already does, over `httpx` — no new dependency), and applies edits deterministically in Python.

The point is not capability — `claude-code` already reaches any Anthropic model.
The point is **speed**.
By making the frontier model emit precise SEARCH/REPLACE blocks that nightshift applies with zero model round-trip, we get most of the "write less + apply cheaply" benefit that Cursor buys with a fast-apply model, without needing a fast-apply model at all.

This is fundamentally a **latency project, not a capability one.**
The honest baseline to beat is vanilla `claude-code`, not Cursor (see §8).

---

## 1. Where the speed actually comes from

Cursor's speed decomposes into three separable levers; only one is genuinely hard to replicate, and that one is **sidesteppable** for nightshift.

1. **Retrieval via an embedding index** — the model sees relevant spans, not whole files.
   This cuts prefill tokens and round-trips.
   Fully replicable; it is the bigger engineering lift (Phase 1, §5).
2. **Fast-apply model** — the frontier model emits a terse/lazy diff (`// ... existing code ...`) and a cheap fine-tuned model expands it into a full-file rewrite.
   The win is that the expensive model writes far fewer output tokens, and output tokens dominate latency.
   Anthropic exposes no fast-apply model, so this lever is **not directly replicable** — but it is **not needed.**
   If the harness makes the model emit precise SEARCH/REPLACE blocks that nightshift applies deterministically in Python, we get the "write less + apply cheaply" benefit with zero apply-model latency.
   This is what aider and Claude Code already do.
3. **Speculative edits + parallel tool calls + warm serving infra** — partially replicable (parallel tool calls, prompt caching), partially not (their serving latency).

The key insight: Cursor needs lever #2 because its main model emits lazy diffs.
Nightshift can decline to need it.
What we cannot beat is the frontier model emitting fewer tokens than a Claude model would for the same edit, plus Anthropic's raw serving latency.
Realistic landing zone: **noticeably faster than vanilla `claude-code`, somewhat slower than Cursor, with access to any Anthropic model.**

---

## 2. Review of the original proposal against the standalone repo

The original analysis was written when nightshift lived **inside** the `longitude` monorepo as `tools/nightshift/`, where `long_llm`, `long_intel_tools`, the `long_metrics_narrative` orchestrator loop, and pgvector were sibling code you could reuse "for free."

**That premise no longer holds.**
Nightshift is now an extracted, deliberately minimal standalone repo.
Its full dependency set is `fastapi`, `uvicorn`, `httpx`, `pydantic`, `asyncpg`, `python-dotenv` (`pyproject.toml`) — there is no `litellm`, no `long_llm`, no `long_intel_tools`, and no pgvector.
The docs state the separation explicitly: nightshift carries its own migrations and its own Postgres DSN and "never inherits longitude's `LONG_PG_DSN`" (`docs/configuration-reference.md`, `docs/setup-guide.md`).
Every `longitude` reference in the nightshift tree is either the example **target-repo name** (`repos.py`) or a note that the two are separate.

So the reuse map must be corrected:

| Component | Original claim | Reality in standalone nightshift |
|---|---|---|
| Tool-call agent loop (`long_metrics_narrative/orchestrator.py:123-166`) | "Have it, needs generalizing past 5 turns" | **In `longitude`, not a dependency.** Must be built in nightshift. It is a small loop (~40 lines), but it is written against `litellm`; nightshift should instead extend its existing `httpx` Anthropic path so no new dependency is added. |
| Direct Anthropic streaming (`AnthropicBackend`) | "Have it, needs tool support" | **Correct and in-repo.** `src/nightshift/backends.py` already streams the Messages API over `httpx`. This is the seam to upgrade. |
| Provider routing / budgets / cache (`long_llm`) | "Have it for free" | **In `longitude`, not a dependency.** Do not pull a sibling repo's library across the boundary. Nightshift already has its own capability-based model routing (`scheduled_models_allow`, `model_aliases`, `auto`/`max`) and its own cost/token rollups via `WorkerResult`. Use those. Prompt caching is an Anthropic **API request feature** (`cache_control`), independent of any gateway cache. |
| Worktree isolation / squash (`engine.py`) | "Have it" | **Correct and in-repo — the biggest real reuse.** `setup_worktree`, `squash_to_main`, the landing lock, and the validate/repair flow are all reused unchanged. |
| Tool seam (`long_intel_tools` registry/executor) | "Shows your established way to define tools" | **In `longitude`, a reference pattern only.** Nightshift needs its own small tool registry (§4). The `ToolDef`/executor shape is a fine model to copy by hand. |
| Embedding index / pgvector | "You have the substrate — pgvector already installed" | **Not in nightshift's database.** pgvector is installed in longitude's Postgres, not nightshift's. Nightshift's DB is separate and optional (in-memory store by default). Retrieval requires a real Postgres with the `vector` extension **and** a nightshift-owned migration + module. This is the genuine lift. |

Two honest caveats the monorepo framing obscured:

- **Capability is already solved.** `claude-code` and `cursor` are agentic backends that already reach any Anthropic / vendor model. This project's entire ROI is latency over `claude-code`. State that up front so the work is measured against the right baseline.
- **Adopt is cheaper here than in a monorepo.** Because nightshift's backend seam is "shell out to a binary in the worktree," adopting aider is just **one more subprocess backend** (`build_aider_argv`, mirroring `build_cursor_argv`), not a library port. That changes the build-vs-adopt calculus (§7).

---

## 3. The backend contract and integration seam

A nightshift backend is a class registered in `backends._BACKENDS` exposing:

- `name: str`, `agentic: bool`, `description: str`
- `available(config) -> bool`
- `run(spec: WorkerSpec, emit_log, should_abort, on_worker_start) -> WorkerResult`

`worker/execute.py` builds a `WorkerSpec(task, prompt, model, max_turns, cwd, env, config)` where **`cwd` is the task's git worktree** (created by `engine.setup_worktree`), calls `backend.run(...)`, then runs the queue's validate command in that worktree and squash-lands via `engine.squash_to_main`.
`WorkerResult` carries `returncode`, `aborted`, `error`, and best-effort telemetry (`turns`, `input_tokens`, `output_tokens`, `cost_usd`).

Implications for the new backend:

- It slots into `_BACKENDS` exactly like the others; **no engine or execute change is required** for Phase 0.
- It must set `agentic = True`.
  The engine's no-commit guard treats non-agentic runs as "no changes"; an agentic backend must actually write files in `spec.cwd` so the squash lands a commit.
- The loop bound is **`spec.max_turns`** (frontmatter `turns`, unlimited by default), not a hardcoded count.
  The original orchestrator's `for _ in range(5)` is the thing to generalize.
- Telemetry flows back through `WorkerResult` so the manager's existing per-worker/model/backend rollups keep working.
  The new backend reads `usage` from the Messages API stream the way `AnthropicBackend` already does, folding `cache_creation_input_tokens` / `cache_read_input_tokens` into input (the `_usage_tokens` helper already does this).

---

## 4. Phase 0 — Agentic backend (the cheap, in-repo win)

**Deliverable:** a new agentic backend (working name `anthropic-agent`) plus a small `src/nightshift/agent/` subpackage.
This alone yields "any Anthropic model, edits files, runs in a worktree, deterministic apply" — already better than Cursor on availability, and the proof point to measure latency against `claude-code` before investing in the index.

Keep it out of `backends.py` (already ~700 lines).
New focused modules:

- `src/nightshift/agent/tools.py` — the tool registry (definitions + JSON schema for the Messages API `tools` field) and the file-tool handlers.
- `src/nightshift/agent/apply.py` — the deterministic SEARCH/REPLACE applier.
- `src/nightshift/agent/loop.py` — the tool loop (request → `tool_use`/`tool_result` blocks → repeat until no tool calls or `max_turns`).
- A thin `AnthropicAgentBackend` in `backends.py` that constructs the loop with `spec.cwd` as the sandbox root and streams via the existing `httpx` path.

### 4.1 Tool set

All paths are resolved **relative to `spec.cwd`** and validated to stay inside it (reject `..` escapes and absolute paths), mirroring the path-traversal posture already used for repo slugs in `repos.py`.

- `read_file(path, start?, end?)` — return a line range (default whole file) with line numbers, so SEARCH blocks can be precise.
- `list_dir(path)` — names + types.
- `grep(pattern, path?, glob?)` — ripgrep-backed; literal vs regex flag.
- `edit_file(path, edits)` — apply one or more SEARCH/REPLACE blocks (§4.2).
- `write_file(path, contents)` — create/overwrite (for genuinely new files).
- `run_bash(command)` — run in `spec.cwd`, time-bounded, output truncated; honours `should_abort`.

The system prompt instructs the model to emit **exact SEARCH/REPLACE blocks** and to prefer `grep`/`read_file` of spans over reading whole files.
Reuse `engine.build_prompt` for the task brief framing so the brief is injected the same way every backend sees it.

### 4.2 Deterministic SEARCH/REPLACE applier (`apply.py`)

This is the latency win and is a few hundred lines, no model in the loop.

- Block format (aider-style):

  ```
  <<<<<<< SEARCH
  exact existing lines
  =======
  replacement lines
  >>>>>>> REPLACE
  ```

- Apply rule: the SEARCH text must match the file **exactly once**.
  Zero matches → return a structured error to the model (so it can re-read and retry); multiple matches → error asking for more surrounding context.
  Empty SEARCH against a non-existent path → file creation.
- Application is pure string surgery (`str.replace` on the unique span); no `difflib` fuzzy matching in the apply path, so results are reproducible.
  A failed block leaves the file untouched and reports which block failed.
- The applier is independently unit-testable with zero network (a pure function `apply_edits(original: str, blocks) -> str | ApplyError`), which is exactly the kind of seam nightshift's test suite favours.

### 4.3 Prompt caching (biggest free API win)

Set Anthropic `cache_control: {"type": "ephemeral"}` breakpoints on the stable prefix — the system prompt (charter + tool instructions) and any large, reused file context.
Cache reads are ~10% of input cost and skip prefill, which is the dominant latency on multi-turn tool loops.
The existing `_usage_tokens` helper already folds cache tokens into the input figure, so telemetry stays correct.

### 4.4 Abort, telemetry, failure

- Poll `should_abort()` between turns and inside `run_bash`, matching the existing backends' early-abort contract.
- Report `turns` = loop iterations, `input_tokens`/`output_tokens` summed across turns, `cost_usd` from the model's pricing (or left `None` and rolled up by the manager).
- On an unrecoverable model/API error, return `WorkerResult(returncode!=0, error=...)` so the engine records an honest failure rather than landing a partial commit.

---

## 5. Phase 1 — Retrieval index (the real lift)

**Deliverable:** a nightshift-owned semantic index so the agent reads spans, not whole files.
This closes most of the remaining latency gap with Cursor.
It is the part that takes real work and tuning, and it has an **infra precondition** the monorepo framing hid.

### 5.1 Infra precondition

pgvector is **not** present in nightshift's database today.
Phase 1 requires:

- a Postgres reachable via `NIGHTSHIFT_PG_DSN` (the in-memory store cannot back this), and
- the `vector` extension available on that host (`CREATE EXTENSION IF NOT EXISTS vector;`), added through a **new nightshift migration** under `src/nightshift/assets/migrations/`, not by borrowing longitude's Postgres.

If the index is unavailable, the agent must **degrade to Phase 0 behaviour** (grep + read spans), never hard-fail.

### 5.2 Components

- **Chunker** — split source files into bounded, overlapping spans keyed by `(repo, path, start_line, end_line)`; language-aware splitting is a future refinement, line/byte windows are the baseline.
- **Embedding pass** — embed chunks via a local Ollama embedding model (`bge-m3` / `nomic-embed-text`), reusing the same Ollama host the `ollama` backend already targets (`ollama_api_base`).
  Local embeddings keep this free and consistent with nightshift's existing local-model path.
- **`code_chunks` table** — `(repo, path, start_line, end_line, content, embedding vector(N), content_hash)` with a vector index; incremental re-embed keyed on `content_hash` so unchanged chunks are skipped.
- **`semantic_search(query, k, repo?)` tool** — added to the Phase 0 tool registry; returns ranked spans the model can then `read_file` precisely.
- **System-prompt bias** — instruct the model to `semantic_search` for relevant spans before reading whole files.

### 5.3 Indexing trigger

Index the target repo lazily on first agentic run against it (or via an explicit manager action), scoped per `repo` slug so the workspace's repos are indexed independently.
Indexing reads the repo's committed `HEAD`, not a worktree, so it is shared across tasks.

---

## 6. Phase 2 — Speed polish

- **Parallel tool execution** — when a turn returns multiple read-only `tool_use` blocks (read/grep/list/search), execute them concurrently and return all `tool_result`s together.
  Serialize writes.
- **Tiered models** — use a Haiku-class model for cheap sub-tasks (file selection, classification) while a frontier model does reasoning, expressed through nightshift's existing model routing rather than a new router.
- **Speculative reads** — prefetch files the model is likely to open next based on the current span set.

---

## 7. Build-vs-adopt

The decision belongs in **this** repo's docs, not longitude's `04-build-vs-adopt-analysis.md` (a different project).

- **Adopt (aider / OpenHands).**
  In nightshift this is unusually cheap: a subprocess backend `build_aider_argv` mirroring `build_cursor_argv`, reusing `_stream_subprocess` + `AgentStreamParser`.
  aider already implements SEARCH/REPLACE + a repo map and is tuned.
  This gets a fast, deterministic-apply agent with near-zero build cost and is the right **baseline-beating proof point** before any in-house loop is written.
- **Build (this spec).**
  Justified only if an in-process loop buys control the CLI cannot — tighter prompt caching, parallel tool calls, nightshift-native telemetry/abort, and a retrieval index keyed to nightshift's own DB.

Recommended order: ship the aider backend first (one file, measurable), then build Phase 0 only if its latency/telemetry advantages prove out against that baseline.
Phase 1 (retrieval) is worth building regardless, because it benefits the in-house loop and is not something the CLIs expose to nightshift's own store.

---

## 8. Honest positioning

- The competitor to beat is **vanilla `claude-code`**, already in `_BACKENDS`.
  Cursor is the aspirational ceiling, not the bar for "done."
- "Advanced models not on Cursor" is **already solved** by `claude-code` and `cursor`; this project adds no new capability there.
- Every phase must be justified by a **measured latency delta** against `claude-code` on the same task, captured through the existing run telemetry — not by assertion.

---

## 9. Testing

- **Applier (`apply.py`):** unit tests with no network — unique-match success, zero-match error, multi-match error, file creation, multi-block atomicity (a failed block leaves the file untouched).
- **Tools (`tools.py`):** path-traversal rejection (`..`, absolute paths) keeps every operation inside `spec.cwd`; `grep`/`read_file` span behaviour.
- **Loop (`loop.py`):** drive with a fake Anthropic transport (canned `tool_use` → `tool_result` → final text) so the loop is deterministic offline, mirroring the `use_fake` path the orchestrator already models; assert `max_turns` is honoured and telemetry is summed.
- **Backend integration:** a fake-backed `anthropic-agent` run inside a temporary worktree lands a squash commit (proving `agentic=True` + real file writes flow through `engine.squash_to_main`), and an API-error run records an honest failure with no commit.
- **Retrieval (Phase 1):** chunk/embed round-trip against a fake embedder; `semantic_search` ranking on a fixture corpus; graceful degradation to grep when the index is absent.
- **Fixtures:** extend the existing worktree/workspace fakes; add a fake embedder and a fake Messages transport.

---

## 10. Invariants

1. **The frontier model never applies edits.**
   It emits SEARCH/REPLACE blocks; a pure-Python applier patches files with no model round-trip.
2. **Edits are deterministic.**
   A SEARCH block matches exactly once or it is an error; no fuzzy apply, no silent partial writes.
3. **The agent is sandboxed to the worktree.**
   Every tool path resolves inside `spec.cwd`; `..` and absolute paths are rejected.
4. **No cross-repo library dependency.**
   Nightshift does not import `longitude` code (`long_llm`, `long_intel_tools`); it talks to Anthropic over `httpx` and keeps its minimal dependency set.
5. **Reuse the engine, don't fork it.**
   Worktree setup, validate/repair, the landing lock, and squash come from `engine.py` unchanged; the only new thing is what happens inside `backend.run`.
6. **Retrieval is additive and degradable.**
   Absent index → grep + read spans; the agent never hard-fails on a missing index, and the index lives in nightshift's own Postgres.
7. **This is a latency project.**
   Every phase is measured against `claude-code` through existing telemetry; capability parity is assumed, not a deliverable.

---

## 11. Out of scope / non-goals

- **No fast-apply model.**
  The SEARCH/REPLACE applier is the deliberate substitute (§1, §4.2).
- **No new heavy dependencies.**
  No `litellm`, no `long_llm` import; the Messages API is reached via the existing `httpx` path.
- **No borrowing longitude's database.**
  Phase 1 adds a nightshift-owned migration + extension, or it does not ship.
- **No replacement of existing backends.**
  `claude-code`, `cursor`, `gemini`, `anthropic`, `ollama` remain; this adds one (or, via §7, two) more.
- **No matching Cursor's serving latency or speculative-edit infra.**
  The target is "faster than `claude-code`," not parity with Cursor.

---

## 12. Open questions / future

- **Upgrade vs. add:** make `anthropic-agent` a new backend (keeping the single-shot `anthropic` latency baseline) versus upgrading `anthropic` in place.
  Adding is recommended so the single-shot baseline survives for measurement.
- **Language-aware chunking** and a repo-map summary layer (aider-style) as a retrieval refinement.
- **Embedding model choice and dimension** (`bge-m3` vs `nomic-embed-text`) and the resulting `vector(N)` width.
- **Index freshness:** lazy-on-run vs. a manager-driven background re-embed, and how stale a chunk may be before a read forces a re-embed.
- **Parallel-tool safety:** which tools are provably side-effect-free and therefore safe to run concurrently.
