# Nightshift — Failure Pause & Retry Policy

**Subject:** Per-queue failure handling: how Nightshift marks failed tasks, defers retries until ready work is drained, pauses a queue after consecutive unrelated failures, retries failed tasks one-at-a-time, and quarantines tasks that fail twice.
**Status:** Descriptive — documents the feature **as implemented**. Where prose and code disagree, the code governs and this doc should be updated.
**Primary sources:** `src/nightshift/manager/failure_policy.py`, `src/nightshift/manager/app.py` (worker_submit, worker_poll), `src/nightshift/worker/loop.py`, `src/nightshift/engine.py` (failed_tasks, frontmatter_held_tasks), `src/nightshift/assets/ui/{app.js,style.css,index.html}`.

---

## 1. Overview

When a task fails (worker error or blocked without quarantine), Nightshift does **not** immediately re-attempt it. Instead it enters a two-phase lifecycle designed to avoid burning compute on systemic failures while still giving transient failures a second chance.

**Phase A — Drain ready tasks first.** Failed tasks are set aside. The queue continues processing non-failed ("ready") tasks. If two *unrelated* tasks fail consecutively (no success in between), the queue pauses and requires operator intervention.

**Phase B — Retry failed tasks.** Once all ready tasks are processed and the queue is not paused, the manager dispatches the earliest failed task for retry. If the retry succeeds, the task is completed normally and the next failed task is tried. If the retry fails again, that task is quarantined and the queue pauses.

## 2. State model — single source of truth

Task states are split across two stores, each authoritative for distinct states:

| State | Source of truth | How set | How cleared |
|---|---|---|---|
| `failed` | **Frontmatter** (`failed: true`) | Manager writes via `_set_frontmatter_flag` | Operator toggles off in detail pane (PATCH) |
| `quarantined` | **Frontmatter** (`quarantined: true`) | Manager writes via `_set_frontmatter_flag` | Operator toggles off in detail pane (PATCH) |
| `blocked` | **DB overlay** (`nightshift.tasks`) | Manager writes via `store.set_task_state` | Auto-clear (Phase B retry, repo-rescan, Resolve) or explicit Reset |
| `repo_unavailable` | **DB overlay** (`nightshift.tasks`) | Manager writes via `store.set_task_state` | Repo-rescan auto-clears |

### Why frontmatter for quarantined/failed

Frontmatter is the file the operator sees and edits. Previously, quarantine/failed lived in a separate DB overlay; the detail pane's Save button sent the full form (always `quarantined: false` for a system-quarantined task), and the backend's implicit "release on ready" logic silently wiped the DB overlay on every unrelated save. This caused manager and worker to disagree on task state.

Now the manager writes `quarantined: true` / `failed: true` (and companion `quarantine_reason` / `failed_reason` fields) directly to the task's `.md` file via `_set_frontmatter_flag`. The detail pane reflects the real state and toggling it off is an explicit operator action, not an accidental side effect.

### Companion reason fields

When setting `failed: true` or `quarantined: true`, the manager also writes:
- `failed_reason: "..."` — human-readable explanation of the failure
- `quarantine_reason: "..."` — human-readable explanation of the quarantine

These are surfaced in the UI detail pane and in `/api/blocked` responses.

## 3. Phase A — Failure watch (two-in-a-row detection)

Managed by `failure_policy.QueueFailureState`, a per-queue dataclass with a single field: `watch_armed: bool`.

| Event | watch_armed before | Effect |
|---|---|---|
| Task fails | `False` | Set `watch_armed = True` — one strike |
| Task fails | `True` | **Pause the queue** (`consecutive_failures`) |
| Task succeeds | any | Set `watch_armed = False` — reset |
| Neutral (no outcome) | any | No change |

When the queue pauses, the manager:
1. Sets `_paused_queues[queue] = "consecutive_failures"`.
2. Emits an SSE `queue_paused` event so the UI shows the amber banner.
3. Stops dispatching from that queue until the operator presses Play.

Pressing Play clears the pause and resets the failure watch for that queue.

## 4. Phase B — Retry dispatch

When `worker_poll` finds a queue with:
- no active leases,
- no ready (non-failed) tasks remaining, and
- at least one failed task (from frontmatter) or `retry_eligible` blocked task (from DB),

it calls `failure_policy.pick_retry()` to select the earliest retryable task according to the queue's configured order. That task is allowed into dispatch by removing it from the blocked exclusion set.

Failed tasks for Phase B are sourced from frontmatter via `engine.failed_tasks()`, which scans `.md` files for `failed: true`. DB-blocked tasks with `retry_eligible: true` are also included.

### Retry detection

A task is considered a "retry" when it is currently marked `failed: true` in frontmatter at the time of submit. The manager checks this via `_task_is_failed_in_frontmatter()`.

### Retry failure → quarantine

If a retried task fails again:
1. The task is quarantined (`quarantined: true` written to frontmatter).
2. The queue is paused with reason `retry_failed`.
3. An SSE `task_quarantined` event is emitted.

The operator must press Play to continue retrying remaining failed tasks.

## 5. Worker-local backoff

Workers independently track consecutive failures per queue in `_queue_failures: dict[str, int]`. After two consecutive failures on the same queue, the worker adds that queue to `_backoff_queues` and passes it as `exclude_queues` on subsequent polls. This prevents a worker with a bad local environment from repeatedly pulling work from a queue it cannot process.

Backoff is cleared when:
- A task on that queue succeeds (resets the counter).
- The manager's poll response indicates the queue is no longer paused (via `queue_pauses` in the response), meaning an operator intervened.

## 6. Pause reasons

The manager tracks pause reasons in `_paused_queues: dict[str, str]`:

| Reason | Trigger | Cleared by |
|---|---|---|
| `operator` | Manual pause via transport API | Play |
| `consecutive_failures` | Two unrelated failures in a row (Phase A) | Play |
| `retry_failed` | A retried task fails again (Phase B) | Play |

The `pause_reason` is exposed in the queue state API and used by the UI to display context-appropriate banner text.

## 7. Releasing tasks

### Frontmatter states (quarantined/failed)

Toggle `quarantined: false` or `failed: false` via the detail pane's status segmented control (PATCH `/api/tasks/{task}`). No separate "release" action needed — the frontmatter change is immediately reflected in dispatch.

### DB overlay states (blocked/repo_unavailable)

- **Auto-clearing:** merge-conflict blocked tasks can be Resolved (existing flow). `repo_unavailable` tasks auto-clear when the repo becomes available.
- **Explicit Reset:** non-auto-clearing blocks (validation-failed, unroutable, bad-repo-reference) are cleared via `POST /api/tasks/{task}/reset`, which calls `store.clear_task_state()` and emits `task_released`.

The old implicit "release on ready" side-effect in `patch_task` has been removed. Saving a task's frontmatter (e.g. editing the title) never clears any state.

## 8. UI elements

### Failed status option

The detail pane's status segmented control includes "Failed" as a 5th option alongside Ready, Disabled, Quarantine, and Completed. Toggling it off clears the frontmatter `failed` flag.

### Reset button

The detail pane shows a "Reset" button for tasks in `blocked` state (from DB overlay) that are not merge conflicts. Clicking it POSTs `/api/tasks/{task}/reset`.

### Failed status pill

Tasks with `failed: true` in frontmatter render with a "Failed" label using the `.status.error` CSS class.

### Pause banner

A `.pause-banner` div appears at the top of the queue screen when the queue is paused due to `consecutive_failures` or `retry_failed`.

| Reason | Banner text |
|---|---|
| `consecutive_failures` | "Two tasks failed in a row — queue paused. Check logs, then press ▶ Play to resume." |
| `retry_failed` | "A retried task failed again and was quarantined — queue paused. Press ▶ Play to continue retrying." |

### Playlist badge

Queue rows in the playlist view show an amber "paused" badge (`.badge.paused-failures`) when the queue is paused for either failure-related reason.

## 9. Migration

`20260731000001_nightshift_failure_retry.sql` adds the `retry_eligible` boolean column:

```sql
ALTER TABLE nightshift.tasks
    ADD COLUMN IF NOT EXISTS retry_eligible boolean NOT NULL DEFAULT false;
```

The `MemoryStore` in-memory implementation mirrors this as a `retry_eligible` key on task dicts.
