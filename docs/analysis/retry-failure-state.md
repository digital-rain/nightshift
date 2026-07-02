# Retry, Failure & State Management — Thermo-Nuclear Code Quality Review

**Date:** 2026-07-02
**Scope:** the whole retry / failure / state subsystem as it stands on `main` —
`worker/execute.py`, `worker/loop.py`, `manager/app.py` (worker API),
`manager/store.py`, `manager/landing.py`, `manager/resolve_job.py`,
`engine.py` (locks, squash, worktrees, resolve), `events.py`,
`manager/scheduler.py`, `manager/registry.py`.
**Method:** thermo-nuclear code quality review (strict maintainability rubric:
structural regressions and code-judo opportunities first, spaghetti growth,
boundary/type-contract problems, file-size decomposition).

**Verdict: does not meet the bar.** The individual functions are carefully
written — docstrings are excellent, git handling is defensive, the scheduler is
genuinely clean — but the *design* of state, failure, and retry is an
accumulation of local patches with no owning model. Every finding below traces
to one root cause: **there is no task state machine, so every endpoint
re-derives "what state is this task in and what should happen next" from
scratch**, using strings, dicts, run-history scans, and git side effects.

Findings in priority order.

---

## 1. Correctness: submits are not fenced against lease validity — a stale worker can double-land

`worker_submit` fetches the lease **only to read `base_ref`**; it never checks
that the lease is still live or owned by the submitting worker:

```944:949:src/nightshift/manager/app.py
        queue = _queue_from_label(body.queue)
        lease = await store.get_lease(body.lease_id)
        base_ref = lease.get("base_ref") if lease else None
        # The target repo the worker ran against is recorded on the run (and is
        # workspace-relative); landing materialises ``workspace / repo``.
        repo = run.get("repo")
```

Meanwhile `reclaim_expired_leases` flips a lease to `expired`, and
`acquire_lease`'s uniqueness guard only excludes `('leased', 'submitted')` — so
the same task can be re-leased to a second worker while a slow first worker is
still running. When the first worker finally submits, nothing rejects it:
`land()` runs, `set_lease_status(body.lease_id, "landed")` resurrects the
expired lease, and one task lands twice (or lands stale work). The transport
`stop` action (lease → `cancelled`) has the same hole: the cancelled worker's
eventual submit still lands.

**Fix:** `worker_submit` must open with a transition guard — lease exists,
`status == 'leased'`, `worker_id` matches — and 409 otherwise. This falls out
of finding 2, but do it even standing alone.

## 2. Missed code-judo: one explicit task state machine would delete most of this subsystem's complexity

Task state is smeared across **six** representations:

1. Lease status strings (`leased`, `submitted`, `released`, `landed`,
   `expired`, plus a `cancelled` that appears nowhere in the store vocabulary).
2. Run status strings (`running`, `completed`, `error`, `blocked`, `skipped`,
   `aborted`).
3. The task-state overlay table (`blocked`, `quarantined`, `repo_unavailable`).
4. Brief frontmatter flags (`disabled` / `quarantined` / `completed`), checked
   separately in `engine.run_task` and `patch_task`.
5. Git artifacts used as implicit state: `_worktree_has_commits`, and
   cross-machine detection via `worktree_dir(...).exists()`
   (`landing.py:151`).
6. In-process app state: `_paused_queues`, `_queue_modes`, `_queue_cursors`,
   `app.state.repo_warnings`, `app.state.resolves` — all lost on manager
   restart, silently unpausing queues and orphaning resolve bookkeeping.

Because no component owns the transition rules, they are re-implemented ad hoc
at every touch point:

- `worker_poll` decides "is this runnable" by combining a leased-set +
  blocked-set + quarantined-set + repo overlay + candidate flags
  (`app.py:736-832`).
- `worker_submit` decides "what does this outcome mean" via a 260-line branch
  cascade (finding 4).
- `patch_task` reverse-engineers "did the operator make this ready again" from
  three frontmatter booleans, then conditionally clears the overlay.
- The overlay writes in `worker_poll` need explicit guard-reads ("don't
  clobber quarantine with blocked", `app.py:736-739, 755-756`) precisely
  because states have no priority ordering — the guard *is* an implicit state
  machine, hand-inlined into a poll loop.

**The move:** `TaskState` / `RunStatus` / `LeaseStatus` enums and a single
`lifecycle.py` owning `apply_outcome(current_state, outcome) -> Transition`,
where a `Transition` is a *value* (run update + lease update + task-state
change + events to emit). `worker_submit` becomes: parse, guard lease, compute
transition, apply atomically, maybe land. The blocked/error/no-change/
quarantine branches become rows in a transition table instead of interleaved
control flow.

## 3. Structural: multi-step state updates are non-atomic everywhere; the stores have no transaction concept

Every transition is a sequence of independent awaits. The blocked branch alone
is five (`app.py:967-985`): `update_run` → `set_lease_status("released")` →
`set_task_state("blocked")` → `set_idle` → `_emit`. A crash between release
and set_task_state means an honest hold instantly re-leases on the next poll.
A crash before the release leaves the task stuck until TTL expiry with a
terminal run row attached to a live lease.

The land path is worse: git has already moved `main` before
`update_run` / `set_lease_status` / `clear_task_state` run
(`app.py:1162-1174`) — a crash there leaves a landed commit with a `running`
run, a `leased` lease, and the brief still in the queue, which the quarantine
streak scanner later reads as "no progress".

**Fix:** the storage half of finding 2 — one `apply_transition(...)` store
method that PgStore executes in a single transaction and MemoryStore under its
lock. `_emit` fan-out (SSE) can stay outside; the durable writes must be one
unit.

## 4. Spaghetti: `worker_submit` is a ~260-line endpoint doing six jobs

`app.py:938-1198`: protocol parsing, lease/state transitions, quarantine
policy, synchronous git landing, brief lifecycle (`drop_completed_task`), LOC
telemetry, and SSE emission — four exit paths, each hand-rolling its own
update/release/emit sequence. The
`body.model_copy(update={"landable": True, ...})` at `app.py:1038-1041` —
mutating the parsed request body to re-route control into the landing branch —
uses the request object as a state variable. This function shrinks to ~40
lines under findings 2 + 8.

## 5. Structural: synchronous git landing runs on the manager's event loop

`worker_submit` calls `land(...)` directly (`app.py:1075`). `land` is
synchronous, holds `integrate_lock`, shells out to git repeatedly, and loops up
to `max_push_retries + 1` times over *network pushes*
(`landing.py:203-303`). For the duration of a land the manager's entire event
loop is frozen: no polls, no heartbeats, no SSE, no operator API. Heartbeats
failing to process during a long land is exactly how leases spuriously expire —
which is how finding 1's double-land becomes reachable in practice. The
codebase knows the pattern (`worker_poll` wraps its origin sync in
`asyncio.to_thread`, `app.py:853`) but the far heavier landing path doesn't.
`drop_completed_task` and `compute_code_loc` in the same endpoint are also
sync.

**Minimal fix:** `await asyncio.to_thread(land, ...)`. **Real fix:** per-repo
serialized landing executor (finding 9).

## 6. Missed simplification: the store duplicates ~1000 lines by hand, and the copies are already drifting

`MemoryStore` and `PgStore` re-implement every method's semantics by hand.
Concrete drift:

- **Terminal-status lists duplicated and both wrong.** `store.py:505` and
  `store.py:978` each hard-code `("completed", "error", "skipped", "aborted")`
  for `finished_at` — neither includes `blocked`, so every blocked run keeps
  `finished_at = NULL` forever and silently drops out of the duration stats.
- **`update_run` silently swallows unknown fields in both stores**
  (`allowed` set at `store.py:962-967`; `if key in row` at `store.py:503`). A
  typo'd or newly-added field disappears with no error.
- **`'submitted'` is dead vocabulary.** Both stores filter on
  `('leased', 'submitted')` (`store.py:290, 317, 771, 782`) but nothing ever
  sets it. Dead states in a stringly-typed enum are how you know the vocabulary
  has no owner.

**The move:** (a) extract the shared vocabulary (`RUN_TERMINAL_STATUSES`,
updatable-field sets, the lease-active set) from the typed models in finding 7,
and make `update_run` raise on unknown keys; (b) honestly evaluate whether
MemoryStore should exist at all versus one SQL implementation over
SQLite/embedded-pg — "the tests exercise MemoryStore" means the production
store's actual SQL semantics (e.g. the partial-unique-index lease race) are the
*less*-tested path.

## 7. Boundary: one outcome shape is maintained by hand in five places

The same ~15-field outcome record exists as: `ExecuteOutcome`
(`execute.py:53-79`), the hand-built submit payload dict (`loop.py:170-193`),
`SubmitBody` (`app.py:130-163`), the run-row template (`store.py:465-493` +
the SQL schema), and the `local.finish` dict (`loop.py:206-227`). One new
telemetry field = five synchronized edits plus the `telemetry` re-packing dict
in `worker_submit` (`app.py:953-960`).

The `failure_kind` vocabulary (10+ strings: `model_unavailable`,
`repo_unavailable`, `backend_unavailable`, `preflight_failed`,
`worker_launch`, `worker_error`, `validation_error`, `publish_failed`,
`merge_conflict`, `merge_rejected`, `blocked`) is defined nowhere; the stale
comment in `events.py:585-589` lists a *different* set (`timeout`,
`no_changes`) than what the worker actually emits.

**The move:** one shared `Outcome` pydantic model with `OutcomeStatus` and
`FailureKind` enums used by all five sites. Separately, the ~8 early-return
`ExecuteOutcome(...)` constructions in `execute_work_order`
(`execute.py:186-398`), each repeating ~10 kwargs, collapse to a local
`fail(kind, reason)` closure — ~100 lines of copy-paste gone from a 460-line
file.

## 8. Spaghetti: "adopt agent land on main" is threaded through three layers; two are redundant

`main_advanced_sha` is checked in `execute.py:377-389` (worker),
`worker_submit` `app.py:1031-1041` (via the `model_copy` hack), and
`_adopt_agent_land_on_main` inside `land()` (`landing.py:180-185, 341-372`).
But `land()` already fully owns the decision. The worker-side check only
cosmetically changes `result_line`; the submit-side check exists solely to flip
`landable` so control reaches `land()`.

**The move:** delete both upstream checks. The worker reports plain facts
(`completed`, no commits); `worker_submit` calls `land()` for every completed
submit and keys off its result ({adopted, landed, nothing-to-land, conflict}).
Bonus: the worker stops importing `nightshift.manager.landing`
(`execute.py:42`), which is currently a worker→manager layering violation.

## 9. Structural: retry is four unrelated mechanisms; the push-retry loop exists twice; lock discipline is prose-enforced

- **Task-level retry is implicit** — an errored task simply re-leases; there is
  no attempt count anywhere. Quarantine is inferred by re-scanning the last 50
  runs on every failure (`_quarantine_if_looping` → `no_progress_streak`,
  `app.py:475-509, 2051-2073`): O(runs) per failure, and interleaved
  other-task runs eat the 50-run window. An `attempts_without_progress`
  counter on the task-state row, maintained inside the finding-2 transition,
  deletes the scanner and makes the threshold O(1).
- **The push-retry loop is duplicated**: `land()`'s attempt loop with
  `orphan_squash` bookkeeping (`landing.py:200-303`) and
  `push_resolved_main`'s loop with `drop_shas={sha}` (`landing.py:441-487`)
  are the same algorithm — sync-to-origin dropping your own orphan commit,
  re-apply, push, retry on non-fast-forward — written twice with different
  idioms (re-squash vs cherry-pick). Extract one
  `integrate_and_push(repo, apply_commit, retries)`.
- **Locking**: two non-reentrant flocks whose nesting constraint lives in
  docstrings (`engine.py:1507-1548`: "never nest", "must never be the landing
  lock (that would self-deadlock)"), plus two process-wide `threading.Lock`s,
  plus `app.state.resolves` subprocess bookkeeping reaped opportunistically.
  This is correctness by careful reading. The judo move: the manager is already
  the sole git authority — route all main-mutations through a **per-repo
  serialized executor** (one worker thread per repo) and in-process locking
  disappears. The one cross-process user, `resolve_job` pushing origin/main
  itself, already reports back to `/resolve-result`; have it submit the
  resolved SHA and let the *manager* push on the repo executor, and the
  cross-process integrate lock becomes unnecessary.

Related: `worker_poll` (`app.py:715-910`) is a read endpoint that performs
writes — per-candidate `get_task_state`/`set_task_state` round-trips and event
emission for every unroutable task, on every poll, from every worker, plus a
full on-disk brief re-parse of every queue. Overlay reconciliation belongs in
one periodic loop; the poll hot path should be pick → lease → return.

## 10. File size / decomposition

- **`engine.py` (3634 lines)** is at least six modules cohabiting: env
  preflight (~100-410), queue/brief CRUD (~560-1300), prompt building
  (~1310-1440), worktree + locks + squash (~1443-2075), rendezvous/sync/recover
  (~2076-2530), resolve (~2534-2906), and the legacy `run_task`/`run_queue`
  orchestration (~3048-3634 — ~580 lines that substantially duplicate
  `worker/execute.py` for the pre-split single-process path). Split it, and
  decide the fate of the legacy runner: with `events.py`'s `RunStore`
  event-folding as a second history reconstruction alongside the store's runs
  table, every outcome-shape change is currently made **three** times.
- **`app.py` (2151 lines)**: the worker API (~600 lines) and the operator API
  are two routers with almost no shared code — mechanical split, high payoff,
  and it isolates the state-transition code where finding 2 wants it.
- **`store.py` (1146 lines)**: shrinks as a byproduct of finding 6.

---

## Suggested sequencing

1. **Fence submits on lease validity** (1) — small, closes a live correctness hole.
2. **Typed `Outcome` + `FailureKind`/status enums** (7) — mechanical, unblocks everything else.
3. **`lifecycle.py` transition model + atomic `apply_transition`** (2+3) — the big judo move; `worker_submit` collapses as a side effect (4).
4. **`asyncio.to_thread(land, ...)`, then per-repo executor + manager-side resolved-push** (5, 9).
5. **Attempt counter replaces the streak scanner; unify the push-retry loops; delete the redundant adopt checks** (9, 8).
6. **Split the three >1k-line files; retire the legacy runner + `RunStore` folding** (10).

The system's *behavior* is thoughtful — the failure handling covers real cases
and the git safety is genuinely careful. But the implementation preserves a
large amount of incidental complexity for which clear deletion paths exist.
Items 1-3 are presumptive blockers by the rubric's bar: an unfenced
distributed-state transition, no atomicity on multi-step updates, and a missing
state model that every new feature keeps paying for.

A greenfield redesign that addresses all of the above is written up in
`docs/spec/greenfield-task-lifecycle.md`.
