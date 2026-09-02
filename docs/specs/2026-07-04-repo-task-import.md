# Repo task import — draining a target repo's publishing inboxes

## Problem

Repositories in the wild publish briefs into one of two **inbox roots**:

- **`.tasks/`** — the legacy root, still carried by repos in the wild;
- **`docs/tasks/`** — plain markdown briefs kept with the repo's docs, with
  **no json control file** (all metadata is frontmatter).

Each root appears in one of two layouts (sometimes both at once):

1. **Flat** — `<root>/*.md` briefs directly in the root; under `.tasks/`
   optionally with a root `config.json` holding an `order`.
2. **Queue dirs** — `<root>/<queue>/` subdirectories with `*.md` briefs (and
   under `.tasks/` a `config.json` order).

Frontmatter in either layout may carry `title`, `priority`, `draft`,
`automerge`, `disabled` — any field Nightshift understands today. External
tooling **publishes tasks this way** and will keep doing so; those briefs must
flow into Nightshift's queues without being lost and without running twice.

This is a first-class, repo-scoped task *source*, not a bolt-on: a target
repo's inbox roots are **publishing inboxes** that the queue bound to that repo
drains.

## Semantics

Import is a *move with git authority on both sides*:

- the brief becomes canonical in the content store
  (`nightshift-tasks/<queue>/`), committed like any other queue churn;
- the source file is removed from the target repo's `main` by the manager (the sole writer to `main`), committed and pushed to `origin` so the removal is never lost;
- the same commit prunes the drained stems from the inbox's `config.json` `order`, so a drained inbox never lists briefs it no longer has.

After an import a brief exists in exactly one place. Re-importing is
idempotent (see *Dedupe*).

## Scan rules (`nightshift/repo_tasks.py`)

For queue X bound to repo R, the importable set is read from the inbox trees
of R's **canonical `main`** (`main_sha`) — the same authority the
removal commits to, never the operator checkout. A checkout parked on a
feature branch neither hides main's briefs nor re-offers drained ones, and
uncommitted working-tree files are not published yet. Within that tree, for
each root (`.tasks`, then `docs/tasks` — an absent root contributes nothing):

- **Flat layout:** `<root>/*.md` directly in the root.
- **Queue-dir layout:** `<root>/X/*.md` — only the subdir matching the
  queue's label (`main` for the default queue). Other subdirs belong to other
  queues and stay untouched. Nothing else under `docs/` is an inbox.
- **Skipped everywhere:** stems starting with `_` or `.` (templates and
  evergreen/autosplit inboxes like `_todo.md` stay in the repo), files whose
  frontmatter sets `autosplit: true` (recurring sources, same reason),
  `config.json`, `runs/`, non-`.md` files.
- **Imported as-is:** brief text (frontmatter + body) is carried verbatim.
  A `disabled: true` brief arrives disabled; nothing is silently rewritten.
- **Ordering:** per root, flat files first, then the queue subdir's files, with
  `.tasks` ahead of `docs/tasks`. Each `.tasks` group is ordered by its local
  `config.json` `order` with a filename fallback; `docs/tasks` has no json
  control file, so it is always filename-ordered (a `config.json` sitting there
  orders nothing). The batch appends to the destination queue's execution
  order. Source `config.json` *settings* (`sort`, `validate`, …) are not
  imported.
- **Dedupe:** a source file whose exact text already matches a brief in the
  destination queue is flagged `duplicate` — import removes it from the repo
  without creating a second copy. This is also crash recovery: if a previous
  import copied the brief but the removal failed, the next import converges
  instead of duplicating.
- Name collisions with *different* content get a `-2` suffix (the existing
  cross-queue copy policy).

## Import flow (order of operations = never lose a task)

`POST /api/queue/repo-tasks/import?queue=X`, all manager-side, one import at
a time (imports are rare, operator-initiated actions):

1. **Scan** (read-only, rules above), then **narrow to the operator's
   selection** (`select_repo_tasks`) — the briefs they ticked in the modal, or
   the whole scanned set when the request carries no selection. Everything
   below operates on the picked subset only: unpicked briefs are neither
   copied nor removed, so the next preview offers them again.
2. **Copy into the content store:** write each non-duplicate brief to
   `nightshift-tasks/X/`, append to `order`, commit the content store
   (`nightshift: import N task(s) from R/.tasks`). *After this commit the
   tasks are durable* — everything later is cleanup.
3. **Remove from the repo** as one commit on R's `main`, run as a job on R's
   git executor (so it can never interleave with a land or sync):
   - sync `origin/main` first (best-effort) so the commit lands on the fresh
     tip;
   - `delete_produce(paths, rewrite=…)` — a third producer next to `squash_produce` / `cherry_produce`: builds the base tree minus the source files in a temporary index (never touches the working tree), `commit-tree`s it, and rides `integrate_and_push_locked` with local-CAS semantics (`LandingMode.NONE`), checkout advanced best-effort exactly like a land;
   - the `rewrite` companion edit (`prune_inbox_orders`) puts the drained inboxes' pruned `config.json` back into that same tree.
     It is computed inside the producer, from the base being produced onto, so a re-produce after a rejected push recomputes against the fresh tip rather than replaying a stale blob over a publisher's concurrent edit;
   - **push `main` to `origin` best-effort.** A failed push keeps the local
     commit and surfaces a warning in the response — never unwound; dedupe
     covers any replay.
4. Emit `queue_changed`; respond
   `{imported, deduped, removed, warning, missing}`.

A removal where none of the paths exist on `main` *and* no config needs
pruning collapses to the base commit (no empty commit) — the idempotent replay
path.
A replay whose files are already gone but whose order still lists them lands
the prune alone, which is how a crash between the two heals.

### Order pruning

The prune rule is the resulting tree: a stem stays in an inbox's `order` only
while its `<stem>.md` survives in that inbox.
So the commit drops what it drains, and in the same pass heals entries an
earlier import left behind — dead weight that anything walking the list
positionally has to step over.
Everything else is left alone: only `.tasks` inboxes carry an order at all
(`docs/tasks` sources rewrite nothing), a missing or unparsable `config.json`
is never touched, other keys and the surviving entries' relative order are
preserved, and a config that needs no change is not rewritten.

## API

- `GET /api/queue/repo-tasks?queue=X` — preview:
  `{queue, repo, available, count, tasks: [{task, title, source, priority,
  disabled, duplicate}]}`. Inert (`available: false`, empty `tasks`) when the
  queue has no bound repo, the repo is unavailable, or it has no inbox at all.
- `POST /api/queue/repo-tasks/import?queue=X` — drains the briefs the operator
  selected. Optional body `{sources: [".tasks/….md", "docs/tasks/….md", …]}`:
  absent (or an absent
  `sources`) drains the full scanned set, `[]` drains nothing. Selection keys on
  the repo-relative `source` path rather than the task name, because the name is
  ambiguous — the same stem can be published under either root, and both flat
  and under the queue's subdir; those are distinct briefs. Picked sources the re-scan no
  longer offers come back as `missing` rather than failing the batch: a stale
  preview imports what is still published and reports the rest. 404 unknown
  queue; 409 when the queue has no available repo.

Registered by `manager/api_repo_tasks.py` (the `api_playlists.py` split
pattern — `api_operator.py` is near the 1k-line budget).

## UI

The queue page's **"+ Add" menu** gains **"Import from repository…"**. It
opens a modal in the established `addfrom` pattern: fetches the preview and
lists each brief (title, source path, an "in queue" tag on duplicates).

Each row carries a tick box and the whole row is its toggle, with a
**Select all** box above the list (tri-state) — the operator controls exactly
what is imported rather than choosing between "all" and "cancel". Everything
starts ticked, since draining the whole inbox is the common case and
de-selecting the exceptions is the shorter path. The **Import** button counts
the selection (`Import 3 tasks`) and goes inert at zero. It reports progress
("Importing…") while the move runs — the removal syncs and pushes the repo's
main, which takes a few seconds. Empty state: "No importable tasks in `R`
(`.tasks/` or `docs/tasks/`)." Success refreshes the queue, reports the count (plus any `missing`
and the push warning), and **re-scans the inbox** so the briefs left unticked
are still on offer for a second pass — the modal empties out only once the
inbox is actually drained.

## Testing

Against `tests/_workspace.py` fixtures (`tests/test_repo_tasks.py`):

- scan-rule units: both roots and both layouts, `_`/`.` and autosplit skipping,
  ordering (including `docs/tasks` staying filename-ordered next to a stray
  `config.json`, and `.tasks` ahead of `docs/tasks`), dedupe flagging,
  uncommitted files ignored, neighbouring `docs/` dirs not scanned;
- selection units: picked subset in scan order (not request order), `None` vs
  `[]`, same stem in two layouts kept distinct, stale picks reported;
- partial import end to end: only the picked briefs land and are removed, the
  rest stay published and import on a second pass; an empty selection touches
  neither side (no removal commit);
- main-tree authority: with the checkout parked on a feature branch the
  preview still serves main's briefs, and drained briefs leave the preview
  even while the branch carries the files on disk;
- end-to-end API: briefs land in the content store (committed), source files
  removed from repo `main` (commit present, clean checkout advanced), order
  appended — for `docs/tasks` sources too, leaving neighbouring docs untouched;
- order pruning: drained stems leave the root and queue-dir `config.json` orders (other keys and unpicked entries intact), stems with no brief left are healed with them, `docs/tasks` imports rewrite no config, a malformed config is left byte-identical, and a replayed removal still lands the prune;
- never-lose: removal push failure → import still succeeds with a warning;
  second import after re-publish dedupes instead of duplicating;
- inert paths: queue without a repo, absent repo, no inbox at all.

## Known non-goals / future work

- **Auto-import on origin sync.** The scan module supports draining in the
  background (e.g. after `sync_main_locked` detects new briefs) behind a
  per-queue config key, if tooling volume grows. Not built now.
- The pre-existing `/api/queue/import` (add-from-playlist) endpoint referenced
  by the UI was dropped in the rebuild-in-place migration and is unrelated to
  this feature (`/api/queue/repo-tasks*` is a distinct namespace).
