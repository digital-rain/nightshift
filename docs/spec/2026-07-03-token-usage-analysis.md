# Token Usage — Run-History Analysis & Reduction Plan

**Subject:** Where the manager/local-worker pipeline actually spends model tokens, measured from the worker's own run history, and five prioritized changes to cut the spend.
**Status:** Analysis + proposal — the findings are measured (not estimated); the recommendations are not yet implemented. Where this doc and code disagree, the code governs.
**Dataset:** `~/workspaces/.nightshift-worker/runs.jsonl` — 114 runs on worker `vm-1` through 2026-07-03, with per-run `turns` / `input_tokens` / `output_tokens` / `cost_usd` telemetry captured by `backends.AgentStreamParser`.
**Primary sources:** `src/nightshift/backends.py` (`AgentStreamParser`, `_usage_tokens`, `_stream_subprocess`), `src/nightshift/engine.py` (`build_claude_argv`, `_attempt_repair`), `src/nightshift/worker/execute.py`, `src/nightshift/agent/loop.py`, `docs/spec/failure-retry-policy.md`, `docs/spec/agentic-backend.md` (§1.3), `docs/spec/faster-claude-code-vscode.md`.

---

## 0. The one idea

Token spend is a **run-shape problem, not a prompt-size problem.**

An agent backend re-processes its whole transcript every turn, so a run's cost scales with *turns × accumulated context* — superlinear in turns. The measured consequence: cost concentrates in a small tail of long and failed runs, while static prompt overhead (charter, template) is a rounding error. Every high-leverage fix below is a stop-loss on that tail; none of them is "make the prompt shorter."

This is the run-history proof of what `agentic-backend.md` §1.3 asserts from first principles, and the headless counterpart of `faster-claude-code-vscode.md` §3C ("context hygiene is the highest-leverage habit").

---

## 1. The dataset and its caveats

114 runs, all `claude-code` backend, three model slugs (`claude-opus-4-6` and `claude-code/claude-opus-4-6` are the same model across the provider-qualified-slug migration; `claude-sonnet-4-6` is distinct).

Caveats to keep in mind when reading the numbers:

- **`input_tokens` folds cache reads in.** `_usage_tokens` sums `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`, so the input figure measures *context throughput*, not billed-at-full-rate prefill. Cache reads bill at ~10%. `cost_usd` (reported by the CLI) is the trustworthy money column. See §4.
- The history spans the period *before* the failure-retry policy (`failure-retry-policy.md`) fully landed; 8 runs show `quarantined`. Attempt-count pathologies below partly predate that policy — but the policy does not close the gaps identified in §3.2.
- Sample size for some cells is small (e.g. 5 runs over 100 turns); treat exact percentages as indicative, the ordering as robust.

To regenerate the stats:

```bash
python3 - <<'EOF'
import json
from collections import defaultdict
rows = [json.loads(l) for l in open(
    '/home/claude/workspaces/.nightshift-worker/runs.jsonl') if l.strip()]
tot_in = sum(r.get('input_tokens') or 0 for r in rows)
tot_cost = sum(r.get('cost_usd') or 0 for r in rows)
landed = sum(1 for r in rows if r.get('landed'))
print(f"runs={len(rows)} landed={landed} input={tot_in/1e6:.1f}M cost=${tot_cost:.2f}"
      f" -> ${tot_cost/landed:.2f}/landed")
EOF
```

---

## 2. Findings

### 2.1 Headline totals

| Metric | Value |
|---|---|
| Runs | 114 (92 `completed`, 22 `error`; 77 landed) |
| Input tokens (incl. cache reads) | 233.1M |
| Output tokens | 1.3M |
| Input : output ratio | **177 : 1** |
| Total cost | $172.45 |
| **Cost per landed change** | **$2.24** |

The 177:1 ratio is the whole story in one number: virtually all spend is re-reading accumulated context, not generating code. Levers that shrink or bound the context window dominate; levers that shrink output are noise.

### 2.2 Waste concentrates in runs that never land

| Bucket | Runs | Input tokens | Cost |
|---|---|---|---|
| Non-landed runs | 37 / 114 | 121.3M (**52%**) | $82.82 |
| Multi-attempt tasks that *never* landed | 10 tasks | 79.5M (34%) | $54.02 |
| `validation_error` failures | 18 | 29.5M | $24.65 |

Worst single task: `longitude/worker-models-in-wrong-settings-category` — **6 attempts, 328 turns, 21.9M tokens, $11.40, landed nothing.** Every retry was the same model, same prompt, same outcome.

### 2.3 Cost per turn grows superlinearly while success collapses

| Turns | Runs | Land rate | Avg input tokens **per turn** |
|---|---|---|---|
| ≤ 20 | 41 | 78% | 27.9K |
| 21–50 | 54 | 67% | 43.7K |
| 51–100 | 14 | 57% | 69.7K |
| > 100 | 5 | **20%** | **87.4K** |

Long runs pay 3× more per turn *and* fail 4× more often. The five runs over 100 turns consumed 71.3M tokens — **31% of all spend** — and landed once. Marginal tokens past turn ~60 buy almost nothing. Nothing bounds this today: `engine.build_claude_argv` passes `--max-turns` only when frontmatter sets it, so the default claude-code run is unbounded.

### 2.4 The expensive model underperforms on this workload

| Model | Runs | Land rate | Cost | **$ / landed change** |
|---|---|---|---|---|
| `claude-sonnet-4-6` | 44 | **80%** | $46.03 | **$1.32** |
| `claude-opus-4-6` (both slugs) | 70 | 60% | $126.43 | $3.01 |

Opus costs 2.3× more per unit of shipped work and lands less often. (Some routing bias is possible — harder tasks may have been given Opus — but Opus was the *default* for most of this window, so the bias is limited.) There is no evidence in this history that Opus-as-default earns its rate.

### 2.5 Validation failures burn whole sessions

18 runs (16%) ran a full agent session and were then rejected by the runner's validate gate — $24.65 spent producing work that failed lint/type/tests. The deterministic zero-token repair path (`engine._attempt_repair`: `ruff check --fix` + `format` + one revalidate) exists but only runs in the legacy local-runner path, not in the worker's (`worker/execute.py`).

---

## 3. Recommendations, in priority order

### 3.1 R1 — Turn cap + token stop-loss on the claude-code backend *(do this first)*

**Evidence:** §2.3 — 31% of all tokens went to 5 unbounded runs with a 20% land rate.

**Change:**
- Default `max_turns` to **50** when frontmatter/config leave it unset, matching the in-house loop's `DEFAULT_MAX_TURNS = 50` (`agent/loop.py`). Frontmatter `turns:` still overrides per task.
- Add a per-run **token budget** (e.g. `max_task_tokens`, default ~5M) enforced in `backends._stream_subprocess`: `AgentStreamParser` already sees live per-event usage, so the existing watcher thread can kill the process group when cumulative input tokens cross the budget — exactly the mechanism `model_timeout_seconds` uses for wall-clock. Report `failure_kind="token_budget"` so the failure-retry policy treats it as a normal failure.

**Expected effect:** caps the tail that produced ~$30+ of near-zero-yield spend in this window; a few dozen lines, all in code we own.

### 3.2 R2 — Retries must change something

**Evidence:** §2.2 — 10 never-landed multi-attempt tasks burned $54; the worst task was attempted 6 times identically.

The failure-retry policy (`failure-retry-policy.md`) already bounds *when* retries happen (drain-first, two-strikes pause, retry-fail → quarantine). What it does not require is that a retry **differ from the failed attempt**: today a Phase B retry re-dispatches the identical model + prompt, and the recorded `failure_reason` is never fed back.

**Change:** on Phase B dispatch of a task marked `failed: true`, the manager applies one of, in order of preference:
1. **Model demotion/switch** — resolve to a different model than the failed attempt (cheaper first; see R3).
2. **Auto-split** — if the failed attempt hit the turn/token cap or the diff cap, dispatch with `split: true` (decomposition instead of re-implementation).
3. **Context injection** — include the prior `failure_reason` (e.g. the validate tail already captured on the outcome) in the work order so the retry starts from the diagnosis instead of rediscovering it.

Quarantine after the second failure stays as-is — the point is that the one retry the policy allows must not be a coin re-flip.

### 3.3 R3 — Sonnet-first routing; Opus becomes opt-in

**Evidence:** §2.4 — $1.32 vs $3.01 per landed change, 80% vs 60% land rate.

**Change:** resolve `auto` to `claude-code/claude-sonnet-4-6` in worker/manager model resolution; require explicit frontmatter `model:` for Opus. Longer term this becomes the tiered routing named in `agentic-backend.md` (haiku-class models for search/classification sub-work), expressed through the existing provider-qualified slugs — no new router.

### 3.4 R4 — Bound context growth inside a run

**Evidence:** §2.3 — tokens/turn triples over a run's life because the transcript accretes full file reads and full validate output, then re-pays for them every turn.

**Change, cheapest first:**
- **Prompt rules** in `nightshift-local.md`: read spans not whole files; run tests quiet (`pytest -q`); when validate fails, feed back only the failure tail — the engine itself already trims to `stdout[-1500:]` for its own gate (`worker/execute.py`); the agent should be told to do the same in-loop.
- **In-house loop** (`agent/loop.py`): truncate or evict `tool_result` blocks older than N turns. The rolling cache breakpoint already marks the boundary; old tool output is the bulk of a long transcript and is rarely re-read.
- **Long term:** the span-index retrieval already named as the largest token-churn reduction in `agentic-backend.md` (Phase 8+).

### 3.5 R5 — Deterministic repair before any model re-entry

**Evidence:** §2.5 — 16% of runs burned full sessions then failed the final gate; `_attempt_repair` costs zero model tokens.

**Change:** wire `_attempt_repair` (ruff autofix + format + one revalidate) into `worker/execute.py` between the failed validate and the `validation_error` outcome. A lint-only failure lands without another model invocation; a real failure proceeds to the failure-retry policy with a cleaner `failure_reason`. Pair with R2's context injection so any model-driven retry starts from the surviving diagnosis.

---

## 4. Telemetry gap: cache visibility — **closed**

`_usage_tokens` folds `cache_creation_input_tokens` and `cache_read_input_tokens` into `input_tokens` before the split reaches the run record, so historically the history could not distinguish cold prefill (full price) from cache reads (~10%). Two consequences:

- The 233M input figure overstates billed tokens by an unknown factor; `cost_usd` is currently the only honest money column (and Gemini/Ollama runs report no cost at all).
- We could not measure the **cache hit rate** — the exact property the in-house loop's byte-stable charter + breakpoints (`agent/loop.py`, spec invariant 7) exist to optimize, and the metric the `agentic-backend.md` §Token-budget test pins.

**Implemented** (see the token-usage-granularity plan): `attempts` now carries `cache_read_input_tokens`/`cache_creation_input_tokens` (the unfolded splits, subsets of `input_tokens`) alongside a raw `usage` jsonb payload (per-backend vendor-shaped detail, including the harness's per-turn breakdown for input attribution). The stats views sum the cache totals; the Stats UI shows a cache-hit-rate figure next to the Tokens card, and a run's detail pane shows the cached split plus a "Token breakdown" expando for harness runs. Track **cost per landed change** (currently $2.24) as the single KPI these recommendations must move; cache hit rate is now measurable as a supporting metric.

---

## 5. What *not* to prioritize

Prompt-size trimming. The full `NIGHTSHIFT.md` charter re-read that every task performs is ~2.5K tokens against a ~2.0M-token average run — about **0.1%** of spend. Inlining a trimmed charter into the template remains nice-to-have hygiene (and the in-house agent already ships its own lean `agent-charter.md`), but no static-prompt change moves the bill. The bill is run shape: §2.2 and §2.3.

---

## 6. Suggested order of work

| Step | Change | Touches | Effort |
|---|---|---|---|
| 1 | R1 turn cap default + token stop-loss | `backends.py`, `config/worker.py`, `engine.build_claude_argv` | S |
| 2 | R3 sonnet-first `auto` | `config/worker.py` / `config/manager.py` resolution | S |
| 3 | R5 deterministic repair in worker path | `worker/execute.py` | S |
| 4 | §4 persist cache splits — **done** | `backends.py`, run record, manager store | S–M |
| 5 | R2 differentiated retries | `manager/app.py` (Phase B dispatch), `failure_policy.py` | M |
| 6 | R4 prompt rules + tool-result eviction | `assets/prompts/`, `agent/loop.py` | M |

Re-run the §1 script after steps 1–3 have a week of history; success is cost per landed change moving from **$2.24** toward **≤ $1.50** with land rate held ≥ 70%.
