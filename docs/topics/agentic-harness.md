# The Agentic Harness

**Subject:** How Nightshift's **in-house agentic backend** (provider name `nightshift`) actually works as implemented — the turn loop, the fixed sandboxed tool set, the deterministic SEARCH/REPLACE applier, the vendor transport layer, the prompt-cache strategy, the per-turn telemetry it records, and how an operator turns it on and tunes it.
**Status:** Descriptive — documents the code **as implemented** in `src/nightshift/agent/`. The design rationale lives in `docs/spec/agentic-backend.md`; where prose and code disagree, the code governs and this doc should be updated.
**Primary sources:** `src/nightshift/agent/loop.py`, `src/nightshift/agent/tools.py`, `src/nightshift/agent/transport.py`, `src/nightshift/agent/apply.py`, `src/nightshift/backends.py` (`NightshiftAgentBackend`), `src/nightshift/config/worker.py` (`NightshiftBackendConfig`), `src/nightshift/assets/prompts/agent-charter.md`.

Related: `docs/spec/agentic-backend.md` (design), `docs/spec/measure-forward-analytics.md` (the metrics this harness feeds), `docs/spec/2026-06-26-provider-qualified-models.md` (the id scheme), `docs/topics/task-state-handling.md` (what happens to the outcome).

---

## 1. What it is, in one idea

Nightshift routes work by **provider-qualified model id** — `<provider>/<model>`, where the provider token is a backend name. The agentic CLI backends (`claude-code`, `cursor`, `antigravity`) shell out to a vendor CLI we don't control; the plain API backends (`anthropic`, `ollama`, `ollama-cloud`) stream a single completion and edit nothing.

The **`nightshift` provider** is a third thing: an **in-process agentic harness we own end to end**. It talks to the model APIs Nightshift already reaches (`anthropic`, `ollama-cloud`, `ollama`) over `httpx`, runs its **own tool loop**, and applies edits **deterministically in Python** (no apply-model round-trip). Its model id carries a *second* vendor segment:

```
nightshift/anthropic/claude-sonnet-4-6
        │        │            └─ upstream model
        │        └─ vendor (which API to call)
        └─ provider (which backend runs the loop)
```

`select_run_backend` strips the `nightshift/` prefix, so the backend receives the bare `anthropic/claude-sonnet-4-6` half; the loop and transport split *that* on the first `/` into vendor + upstream model.

Because the harness is ours, the request is ours: we choose what context goes up, how files are read, how many turns run, where the prompt cache breaks, and which vendor serves the run. The intended payoff (see the spec) is **speed, vendor independence, and an owned, measurable token budget** — the last of which is what `docs/spec/measure-forward-analytics.md` turns into a KPI.

---

## 2. The turn loop

`run_loop` (`agent/loop.py`) drives the model through turns until it stops asking for tools (success) or an abort/timeout/turn-limit/transport failure ends it.

### 2.1 The fixed render order

Each request is assembled in a **fixed** order: `tools → system(charter) → messages(brief, then turns)`. The **system block (tools + charter) is byte-stable across the whole run** — that stability is exactly what makes the prompt cache pay off (§5). The charter is loaded from a static asset with **no per-run interpolation** (a timestamp or task id in it would bust the cache prefix every run).

### 2.2 One turn

1. Check abort / deadline; place cache breakpoints (§5).
2. Call `transport_complete(messages, tools, knobs, model=…, system=…, timeout=remaining, should_abort=…)` — one **non-streaming** completion (§4). Time it (`ms_model`).
3. Record the turn: raw `usage` verbatim, `stop_reason`, timings, transcript size, and (after tools run) the per-tool records (§6).
4. **If the model asked for no tools** and didn't stop with `tool_use` → the run is **done** (`exit_reason="completed"`).
5. Otherwise append the assistant turn (text + `tool_use` blocks), **dispatch each tool in the sandbox** (§3), append a user turn of `tool_result` blocks, and loop.

`transport_complete` is **injected** (the real one is `transport.complete`; tests pass a fake), so the loop is network-free under test.

### 2.3 How a run ends — `exit_reason`

The loop records an explicit, machine-readable outcome so downstream consumers never re-parse free text:

| `exit_reason` | Meaning |
|---|---|
| `completed` | Model finished (no more tool calls) |
| `max_turns` | Hit the turn cap (`error` also set) |
| `timeout` | Deadline elapsed mid-run (`aborted="timeout"`) |
| `aborted` | Controller asked to stop (operator Stop) |
| `transport_error` | Upstream HTTP/transport failure — **honest failure, no edits claimed** |

A transport failure sets `error` and returns immediately with no partial state: the harness never pretends a failed call produced an edit.

---

## 3. The tool set (fixed, sandboxed, immutable)

`build_registry` (`agent/tools.py`) constructs an **immutable per-run** `ToolRegistry` from a fixed allow-list. The full set:

| Tool | Purpose | Notes |
|---|---|---|
| `read_file` | Read a file, line-numbered | `start`/`end` (1-based) read a span; `context_policy=spans` nudges away from whole-file dumps on large files |
| `list_dir` | List directory entries | directories end with `/` |
| `grep` | Search file contents | prefers `rg`, falls back to a pure-Python scan; `literal`/`glob` options |
| `edit_file` | Edit via SEARCH/REPLACE blocks | deterministic applier (§3.3); **atomic** |
| `write_file` | Create/overwrite a file | creates parent dirs |
| `run_bash` | Run a shell command in the sandbox root | gated by `should_abort`; time-boxed |

### 3.1 Sandbox

Every path argument is resolved through `_resolve_in_sandbox(root, rel)`, which **rejects absolute paths** and anything escaping `root` (`..` traversal, symlinks pointing outside). `root` is the worker's task worktree (`spec.cwd`). Tool-level failures (bad path, failed edit, unknown tool) come back as `is_error=True` **text** so the model can retry — they never crash the loop.

### 3.2 Immutability and byte-stability

The registry is built **once** and never mutated. Tools sit at cache-prefix position 0, so `specs()` is deterministically ordered (sorted names, `sort_keys=True` in `specs_json()`) — the serialized tool block is byte-stable across the run, a hard requirement for the cache prefix.

### 3.3 The deterministic SEARCH/REPLACE applier

`edit_file` uses `agent/apply.py` — aider-style blocks applied with **literal string matching**, no apply-model, no fuzzy matching:

```
<<<<<<< SEARCH
old text
=======
new text
>>>>>>> REPLACE
```

Rules: the SEARCH text must occur **exactly once** (zero → `zero_match`, many → `multi_match` — we refuse to guess); an **empty** SEARCH means "create" (the tool layer, not the applier, owns file creation — it never silently clobbers); blocks apply **sequentially** against an in-memory copy; **any failure aborts the whole batch** (all-or-none), leaving the original intact. This makes edits cheap and, crucially, *deterministic and regression-testable*.

### 3.4 Output caps (telemetry, not just safety)

Every tool result is clipped at `_MAX_OUTPUT_CHARS` (20 000) with a truncation note. A clipped result sets `ToolResult.truncated=True`, which the loop records as `"trunc": true` on that tool call — telemetry for tuning the cap, surfaced in the run-shape analytics.

---

## 4. The transport layer (vendors)

`transport.complete` (`agent/transport.py`) picks the upstream API from the **vendor** token and returns a uniform `Completion` (`text`, `tool_calls`, `usage`, `stop_reason`, `honoured`). It is **non-streaming throughout** — simpler `tool_use` accumulation, and `emit_log` gets the full assistant text per turn.

| Vendor | Endpoint | Notes |
|---|---|---|
| `anthropic` | `POST /v1/messages` (`stream:false`) | Carries the thinking knob (§4.1); `usage` returned verbatim so cache splits fold through downstream |
| `ollama-cloud` | `POST https://ollama.com/api/chat` | Bearer auth (`OLLAMA_API_KEY`); tools mapped to Ollama's `{type:function,…}` shape |
| `ollama` | `POST {OLLAMA_HOST or localhost:11434}/api/chat` | Local; cache/thinking are no-ops here |

`/api/chat` (not `/api/generate`) is used because only chat supports tools; the system block is passed as a `system` **message** rather than a top-level field, and Ollama's `prompt_eval_count`/`eval_count` are mapped into the Anthropic-shaped `usage` so token folding is uniform.

### 4.1 Thinking + `honoured`

The `effort` knob maps to request fields per model generation: **current** models take `thinking:{type:"adaptive"}` + `output_config:{effort}`; **legacy** ids (`claude-3-5`, `claude-3-7`, `claude-sonnet-3`, `claude-opus-3`) take the older `thinking:{type:"enabled", budget_tokens:N}`. `effort` of `off`/empty disables thinking. Every completion returns a `honoured` dict recording which knobs the vendor actually applied (Ollama drops cache/thinking) so the loop and tests can assert intent without inspecting the wire body.

---

## 5. Prompt-cache strategy

Cache breakpoints are placed **only for the `anthropic` vendor** (Ollama skips placement entirely). Two `cache_control` markers per request by default:

1. **Stable prefix** — a marker on the system block, caching `tools + charter` (both byte-stable).
2. **Rolling** — a marker on the last content block of the latest turn, so the cached lookback stays in range as the conversation grows.

The TTL is the `cache_ttl` knob (`5m` default, `1h` optional). Caching is a no-op when `enable_cache` is false or the vendor isn't Anthropic. **Cache hit rate** is the property this whole arrangement exists to optimize, and it's now a first-class analytics metric (`measure-forward-analytics.md`).

> **Note.** The cache only pays off when the stable prefix exceeds Anthropic's minimum cacheable size. A charter + tool-spec prefix that is too small is silently *not* cached — worth watching when the charter is trimmed.

---

## 6. Telemetry the loop records

The loop's `LoopResult` is telemetry-first. Beyond summed `usage` (input/output with cache splits folded by the caller) it records **one `per_turn_usage` record per turn**, which is what makes forward tuning possible with no extra API calls:

| Field | Meaning |
|---|---|
| `usage` | that turn's raw completion usage **verbatim** (pre-fold — cache splits survive per turn) |
| `stop` | the vendor's `stop_reason` as-is |
| `ms_model` | wall-clock ms around the one completion call |
| `ms_tools` | (when tools ran) sum of per-call `ms` |
| `transcript_chars` | serialized size of the accumulated post-brief conversation sent with this turn — the growing prompt region |
| `tool_calls` | per tool dispatched: `{name, result_chars, ms}` plus `err`/`trunc` only when true |

`prompt_chars` sizes the run-constant regions once (`system` = charter + tool specs, `brief`). The key trick: **turn N's input minus turn (N−1)'s output ≈ the tokens turn (N−1)'s tool calls added to the transcript**, split across those calls by `result_chars`. This "delta method" is what the analytics run-shape view uses to attribute context growth to specific tools — computed client-side from `usage.per_turn`, no extra calls.

`NightshiftAgentBackend.run` folds this into the `WorkerResult`: normalized token/cache fields, the raw `usage` payload (with `per_turn` attached), and — since the analytics work — a `cost_usd` computed from the owned price table (`price.py`) over the accumulated usage (`None` for unpriced vendors like Ollama).

---

## 7. Turning it on (operator configuration)

The harness is a worker-level backend, configured in the worker's `config.json.local` under a `nightshift` block (surfaced in the worker Settings UI under **"Nightshift harness"**). Defaults live in `NightshiftBackendConfig` (`config/worker.py`) and **must agree** with the loop's module constants.

| Key | Default | What it does |
|---|---|---|
| `enabled` | `false` | Master toggle. When on, the worker **rewrites** its CLI-agentic `auto`/`max` routing to `nightshift/<vendor>/<model>` (the Phase-8 routing seam). Needs the chosen vendor's API key. |
| `vendor` | `anthropic` | Which upstream API the harness drives (`anthropic` / `ollama-cloud` / `ollama`). |
| `model` | — | Upstream model id the harness uses when enabled. |
| `max_tokens` | `4096` | Max output tokens per completion. |
| `effort` | `off` | Extended-thinking effort (`off` disables thinking). |
| `enable_cache` | `true` | Place Anthropic cache breakpoints (Anthropic vendor only). |
| `cache_ttl` | `5m` | Cache breakpoint TTL (`5m` / `1h`). |
| `tools_enabled` | all | Allow-list subset of the tool set (empty = all). |
| `context_policy` | `spans` | `read_file` behavior — `spans` nudges the model to read line ranges instead of whole files; `whole_file` doesn't. |

### 7.1 The routing seam

When `nightshift.enabled` is true and a resolved model id is a CLI-agentic provider (`auto`/`max` → `cursor`/`claude-code`), `resolve_model` rewrites it to `nightshift/<vendor>/<model>`. Off (the default) it's a no-op — explicit ids and non-CLI ids pass through untouched. **If the chosen vendor's backend is unavailable** (no API key), the rewrite is skipped and the original id is preserved — the harness never silently breaks routing. `cursor` and `claude-code` stay registered as benchmarks regardless.

### 7.2 Availability

`NightshiftAgentBackend.available` is true if *any* supported vendor's credentials are present (`OLLAMA_API_KEY`, `ANTHROPIC_API_KEY`, or a local `ollama` binary). The precise per-vendor error (which vendor, which key) is raised at run time.

---

## 8. Tuning the harness

Every knob above is a tuning lever, and the effect of moving it is judged **forward** against production telemetry (the KPI is **cost per landed change**; see `docs/spec/measure-forward-analytics.md` §6 for the full loop). The harness-specific levers:

- **Model / vendor** (`vendor`, `model`) — the biggest cost lever. Compare `$/landed` across models in the analytics **By model** breakdown at equal land rate; the harness's whole promise (own the token budget, beat the CLI) is only verifiable once its runs report cost.
- **`effort`** — extended thinking trades output tokens (and latency) for solution quality. Watch avg tokens/task and land rate together; thinking that doesn't lift land rate is pure spend.
- **`enable_cache` / `cache_ttl`** — the cache-hit-rate lever. A low hit rate means the byte-stable prefix isn't landing on the cache (too small, or busted by an accidental per-run change). `1h` TTL helps long or bursty runs.
- **`context_policy`** — `spans` shrinks `read_file` results on large files; the analytics **run-shape** view shows per-tool token attribution, so if `read_file` dominates the input delta, `spans` (and the output cap) is the target.
- **`tools_enabled`** — removing an unused tool shrinks the byte-stable tool block (a touch more prefix efficiency) and narrows what the model can do; the run-shape view shows which tools actually earn their place.
- **`max_tokens`** — caps output per turn; too low truncates useful work, too high invites runaway generations. Read it against avg turns and `max_turns` exits.

Change **one knob at a time**, give it a window of real runs, then read the analytics **delta badges** (current vs prior equal window). The loop's `exit_reason` distribution (`completed` vs `max_turns`/`timeout`) is the fast health check: a rise in `max_turns` after a change usually means the change made tasks harder to finish, not cheaper.

---

## 9. Invariants

1. **The system prefix is byte-stable per run.** Tools + charter never change mid-run; the charter has no per-run interpolation. This is the precondition for the prompt cache.
2. **The tool set is fixed and immutable per run.** Built once from `tools_enabled`, sorted for deterministic serialization; never mutated.
3. **Edits are deterministic and atomic.** Literal SEARCH/REPLACE, exactly-once match, all-or-none batch; no apply-model, no fuzzy matching.
4. **The sandbox is absolute.** Every path resolves under the task worktree; absolute paths, `..` escapes, and outward symlinks are refused.
5. **Failure is honest.** A transport error returns with `error` set and **no claimed edits**; tool-level failures return as retryable `is_error` text, never crashes.
6. **Every run is fully instrumented.** Per-turn usage (verbatim), timings, transcript growth, and per-tool attribution are recorded so tuning is measured forward, not guessed.
