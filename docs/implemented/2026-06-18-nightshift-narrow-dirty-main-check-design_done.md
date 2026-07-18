# Nightshift — narrow the "dirty main" land check

**Status:** Design, approved for spec review.
**Area:** `tools/nightshift/engine.py` (plus `run_local.py`, tests, config docs).
**Author:** pairing session, 2026-06-18.
**Scope decision:** Implement Layers 1–4 for the **single-executor** model now.
The landing-lock deltas in "Concurrency compatibility" are documented invariants
only — they are **out of scope here** and fold into the concurrent multi-queue
effort.

## Problem

Nightshift refuses to land tasks when local `main` has uncommitted changes. The
guard lives in `squash_to_main`:

```
blockers = _tracked_changes(root)        # ALL tracked modifications
if blockers:
    return None, "main has uncommitted changes — commit or stash them …", True
```

`_tracked_changes` flags **every** tracked modification in the working tree. In
normal operation `.tasks/` is routinely dirty mid-run, from two sources:

1. **UI queue edits.** Adding or editing a task writes `.tasks/<queue>/<task>.md`.
   The operator can do this any time, and the next task's squash trips on it.
2. **Live playlist run records.** A playlist's `runs/run.json` is *tracked*
   (the `.gitignore` only covers `.tasks/runs`), and it is rewritten while the
   run is in flight — so it shows up as a tracked modification.

`commit_queue_state(root)` only snapshots `.tasks/` **once**, at the top of
`run_queue`. Anything that goes dirty after that (a mid-run task add, the live
run record) is caught by the per-task `squash_to_main` precheck, which then
reports the task as a `recoverable=True` failure and parks its worktree. This is
the dominant source of nightshift "failures" in day-to-day use.

A second, rarer case is genuine operator code edits in `main` while nightshift
runs. Today these also block every land.

## Goals

- Adding/editing tasks while a run is in progress must never block a land,
  regardless of when the next task happens to finish.
- A task added to a queue **while that queue is running** must be picked up and
  executed within the *current* run (appended into its configured position),
  not deferred to the next repeat cycle.
- Live playlist `runs/`/`logs/` records must never block a land.
- Genuine operator code WIP in `main` must not refuse the land either: it is set
  aside for the brief land critical section and restored afterward, never lost
  and never swept into a task's squash commit.
- The operator can disable the code-WIP auto-stash, **per playlist**.

## Non-goals

- Concurrent multi-queue execution / landing lock (see
  `docs/analysis/nightshift-multi-queue-concurrency.md`). This change does not
  implement it, but is explicitly designed to compose with it — see the
  "Concurrency compatibility" section for the invariants and the (additive)
  adjustments required once the landing lock exists.
- Changing what a worktree is cut from. Worktrees still branch from committed
  `HEAD`; operator working-tree dirt is (as today) not part of the worktree.
- Changing the content-conflict path (`recoverable=False`). That still routes to
  the resolver / manual resolution unchanged.

## Design

Four layers, smallest blast radius first. Layers 1–3 fix the dirty-main refusal;
Layer 4 makes mid-run additions execute in the current run. They are independent
and can ship in separate PRs.

### Layer 1 — Queue state never blocks a land (always on)

Introduce a landing-blocker helper that ignores everything under `.tasks/`:

```python
def _landing_blockers(root: Path) -> list[str]:
    """Tracked changes that should block a squash-merge: everything from
    `_tracked_changes` EXCEPT paths under `.tasks/`. Queue definition edits,
    live run records, and logs are the operator's queue state, not code that can
    conflict with a worker's squash — the worker never edits the parent queue's
    `.tasks/`, so leaving them dirty is safe for `git merge --squash`."""
```

Path classification parses each porcelain line's path (handling the
`R old -> new` rename form by taking the destination) and excludes any path
equal to `.tasks` or under `.tasks/`.

`squash_to_main`:

1. Calls `commit_queue_state(root)` first, so genuine queue-*definition* edits
   land in their own snapshot commit (consistent with today's run-start
   snapshot). Live `runs/`/`logs/` are excluded from the snapshot and remain as
   working-tree state.
2. Computes `_landing_blockers(root)` instead of `_tracked_changes(root)`.

Because the worker never modifies the parent queue's `.tasks/`, the squash never
touches those paths, so any residual `.tasks/` dirt (a mid-run add that arrived
after the snapshot, the live run record) survives the merge untouched and never
blocks it.

### Layer 2 — Operator code WIP is set aside, not refused (default on, per-playlist toggle)

When, after excluding `.tasks/`, real code remains in the working tree,
`squash_to_main` sets it aside for the land critical section rather than
refusing:

```
commit_queue_state(root)                  # .tasks/ → committed / out of the way
wip = _stash_operator_work(root)          # only if autostash enabled AND blockers remain
try:
    git merge --squash <branch>
    git commit -m "task: <title>"
finally:
    if wip:
        _restore_operator_work(root)      # git stash pop
```

Details:

- **Stash, not WIP-commit.** `git stash push` (tracked changes only; untracked
  files do not block `merge --squash` and are left in place) tagged with a
  recognizable message, e.g. `nightshift-autostash`. This returns the tree to
  its exact prior state and never pollutes `main` history. A WIP commit was
  considered and rejected: task squashes land on top of it, burying the
  operator's "work in progress" commit and making it awkward to get back to
  uncommitted.
- **Scoped to the land step — never the whole run.** The stash/pop lives
  *inside* `squash_to_main`, so the window where the operator's work is set aside
  is the few seconds around a single task's merge+commit. While a worker runs
  (the long phase) and between tasks, the working tree is fully the operator's:
  they may edit and even commit on `main` throughout the run. After each land
  the operator's checkout holds the new task commit with their WIP restored on
  top — the correct result.
  - **Rejected alternative — run-boundary stash.** Stashing once at the start of
    `run_queue` and popping at the end would force `main` to stay quiet for the
    entire run (all tasks). That is unacceptable: the operator must be able to
    keep working on `main` while the queue runs. Per-land stashing is therefore
    required, not run-boundary.
- **Pop-conflict safety.** `git stash pop` only conflicts if the operator's WIP
  overlaps the file the task just landed. On a conflicting pop we **keep** the
  stash entry (work is never lost) and return a clear, non-recoverable failure
  detail naming the situation, leaving the conflict markers for the operator.
  (The land itself already committed; this is a post-land restore problem.)
- **Optional hardening (include if cheap):** at run start, detect a leftover
  `nightshift-autostash` stash entry and restore it before proceeding, covering
  a prior crash mid-land.

### Layer 3 — Startup gate parity (CLI)

`check_preconditions` (CLI startup, `run_local.py`) currently `sys.exit`s on any
dirty tree. Narrow it consistently:

- Ignore `.tasks/` (reuse the Layer 1 classification).
- For remaining code WIP, downgrade from hard-exit to a printed notice that the
  work will be set aside during landings (Layer 2 handles it at land time). If
  autostash is disabled for the main queue, keep the hard-exit so behavior is
  coherent with the toggle.

The server path does not call `check_preconditions`, so no change is needed
there beyond Layers 1–2.

### Layer 4 — Mid-run task additions execute in the current run

Today `run_queue` consumes a **frozen** `tasks` list, and `commit_queue_state`
runs **once** at the top of the run. Two consequences: a task added to the queue
mid-run (a) isn't in any later worktree (worktrees branch from committed `HEAD`,
and the new file is uncommitted), and (b) isn't iterated at all — it waits for
the next repeat cycle's `_build_tasks` rebuild. We change both.

**Snapshot per task, not per run.** Move the `commit_queue_state(root)` call to
the top of each task iteration in `run_queue` (immediately before `run_task` cuts
the worktree). This commits any newly-added/edited task definition to `HEAD`
*before* the worktree is cut, so the worker is handed a task file that actually
exists in its checkout. It also keeps `.tasks/` clean going into the squash, so
Layer 1's exclusion is belt-and-suspenders in the `run_queue` path. The snapshot
inside `squash_to_main` (Layer 1) stays, because `recover_task` / `resolve_task`
call it outside `run_queue`.

**Re-scan the live queue each iteration.** Replace the `for task in tasks:` loop
with a drain loop seeded by the passed `tasks` (which already carries the
autosplit-spawned subtasks and any `start_task` slice from `build_task_list`),
then folds in tasks that appear later:

```
attempted: set[str] = set()
order: list[str] = list(tasks)            # seed: autosplit-spawned + start_task slice
while not (controller and controller.stopped):
    # fold in tasks that appeared since we started, in configured order
    for t in live_ordered_queue(root, tasks_rel):
        if t not in order and t not in attempted:
            order.append(t)
    pending = [t for t in order if t not in attempted]
    if not pending:
        break
    task = pending[0]
    attempted.add(task)
    run_task(root, task, …)
```

- `live_ordered_queue` is a **read-only** ordered scan of `<tasks_rel>/*.md`
  (configured order via `order_stems`, skipping `disabled` and `autosplit`
  files) — i.e. `build_task_list("all")` **without** the autosplit spawn/commit
  side effects. Factor that read-only core out so both share it. It is used only
  to *detect additions*; the seed (`tasks`) still drives the initial set so
  autosplit expansion (which the seed already performed) is preserved.
- When `follow_queue` is false the loop degenerates to draining exactly the seed
  (`live_ordered_queue` is not consulted), matching today's behavior.
- **`attempted` is the loop guard and the termination guarantee.** Each task runs
  at most once per run. Completed regular tasks also leave the queue (worker
  `git rm`s them, landed by the squash), so they fall out of `live_ordered_queue`
  too. Evergreen and failed tasks remain on disk but are in `attempted`, so they
  do not re-run within the same run (they re-run next cycle, as today). The set
  of files is finite and each is attempted once ⇒ the loop always terminates.
- **Ordering.** A mid-run addition slots into its configured position among the
  not-yet-attempted tasks; it cannot preempt the currently-running task, but runs
  before later not-yet-started tasks if it sorts ahead of them.
- **Gating (`follow_queue`).** This drain behavior is for queue/"all"/repeat runs.
  A **oneshot** run of a single named task must not start draining the whole
  queue, so `run_queue` takes `follow_queue: bool` (default preserves today's
  fixed-list behavior). The server sets it for all/repeat runs; oneshot leaves it
  off and uses the existing fixed-list path. The CLI sets it for `all`.

This layer is independent of Layers 1–3 (it can ship separately), but together
they deliver the full experience: add a task while a queue runs, and it lands in
this run without blocking anything.

## Concurrency compatibility (multiple executors, one physical repo)

This spec targets today's single-executor model (one run at a time; no landing
lock). All queues land into the same local `main` through the same working tree,
index, **and** the repo-global stash stack. The operations this spec adds are
shared-state mutations, so under two concurrent executors (e.g. a `nightshift`
playlist and a `longitude` queue running together) they are **not** safe as
written:

- `commit_queue_state(root)` — `git add`/`commit` against the shared index/HEAD;
  concurrent calls interleave.
- `git stash push/pop` (Layer 2) — the stash stack is **repo-global and LIFO**.
  A second executor — or a human running `git stash` — can cause a pop to take
  the wrong entry, and there is a window where operator WIP is absent from the
  tree. This is a *new* shared-state dependency this spec introduces beyond the
  merge.
- `git merge --squash` + commit (existing) — shared index/tree; concurrent
  merges corrupt each other (analysis §2.2).

**These compose with your multi-queue work if the whole land critical section
runs under one landing lock.** The required adjustments — none of which change
single-executor behavior — are:

1. **Lock every root-repo write, not just the land step.** Any mutation of the
   root index/HEAD is the critical section — both the squash *and* Layer 4's
   per-task `commit_queue_state` snapshot. They must take the same single
   process-wide `threading.Lock` (server) **plus** a cross-process file lock
   (generalize the existing `acquire_lock` from whole-run to per-write) so CLI
   and server can't write the root repo at once. This matters specifically for
   Layer 4: the per-task snapshot is a `git add`/`commit` against the shared
   index/HEAD that happens *before* `run_task` cuts the worktree — i.e. outside
   the squash. Two concurrent snapshots contend on `index.lock` and one commit
   can sweep in the other's staged changes; subtree-scoping (fix #3) removes only
   the *content* collision, not the index contention. So the snapshot takes the
   landing lock too (or a short dedicated repo-index lock that the squash also
   honors). Inside the lock the stash strategy is safe — no other executor
   touches the index, HEAD, or the stash while we hold it.
2. **Make the set-aside stack-independent — with an explicit clean step.** Even
   under the lock a human could `git stash` mid-land, so don't use the LIFO
   stack. Sequence:
   - `sha=$(git stash create)` — captures tracked WIP as a commit object
     **without** touching the stack **and without reverting the working tree**.
   - **Explicitly clean** the tracked WIP from the tree now (`git reset --hard
     HEAD`, or `git checkout -- <captured paths>`) — `git stash create` does not
     do this, unlike `push`. The merge needs a clean tree.
   - `merge --squash` + commit.
   - `git stash apply <sha>` to restore the WIP on top.
   Optionally `git stash store` the sha under the `nightshift-autostash` message
   so a crash between create and apply leaves it findable for startup
   reconciliation. This decouples the set-aside from any other executor's or
   human's stash activity.
3. **Queue-scope `commit_queue_state` and add rebase-before-land.** Snapshot only
   the executor's own `.tasks/<playlist>/` subtree (analysis §4.1) so concurrent
   snapshots don't *content*-collide (index contention is handled by fix #1's
   lock); and because `main` advances between worktree-cut and land far more
   often under concurrency, the lock holder rebases the task branch onto current
   `main` before squashing (reuse `_rebase_onto_main` / `resolve_task`).
   Single-executor doesn't need the rebase — the 3-way `merge --squash` + resolve
   path already handles advances — but concurrency does.

Concurrency-variant tests to add when the lock lands: (a) two executors landing
back-to-back each round-trip the *same* operator WIP correctly (stash sha →
clean → merge → apply, sequentially); (b) a per-task snapshot from executor A
and a squash from executor B serialize rather than corrupting `index.lock` or
cross-committing staged changes.

**Net:** ship this spec for the single-executor model now; it is designed to live
*inside* the landing lock your multi-queue effort introduces. When that lock
lands, the only deltas to this spec are (1) wrap **every root-repo write** — the
per-task snapshot *and* the squash — in it, (2) switch the stash to
`create`/explicit-clean/`apply <sha>`, and (3) queue-scope the snapshot +
rebase-before-land — all additive.

## Config

New key, resolved through the existing `resolve_config(root, tasks_rel)` layering
(shipped defaults → `.tasks/config.json` → playlist `config.json`), so it is
overridable **per playlist**:

```jsonc
{
  // default true; set false to make a queue refuse to land while main has
  // uncommitted code (the pre-change behavior).
  "autostash_operator_work": true
}
```

`squash_to_main` gains the resolved boolean. Threading: the flag is read by each
caller via `resolve_config(root, tasks_rel)` and passed in, since `squash_to_main`
is queue-agnostic today. Call sites that pass it: `run_task`, `resolve_task` /
`_agent_resolve` (have `config`), `recover_task` (resolve from `.tasks` or the
task's queue). Default the parameter to `True` so existing call sites and tests
that don't care keep working.

## Interfaces touched

- `engine._tracked_changes` — unchanged; still used where "any tracked change"
  is the right question.
- `engine._landing_blockers(root)` — **new.**
- `engine._stash_operator_work(root) -> bool` / `engine._restore_operator_work(root) -> str | None`
  — **new** (return whether a stash was made / a conflict detail).
- `engine.squash_to_main(root, task, title, *, autostash: bool = True)` — new
  keyword; precheck swapped to `_landing_blockers`; stash/restore wrapper added.
- `engine.commit_queue_state` — unchanged internally, but **called per task**
  inside `run_queue` (Layer 4) rather than once at run start.
- `engine.run_queue(…, follow_queue: bool = False)` — new keyword; drain-loop
  with live re-scan when `follow_queue` is set, fixed-list path otherwise.
- `engine.live_ordered_queue(root, tasks_rel)` — **new** read-only ordered scan
  (the side-effect-free core of `build_task_list("all")`), shared by both.
- `server.player._run_loop` / `_build_tasks` — pass `follow_queue=True` for
  all/repeat runs; oneshot stays fixed-list.
- `run_local.main` — pass `follow_queue=True` for the `all` target.
- `run_local.check_preconditions` — narrowed per Layer 3.

## Error / result semantics

- `.tasks/`-only dirt → land succeeds (no stash needed).
- Code WIP + autostash on → land succeeds; tree restored identically.
- Code WIP + autostash off → unchanged `recoverable=True` "main has uncommitted
  changes" blocker (now listing only non-`.tasks/` paths).
- Pop conflict after a successful land → `recoverable=False` failure detail; the
  squash already landed, stash preserved for manual restore.

## Testing

- `.tasks/` dirty (a task-brief edit **and** a live playlist `runs/run.json`) →
  `squash_to_main` lands; queue defs committed, run record left dirty/untouched.
- Code WIP (dirty `config.json`) + autostash on → lands; WIP byte-identical
  after; no `nightshift-autostash` left in `git stash list`.
- Code WIP + autostash off → blocks `recoverable=True`, listing only the code
  path, not `.tasks/`.
- WIP overlapping the landed file → pop conflict surfaced, stash entry preserved.
- Rewrite `test_squash_to_main_refuses_dirty_main` to assert the new
  stash-and-land default (and add an autostash-off variant for the old behavior).
- `check_preconditions`: `.tasks/` dirt does not exit; code WIP prints a notice
  (autostash on) / exits (autostash off).
- **Layer 4 — mid-run addition (`follow_queue=True`):** seed queue `[a]`; while
  `a` runs, a new `b.md` appears in the queue dir → the drain loop picks up `b`
  in the same run and `attempted == {a, b}`.
- **Layer 4 — termination / no re-run:** an evergreen task and a failed task that
  remain on disk are each attempted exactly once per run (no infinite loop); a
  completed regular task is `git rm`-ed and absent from the next scan.
- **Layer 4 — oneshot unaffected (`follow_queue=False`):** running a single named
  task does not drain sibling tasks added during the run.
- **Layer 4 — worktree sees the new file:** the per-task `commit_queue_state`
  means `b`'s worktree (cut from `HEAD`) contains `b.md`.

