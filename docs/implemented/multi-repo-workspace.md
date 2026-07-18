# Nightshift — Multi-Repo Workspace Specification

**Subject:** Replace the single `--root` target repo with a `--workspace` that parents many git repos, bind each queue to a default target repo (overridable per task), move task briefs into a dedicated `nightshift-tasks` content-store repo, and degrade gracefully (pause + warn + rescan) when a target repo is not yet present.
**Status:** Proposed — design for unimplemented work. Supersedes the single-`--root` model. Where this doc and code disagree once implemented, the code governs and this doc should be updated.
**Primary sources (to change):** `src/nightshift/engine.py`, `src/nightshift/playlists.py`, `src/nightshift/spawn_daily.py` (`resolve_config`), `src/nightshift/manager/{app.py,store.py}`, `src/nightshift/worker/{config.py,execute.py}`, `src/nightshift/assets/prompts/{nightshift-local.md,nightshift-resolve.md}`, `src/nightshift/assets/migrations/`, `src/nightshift/assets/ui/`, `justfile`.

---

## 0. The one idea

Today a Nightshift manager (and worker) operates on exactly **one** git repo, passed as `--root`. That single `root` is overloaded: it is both *where task briefs live* (`<root>/.tasks/`) and *where git operations run* (`worktree add`, `base_ref`, squash-land).

This design splits those two responsibilities and lifts the unit of operation from **one repo** to **a workspace of many repos**:

- **`--workspace`** (e.g. `~/workspaces`) — a directory that parents every git repo Nightshift may touch. Both manager and worker are initialised against it.
- **`tasks_root`** = `<workspace>/nightshift-tasks` — a dedicated content-store repo holding briefs and queue config.
- **`repo_root`** = `<workspace>/<repo>` — the target repo, resolved **per task** from the queue's default `repo` (or a task-level override). All git operations run here.

A **queue is aligned with a repo**: the queue's config names a default target repo; an individual task may override it (rare). Every referenced repo must be a direct child of the workspace, or the task is **paused** (not failed) until the repo appears.

### Cross-cutting rule: workspace-relative paths only

Every persisted or transmitted repo/brief reference is **`--workspace`-relative** (a bare child name like `longitude`, or a workspace-relative path like `nightshift-tasks/main/10.foo.md`). Absolute paths are materialised **only transiently** at the moment of a filesystem or git call — never stored in `config.json`, the work order, or the database. This keeps briefs, queue config, and run history portable across machines and prevents host paths from leaking into the content store.

---

## 1. Workspace & the two-root split (`engine.py`, entry points)

The core refactor threads two roots wherever the engine currently threads one `root`:

| Concept | Today | Proposed |
|---|---|---|
| Where briefs/config live | `<root>/.tasks/...` | `<workspace>/nightshift-tasks/<queue>/...` (`tasks_root`) |
| Where git ops run | `<root>` | `<workspace>/<repo>` (`repo_root`, per task) |
| Worktree location | `<root>/.worktrees/...` | `<workspace>/.worktrees/<repo>/<task>/` (outside any target repo) |
| `base_ref` | `canonical_head(root)` | `canonical_head(repo_root)` |
| Landing | squash-merge into `<root>` main | squash-merge into `<repo_root>` main |

- Functions that today take `root: Path` and a `tasks_rel: str` split into a `tasks_root: Path` (queue file ops) and a `repo_root: Path` (git ops). `tasks_rel` becomes relative to `tasks_root`.
- **Worktrees move out of the target repo.** Placing `.worktrees/` inside an arbitrary target repo would leave it as untracked clutter. With a workspace we put worktrees under a workspace-level `<workspace>/.worktrees/<repo>/<task>/`, so target repos stay pristine. The worktree is still a checkout of `repo_root` via `git -C <repo_root> worktree add <path>`.
- The current **pre-run `git add .tasks/` snapshot** in the target repo is **removed**; queue state is captured by the `nightshift-tasks` commit lifecycle (§6) instead. The target repo only ever receives the implementation squash commit.

---

## 2. Repo addressing & resolution (`engine.py`, `manager/`)

A repo is referenced by **bare child name** and resolved against the workspace.

- Reference form: `repo: "longitude"` → `repo_root = <workspace>/longitude`.
- **Valid** iff the resolved path is a **direct child of `--workspace`** and **contains `.git`**.
- Resolution order for a task: task frontmatter `repo:` → queue `config.json` `repo` → (neither set) authoring error on the queue.
- Two distinct failure classes:
  - **Malformed / unsafe reference** — anything that is not a bare child slug (`^[a-z0-9][a-z0-9-]*$`): a path, `..`, `/`, or absolute path. This is an **authoring-time config error** surfaced where the queue/task is edited; it is the path-traversal guard and is never dispatched. (Reuses the `is_valid_name` slug guard already in `playlists.py`.)
  - **Well-formed name, repo absent or missing `.git`** — not an error: the task is **paused** (`repo_unavailable`), see §4.
- The tasks-store repo (`nightshift-tasks`) is itself resolved the same way (a workspace child); its name is configurable (default `nightshift-tasks`).

---

## 3. The `nightshift-tasks` content store (`playlists.py`, `spawn_daily.py`)

`nightshift-tasks` is a dedicated git repo, a sibling of the target repos under the workspace. Keeping briefs out of the engine repo keeps churn low even when Nightshift works on itself.

**Layout — queues hoisted to the repo root (no literal `.tasks/`):**

```
nightshift-tasks/
    main/                 the default queue
        *.md              task briefs
        config.json       { "repo": "<default-target>", "order": [...], ...overrides }
    <queue>/              additional queues (former "playlists")
        *.md
        config.json
```

- This **removes today's main-vs-playlist asymmetry**: every queue is a top-level directory; the default queue is `main`. `tasks_rel(name)` returns the queue directory name (default `"main"`); `queue_from_tasks_rel` is its inverse. There is no implicit root-level task set.
- Content store holds **briefs (`*.md`) and queue `config.json` only**. Run metrics/events live in Postgres; logs stay local and gitignored. Nothing unbounded is committed.
- **Config inheritance** (`resolve_config`) keeps its layered model, rebased onto `tasks_root`:
  - shipped defaults (package `config.json` assets) ← per-queue `config.json`.
  - The system-wide layer that today lives at `<root>/.tasks/config.json` becomes a workspace-level `nightshift-tasks/config.json` (optional), then the per-queue `config.json` on top.
- New per-queue key: **`repo`** — the queue's default target repo (workspace-relative child name).
- Per-task override: a task's frontmatter may set **`repo:`**.

---

## 4. Repo availability: discovery, pause, warn, rescan (`manager/`)

The manager maintains a **known-repos set**: the direct children of `--workspace` that contain `.git`, computed on manager start.

- **Pause, don't fail.** When a task's resolved repo is not in the known-repos set, the task is moved to a dedicated, **auto-resumable** state rendered as **"Paused"** with reason `repo_unavailable`. No run is started; no `error`/failed run is recorded. This is distinct from:
  - the per-task **`blocked`** state (merge-conflict holds needing agent/human resolution), and
  - the **runner transport** pause (the whole run loop).
- **One warning per queue.** The manager emits a single warning per *queue* whose default `repo` is unavailable (deduped — not one per task).
- **Rescan.** A manager-config action re-scans the workspace and **auto-resumes** any `repo_unavailable` task whose repo is now present (state → `queued`). The known-repos set is also recomputed.
- Operator resolution flow: `git clone <repo>` into the workspace → press **Rescan** → paused tasks return to `queued`.

---

## 5. Per-task data flow (`manager/app.py`, `worker/execute.py`, prompts)

1. **Resolve** the target repo (§2). If unavailable → pause (§4) and stop.
2. **Build work order.** The manager reads the brief from `tasks_root` and **embeds the brief body** into the work order, alongside `task`, the workspace-relative `task_path`, and the resolved workspace-relative `repo`. No absolute paths cross the wire.
3. **Deliver.** The worker writes the embedded body to a run-scratch file and points `$TASK_FILE` at it. The brief never enters the target worktree's tracked tree, so the agent cannot accidentally commit it.
4. **Execute & land.** The worktree is a checkout of `repo_root`; `base_ref = canonical_head(repo_root)`; the landed squash commit is **implementation-only** — the agent no longer `git rm`s the brief.
5. **Completion.** The manager removes the brief from `nightshift-tasks` and commits there (§6). Run metrics/events are written to Postgres as today (now carrying the resolved `repo`, §8).
6. **Blocked / conflict.** The agent no longer writes `.BLOCKED` into a target repo. The worker reports a `blocked` status + reason; the manager records it (DB `blocked` state, optional `.BLOCKED` marker inside `nightshift-tasks`). The `nightshift-local.md` and `nightshift-resolve.md` charters are updated to drop the `git rm "$TASK_FILE"` and `.tasks/$TASK.BLOCKED`-in-target instructions and to treat `$TASK_FILE` as a read-only scratch path.

---

## 6. `nightshift-tasks` git lifecycle (`manager/`)

The manager owns the content store as a git repo and commits brief/config churn there — **local commits only, no remote required, no auto-push.**

- task created → write `*.md` + `git add` + commit.
- task completed → `git rm` the brief + commit.
- queue `config.json` edited → commit.
- Pushing is a **separable future**: if a remote is configured, an opt-in `tasks.auto_push` (default off) or a `just sync-tasks` recipe can push. Nothing depends on a remote existing.
- Single-manager assumption: two managers = two independent workspaces, each with its own `nightshift-tasks`.

---

## 7. CLI & config changes (entry points, `worker/config.py`, `justfile`)

- `--root` → **`--workspace`** on the manager and worker entry points. The worker resolves its identity/backend/manager-URL as today, just rooted at the workspace.
- New config keys: workspace path and tasks-store repo name (default `nightshift-tasks`).
- `justfile` recipes (`manager`, `worker`, `worker-headless`, `server`, …) pass `--workspace` instead of `--root "{{root}}"`.
- The worker validates at startup that its `--workspace` exists; per-task repo availability is handled by §4 (pause), not a startup failure.

---

## 8. Database schema (`assets/migrations/`)

Add a **workspace-relative `repo`** column to the run record (and `tasks` where state is tracked) so each run records which target repo it ran against.

- A new **idempotent** migration under `src/nightshift/assets/migrations/` (tracked in `_meta.schema_migrations`).
- `repo` stores the workspace-relative child name (never an absolute path).
- Surfaced in run history and the UI; used for debugging and per-repo filtering.

---

## 9. UI — the product surface (`assets/ui/`, `manager/app.py`)

Every capability here must be reachable from the dashboard (talking only to the backend API).

- **Repos page (Manager config):**
  - Lists the known-repos set (direct children of `--workspace` with `.git`) and which queues bind to each.
  - Flags queues whose `repo` is unavailable (the one-per-queue warning).
  - **Rescan** button (POST endpoint) → re-scan + auto-resume `repo_unavailable` tasks.
  - The workspace path is shown read-only (operator config).
- **Queues page:** per-queue **default-repo selector**, populated from the known-repos endpoint.
- **Task create/edit:** optional **repo override** (defaults to the queue's repo).
- **State surfacing:** the new "Paused" (`repo_unavailable`) task state renders with the existing `.status.paused` style; run history shows the target `repo`.

New/changed manager endpoints (illustrative): `GET /api/repos` (known set + per-queue binding + warnings), `POST /api/repos/rescan`, and `repo` added to queue-config and task-create/edit payloads.

---

## 10. Testing

- **Resolution & validation:** direct-child + `.git` acceptance; malformed/unsafe references rejected; task-over-queue override precedence; `repo_unavailable` → paused (no failed run).
- **Two-root split:** work-order construction reads briefs from `tasks_root`; git ops, worktree placement, `base_ref`, and landing target `repo_root`.
- **Availability lifecycle:** pause on missing repo → rescan after clone → auto-resume to `queued`; one-warning-per-queue dedupe.
- **`nightshift-tasks` lifecycle:** create/complete/edit produce local commits; no push.
- **Fixtures:** extend the existing fakes to build a fake workspace containing a `nightshift-tasks` repo + at least one target repo (and an absent repo to exercise pause/rescan).

---

## 11. Invariants

1. **One workspace, many repos.** Manager and worker are initialised against a single `--workspace`; every repo they touch is a direct child of it.
2. **Two roots, one source each.** Briefs/config come from `tasks_root` (`nightshift-tasks`); git ops run in `repo_root` (`<workspace>/<repo>`). They are never conflated.
3. **A queue is bound to a repo.** The queue's `config.json` sets a default `repo`; a task may override it; the resolution order is task → queue.
4. **Workspace-relative paths only.** No absolute path is ever persisted to config, the work order, or the DB.
5. **Missing repo pauses, never fails.** A well-formed reference to an absent/invalid repo pauses the task (`repo_unavailable`) and warns once per queue; it auto-resumes on rescan.
6. **The target repo stays clean.** Briefs never enter the target worktree; worktrees live outside the target repo; the only thing that lands is the implementation squash commit.
7. **The content store is git-tracked, low-churn, local.** `nightshift-tasks` holds only briefs + queue config, auto-committed locally, with no required remote.

---

## 12. Out of scope / non-goals

- **No multi-machine sync now.** Local-only `nightshift-tasks` is assumed; remotes/auto-push are a future, gated, opt-in.
- **No nested repo paths.** Only direct children of the workspace are addressable (`group/repo` is not supported).
- **No automatic cloning.** The manager never clones a missing repo; the operator clones, then rescans.
- **No per-task repo discovery beyond frontmatter.** A task targets exactly one repo (queue default or its own override).
- **No backward-compatible `--root` mode.** The single-repo entry point is replaced, not kept alongside.

---

## 13. Open questions / future

- **Periodic rescan** vs. manual-only: a config-driven (per `dashboard.refresh`/`ingest.*` cadence) re-evaluation could complement the Rescan button; manual is the baseline.
- **`tasks.auto_push`** + remote conventions for eventual multi-machine operation.
- **Log retention:** scrape-to-Postgres-then-delete for local logs, so they never grow unbounded (separate from this change).
