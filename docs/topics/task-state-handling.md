# Task State Handling

How Nightshift tracks, stores, and transitions task states — the split between
frontmatter-authoritative states and DB-overlay states, who writes each, and
how operators and the system interact with them.

---

## The two stores

A task's operational state lives in one of two places, never both:

| Store | What lives here | Writer | Cleared by |
|---|---|---|---|
| **Frontmatter** (the `.md` file) | `disabled`, `quarantined`, `failed`, `completed` | Manager (`_set_frontmatter_flag`) or operator (detail pane PATCH) | Operator toggles the flag off |
| **DB overlay** (`nightshift.tasks`) | `blocked`, `repo_unavailable` | Manager (`store.set_task_state`) | Auto-clear or explicit Reset |

Frontmatter is the single source of truth for operator-visible lifecycle states.
The DB overlay exists only for transient, externally-caused conditions that the
system can often auto-resolve.

### Why the split matters

Before this design, the manager wrote `quarantined` and `failed` to the DB
overlay. The detail pane's Save button sent the full form — always with
`quarantined: false` for a system-quarantined task — and the backend's implicit
"release on ready" logic silently wiped the overlay on every unrelated save.
Worker and manager disagreed on state, and the wipe was invisible.

Now the manager writes `quarantined: true` / `failed: true` directly to the
task's `.md` file. The detail pane reflects real state. An unrelated edit
(changing the title) never touches the quarantine or failed flags.

---

## State catalogue

### Frontmatter states

These are boolean frontmatter fields set directly in `<tasks_root>/<queue>/<task>.md`.

#### `disabled`

- **Meaning:** Operator has manually parked the task.
- **Set by:** Operator via the detail pane.
- **Effect:** Excluded from `live_ordered_queue` — never dispatched.
- **Cleared by:** Operator toggles it off.

#### `quarantined`

- **Meaning:** The system has quarantined this task after repeated failures.
- **Set by:** Manager via `_set_frontmatter_flag` when:
  - `_quarantine_if_looping` — streak of no-progress runs hits the configured threshold.
  - `_quarantine_immediate` — worker-side quarantine mode (first failure quarantines).
  - `_quarantine_retry_failure` — Phase B retry failed again (double failure).
- **Companion field:** `quarantine_reason` — human-readable explanation written alongside the boolean.
- **Effect:** Excluded from dispatch (frontmatter scan in `worker_poll`). Surfaced in `/api/blocked` and the "needs attention" list.
- **Cleared by:** Operator toggles "Quarantine" off in the detail pane.

#### `failed`

- **Meaning:** The task's most recent run failed. It is excluded from normal (Phase A) dispatch but eligible for Phase B retry.
- **Set by:** Manager via `_set_frontmatter_flag` in `_record_failure_outcome` when `state="failed"`.
- **Companion field:** `failed_reason` — human-readable explanation.
- **Effect:** Included in candidates (so Phase B can re-admit it) but excluded from normal dispatch via the `failed` set in `worker_poll`. Surfaced in `/api/blocked`.
- **Cleared by:** Operator toggles "Failed" off in the detail pane, or the task succeeds on a Phase B retry.

#### `completed`

- **Meaning:** The task has been completed and its brief should be retained (e.g. evergreen tasks reset from template).
- **Set by:** Operator via the detail pane, or landing logic for certain task types.
- **Effect:** Excluded from `live_ordered_queue`.
- **Cleared by:** Operator toggles it off or the task is reset.

### DB overlay states

These are rows in the `nightshift.tasks` table, written by `store.set_task_state`.

#### `blocked`

- **Meaning:** An external condition prevents this task from running.
- **Subtypes:**
  - **Honest agent block** — the agent emitted `NIGHTSHIFT_BLOCKED: <reason>` with no commits. The branch is preserved; a Resolve can pick it up.
  - **Merge conflict / rejection** — the land step failed. The branch is preserved; the existing Resolve flow handles it.
  - **Validation failure** — the agent produced commits but the validate command rejected them. Recoverable; the branch is preserved.
  - **Unroutable** — no online worker advertises the model or MCP connector the task requires.
  - **Bad repo reference** — malformed or missing `repo:` in frontmatter or queue config.
- **Set by:** Manager in `worker_submit` (for agent/land blocks) or `worker_poll` (for unroutable/repo-error detection).
- **Effect:** Excluded from dispatch via the `blocked` set in `worker_poll`.
- **Cleared by:**
  - **Auto-clear:** Resolve flow (merge conflicts), repo-rescan (`repo_unavailable`), unroutable check passes on next poll.
  - **Explicit:** `POST /api/tasks/{task}/reset` — the Reset button in the detail pane.

#### `repo_unavailable`

- **Meaning:** The task's target repo is not cloned into the workspace.
- **Set by:** Manager in `worker_poll` when `repo_available()` returns false.
- **Effect:** Excluded from dispatch. One warning emitted per queue (deduped).
- **Cleared by:** Repo-rescan (`POST /api/repos/rescan`) when the repo appears.

---

## State transitions

```
                    ┌──────────────────────────────────────────────────┐
                    │                                                  │
 ┌───────┐   fail  │  ┌────────┐  retry fail  ┌─────────────┐        │
 │ Ready ├────────►│  │ Failed ├─────────────►│ Quarantined │        │
 └───┬───┘         │  └───┬────┘              └──────┬──────┘        │
     │              │      │ retry ok                  │               │
     │              │      └──────────►┌───────────┐  │               │
     │   success    │                  │ Completed │  │  operator     │
     └──────────────┼─────────────────►│ (landed)  │  │  clears       │
                    │                  └───────────┘  │               │
                    │                                  │               │
                    │◄─────────────────────────────────┘               │
                    │    operator clears quarantine                    │
                    │                                                  │
                    │   ┌─────────┐                                   │
                    │   │ Blocked │   (DB overlay, auto/manual clear) │
                    │   └────┬────┘                                   │
                    │        │ Reset / Resolve / auto                 │
                    │◄───────┘                                        │
                    └──────────────────────────────────────────────────┘
```

### Failure policy phases

The failure policy operates in two phases per queue (see `failure-retry-policy.md`):

**Phase A (drain):** When a task fails, it is marked `failed` in frontmatter and
set aside. The queue continues with remaining ready tasks. Two consecutive
unrelated failures pause the queue (`consecutive_failures`).

**Phase B (retry):** Once no ready tasks remain and the queue is not paused, the
earliest failed task is retried. If it fails again, it is quarantined and the
queue pauses (`retry_failed`).

### Retry detection

A task is considered a "retry" when its frontmatter has `failed: true` at the
time the worker submits its result. The manager checks this via
`_task_is_failed_in_frontmatter()`. This replaces the old `retry_eligible`
DB column for the failed→quarantine path (the column still exists for
DB-blocked tasks that are retry-eligible).

---

## API surface

### Reading state

| Endpoint | Returns |
|---|---|
| `GET /api/tasks/{task}` | Full brief including frontmatter `quarantined`, `failed`, `quarantine_reason`, `failed_reason` |
| `GET /api/queue` | List of briefs with `quarantined`, `failed` fields |
| `GET /api/blocked` | Union of DB-blocked tasks + frontmatter quarantined/failed tasks (the "needs attention" list) |
| `GET /api/events` (SSE snapshot) | Same union in `blocked` array |

### Writing state

| Action | Endpoint | What it does |
|---|---|---|
| Toggle quarantined/failed/disabled/completed | `PATCH /api/tasks/{task}` | Writes frontmatter; no DB overlay involved |
| Reset a blocked task | `POST /api/tasks/{task}/reset` | Clears `blocked`/`repo_unavailable` DB overlay only |
| Resolve a merge conflict | `POST /api/runs/{run_id}/{task}/resolve` | Existing flow; clears `blocked` DB overlay |

### Events

| Event | When |
|---|---|
| `task_quarantined` | Manager sets `quarantined: true` in frontmatter |
| `task_blocked` | Manager sets `blocked` in DB overlay |
| `task_released` | Operator clears quarantine/failed (PATCH) or resets a blocked task |
| `queue_paused` | Two consecutive failures or retry failure |
| `queue_changed` | Any task state change |

---

## UI rendering

The detail pane derives a task's display status in priority order:

1. `completed` (frontmatter) → "Completed"
2. `quarantined` (frontmatter) → "Quarantine"
3. `failed` (frontmatter) → "Failed"
4. Active run record → run status (running, error, etc.)
5. `blockedTasks[task]` (from `/api/blocked`) → "Blocked" or other DB overlay state
6. Default → "Pending"

The status segmented control offers five mutually exclusive options: Ready,
Disabled, Quarantine, Failed, Completed. Selecting one sets the corresponding
frontmatter boolean and clears the others.

A **Reset** button appears in the detail pane when a task is in `blocked` state
(from DB overlay) and is not a merge conflict (which has its own Resolve flow).

---

## Invariants

1. **Frontmatter is authoritative for `quarantined` and `failed`.** The DB overlay never stores these states. The manager writes them via `_set_frontmatter_flag`; the detail pane reads and writes them via normal frontmatter PATCH.

2. **An unrelated save never clears system state.** Editing a task's title, body, model, or any other field does not affect `quarantined`, `failed`, or `blocked` state. Only an explicit toggle (PATCH with `quarantined: false`) or Reset clears state.

3. **`blocked` and `repo_unavailable` are DB-only.** They represent externally-caused conditions, often auto-clearing. The Reset endpoint is the explicit manual clear; PATCH never touches DB overlay.

4. **Worker and manager always agree.** Since both read frontmatter from the same `.md` file (via `_set_frontmatter_flag` which commits immediately), there is no window where one side sees stale state.
