# Nightshift — Harness Turn Telemetry (arena-selected metric set)

**Subject:** Deep, owned instrumentation for the in-house agentic harness (`NightshiftAgentBackend`): what each turn spent, what the cache did, where the wall-clock went, and what the turns were doing — recorded per turn inside the existing `usage.per_turn` payload and rendered by the shared analytics module.
**Status:** Implemented. Where this doc and the code disagree, the code governs and this doc should be updated.
**Relationship:** Extends [measure-forward-analytics.md](measure-forward-analytics.md) (the no-harness measurement foundation) with the harness-only signals only an owned loop can capture. Same KPI: **cost per landed change** ≤ $1.50 at land rate ≥ 70%.
**Primary sources:** `src/nightshift/agent/loop.py` (`LoopResult`, `run_loop`), `src/nightshift/agent/tools.py` (`ToolResult.truncated`), `src/nightshift/backends.py` (`NightshiftAgentBackend.run`), `src/nightshift/assets/ui/analytics.js` (`harnessStats`, `renderRunShape`, `renderTurnComposition`).

---

## 0. The one idea

The CLI backends are closed boxes: we measure what they print.
The harness is ours: every completion, dispatch, and byte of transcript passes through code we control, so we can measure anything — which makes *restraint* the design problem.
The metric set below was selected by an arena (three independent candidate designs, cross-judged, best ideas grafted): each metric survives only by naming the concrete tuning decision it informs, and the kill list is part of the record (§6).

Hard constraints, all held: no new API calls, no streaming rework, instrumentation never alters the messages sent to the vendor (cache-prefix invariant 7 untouched), per-turn payload stays ~250 bytes typical, missing data is null/absent (never a guessed zero), old records still render, aggregation stays client-side in the shared vanilla-JS module.

## 1. Per-turn record

`LoopResult.per_turn_usage` (folded into the run record as `usage.per_turn`) extends the pre-existing `{turn, usage, tool_calls}` shape:

```jsonc
{
  "turn": 7,
  "usage": { /* raw completion usage, VERBATIM — cache splits survive per turn */ },
  "stop": "tool_use",          // vendor stop_reason as-is (null if omitted)
  "ms_model": 4820,            // wall-clock ms around the one transport_complete call
  "ms_tools": 1310,            // summed per-call ms (present when tools ran)
  "transcript_chars": 41850,   // serialized chars of post-brief conversation sent this turn
  "tool_calls": [
    { "name": "read_file", "result_chars": 8123, "ms": 190,
      "err": true,             // present ONLY when the call errored
      "trunc": true }          // present ONLY when clipped at the output cap
  ]
}
```

Run-level companions ride in the same `usage` payload (jsonb / JSONL — no schema migration):

- `exit_reason` — `completed` | `max_turns` | `timeout` | `aborted` | `transport_error`, recorded explicitly so consumers never re-parse the free-text `error`.
- `prompt_chars` — `{system, brief}` sized **once**: the system prefix (charter + tool specs) and the brief are byte-stable for the whole run, so per-turn copies would be pure duplication; `transcript_chars` per turn covers the only region that grows.

`transcript_chars` is tracked incrementally (only newly-appended messages are serialized), so instrumentation cost is O(run), and it includes `tool_use` args — the model's own output pushed back into context.

**Errored loops keep their telemetry.**
Previously a `max_turns`/transport-failure run returned only the error, dropping usage and per-turn detail — hiding exactly the expensive tail the KPI must see.
The backend now attaches full telemetry (tokens, per-turn, `exit_reason`, price-table `cost_usd`) to the honest failure result.

## 2. What each metric decides (survival justifications)

Cache (from the raw per-turn splits — localizes what the run-level hit rate can't):

- **Uncached input share by turn** — a rising late-run share means the rolling breakpoint fell out of range; tune `cache_breakpoints`/TTL.
- **Warm turn** (first turn with a cache read) — warming later than turn 2 means the first-turn breakpoint wastes cold prefill on every run.
- **Write tax** (creation share of throughput) — high churn means breakpoint placement or TTL is forcing re-writes.

Time (the lever cost-only telemetry is blind to):

- **Model vs tools wall-clock split** — decides whether the time lever is routing/effort (model-bound) or tool timeouts/pagination (tool-bound).
- **p50 latency by tool** — names the slow tool (`run_bash` timeouts vs `grep` scope vs file IO).
- **Median model ms by turn** — latency growing with context strengthens the eviction case: transcript costs wall-clock, not just dollars.

Tokens (estimated attribution, paired with exact companions):

- **Median input added by turn** (est., existing delta method) — the context-growth curve; its shape says whether eviction/admission caps are needed and from which turn.
- **Tokens added per tool** (est.) next to **result chars per tool** (exact) — which tool inflates the transcript, with ground truth to sanity-check the estimate.

Turn composition and churn:

- **Turns doing what** (edit / bash / search / read / text, edit-first precedence) — hunting-heavy runs point at repo-map/admission; text-heavy runs point at the charter.
- **Tool error rate** (per tool) — a tool the model keeps failing to call is a schema/affordance problem burning retry turns.
- **Truncation rate** (per tool) — tunes the 20K-char output cap: clipping constantly (starving the model) vs never (cap idle).
- **Exit reasons weighted by spend** — "we cut it off" exits carrying a large spend share are the stop-loss signal (R1).
- **Marginal turn yield** — of runs reaching each turn bucket, land rate and median tokens burned in the bucket; where extra turns stop buying lands is where the turn cap belongs.
- **max_tokens stops** — turns clipped mid-output say raise `max_tokens`.

## 3. UI surfacing

Everything is gated on `usage.per_turn` presence, so CLI runs and pre-instrumentation records render exactly as before.

- **Run shape (harness runs)** panel, extended: input-added-by-turn (existing), uncached-share-by-turn and cache caption (read % · write tax · warm turn), "where the time goes" stacked bar, and the per-tool table grown to Calls / Tokens added (est.) / Result chars / p50 ms / Err % / Trunc %.
- **Turn composition & exits (harness runs)** panel, new: turns-doing-what stacked bar, exit-reason table with spend, marginal-turn-yield table, max_tokens warning line.

Both panels ship in the shared module, so the manager (fleet) and worker (local) surfaces get them identically.

## 4. Testing

- `tests/test_agent_loop.py` — per-turn stop/timing/transcript fields, err/trunc present-only-when-true, `prompt_chars` sized once, `exit_reason` for all five loop outcomes.
- `tests/test_agent_tools.py` — `ToolResult.truncated` set at the output cap, false otherwise.
- `tests/test_nightshift_agent_backend.py` — instrumented payload through the backend, and the error path keeping usage/per-turn/`exit_reason`/cost.
- `tests/ui/analytics_harness.test.mjs` — renders the real `analytics.js` against instrumented + legacy synthetic records: cache localization, time split, tool columns, composition, exits, marginal yield, and backward compatibility.

## 5. Synthesis record (arena)

Three candidates (Opus, GPT, Gemini model families) designed metric sets against the same grounding; a fourth-model cross-judge scored them 24/21/15 against a five-criterion rubric and agreed with the parent's pick.

- **Base: candidate 1** — strict minimal extension of the production `per_turn` shape, every metric pinned to a named tuning knob, tightest byte/honesty discipline.
- **Grafted from candidate 2:** marginal turn yield (its strongest idea — turn caps deserve outcome data, not just cost data); per-call truncation flag (the output cap is a real knob and `result_chars`-near-cap detection is fuzzy); cache write tax; prompt-region sizing — reshaped from its per-turn `prompt_chars` triple into run-once `{system, brief}` + per-turn `transcript_chars`, keeping candidate 1's byte-thrift while answering candidate 2's question (which prompt region grows).
- **Rejected grafts:** candidate 2's per-turn system/brief chars (run-level constants; duplication), `arg_chars` (transcript_chars already includes serialized args; per-call split adds bytes without naming a distinct knob), `latency_ms.turn` (derivable; a third timing figure to save one subtraction); candidate 3's `input_chars` (collinear with input tokens), shared-breakdown-table latency/error columns (those tables serve all backends; harness-only columns would render "—" for the CLI majority), action-vs-think ratio (subsumed by the turns-doing-what mix); candidate 1's KPI-header tool-error card (the KPI row stays the five fleet-wide KPIs; the signal lives in the harness panels).
- **Convergence:** all three candidates independently proposed per-turn stop reason, model wall-clock, per-tool wall-clock, and a tool error flag — that consensus core shipped as-is.
- **Dropouts:** none.
- **Verification:** full `ruff` + `pytest` suite green; both node UI tests green (see §4).

## 6. Kill list (metrics that lost the arena)

1. Tool input arguments — unbounded (a `write_file` payload), secret-leak risk; no knob needs the args, only name + sizes.
2. Full tool result text / transcript snapshots — duplicates the transcript at multiples of the budget; `result_chars` carries the decision signal.
3. Stored (rather than computed) token attribution — would launder an estimate into a stored fact; computed client-side, labeled estimated.
4. Per-turn cost USD — derivable from stored usage + the price table; storing it duplicates `price.py`.
5. Time-to-first-token / streaming percentiles — the transport is non-streaming by design; unmeasurable without the rework the constraints forbid.
6. Tokens-per-second throughput — vanity quotient; `ms_model` and token counts are each independently actionable.
7. Per-turn knob echo (temperature/effort/cache) — constant per run, already in `honoured`.
8. Transport retry counts — there is no retry loop in `transport.py`; the metric would always read zero.
9. Cache breakpoint positions — deterministic from loop policy; the split outcomes are the metric.
10. Sandbox-violation counter — already surfaces through `err` + the per-tool error rate; one signal, not two.
11. Message-count / whole-transcript re-serialization per turn — collinear with input tokens (and O(n²) to compute naively).
12. Semantic loop detection (turn similarity) — needs embedding calls; constraint violation.
13. Cache-creation event counts — the token amount is what bills; an event count adds nothing.
14. Per-worker/repo/task/tool heatmaps — dashboard-ware; no global harness knob reads off it.
