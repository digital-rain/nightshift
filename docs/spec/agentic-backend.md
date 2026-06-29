# Nightshift — In-House Agentic Backend Specification

**Subject:** Add a **nightshift-owned agentic backend** — an in-process tool loop that drives any model API nightshift can already reach (`anthropic`, `ollama-cloud`, `ollama`), edits files in the task worktree through a **deterministic Python SEARCH/REPLACE applier** (no apply-model round-trip), and exposes every request knob (context assembly, thinking budget, `max_tokens`, prompt-cache breakpoints, retries, tool allow-list) as owned, version-controlled config.
The purpose is threefold: **speed** (faster than vanilla `claude-code`), **freedom** (vendor independence after Cursor's acquisition by X; first-class use of open models via `ollama-cloud`), and **cost control** (own the token budget instead of paying for a closed harness's drift and token churn).
**Status:** Proposed — design for unimplemented work.
This supersedes the "single-shot completion, NOT agentic" framing of the `anthropic` / `ollama` / `ollama-cloud` API backends, which the harness upgrades into agentic providers.
Where this doc and the code disagree once implemented, the code governs and this doc should be updated.
**Primary sources (to change):** `src/nightshift/backends.py` (backend registry, `WorkerSpec.timeout`, the API backends), a new `src/nightshift/agent/` subpackage (`loop.py`, `tools.py`, `apply.py`, `transport.py`), `src/nightshift/model_id.py` (provider-qualified ids, reused), `src/nightshift/engine.py` (`select_run_backend`, worktree/squash primitives, reused), `src/nightshift/worker/execute.py` (dispatch, reused), and — retrieval phase only — `src/nightshift/assets/migrations/`.

---

## 0. The one idea

Nightshift already routes work by **provider-qualified model id** (`<provider>/<model>`, where the provider token is exactly a backend name): `claude-code/claude-opus-4-8`, `cursor/gpt-5`, `ollama-cloud/qwen3-coder:480b`.
One worker can serve many providers at once; the provider half selects the backend that executes the task.
Today the agentic providers (`claude-code`, `cursor`, `gemini`) all **shell out to a vendor CLI we do not control**, and the API providers (`anthropic`, `ollama`, `ollama-cloud`) stream a single completion and edit nothing.

This spec adds one more provider — working name **`nightshift`** — that is an **in-process agentic harness we own end to end.**
It talks to the model APIs nightshift already reaches over `httpx`, runs its own tool loop, and applies edits deterministically in Python.
Because the harness is ours, the request is ours: we choose exactly what context goes up, how files are read, how many turns run, where the prompt cache breaks, and which model serves each sub-task — across **any** vendor we can call, not just the ones a closed CLI resells.

The intended outcome is to **replace the `cursor` backend** for nightshift's own runs: same agentic file-editing, but faster, vendor-independent, and with an owned (measurable, regression-testable) token budget.
`cursor` and `claude-code` stay registered as benchmarks.

---

## 1. Why own the harness (three drivers)

### 1.1 Speed

A closed agent emits lazy diffs and relies on a proprietary fast-apply model to expand them.
We sidestep that entirely (§2): the frontier model emits precise SEARCH/REPLACE blocks and we apply them in Python with zero model round-trip.
Combined with span-level reads and prompt caching, the realistic landing zone is **noticeably faster than vanilla `claude-code`, somewhat slower than Cursor**, with none of Cursor's serving-infra advantages but also none of its constraints.

### 1.2 Freedom — vendor independence and model selection

Cursor has been acquired by X.
Routing nightshift's overnight work through `cursor-agent` means a closed, third-party harness sits on the critical path, gating **which models we may use** and **which model features we may touch**, and subject to that vendor's roadmap and pricing.

Owning the harness restores two freedoms the provider-qualified architecture was already built to support:

- **Model selection.** The agentic loop is vendor-agnostic: it drives Anthropic models *and* the large open models now reachable through `ollama-cloud` (`qwen3-coder:480b`, `gpt-oss:120b`, `deepseek-v3.1:671b`) and local `ollama` — through the **same** tool protocol. `model_aliases` already absorbs vendor renames/sunsets; `auto`/`max` already resolve to per-worker defaults. The harness simply makes those models *agentic*, which a closed CLI only does for the vendors it chooses.
- **Model feature control.** A closed CLI hides the request. We cannot pin a thinking budget, `max_tokens`, cache breakpoints, the tool allow-list, or the retry policy — and we cannot see them. The in-house harness makes every one of these an explicit, owned knob (§5.4).

### 1.3 Cost — harness drift and token churn

A closed harness's token economics are not ours and they **drift**: across versions it may re-read whole files each turn, re-inject growing context, expand a hidden system prompt, or retry opaquely.
Every one of those inflates input tokens for output we did not ask for — **token churn we pay for but cannot inspect or cap.**

Owning the harness turns the token budget into an owned, testable property:

- **Deterministic context assembly** — send spans (grep / retrieval), not whole files; nothing is re-read unless the loop chose to.
- **Explicit cache breakpoints** — the stable prefix (charter + tool spec) is marked `cache_control` so it is charged at cache-read rates, not re-prefilled every turn.
- **Bounded, inspectable retries** — no hidden re-asks.
- **A pinned, version-controlled system prompt** — drift becomes a reviewed diff, not a silent vendor change.
- **Cheap-tier offload** — route mechanical sub-tasks (file selection, classification) to `ollama-cloud`/local at ~zero marginal cost, reserving the frontier model for reasoning.

Token usage per task becomes something we **assert in tests** (§10) and watch in the existing per-model/backend rollups — the lever this driver is about.

---

## 2. Where the speed actually comes from

Cursor's speed decomposes into three separable levers; only one is hard to replicate, and that one is **sidesteppable**.

1. **Retrieval via an embedding index** — the model sees relevant spans, not whole files; cuts prefill tokens and round-trips. Fully replicable; the bigger lift (retrieval phase, §6).
2. **Fast-apply model** — a cheap fine-tuned model expands the frontier model's lazy diff into a full-file rewrite, so the expensive model writes far fewer (latency-dominant) output tokens. Anthropic exposes no such model — but it is **not needed.** Precise SEARCH/REPLACE applied deterministically in Python gives the "write less + apply cheaply" benefit with zero apply-model latency. This is what aider and Claude Code already do.
3. **Speculative edits + parallel tool calls + warm serving infra** — partially replicable (parallel tool calls, prompt caching), partially not (serving latency).

The insight: Cursor needs lever #2 because its main model emits lazy diffs.
Nightshift declines to need it.
What we cannot beat is the frontier model emitting fewer tokens than a Claude model would for the same edit, plus the vendor's raw serving latency.

---

## 3. Review against the current standalone repo

The original analysis assumed the `longitude` monorepo, where `long_llm`, `long_intel_tools`, the `long_metrics_narrative` orchestrator loop, and pgvector were siblings to reuse "for free."
**That premise does not hold.**
Nightshift is a standalone repo whose full dependency set is `fastapi`, `uvicorn`, `httpx`, `pydantic`, `asyncpg`, `python-dotenv` — no `litellm`, no `long_llm`, no pgvector — and its DB is separate from longitude's (`docs/configuration-reference.md`).

Since that first review the repo has also moved decisively toward the freedom this spec wants:

- **Provider-qualified model ids** (`src/nightshift/model_id.py`): `split_model` / `provider_of` split on the first `/`; the model half may contain `/` and `:`. The single `backend` selector is being removed (`docs/plans/2026-06-26-provider-qualified-models.md`).
- **Multi-provider workers** (`config/worker.py`): `providers()`, `advertised_models()` (availability-gated), `resolve_model()` mapping `auto`/`max` to qualified defaults, and `model_aliases` to absorb renames.
- **`ollama-cloud` backend** (`backends.py`): hosted open models over the native Ollama API + Bearer `OLLAMA_API_KEY`.
- **`WorkerSpec.timeout`** + `select_run_backend(model, fallback)` (`engine.py:78`): per-run wall-clock bound and qualified-id dispatch.

Corrected reuse map:

| Component | Original claim | Reality now |
|---|---|---|
| Tool-call agent loop | "Have it (orchestrator)" | **In `longitude`, not a dependency, and `litellm`-based.** Build it in nightshift over the existing `httpx` API path — no new dependency. |
| Direct model streaming | "Have it (AnthropicBackend), needs tool support" | **Correct and in-repo.** `AnthropicBackend` and `_ollama_generate` already stream over `httpx`. These are the transports the harness wraps. |
| Provider routing / budgets / cache | "Have it for free (`long_llm`)" | **Don't import a sibling repo.** Nightshift already has its own routing (provider-qualified ids, `model_aliases`, `auto`/`max`) and per-model/backend token rollups via `WorkerResult`. Prompt caching is an Anthropic **API request feature** (`cache_control`), not a gateway. |
| Worktree isolation / squash | "Have it" | **Correct and in-repo — the biggest reuse.** `setup_worktree`, `squash_to_main`, the landing lock, validate/repair: reused unchanged. |
| Tool seam | "Have it (`long_intel_tools`)" | **In `longitude`, a reference pattern only.** Nightshift needs its own small tool registry (§5.2). |
| Embedding index / pgvector | "Substrate already installed" | **Not in nightshift's DB.** Requires a real Postgres with the `vector` extension + a nightshift-owned migration (§6). |
| Cursor as the thing to replace | (not in scope then) | **Now the explicit target.** Provider-qualified ids make the swap a config change: stop advertising `cursor/...`, advertise `nightshift/...`. |

---

## 4. The backend contract and integration seam

A backend is a class in `backends._BACKENDS` exposing `name`, `agentic`, `description`, `available(config)`, and `run(spec, emit_log, should_abort, on_worker_start) -> WorkerResult`.
`worker/execute.py` resolves the work-order model to a qualified id, uses the provider half to pick the backend (`select_run_backend` / `require_backend`), builds a `WorkerSpec(task, prompt, model, max_turns, cwd, env, config, timeout)` where **`cwd` is the task worktree** (`engine.setup_worktree`), calls `backend.run(...)`, runs the queue's validate command in that worktree, and squash-lands via `engine.squash_to_main`.

Implications for the new `nightshift` provider:

- It registers in `_BACKENDS` like the others; **no engine/execute change is required** for the core harness. `known_providers()` / `require_backend()` pick it up automatically.
- It sets **`agentic = True`** and actually writes files in `spec.cwd`, so the engine's no-commit guard lets the squash land a commit (unlike today's API backends, which finish as "no changes").
- The loop is bounded by **`spec.max_turns`** (frontmatter `turns`, unlimited by default) and **`spec.timeout`** (the per-worker wall-clock cap), not a hardcoded count.
- Telemetry flows back through `WorkerResult` (`turns`, `input_tokens`, `output_tokens`, `cost_usd`), folding Anthropic `cache_creation`/`cache_read` tokens into input via the existing `_usage_tokens` helper — so the manager's rollups and the token-budget assertions (§10) work unchanged.
- **Migration off Cursor is a config edit:** a worker drops `cursor/<model>` from its advertised `models` and adds `nightshift/<vendor>/<model>`; `auto_model`/`max_model` repoint. No scheduler or manager change.

---

## 5. The `nightshift` agentic harness

**Deliverable:** a new agentic provider `nightshift` plus a focused `src/nightshift/agent/` subpackage (kept out of the already-large `backends.py`):

- `agent/transport.py` — a thin vendor adapter over the existing `httpx` paths: Anthropic Messages API (`AnthropicBackend`'s streaming) and the Ollama native API (`_ollama_generate`), behind one `complete(messages, tools, knobs) -> (text, tool_calls, usage)` call.
- `agent/tools.py` — the tool registry (definitions + JSON schema) and file-tool handlers, all sandboxed to `spec.cwd`.
- `agent/apply.py` — the deterministic SEARCH/REPLACE applier.
- `agent/loop.py` — the tool loop (request → tool calls → tool results → repeat until no calls, `max_turns`, or `timeout`).
- a thin `NightshiftAgentBackend` in `backends.py` wiring the loop to `spec`.

### 5.1 One harness, many vendors

`nightshift` is a single agentic provider whose **model half names the upstream vendor and model**: `nightshift/anthropic/claude-sonnet-4-6`, `nightshift/ollama-cloud/qwen3-coder:480b`, `nightshift/ollama/llama3.1`.
Because `model_id.split_model` splits on the first `/` only, the model half (`anthropic/claude-sonnet-4-6`) is opaque to the scheduler and parsed solely inside the backend, exactly as `ollama/hf.co/user/repo` already is.
`agent/transport.py` reads the leading vendor token to pick the API; everything else in the loop is vendor-agnostic.
This is what delivers driver §1.2: any model nightshift can reach becomes agentic through one owned harness.
(Per-vendor agentic backends, e.g. `anthropic-agent`, are the alternative — see §13.)

### 5.2 Tool set

All paths resolve **relative to `spec.cwd`** and are validated to stay inside it (reject `..` and absolute paths), mirroring the slug guard in `repos.py`.

- `read_file(path, start?, end?)` — a line range (default whole file) with line numbers, so SEARCH blocks are precise.
- `list_dir(path)` — names + types.
- `grep(pattern, path?, glob?)` — ripgrep-backed; literal vs regex flag.
- `edit_file(path, edits)` — apply SEARCH/REPLACE blocks (§5.3).
- `write_file(path, contents)` — create/overwrite genuinely new files.
- `run_bash(command)` — run in `spec.cwd`, time-bounded by `spec.timeout`, output truncated, honours `should_abort`.

The brief is framed via `engine.build_prompt` so every backend injects it the same way.
The system prompt instructs the model to emit exact SEARCH/REPLACE blocks and to prefer `grep`/span reads over whole files (driver §1.3).

### 5.3 Deterministic SEARCH/REPLACE applier (`apply.py`)

The latency win, no model in the loop.
Aider-style blocks:

```
<<<<<<< SEARCH
exact existing lines
=======
replacement lines
>>>>>>> REPLACE
```

- SEARCH must match the file **exactly once**: zero matches → structured error (model re-reads and retries); multiple matches → error asking for more context; empty SEARCH on a missing path → file creation.
- Application is pure string surgery on the unique span — no fuzzy `difflib` in the apply path, so results are reproducible.
- A failed block leaves the file untouched and reports which block failed.
- `apply_edits(original: str, blocks) -> str | ApplyError` is a pure function, unit-testable with zero network — the seam nightshift's tests favour.

### 5.4 Owned request knobs (model feature control)

Every knob a closed CLI hides becomes explicit backend config (read from `worker.json`, overridable per queue), all defaulting to conservative values:

- `thinking_budget` (Anthropic extended thinking tokens), `max_tokens`, `temperature`.
- `cache_breakpoints` — where `cache_control: {"type": "ephemeral"}` is placed (default: after the charter + tool spec, and after large pinned file context).
- `max_turns` / `timeout` — already in `WorkerSpec`; the loop honours both.
- `max_retries` and which HTTP statuses retry (bounded, logged).
- `tools_enabled` — the allow-list of tools exposed this run (e.g. disable `run_bash` for a read-only queue).
- `context_policy` — `spans` (default) vs `whole_file`, so whole-file reads are an opt-in, not a silent default.

These map cleanly onto the Anthropic request body; for `ollama-cloud`/`ollama` the unsupported knobs are no-ops, and the harness records which were honoured.

### 5.5 Abort, telemetry, failure

Poll `should_abort()` between turns and inside `run_bash`; enforce `spec.timeout` as a wall-clock deadline.
Report `turns` (loop iterations), summed `input_tokens`/`output_tokens`, and `cost_usd` (or `None`, rolled up by the manager).
On an unrecoverable API error, return `WorkerResult(returncode!=0, error=...)` so the engine records an honest failure with no partial commit.

---

## 6. Retrieval index (the real lift)

A nightshift-owned semantic index so the agent reads spans, not whole files — the largest token-churn reduction (driver §1.3) and the remaining latency gap with Cursor.

### 6.1 Infra precondition

pgvector is **not** in nightshift's DB today.
This phase requires a Postgres reachable via `NIGHTSHIFT_PG_DSN` (the in-memory store cannot back it) and the `vector` extension on that host, added through a **new nightshift migration** under `src/nightshift/assets/migrations/`.
If the index is unavailable, the harness **degrades to grep + span reads**, never hard-fails.

### 6.2 Components

- **Chunker** — bounded, overlapping spans keyed by `(repo, path, start_line, end_line)`; line/byte windows first, language-aware later.
- **Embedding pass** — local `ollama` / `ollama-cloud` embedding models (`bge-m3`, `nomic-embed-text`), reusing the Ollama transport the backends already use; keeps embeddings cheap and on the same owned path.
- **`code_chunks` table** — `(repo, path, start_line, end_line, content, embedding vector(N), content_hash)` with a vector index; re-embed keyed on `content_hash` so unchanged chunks are skipped.
- **`semantic_search(query, k, repo?)` tool** — added to §5.2; returns ranked spans the model then reads precisely.
- **Prompt bias** — search for spans before reading whole files.

### 6.3 Indexing trigger

Index the target repo lazily on first `nightshift` run against it (or via a manager action), scoped per `repo` slug, reading committed `HEAD` (not a worktree) so it is shared across tasks.

---

## 7. Speed and cost polish

- **Parallel tool execution** — concurrent read-only calls (read/grep/list/search) returned together; writes serialized.
- **Tiered models** — a Haiku-class or `ollama-cloud` model for cheap sub-tasks (file selection, classification) while a frontier model reasons, expressed through the existing provider-qualified routing rather than a new router. Directly attacks cost (driver §1.3).
- **Speculative reads** — prefetch likely-next files from the current span set.

---

## 8. Build vs adopt

The new drivers tilt the decision toward **build**, and the venue is this repo, not longitude's `04-build-vs-adopt-analysis.md` (a different project).

- **Adopt (aider / OpenHands)** is still the cheapest *benchmark*: in nightshift it is just one more subprocess backend (`build_aider_argv`, mirroring `build_cursor_argv`), reusing `_stream_subprocess` + `AgentStreamParser`. Worth adding to measure against.
- **But adopting re-introduces a harness we do not control** — its own context policy, token economics, and version drift — which is exactly what drivers §1.2 and §1.3 are trying to escape. Feature control and an owned token budget are build arguments.

Recommended order: add the `aider` subprocess backend as a fast benchmark; build the `nightshift` harness (§5) and prove its latency **and token/cost** deltas against `cursor`, `claude-code`, and `aider`; then retire `cursor` from nightshift's advertised models.
The retrieval index (§6) is worth building regardless — the CLIs cannot expose it against nightshift's own store.

---

## 9. Honest positioning

- The thing being replaced is the **`cursor` backend**; the baselines to beat are `cursor` (latency), `claude-code` (latency + availability), and any adopted `aider` (token economics).
- Capability is **already solved** — `claude-code` and `cursor` already reach frontier models, and `ollama-cloud` already reaches large open ones. This project adds no model that routing cannot already select; it adds an **owned harness** around them.
- Every phase is justified by **measured deltas** in latency *and* tokens-per-task, captured through existing run telemetry — not by assertion.

---

## 10. Testing

- **Applier (`apply.py`):** unique-match success, zero-match error, multi-match error, file creation, multi-block atomicity (a failed block leaves the file untouched) — no network.
- **Tools (`tools.py`):** path-traversal rejection keeps every op inside `spec.cwd`; `grep`/span behaviour.
- **Transport (`transport.py`):** vendor token selects the right API (Anthropic vs Ollama); knob mapping onto the request body; unsupported knobs no-op for Ollama.
- **Loop (`loop.py`):** a fake transport (canned tool-call → tool-result → final text) makes the loop deterministic offline (mirroring the existing `use_fake` pattern); assert `max_turns` and `timeout` are honoured and telemetry is summed.
- **Token budget (the §1.3 lever):** with the fake transport recording every request, assert the stable prefix carries a cache breakpoint and that tokens-per-task for a fixture stay within a pinned budget — a regression test against harness drift.
- **Backend integration:** a fake-backed `nightshift` run in a temp worktree lands a squash commit (proving `agentic=True` + real writes flow through `engine.squash_to_main`); an API-error run records an honest failure with no commit; `select_run_backend("nightshift/anthropic/claude-sonnet-4-6", ...)` resolves to this backend with the model half intact.
- **Retrieval:** chunk/embed round-trip against a fake embedder; `semantic_search` ranking on a fixture corpus; graceful degradation to grep when the index is absent.

---

## 11. Invariants

1. **The frontier model never applies edits.** It emits SEARCH/REPLACE blocks; a pure-Python applier patches files with no model round-trip.
2. **Edits are deterministic.** A SEARCH block matches exactly once or it is an error; no fuzzy apply, no silent partial writes.
3. **The agent is sandboxed to the worktree.** Every tool path resolves inside `spec.cwd`; `..` and absolute paths are rejected.
4. **One harness, many vendors.** The loop is vendor-agnostic; the model half names the upstream API. Adding a vendor adds a transport adapter, not a new loop.
5. **The request is owned.** Context assembly, thinking budget, `max_tokens`, cache breakpoints, retries, and the tool allow-list are explicit, version-controlled config — never a hidden vendor default.
6. **The token budget is owned and tested.** Per-task token usage is a measured, regression-tested property; nothing is re-read or re-sent unless the loop chose to.
7. **No cross-repo or new heavy dependency.** Nightshift does not import `longitude` code and adds no `litellm`; it reaches model APIs over the existing `httpx` paths.
8. **Reuse the engine, don't fork it.** Worktree setup, validate/repair, the landing lock, and squash come from `engine.py` unchanged.
9. **Retrieval is additive and degradable.** Absent index → grep + span reads; the index lives in nightshift's own Postgres.

---

## 12. Out of scope / non-goals

- **No fast-apply model** — the deterministic applier is the deliberate substitute.
- **No new heavy dependencies** — no `litellm`, no `long_llm`; APIs reached via existing `httpx` paths.
- **No borrowing longitude's database** — the retrieval phase adds a nightshift-owned migration + extension, or it does not ship.
- **No removal of existing backends in this work** — `cursor` is retired from advertised models only after the harness wins on measured latency and cost; the backend stays registered as a benchmark.
- **No matching Cursor's serving latency or speculative-edit infra** — the target is "faster than `claude-code`, with an owned token budget," not parity with Cursor's infrastructure.

---

## 13. Open questions / future

- **One `nightshift` provider (vendor in the model half) vs. per-vendor agentic backends (`anthropic-agent`, `ollama-cloud-agent`).** §5.1 recommends the single provider for one owned loop across vendors; per-vendor backends are simpler to dispatch but multiply the harness. Decide before implementing.
- **Thinking-budget and cache semantics across vendors** — Anthropic exposes both; Ollama models do not. How the harness records "knob honoured vs ignored" per vendor.
- **Whether to upgrade the existing `anthropic`/`ollama`/`ollama-cloud` providers to agentic in place** (losing the single-shot latency baseline) or keep them single-shot and add `nightshift` alongside (recommended, preserves the baseline for measurement).
- **Embedding model + `vector(N)` width** (`bge-m3` vs `nomic-embed-text`); index freshness (lazy-on-run vs background re-embed); per-task staleness tolerance.
- **Parallel-tool safety** — which tools are provably side-effect-free and safe to run concurrently.
- **Token-budget enforcement** — assert-only in tests first; later, a soft per-task ceiling that aborts a runaway loop (paired with `max_turns`/`timeout`).
