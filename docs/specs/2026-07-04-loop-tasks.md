# Nightshift — Loop tasks (`loop: true` goal-loop briefs)

**Subject:** A task frontmatter field that turns a brief into a *goal loop*: the queue re-runs it — one nightshift run per iteration — until a machine-checkable done condition passes, then the worker signals completion and the loop ends.
**Status:** Proposed — for implementation on the nightshift VM. Where this doc and the code disagree once implemented, the code governs and this doc should be updated.
**Relationship:** The operator-side contract (how a loop brief is written: goal, done-check, budget, state-note path) is the `until` skill in the target repo's agent codex. This spec is the engine side: retention, re-dispatch, termination, and safety rails. `evergreen` is a *different* knob and is untouched: retain-and-reset for recurring template tasks, the beginnings of scheduled tasks — it never self-terminates.

---

## 0. The one idea

A loop task pursues one goal that is too large for a single run.
Its brief carries a **done-check** — a command whose output decides, not the agent's self-assessment.
Each nightshift run is one iteration: run the check first, make the single smallest verified increment, land it, append one line to a state note in the target repo.
The brief survives every land and re-enters the queue, until a run proves the check passes — then the worker emits a completion sentinel, the engine marks the brief `completed`, and the loop is over.

The engine never parses the done-check; convergence is the worker's job (per the prompt contract, §4).
The engine's job is retention, re-dispatch, honest termination, and refusing to let a stuck loop burn budget forever.

## 1. Today's state (what this spec changes)

`loop` already exists in frontmatter — but as a different, half-built feature:

- `work_orders.py` forwards `loop` / `loop_max_iterations` to the worker, whose only effect is a prompt swap: `prompts.build_prompt` uses `assets/prompts/nightshift-ralph-loop.md` instead of `nightshift-local.md`.
- That ralph prompt tells the agent "the harness re-invokes you automatically" and defines a `NIGHTSHIFT_LOOP_COMPLETE:` sentinel — **neither is implemented anywhere**. No backend re-invokes; nothing scans for the sentinel. A `loop: true` task today runs once with a misleading prompt and is then consumed like any regular task.
- The manager UI already has a Loop attribute toggle and a `loop_max_iterations` ("Turns limit") panel.

This spec repurposes the field: **iteration moves from inside one run (never built) to across runs (one run per iteration)**, which composes with everything the engine already does per run — worktree, validate, land, telemetry, retry ladders.
The ralph-loop prompt is replaced by the goal-loop prompt (§4); `loop_max_iterations` keeps its name but becomes an engine-enforced across-run budget.

## 2. Frontmatter contract

| Field | Type | Owner | Meaning |
|---|---|---|---|
| `loop` | bool (default `false`) | operator / brief author | Goal-loop task: brief survives lands and re-queues until completed. |
| `loop_max_iterations` | int (default `0` = unlimited) | operator / brief author | Across-run iteration budget; on exhaustion the engine quarantines with reason `loop budget exhausted` (§3). Recommend always setting it. |
| `loop_iteration` | int (default `0`) | **engine** | Iterations consumed so far; incremented on each terminal run (landed or no-change). Operator-visible progress; never hand-edited. |

The brief **body** carries the worker-facing loop contract, which the engine never parses:
the goal in one sentence, the done-check command, and the state-note path in the target repo.
A loop brief without a done-check is an authoring error the worker should surface as `NIGHTSHIFT_BLOCKED: brief has no machine-checkable done condition` on its first run — not something the engine validates.

Exclusions:

- `loop` + `evergreen` is a contradiction (one must terminate, the other never does): treat as a brief error — the UI prevents the combination, and the engine gives `evergreen` no effect on a loop task (retention is already covered; completion still terminates).
- `loop` + `split` is unsupported: split consumes the brief by design.

## 3. Engine semantics

**Retention.** On a confirmed land, the brief-drop rule becomes `drop_brief = not (evergreen or loop)` — in `transitions._landed_transition` (via a new `SubmitPolicy.loop` field mirroring `SubmitPolicy.evergreen`) and in both backstops (`reconciler._recover_landed` / `_task_evergreen`, `resolve_runner`).
A loop brief is never consumed by a land; it leaves dispatch only via `completed` (success), `quarantined` (budget / stuck), or operator deletion.

**Iteration accounting.** Each terminal run (landed or no-change) increments `loop_iteration` — a frontmatter write applied as a transition effect through the existing tasks-repo executor job, alongside the `FrontmatterFlag` mechanism (extended to carry an int field, or a sibling effect; implementer's choice).
Environment failures, aborts, and blocks do not consume budget — same neutrality the `attempts_without_progress` counter already has.

**Termination — success.** The worker signals completion with the (currently orphaned) sentinel, scanned exactly like `NIGHTSHIFT_BLOCKED` today (`prompts.extract_blocked_reason` pattern, last match wins):

```
NIGHTSHIFT_LOOP_COMPLETE: <one line of proof — the done-check's actual output>
```

The worker executor extracts it into a field on `Outcome`; the submit transition then writes `completed: true` to the brief's frontmatter (existing flag — dispatch already excludes `completed` tasks, and the queue UI already groups them).
The brief is **retained**, not dropped: it holds the goal, the budget spent, and the pointer to the state note — the loop's record.
The operator deletes it when satisfied, like any completed item.

Two shapes of a completing run, both terminal successes:

- *Landed + sentinel* — the final increment and the passing check in one run: normal landed transition, plus the `completed` write.
- *No-change + sentinel* — the previous iteration already finished the job; this run verified and stopped, exactly what the contract's "run the check first" demands. This must **not** feed the no-change failure ladder: record the run as a completed no-change, write `completed: true`, no `attempts_without_progress` increment.

**Termination — budget.** When the increment would take `loop_iteration` past a nonzero `loop_max_iterations` and no sentinel arrived: quarantine the task (existing `quarantined` frontmatter flag + reason field) with reason `loop budget exhausted after N iterations`.
Quarantine, not `failed`: the retry machinery lifts `failed` for a single re-pick, which would silently grant a stuck loop one more iteration — budget exhaustion is an operator decision, not a retry.

**Termination — stuck.** No new machinery.
A loop iteration that lands resets `attempts_without_progress` (existing `Progress.RESET`); a run that produces no change and no sentinel increments it, and the standard ladder quarantines after the queue's configured threshold.
That *is* the `until` contract's "no measurable movement → stop and escalate", enforced by the engine rather than trusted to the agent.
A worker that hits an architectural wall emits `NIGHTSHIFT_BLOCKED: <reason>` as it does today, with the same handling.

**Re-dispatch fairness.** After a landed iteration, the task is moved to the **end of its queue's execution order** (existing `save_order` machinery).
Without this, a loop task at the head of a manual-order queue monopolizes it — always present, always eligible.
Round-robin keeps loop iterations interleaved with the rest of the queue; in priority sort mode the move is recorded but ordering follows priority as usual.

## 4. Worker contract (the prompt)

`assets/prompts/nightshift-ralph-loop.md` is replaced by a goal-loop prompt selected exactly as today (`loop=True` in `build_prompt`); the header injects `LOOP_ITERATION` and `MAX_ITERATIONS` so the worker can report budget honestly.
The body encodes the per-iteration contract (mirroring the codex `until` skill, so briefs written by that skill and this prompt agree):

1. Read the brief's goal, done-check, and the state note at the path the brief names; trust them over anything remembered.
2. **Run the done-check first.** A pass means emit `NIGHTSHIFT_LOOP_COMPLETE: <check output>` and stop — no polish laps.
3. Otherwise: the single smallest step that moves the check, done under the standard worker rules (worktree, validate, commit) — one verified increment, not a batch.
4. Run the check again; append one line to the state note (iteration, what changed, check result with actual numbers, next obstacle) and commit it with the increment.
5. Never weaken the done-check to make it pass; if the check itself is wrong, emit `NIGHTSHIFT_BLOCKED:` explaining why.

The state note lives in the **target repo** and travels with the landed commits — the loop's memory is the note plus `git log`, never conversation history, which makes every iteration cold-start safe by construction.

## 5. UI

The existing Loop toggle and settings panel carry over with relabeling: "Turns limit" becomes "Iteration budget" (`loop_max_iterations`).
Queue row and detail pane for a loop task show `loop_iteration` against the budget (e.g. `loop 3/10`), and the completed state shows the sentinel's proof line as the result line.
The Evergreen and Loop toggles become mutually exclusive in the editor (§2).

## 6. Non-goals

- **Scheduling.** Evergreen remains the seed of scheduled/recurring tasks; loop tasks have no cadence — they re-queue immediately (modulo fairness) until done.
- **Intra-run iteration.** No harness re-invocation, no stop-hooks; one run is one iteration. In-session looping is a harness-level concern outside nightshift.
- **Engine-parsed done-checks.** The engine never runs or validates the done-check; it trusts the sentinel + the standard land verification. The check's integrity is the worker contract's job, and the operator's on review.

## 7. Touch points (implementation checklist)

- `lifecycle.py` — `SubmitPolicy.loop`; extend the frontmatter-write effect for the int `loop_iteration` field.
- `transitions.py` — retention rule in `_landed_transition`; sentinel-aware `_no_change_transition`; `completed` write on sentinel; budget quarantine; iteration increment; end-of-order move on land.
- `worker/execute.py` + `prompts.py` — sentinel extraction onto `Outcome` (mirror `extract_blocked_reason`); new prompt asset replacing `nightshift-ralph-loop.md`; inject `LOOP_ITERATION`.
- `manager/work_orders.py` — pass `loop_iteration` through to the worker.
- `manager/reconciler.py`, `resolve_runner.py` — retention backstops (`_task_evergreen` generalizes to "retained").
- `assets/ui/app.js` — relabel, mutual exclusion, `loop_iteration` display.
- `docs/spec/configuration-reference.md` — frontmatter table rows for `loop`, `loop_max_iterations`, `loop_iteration` (the existing table predates the ralph field and lists neither).
- Tests — retention on land, both completing-run shapes, budget quarantine, no-change ladder interplay, evergreen/split exclusions.
