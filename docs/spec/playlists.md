# Nightshift — Playlists Specification

**Subject:** Playlists — directory-backed alternate queues under `.tasks/<name>/`, the config inheritance that lets a playlist override only what it needs, and how the active-queue context threads through the player, queue, history, and UI.
**Status:** Descriptive — documents the feature **as implemented** in `tools/nightshift/`. Where prose and code disagree, the code governs.
**Primary sources:** `tools/nightshift/playlists.py`, `tools/nightshift/spawn_daily.py` (`resolve_config`), `tools/nightshift/engine.py`, `tools/nightshift/events.py`, `tools/nightshift/server/{app.py,player.py}`, `tools/nightshift/ui/{index.html,app.js,style.css}`.

---

## 0. The one idea

The media-player model has a single implicit queue: the `.tasks/` directory. A **playlist**
generalises that into **multiple, fully self-contained queues**. Each playlist is its own
directory under `.tasks/`:

```
.tasks/<name>/
    *.md          the playlist's own task files
    config.json   the playlist's queue order (+ any setting overrides)
    runs/         this playlist's run history
```

Unlike the old reference-based "workflows", a playlist does **not** point at tasks in the
main queue — it owns its task files outright. Switching to a playlist makes it the
**active queue**: the transport (play/pause/stop/skip), the Queue screen, task
create/delete, and History all operate on the active queue. A **Home** action returns to
the main `.tasks/` queue.

---

## 1. Identity & naming (`playlists.py`)

A playlist's name doubles as its on-disk directory name, so it must be a safe slug.

- `slugify_name(name)` lowercases, maps `[^a-z0-9]+` → `-`, and strips leading/trailing
  dashes.
- `is_valid_name(name)` accepts `^[a-z0-9][a-z0-9-]*$` and rejects the reserved name
  `runs` (the main queue's run-store dir). This is also the path-traversal guard — `..`,
  `/`, and absolute paths can never be valid names.
- `tasks_rel(name)` → `.tasks` for the main queue (`None`), else `.tasks/<name>`.
- `runs_rel(name)` → `<tasks_rel>/runs`.

A directory under `.tasks/` is recognised as a playlist **iff it contains a
`config.json`** — this is what distinguishes a playlist from `runs/` or any incidental
sub-directory.

---

## 2. Config inheritance (`spawn_daily.resolve_config`)

Runner configuration resolves in three deep-merged layers, lowest priority first:

```
tools/nightshift/config.json     shipped defaults (security keys, e.g. forbidden_paths)
        ↓  overridden by
.tasks/config.json               system-wide operator overrides (validate, automerge, …)
        ↓  overridden by
.tasks/<name>/config.json        per-playlist overrides (typically just `order`)
```

- `resolve_config(root, tasks_rel)` returns the merged config for a queue. For the main
  queue (`tasks_rel == ".tasks"`) it merges shipped ← `.tasks/config.json`. For a playlist
  it adds the playlist's `config.json` on top.
- `_deep_merge` merges nested dicts recursively; scalars and lists are replaced wholesale.
- A **freshly created** playlist's `config.json` holds only `{"order": []}` — everything
  else (model, automerge, draft, diff caps, evergreen rules, and `validate`) is inherited.

**Per-playlist validate:** the validate command is the resolved `validate` key
(default `"just validate"`, `shlex.split` at run time). The system-wide default lives in
`.tasks/config.json`; a playlist overrides it, e.g. the Nightshift playlist sets
`"validate": "just validate-nightshift"`.
The active queue's `validate` command is editable from the settings pane:
`GET /api/settings` surfaces it (resolved, with the engine default as fallback) and
`PUT /api/settings` persists an edit to that queue's `config.json` (sibling keys preserved),
so the field always edits whichever queue is active.

---

## 3. Threading the active queue (`engine.py`)

Engine queue operations take a `tasks_rel: str = ".tasks"` argument so they can target a
playlist directory; the default preserves main-queue behaviour:

`load_order`/`save_order`, `order_stems`/`reorder_queue`, `build_task_list`,
`create_task`/`delete_task`/`read_task`/`list_queue`, `build_prompt`, and `run_task`.

- `list_queue` lists only top-level `<tasks_rel>/*.md` (sub-directories skipped), so the
  main queue never shows playlist tasks.
- `build_prompt` emits `Your task file is: <tasks_rel>/<task>.md`, matching the worker's
  cwd.
- `build_task_list`'s autosplit dispatch (spawning daily subtasks, committing the queue)
  applies **only** to the main `.tasks` queue; a playlist is a plain ordered set of its
  own `*.md` files.
- Git worktrees still branch off the repo root unchanged — only the tasks/runs paths move.

---

## 4. Stop that actually stops (`engine.py`, `backends.py`, `player.py`)

Stopping is honoured mid-worker and mid-validate, not just at task boundaries:

- `run_interruptible(cmd, *, cwd, env, should_abort)` runs a command via
  `Popen(..., start_new_session=True)`, polling `should_abort()` every 0.5s. On abort it
  sends `SIGTERM` then `SIGKILL` to the **process group** and returns a non-zero result.
- `run_task` runs the resolved `validate` command through `run_interruptible` and checks
  for an abort at phase boundaries (after the worker / before validate, after validate /
  before commit), emitting an `aborted` `TASK_RESULT` and returning early.
- `_attempt_repair` takes `should_abort` and uses the same interruptible validate.
- `backends._stream_subprocess` launches the worker in its own session and a watcher
  thread escalates `terminate()` → process-group kill when an abort is requested.
- `Player.stop()` flips state to `idle` and clears now-playing **immediately** (so the UI
  reflects the stop even while the worker winds down) and signals the controller, which is
  also honoured during the inter-task / repeat-interval sleep.

---

## 5. Active-queue state (`server/player.py`)

`Player` tracks `_active_playlist: str | None` (`None` = main queue):

- `active_playlist()` / `tasks_rel()` expose the current context; `state()` includes
  `active_playlist`.
- `set_active(name|None)` is **refused while a run is in progress**
  (`{"ok": False, "error": …}`); otherwise it switches the context, **rebuilds
  `self.store`** to the queue's own `runs/` dir, and clears the play cursor/now-playing.
- `_build_tasks` and `_run_loop` use `tasks_rel()` and thread `playlist` provenance into
  `store.start(...)` and `run_queue(...)`.

---

## 6. HTTP API (`server/app.py`)

Every queue/task/transport/runs route operates on the **active** context
(`player.tasks_rel()` and `player.store`), so they implicitly follow the selected
playlist.

| Method & path | Body | Success | Errors |
|---|---|---|---|
| `GET /api/active` | — | `200 {active_playlist}` | — |
| `POST /api/active` | `{playlist: name\|null}` | `200 {ok, active_playlist}` | `404` unknown playlist, `409` run in progress |
| `GET /api/playlists` | — | `200 [{name, task_count}]` | — |
| `POST /api/playlists` | `{name}` | `201 {name, task_count}` | `400` empty/invalid name, `409` already exists |
| `DELETE /api/playlists/{name}` | — | `200 {name, deleted: true}` | `404` not found, `409` active & running |

- `null` playlist = Home / main queue.
- Deleting the active (idle) playlist drops back to the main queue first.
- `GET /api/state` includes `active_playlist`; the SSE stream reads `player.store` afresh
  each tick so events follow the active queue.

---

## 7. History provenance (`events.py`)

Each run records the playlist it ran under so History can tag it:

- `RunStore(root, runs_rel=".tasks/runs")` — base is the active queue's `runs/` dir.
- `RunStore.start(launched_by, *, playlist=None)` writes `playlist` into `run.json`
  metadata; `_read_run` surfaces it in the run summary. Main-queue runs carry `null`.
- `run.json` lives at `<runs_rel>/<run-id>/run.json` (with `events.jsonl` and per-task
  `<task>.log` siblings), so each playlist keeps its own isolated history.

---

## 8. UI (`ui/index.html`, `ui/app.js`, `ui/style.css`)

**Layout (iPhone-Music style):** global actions (`+`, settings, theme) stay in the top
bar. A fixed bottom region stacks two strips with a gap: a **mini-player** (now-playing
label + play/pause/stop/skip + mode toggles) above the **tab icons**
(Home · Now · Queue · Playlists · History). The strips render at all widths.

**Now screen** (`renderNow`): single column ordered Now Executing → spacer → Up Next
(n queued) → spacer → embedded Queue. On a wide viewport **and** when a queue item is
selected (`state.selectedTask`), a right-hand detail column appears (`body.has-detail`);
on narrow widths a selection opens the detail modal instead.

**Playlists screen** (`renderPlaylists`): lists every selectable queue.
The main `.tasks/` queue is shown first as the **library** row (tagged "main queue"); selecting it POSTs `/api/active {playlist: null}` — exactly what the **Home** tab does — so the main queue is reachable as a playlist.
Real playlists follow; selecting one POSTs `/api/active` then navigates to the Queue.
The library count comes from `state.queue` when the main queue is active, else from `/api/main/tasks`.
Each row carries a leading play-state spinner — the same `.q-spinner` the Queue screen shows on its now-playing row — which animates when that queue has a live run, so running playlists are visible from the Playlists screen.
The library row carries the active marker, the running spinner, and the `›` chevron (which drops into the main queue's Queue) like any playlist, but it has no delete control — the main queue can't be deleted.
`+ New` creates a playlist by name. A small detail offers delete.
The active playlist name is surfaced in the brand/mini-player (`updatePlayerState`).

---

## 9. Invariants

1. **A playlist owns its tasks.** Its `*.md`, `config.json`, and `runs/` live under
   `.tasks/<name>/`; the main queue never lists them.
2. **Inherit, don't copy.** A playlist's `config.json` overrides only what it sets;
   everything else resolves from `.tasks/config.json` then the shipped defaults.
3. **The active queue is the whole context.** Transport, Queue, task CRUD, and History all
   follow it; switching is refused mid-run.
4. **Names are slugs.** Validity doubles as the traversal guard.
5. **Stop is prompt.** Mid-validate and mid-worker aborts kill the process group; the UI
   goes idle immediately.

---

## 10. Out of scope (not implemented)

- No nesting (a playlist cannot contain another playlist).
- No per-task overrides beyond the task file's own frontmatter.
- The nightly scheduler still runs the discovered main `.tasks/` queue, not playlists.
- The CLI runner has no playlist flag — playlists are a server/UI feature.
