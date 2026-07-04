# Repo task import — draining a target repo's `.tasks/` publishing inbox

## Problem

Repositories in the wild still carry a `.tasks/` directory in one of two
legacy layouts (sometimes both at once):

1. **Flat** — `.tasks/*.md` briefs directly in the root, optionally with a
   root `config.json` holding an `order`.
2. **Queue dirs** — `.tasks/<queue>/` subdirectories, each with a
   `config.json` (order) and `*.md` briefs.

Frontmatter in either layout may carry `title`, `priority`, `draft`,
`automerge`, `disabled` — any field Nightshift understands today. External
tooling **publishes tasks this way** and will keep doing so; those briefs must
flow into Nightshift's queues without being lost and without running twice.

This is a first-class, repo-scoped task *source*, not a bolt-on: a target
repo's `.tasks/` is a **publishing inbox** that the queue bound to that repo
drains.

## Semantics

Import is a *move with git authority on both sides*:

- the brief becomes canonical in the content store
  (`nightshift-tasks/<queue>/`), committed like any other queue churn;
- the source file is removed from the target repo's `main` by the manager
  (the sole writer to `main`), committed and pushed to `origin` so the
  removal is never lost.

After an import a brief exists in exactly one place. Re-importing is
idempotent (see *Dedupe*).

## Scan rules (`nightshift/repo_tasks.py`)

For queue X bound to repo R, the importable set from `<workspace>/R/.tasks`:

- **Flat layout:** `.tasks/*.md` directly in the root.
- **Queue-dir layout:** `.tasks/X/*.md` — only the subdir matching the
  queue's label (`main` for the default queue). Other subdirs belong to other
  queues and stay untouched.
- **Skipped everywhere:** stems starting with `_` or `.` (templates and
  evergreen/autosplit inboxes like `_todo.md` stay in the repo), files whose
  frontmatter sets `autosplit: true` (recurring sources, same reason),
  `config.json`, `runs/`, non-`.md` files.
- **Imported as-is:** brief text (frontmatter + body) is carried verbatim.
  A `disabled: true` brief arrives disabled; nothing is silently rewritten.
- **Ordering:** root files first, then the queue subdir's files; each group
  ordered by its local `config.json` `order` with a filename fallback. The
  batch appends to the destination queue's execution order. Source
  `config.json` *settings* (`sort`, `validate`, …) are not imported.
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

1. **Scan** (read-only, rules above).
2. **Copy into the content store:** write each non-duplicate brief to
   `nightshift-tasks/X/`, append to `order`, commit the content store
   (`nightshift: import N task(s) from R/.tasks`). *After this commit the
   tasks are durable* — everything later is cleanup.
3. **Remove from the repo** as one commit on R's `main`, run as a job on R's
   git executor (so it can never interleave with a land or sync):
   - sync `origin/main` first (best-effort) so the commit lands on the fresh
     tip;
   - `delete_produce(paths)` — a third producer next to `squash_produce` /
     `cherry_produce`: builds the base tree minus the source files in a
     temporary index (never touches the working tree), `commit-tree`s it, and
     rides `integrate_and_push_locked` with local-CAS semantics
     (`LandingMode.NONE`), checkout advanced best-effort exactly like a land;
   - **push `main` to `origin` best-effort.** A failed push keeps the local
     commit and surfaces a warning in the response — never unwound; dedupe
     covers any replay.
4. Emit `queue_changed`; respond `{imported, deduped, removed, warning}`.

A removal where none of the paths exist on `main` collapses to the base
commit (no empty commit) — the idempotent replay path.

## API

- `GET /api/queue/repo-tasks?queue=X` — preview:
  `{queue, repo, available, count, tasks: [{task, title, source, priority,
  disabled, duplicate}]}`. Inert (`available: false`, empty `tasks`) when the
  queue has no bound repo, the repo is unavailable, or there is no `.tasks`.
- `POST /api/queue/repo-tasks/import?queue=X` — drains the full scanned set
  (no partial selection). 404 unknown queue; 409 when the queue has no
  available repo.

Registered by `manager/api_repo_tasks.py` (the `api_playlists.py` split
pattern — `api_operator.py` is near the 1k-line budget).

## UI

The queue page's **"+ Add" menu** gains **"Import from repository…"**. It
opens a modal in the established `addfrom` pattern: fetches the preview,
lists each brief (title, source path, an "in queue" tag on duplicates), one
**Import** button. Empty state: "No importable tasks in `R/.tasks`."
Success refreshes the queue and reports the count plus the push warning, if
any.

## Testing

Against `tests/_workspace.py` fixtures (`tests/test_repo_tasks.py`):

- scan-rule units: both layouts, `_`/`.` and autosplit skipping, ordering,
  dedupe flagging;
- end-to-end API: briefs land in the content store (committed), source files
  removed from repo `main` (commit present, clean checkout advanced), order
  appended;
- never-lose: removal push failure → import still succeeds with a warning;
  second import after re-publish dedupes instead of duplicating;
- inert paths: queue without a repo, absent repo, no `.tasks`.

## Known non-goals / future work

- **Auto-import on origin sync.** The scan module supports draining in the
  background (e.g. after `sync_main_locked` detects new briefs) behind a
  per-queue config key, if tooling volume grows. Not built now.
- **Partial selection.** The modal drains the whole scanned set; per-task
  pick can be added to the POST body (`tasks: [...]`) later.
- The pre-existing `/api/queue/import` (add-from-playlist) endpoint referenced
  by the UI was dropped in the rebuild-in-place migration and is unrelated to
  this feature (`/api/queue/repo-tasks*` is a distinct namespace).
