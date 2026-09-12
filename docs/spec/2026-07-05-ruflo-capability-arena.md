# Nightshift — Ruflo capability mining (arena-selected top 10)

**Subject:** Capabilities to import into nightshift from a review of [ruvnet/ruflo](https://github.com/ruvnet/ruflo) ("claude-flow"), selected by a five-candidate / one-judge arena against an operator-approved rubric.
**Status:** Selection record. The top 10 below is the prioritized backlog; nothing here is implemented yet.
**Relationship:** Extends the arena method used by [2026-07-04-harness-telemetry-metrics.md](2026-07-04-harness-telemetry-metrics.md) §5 (independent candidates, cross-judge, grafts, kill list in the record).
**Primary sources:** ruflo README/STATUS/USERGUIDE plus plugin docs (`ruflo-swarm`, `ruflo-loop-workers`, `ruflo-intelligence`, `ruflo-cost-tracker`, `ruflo-jujutsu`, `ruflo-testgen`, `ruflo-goals`, `ruflo-metaharness`, `ruflo-arena`, `ruflo-federation`), fetched 2026-07-05.

---

## 0. The one idea

Ruflo is an agent meta-harness with heavy marketing and a real substance core: swarm decomposition, background loop workers, outcome-learning model routing, budget governance, self-repair loops, readiness scoring, and diff risk analysis.
The arena's job was to mine that core for capabilities that survive nightshift's invariants and rubric — not to import ruflo's architecture.
Candidates were explicitly told to distrust README claims ("1953× faster", "89% routing accuracy") and to trace every card to a documented plugin capability.

## 1. Rubric (operator-approved)

Unit of evaluation: a **capability card** = name · ruflo evidence · nightshift build sketch · delivery route · measurement plan · UI home.

Delivery routes: **R** (rules only — prompt/charter/AGENTS.md), **U** (UI + manager/worker code), **W** (worker/harness code surfaced via existing screens).

Hard gates: **G1** invariant-safe (manager sole git authority, pull-based routing, one DSN, package-relative assets, stateless workers, config-driven cadences, measure-forward analytics), **G2** novel (not built; may improve an existing spec only by materially changing its design), **G3** substance (traceable to real ruflo capability, not marketing).

Scored criteria (0–5 each, weighted, max 80):

| # | Criterion | Weight | 5 means |
|---|---|---|---|
| C1 | Operator outcome | ×3 | Moves cost-per-landed-change, land rate, blocked/quarantine time, or operator toil |
| C2 | Autonomy yield | ×2 | More useful work lands unattended overnight |
| C3 | Large-job capability | ×2 | Large jobs/projects complete in less time (decomposition, parallelism, coordination) |
| C4 | Time to completion | ×2 | Reduces wall-clock from task start to landed change |
| C5 | Cost per unit time | ×1 | Better dollars per unit wall-clock |
| C6 | Measurability | ×2 | Verifiable with existing telemetry; measure-forward only |
| C7 | Build cost (inverse) | ×2 | Few subsystems touched; ≤1 additive migration; rules-only = 5 |
| C8 | UI surfaceability | ×1 | Obvious home in an existing screen |
| C9 | Risk (inverse) | ×1 | Small blast radius; degrades gracefully; removable |

Portfolio constraint: exactly 10 items; at most 1 rules-only; the other 9 are product changes (U/W) with a named UI surface.
Ties break toward lower build cost.

## 2. Arena mechanics

- Shared grounding pack: nightshift capability/gap inventory, invariants, evidence-gathered ruflo capability summary, rubric.
- Five independent candidates, five model families: Opus 4.8, Sonnet 4.6, GPT-5.5, GPT-5.3 Codex, Composer 2.5. Each produced a ranked 10-card deck with self-scores and a kill list, with read access to this repo.
- One judge (Fable 5, xhigh thinking) with repo access: de-duplicated cards across decks, enforced gates, re-scored every unified candidate from scratch (spot-checking candidates' codebase claims), enforced the portfolio constraint, and grafted the best fragments from losing cards into winners.

### Deck scores (judge's re-scores of each deck's 10 cards as proposed; gate-killed cards score 0)

| Deck | Re-score sum | Gate kills | Rank | Judge's assessment |
|---|---|---|---|---|
| opus-4.8 | 525 | 0 | 1 | Most honest scorer; sharp invariant reasoning; missed the existing `max_fix_attempts` knob |
| gpt-5.5 | 500 | 0 | 2 | Broadest seam knowledge; systematic +5–11 self-score inflation |
| gpt-5.3-codex | 462 | 1 | 3 | Best single codebase find (`max_fix_attempts` exists, unused); moderate inflation |
| sonnet-4.6 | 439 | 1 | 4 | Detailed and mostly honest; flagship goal-loop redesign broke the feature's semantics |
| composer-2.5 | 420 | 2 | 5 | Densest accurate code citations; worst judgment — bet slot #1 (69/80 self-score) on a gate-dead card |

## 3. Final top 10 (judge's scores; build sketches are composites with corrections)

### 1. Post-validate repair loop — W — 64/80

All five decks converged here; no single deck had the full correct build.
On `validation_error` in `worker/execute.py`: first run the deterministic `attempt_repair` pass that already exists on the resolve path (`resolve_runner.py` — ruff fix + format + recommit + revalidate), then at most one **backend-generic** repair prompt in the preserved worktree; bounds wired to the existing, currently-unconsumed `max_fix_attempts` config knob.
Tag outcomes with a new `failure_kind` value `validate_repaired` (enum addition, no migration); submit semantics unchanged.
UI: Now (`repair` phase pill) + History/Statistics via the existing `failure_kind` breakdown.
Measure: `validate_repaired / (validate_repaired + validation_error)` and land-rate delta from `stats_overall`.
Ruflo evidence: goals adaptive replanning; loop-workers iterative recovery.

### 2. Spend budget governor with alert ladder + hard stop — U — 59/80

`FieldSpec` budget fields (`budget_usd_daily`, ladder percents) auto-surfacing in Settings; reconciler-loop `sum(cost_usd)` check over a rolling window; ladder rungs (50/75/90/100%) as `append_event` → SSE; at 100% `set_queue_pause(reason="budget_cap_reached")` on active queues; `/api/budget` endpoint.
Add the pause-reason copy string in `app.js`; include a days-to-exhaustion projection and burn gauge.
No migration — rides `attempts.cost_usd` and the existing pause machinery.
UI: Settings + Statistics budget panel + Now banner (amber→red as rungs trip).
Measure: window spend vs cap; count of `budget_cap_reached` pause events.
Ruflo evidence: cost-tracker budget ladder + hard stop.

### 3. Autosplit-on-play — U — 59/80

Promote the tested CLI-only autosplit (`spawn_daily.py` `spawn_all`/`spawn_source`) into the manager play path, serialized on the existing git executor so content-store writes can't race; `queue_changed` SSE; reconciler pre-scan for stranded autosplit sources.
Correction adopted from the judge: do not conflate with split-task `harvest_split_output`, and do not auto-assign `after:` DAGs between spawned bullets (that needs decomposition intelligence autosplit doesn't have).
UI: Up Next (children appear) + Playlists (source badge, item count, last harvest timestamp).
Measure: subtasks spawned per play; spawned-child land rate vs manually authored; parent wall-clock-to-all-landed.
Ruflo evidence: loop-workers trigger dispatch; swarm decomposition.

### 4. Auto repo-task intake on cadence — U — 53/80

Sole-deck sleeper (gpt-5.3-codex) placed above several five-deck consensus cards.
Opt-in per-queue key; reconciler duty reusing `scan_repo_tasks`/`copy_repo_tasks`/`remove_repo_tasks_locked` under the repo executor; manual import modal kept as fallback.
The import spec already marks this "future work"; never-lose/dedupe semantics are built and tested, so the marginal design is a cadence and a config key.
UI: Repos screen (per-queue auto-drain toggle + last-drain status).
Measure: publish-to-queue latency; % of imports completed with zero operator clicks.
Ruflo evidence: loop-workers background dispatch on timers.

### 5. Outcome-learning `auto` model router — U — 52/80

Router module before `build_work_order`, only for `AGNOSTIC_MODELS` tasks; choose only among models live workers advertise (`WorkerFilter._model_ok` untouched — pull-based intact); derive win/loss posteriors directly from `attempts` (queue, model, state, cost) so no migration; one-line `routing_rationale` on the work order for operator audit.
Explicit task model pins always win.
UI: Settings (on/off, explore rate, threshold) + Statistics per-model table (exists).
Measure: cost-per-landed and land rate for `auto` tasks before/after, from `stats_by_model`; operator-override rate as the distrust signal.
Ruflo evidence: intelligence model-outcome feedback; Thompson-sampling router.
Judge's caution: C9=2 — feedback loops and cold starts can silently degrade routing.

### 6. Per-task-tree spend envelope — U — 51/80

Manager checks a task tree's summed `attempts` cost/tokens/turns **before granting the next lease** (never at submit time — that gates after the money is spent), blocking with a constant reason string mirroring `UNROUTABLE_REASON_PREFIXES`.
Envelope defaults (`max_iterations`, `max_tokens`, `max_usd`) via `FieldSpec`.
Bounds a single runaway program where card 2 bounds the fleet; the pair makes split trees and future loops safe to leave alone.
UI: Settings (defaults) + task detail (spend meter, blocked reason) + Workers screen per-worker spend column.
Measure: tasks stopped at envelope; spend-at-stop vs envelope.
Ruflo evidence: federation budget circuit breaker (maxHops/maxTokens/maxUsd).

### 7. Cost-anomaly (MAD) flags in the Waste panel — U — 50/80

Frontend-only: MAD band per model over the existing `fetchRuns` adapter; red rows in the existing Waste table for runs beyond k·MAD.
No backend, no migration, fully removable — the cheapest genuine capability in the arena.
UI: Statistics → Waste panel (exists).
Measure: anomaly count and summed `cost_usd` flagged per window.
Ruflo evidence: cost-tracker MAD-based per-session anomaly detection.
Shelved graft: a `cost_anomaly` SSE event if in-run alerting is later wanted.

### 8. Worker readiness scorecard — U — 50/80

Manager-side composite score per worker from checkins, advertised models/MCPs, recent `failure_kind` history, preflight failures, and stale heartbeats; recommendation hints placed next to the queue-dedication controls.
Advisory only — zero invariant pressure; the novel part is the composite + mismatch recommendations, not per-worker stats (comparison tables already exist).
UI: Workers screen (score column + hints near dedication).
Measure: readiness score vs subsequent failure rate; unroutable holds cleared after operator acts on a recommendation.
Ruflo evidence: metaharness readiness scorecard; federation behavioral trust scoring.

### 9. Cheap-first, scope-minimal effort charter — R — 49/80 (the single rules-only slot)

Edit the byte-stable `agent-charter.md` + `AGENTS.md`: mandate smallest-scoped landing change, stop-on-green, no speculative refactors.
The charter is interpolation-free, so the edit re-caches cleanly at the next run.
UI: none required; effect reads on Statistics (avg turns, $-per-landed).
Measure: `avg_turns` and cost-per-landed before/after (attribution noisy — scored honestly at C6=3).
Ruflo evidence: cost-tracker optimization strategies + 3-tier routing philosophy, distilled into agent guidance.

### 10. Counterfactual routing panel — U — 47/80

Reprice recorded tokens against fixed-model baselines via `price.py` in `api_operator.py` + an `analytics.js` panel; `/api/price_rates` endpoint so JS never hardcodes prices.
Honesty label required: "cost-only, current token shape" — a different model would have produced a different token shape, so this is not a true counterfactual.
Earns the last slot on measurability and near-zero risk, and because it's the instrument that tells the operator whether card 5's router is earning its keep.
UI: Statistics (model/cost section).
Measure: actual vs baseline $-per-landed per window.
Ruflo evidence: cost-tracker counterfactual analysis.

## 4. Kill list (unified candidates that lost)

| Candidate | Source decks | Ruling |
|---|---|---|
| Cross-run goal-loop engine | composer #1, sonnet #3, codex #5 | **G2 kill.** Pure implementation of the existing `2026-07-04-loop-tasks.md` spec (composer), or material changes that made it worse (sonnet: terminates on first land; codex: new DB table vs the spec's leaner frontmatter-canonical state). Implement the spec as roadmap work, not an arena card. |
| Goal-loop adaptive replan layer | gpt-5.5 #6 | Score kill (46). The only genuinely additive loop variant, but heavy manager-side brief authoring and depends on the unbuilt base spec. |
| Cache-warm heartbeat / dispatch cadence | composer #5, opus #4 | Composer: **G3 substance kill** — manager lease heartbeats never touch the Anthropic API; the mechanism is inert. Opus (corrected form): score kill at 47, lost the final-slot tiebreak. |
| Evergreen scheduled maintenance queues | opus #9 | Score kill (47); needs the budget governor first to be safe. |
| Slack budget & worker-health alerts | sonnet #7 | Score kill (46); the budget events it depends on should land first (card 2). |
| Coverage-gap task seeding | sonnet #6, gpt-5.5 #8, composer #9 | Score kill (45). Reliable coverage parsing across heterogeneous repos breaks the measure-forward-cheap promise. |
| Diff risk scoring / review lens | sonnet #9, gpt-5.5 #9, codex #7 | Score kill (43). Diagnostic, not throughput-driving; auto-pause-on-risk rejected outright (false-positive quarantine storms). |
| Hierarchical split charter | composer #10 | Score kill (43); lost the single R slot to card 9. |
| Brief/MCP safety scanner | gpt-5.5 #10, codex #10 | Score kill (35). Briefs are operator-authored in a controlled store; Slack intake is confirmation-gated; `forbidden_paths` fencing exists. |
| ADR drift detection | sonnet #10 | Score kill (33). Per-land LLM classification noise; belongs in a task brief. |

Cross-deck consensus kills the judge affirmed: ruflo-arena tournaments (measure-forward invariant), swarm queen/Raft coordination (pull-based + stateless-worker invariants), metaharness policy-surface evolution (measure-forward invariant), HNSW/agentdb vector memory (subsystem-scale build cost), federation zero-trust/hop delegation (git-authority + routing invariants), Prometheus export (UI-first), docs drift (task brief, not capability).

## 5. Synthesis record

- Candidates: five decks, five model families, identical grounding pack, repo read access, self-scored with kill lists.
- Judge: sole cross-judge with repo access; de-duplication, gate enforcement, independent re-scoring, portfolio enforcement, grafting.
- Notable judge dissents from candidate consensus: killed the four-deck goal-loop cluster (re-badged roadmap, not mined capability); scored the budget governor C1=4 against a four-deck C1=5 (a cap prevents waste, it doesn't land work); promoted a single-deck card (auto repo-task intake) above three five-deck consensus cards; flagged the five-deck counterfactual enthusiasm as mild collective inflation (only one deck admitted token-shape dependence).
- Judge verification highlights: `max_fix_attempts` exists in `config/manager.py` and is consumed nowhere; the ralph-loop `NIGHTSHIFT_LOOP_COMPLETE` sentinel is orphaned; `queue_state.paused_reason` + `set_queue_pause` exist; autosplit is real and manager-unwired; `attempt_repair` is deterministic (not an LLM loop); the charter is byte-stable and `cache_ttl` is partially pre-built.
- Dropouts: none — all five candidates and the judge completed.
