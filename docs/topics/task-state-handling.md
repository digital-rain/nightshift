# Task State Handling

**Subject:** How Nightshift tracks, stores, and transitions task state — the split between frontmatter-authoritative flags and the DB overlay, the typed attempt lifecycle, the failure/retry/quarantine policy, and how operators release held tasks.
**Status:** Descriptive — documents the system **as implemented** after the rebuild-in-place migration (see `docs/spec/rebuild-in-place-migration-plan.md`, phases 0–9). Where prose and code disagree, the code governs and this doc should be updated.
**Primary sources:** `src/nightshift/lifecycle.py`, `src/nightshift/transitions.py`, `src/nightshift/manager/{api_worker.py,api_operator.py,reconciler.py,failure_policy.py,store.py}`, `src/nightshift/task_files.py`, `src/nightshift/spawn_daily.py`, `src/nightshift/worker/loop.py`, `src/nightshift/assets/ui/app.js`.

This doc supersedes the former `docs/spec/failure-retry-policy.md` and the pre-rebuild version of this file.

---

## 1. The two stores

A task's operational state lives in exactly one of two places, split by who owns it:

| Store | States | System writer | Cleared by |
|---|---|---|---|
| **Frontmatter** (the task's `.md` file) | `disabled`, `quarantined`, `failed`, `completed` | Transition effects (`FrontmatterFlag` → tasks-repo executor job) | Operator toggles the flag off (PATCH) |
| **DB overlay** (`nightshift.tasks`, `TaskHoldKind`) | `blocked`, `repo_unavailable` | `TaskHold` transition effects; the reconciler | Auto-clear, Resolve, or explicit Reset |

Frontmatter is the **single source of truth** for the operator-visible lifecycle flags. The DB overlay holds only transient, externally-caused conditions the system can often clear itself. The task row additionally carries the retry bookkeeping: `attempts_without_progress` (counter), `next_eligible_at` (backoff), and `retry_eligible`.

### Why the split exists

Originally quarantine/failure lived in the DB overlay while the detail-pane editor read and wrote frontmatter. The Save button sent the whole form — always `quarantined: false` for a system-quarantined task — and an implicit "release on ready" side effect silently wiped the overlay on every unrelated save. Worker and manager disagreed about state, invisibly. Moving `quarantined`/`failed` into frontmatter made the editor a true reflection of real state; clearing them is now an explicit operator action, and an unrelated edit (title, body, model) can never release a held task.

### Companion reason fields

When the system sets `quarantined: true` or `failed: true` it writes a companion field in the same commit — `quarantine_reason` / `failed_reason` — surfaced in the detail pane and in `/api/blocked` rows.

---

## 2. The attempt lifecycle

Since the attempts schema migration (rebuild Phase 8), a dispatched task run is one **attempt** row — the former lease + run pair merged. `AttemptState` (`lifecycle.py`) is the single stored lifecycle column:

| State | Meaning |
|---|---|
| `running` | Leased to a worker, executing |
| `landing` | Submit accepted; land job queued/running on the repo executor |
| `resolving` | An out-of-process resolve child is repairing this task's work |
| `landed` | Terminal — squash (or adopted agent land) confirmed on `main` |
| `no_change` | Terminal — completed but nothing landed |
| `blocked` | Terminal — honest block or HOLD-classified failure |
| `failed` | Terminal — worker error |
| `conflict` | Terminal — land refused (merge conflict/rejection) |
| `expired` | Terminal — deadline elapsed without a submit |
| `aborted` / `skipped` | Terminal — operator stop / neutral submit |

A task with a **live** attempt (`running`, `landing`) cannot be re-leased (partial unique index `attempts_live_task_uniq`). Every terminal attempt gets `finished_at`. The old `RunStatus` vocabulary survives only on the wire (worker submits, `/api/runs*` views).

### Transitions: pure core, atomic shell

No handler mutates state directly. The pure functions in `transitions.py` (`on_submit`, `on_land_result`, `on_split_result`, `on_land_enqueued`, `on_deadline`, `on_operator_stop`, …) compute a `Transition` **value**: the attempt-row updates, the new `AttemptState`, the task-level `TaskEffects`, and the events to append. `store.apply_transition` applies it in one transaction with a CAS on `attempts.state` — a stale or duplicate submit simply fails the CAS and writes nothing.

`TaskEffects` splits into transactional and post-commit parts:

- **In the transaction:** the DB hold (`TaskHold` upsert / `clear_hold`), the `attempts_without_progress` counter op (`Progress.INCREMENT`/`RESET`/`NONE`), the `next_eligible_at` backoff stamp, and the events (a transactional outbox — SSE broadcast happens after commit).
- **After commit:** frontmatter flag writes (queued as tasks-repo executor jobs), the phase-A watch update, the durable queue pause, the worker cooldown, and brief consumption.

---

## 3. Failure classification

Every failure carries a `FailureKind`; `RetryPolicy.on_failure(kind)` (`lifecycle.py`) maps it to one of four actions:

| Category | Kinds | Action | Consequence |
|---|---|---|---|
| **Environment** — the box is at fault | `model_unavailable`, `backend_unavailable`, `repo_unavailable`, `preflight_failed`, `worktree_failed`, `worker_launch`, `publish_failed` | `RETRY_ELSEWHERE` | Neutral to the task: no counter, no flags, no watch. The **submitting worker** is cooled down for that queue (300 s, `WORKER_COOLDOWN_SECONDS`) so a broken box stops eating the queue while other workers retry the task. |
| **Task** — the work failed | `worker_error` | `RETRY` (or `QUARANTINE` in worker quarantine mode) | Climbs the failure ladder (§4). |
| **Recoverable** — work is preserved | `validation_error`, `blocked`, `merge_conflict`, `merge_rejected` | `HOLD` | DB `blocked` hold with an operator-visible reason; the branch survives for the Resolve flow. Does not arm the failure watch. |

An honest `blocked` submit (the agent declared `NIGHTSHIFT_BLOCKED`) takes its own transition: a DB `blocked` hold with `retry_eligible: true`, and it **does** arm the phase-A watch.

---

## 4. The failure ladder and the two-phase policy

### Retry as data

The task row's `attempts_without_progress` counter replaces the old 50-run history scan (`no_progress_streak`, deleted in rebuild Phase 5) — interleaved runs of other tasks can no longer mask a streak. Task-category failures and no-change completions increment it; a land (or adopted agent land, or split harvest) resets it; environment failures, aborts, and blocks are neutral.

Each counted failure also stamps `next_eligible_at = now + base · 2^(n−1)` (base `retry_backoff_seconds`, default 60 s; capped at 3600 s). Dispatch — including the Phase B retry path — skips tasks whose backoff has not elapsed.

### The ladder

For a `RETRY`/`QUARANTINE`-classified error or a no-change completion, `transitions._failure_ladder` walks three rungs:

1. **Worker quarantine mode** (`immediate_quarantine`): frontmatter `quarantined: true` on the first counted failure.
2. **Counter threshold** (`quarantine_after` = `cfg.quarantine_threshold`; 0 disables): when the counter (including this outcome) reaches it, frontmatter `quarantined: true` — the budget-protection stop for a task stuck re-executing without progress.
3. **Otherwise:** frontmatter `failed: true` (+ `failed_reason`), arm the phase-A watch, stamp the retry backoff. If this run **was itself a retry** (see below), quarantine instead and pause the queue with `retry_failed`.

### Phase A — drain, with the two-in-a-row watch

While ready (non-failed) tasks remain, the queue keeps running; failed tasks are set aside. Each queue holds an in-memory watch (`failure_policy.QueueFailureState.watch_armed`):

| Event | Watch before | Effect |
|---|---|---|
| Failure | disarmed | Arm — one strike |
| Failure | armed | **Pause the queue** (`consecutive_failures`) |
| Landed success | any | Disarm |
| Neutral (abort/skip/environment) | any | No change |

Two *unrelated* tasks failing consecutively — no landed success in between — is treated as evidence of a systemic problem, so the queue stops rather than burn budget.

### Phase B — retry, one at a time

`worker_poll` admits a retry when a queue has **no live attempts** and **no ready candidate left**. The retryable set is the union of frontmatter `failed: true` tasks (`task_files.failed_tasks`, a disk scan) and DB `blocked AND retry_eligible` rows, minus tasks still backing off. `failure_policy.pick_retry` picks the earliest in the queue's configured order and removes just that task from the dispatch exclusion set.

**Retry detection:** a submit counts as a retry when the task's frontmatter reads `failed: true` at submit time (`_task_is_failed_in_frontmatter` feeds `SubmitPolicy.was_retry`). A retry that fails again is quarantined (frontmatter) and the queue pauses with `retry_failed` — a repeat failure on a fresh attempt is strong evidence the task itself is the problem.

---

## 5. Queue pauses (durable)

Pause state lives in the store (`queue_state` table) since rebuild Phase 7 — a manager restart no longer silently unpauses a failure-tripped queue.

| Reason | Trigger | Cleared by |
|---|---|---|
| `operator` | Transport Pause / Stop | Play |
| `consecutive_failures` | Phase A: two unrelated failures in a row | Play |
| `retry_failed` | Phase B: a retried task failed again | Play |

Pressing **Play** clears the pause and re-arms a fresh phase-A watch. A paused queue is excluded from candidate building entirely; the `pause_reason` is exposed in the state API for the UI banner.

---

## 6. Who writes holds: the reconciler

Since rebuild Phase 7 the poll hot path is **read-only** — a no-work poll performs zero store writes. The periodic reconciler (`manager/reconciler.py`) owns:

- **Hold set/clear:** the unroutable (`no_capable_worker`-family) and bad-repo-reference `blocked` holds and the `repo_unavailable` pauses, with the same dedup and one-warning-per-queue behavior the poll path used to have. It also **auto-clears its own holds** when they no longer apply (a capable worker checked in; the repo reappeared) — but only holds whose reason matches the unroutable vocabulary. Operator-actionable blocks (validation, resolve, bad repo reference) are left for the explicit Reset.
- **Deadline expiry:** live attempts past their deadline transition `running → expired` (terminal, `finished_at` stamped). `landing` attempts are exempt — only the land job or startup recovery may consume them.
- **Terminal GC and land recovery:** abandoned worktrees/branches torn down; interrupted `landing` attempts re-driven or parked on startup.
- **Worker liveness:** silent workers marked offline.

Quarantined tasks (frontmatter) are exempt from reconciler hold writes — the frontmatter flag is the stronger, operator-owned state and its reason is never clobbered by an overlay.

---

## 7. Dispatch exclusion — what `worker_poll` skips

For each poll, a task is not handed out when any of these hold:

1. Its queue is paused (any reason) or excluded by the worker (`exclude_queues`).
2. The submitting worker is cooled down for that queue (environment failure within 300 s).
3. It has a live attempt (`running`/`landing`).
4. Frontmatter says `disabled`, `quarantined`, `failed`, or `completed` (the `failed` exclusion is lifted for the single Phase B pick).
5. A DB hold exists (`blocked`, `repo_unavailable`), or the candidate's repo is unresolvable/absent (read-only check; the hold write is the reconciler's).
6. Its retry backoff (`next_eligible_at`) has not elapsed.

### Worker-local backoff

Independently of the manager, each worker counts consecutive `error`/`blocked` submits per queue; at two it stops polling that queue (`exclude_queues`) — its own environment may be the problem. The backoff clears when a task on that queue succeeds or when the manager's poll response (`queue_pauses`) shows the queue is not paused, i.e. an operator pressed Play.

---

## 8. Operator actions

| Action | Surface | Effect |
|---|---|---|
| Toggle `quarantined`/`failed`/`disabled`/`completed` | Detail-pane status control → `PATCH /api/tasks/{task}` | Plain frontmatter write. Toggling quarantined/failed **off** also emits `task_released`, resets the phase-A watch, and clears the retry backoff (`clear_task_backoff`) so "dispatchable now" means now. The counter survives, as streak history did. |
| **Reset** | Detail-pane button (non-conflict blocked tasks) → `POST /api/tasks/{task}/reset` | Clears the DB `blocked`/`repo_unavailable` hold only; 404 if the task holds neither. Never touches frontmatter. |
| **Resolve** | Detail-pane button (conflict/validation holds) | Spawns the resolve subprocess; the resolved SHA lands via the repo executor and the origin attempt transitions accordingly. |
| **Play / Pause / Stop** | Transport | Durable pause set/clear (§5); Stop also aborts live attempts. |

An unrelated Save never releases anything — there is no implicit clearing anywhere in `patch_task`.

---

## 9. API and events

**Reading:**

| Endpoint | Returns |
|---|---|
| `GET /api/tasks/{task}` | Brief incl. `quarantined`, `quarantine_reason`, `failed`, `failed_reason` |
| `GET /api/queue` | Per-task flags alongside title/priority |
| `GET /api/blocked` | DB holds (`list_blocked`) ∪ frontmatter quarantined/failed (`frontmatter_held_tasks` scan) — the "needs attention" list |
| `GET /api/runs*` | Compat views projected from `attempts` (legacy field names preserved) |
| SSE snapshot | Same `blocked` union; leases/runs are attempt views |

**Events** (inserted transactionally with the state change, broadcast after commit): `task_quarantined`, `task_blocked`, `task_released`, `task_result`, `queue_paused`, `queue_changed`, `run_finished`.

---

## 10. UI rendering

Display status priority in the queue rows and detail pane:

1. `completed` → Completed
2. `quarantined` → Quarantined
3. `failed` → Failed (`.status.error` treatment)
4. Latest run record → run status
5. `blockedTasks[task]` (from `/api/blocked`) → Blocked / Paused
6. default → Pending

The status segmented control offers Ready / Disabled / Quarantine / Failed / Completed as mutually exclusive options. Paused queues show an amber banner with reason-specific copy (`PAUSE_REASON_COPY`) and an amber "paused" badge in the playlists view:

| Reason | Banner |
|---|---|
| `consecutive_failures` | "Paused: two unrelated tasks failed in a row. Fix the issue, then press Play to retry the failed tasks." |
| `retry_failed` | "Paused: a retried task failed again and was quarantined. Fix the issue, then press Play to continue." |

---

## 11. Invariants

1. **Frontmatter is authoritative for `quarantined`/`failed`.** The DB overlay never stores these two states; the system writes them only through transition effects, and the editor toggles them like any other frontmatter boolean.
2. **An unrelated save never clears system state.** Releasing a task is always an explicit act: a flag toggle, a Reset, or a Resolve.
3. **`blocked`/`repo_unavailable` are DB-only,** written by transitions and the reconciler, cleared by auto-clear paths or the Reset endpoint — never by `PATCH`.
4. **State changes are atomic and fenced.** One `apply_transition` per outcome, CAS on `attempts.state`; a stale submit cannot double-write. Frontmatter writes are post-commit effects serialized on the tasks-repo executor, so worker and manager converge on the same file state.
5. **Environment failures never count against a task.** They cool the worker down; the task retries elsewhere with its counter untouched.
6. **Pauses survive restarts.** A failure-tripped queue stays paused until an operator presses Play.
