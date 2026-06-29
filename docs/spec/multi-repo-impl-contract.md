# Multi-Repo Workspace — Implementation Contract (internal)

Authoritative engineering contract for implementing
`docs/spec/multi-repo-workspace.md`. **Read the spec first**, then follow this
contract for exact signatures, shapes, and layout so independently-authored
modules compose. Where this contract and the spec disagree, the spec governs;
flag the conflict.

Decisions locked by the orchestrator (defaults the user accepted):
- Legacy single-box `server`/`run_local`/`player` is **minimally adapted** to the
  two-root engine so the suite stays green; the new product surfaces also work
  there because we **mirror** the new endpoints on both `manager` and `server`.
- Operator/host config lives at **`<workspace>/config.json`**.
- Verify with `just validate` (ruff + pytest, in-memory store); the new
  migration SQL must be idempotent in the `-- migrate:up/down` style.
- Blocked is a **DB state + reason only** (no `.BLOCKED` file).

---

## 0. Vocabulary & the two roots

- `workspace: Path` — the `--workspace` dir; parents every git repo.
- `tasks_repo: str` — name of the content-store repo (default `nightshift-tasks`,
  `nightshift.repos.DEFAULT_TASKS_REPO`); configurable via `<workspace>/config.json`.
- `tasks_root: Path` = `workspace / tasks_repo` — briefs + queue config live here.
- `repo: str` — workspace-relative **bare child name** of the target repo,
  resolved **per task** (queue default or task override).
- `repo_root: Path` = `workspace / repo` — all git ops for a task run here.
- **Workspace-relative paths only** are persisted/transmitted. Absolute paths are
  materialised transiently at a filesystem/git call site (`workspace / repo`,
  `tasks_root / ...`). Never store an absolute path in config.json, the work
  order, or the DB.

## 1. Already-implemented foundation (do not re-author; import these)

### `src/nightshift/repos.py` (NEW — done)
- `DEFAULT_TASKS_REPO = "nightshift-tasks"`
- `class RepoConfigError(ValueError)` — malformed/unsafe ref OR no repo set; an
  authoring error, never dispatched.
- `is_valid_repo_ref(ref) -> bool` — bare slug guard (reuses `playlists.is_valid_name`).
- `repo_root(workspace, repo) -> Path`
- `repo_available(workspace, repo) -> bool` — direct child + contains `.git`.
- `known_repos(workspace) -> list[str]` — sorted workspace children with `.git`.
- `resolve_repo(task_repo, queue_repo) -> str` — precedence task→queue; raises
  `RepoConfigError`. Availability is a **separate** check.

### `src/nightshift/playlists.py` (REWRITTEN — done)
Queues are now top-level dirs of `tasks_root`; `main` is the default queue; **no
literal `.tasks/`**.
- `DEFAULT_QUEUE = "main"`
- `tasks_rel(name: str | None) -> str` → `name or "main"`.
- `queue_from_tasks_rel(tasks_rel) -> str | None` → `"main"`/empty → `None`, else name.
- `runs_rel(name) -> str` → `f"{tasks_rel(name)}/runs"`.
- `list_playlists(tasks_root) -> list[dict]` — alternate queues only (excludes
  `main`, reserved, hidden/`.`-dirs).
- `exists(tasks_root, name) -> bool`, `create_playlist(tasks_root, name)`,
  `delete_playlist(tasks_root, name)` (never deletes `main`).
- `is_valid_name`, `slugify_name` unchanged.

### `tests/_workspace.py` (NEW — done)
Shared fake-workspace builder. Use it in test files instead of bespoke single-root
seeds.
- `build_workspace(workspace, *, tasks=None, main_repo="longitude", repos=("longitude",), queues=None, config=None, tasks_repo="nightshift-tasks", commit_tasks=True) -> Path`
- helpers: `git`, `git_init`, `git_commit_all`, `make_target_repo`.
- Creates `<ws>/config.json`, `<ws>/nightshift-tasks/` (git repo, `main/` + alt
  queues, `.gitignore` for `*/runs/` `*/logs/`), and target repos on branch `main`.
- A repo named in config but omitted from `repos=` stays **absent** (for pause/rescan).

## 2. Config resolution (`spawn_daily.py`)

`<workspace>/config.json` holds operator/host config (manager block: dsn,
cadences, host, port, landing_mode, shared_secret; plus `tasks_repo` name and
global runner defaults). The content store holds portable runner/queue config.

- `load_config(workspace) -> dict` — reads `<workspace>/config.json` (rename the
  param from `root`; behaviour unchanged).
- `load_queue_config(tasks_root, tasks_rel="main") -> dict` — reads
  `tasks_root / tasks_rel / config.json`.
- `load_store_config(tasks_root) -> dict` — NEW: reads `tasks_root / config.json`
  (the optional workspace-level/system-wide layer).
- `resolve_config(workspace, tasks_root, tasks_rel="main") -> dict` — layered,
  later wins: `load_config(workspace)` ← `load_store_config(tasks_root)` ←
  `load_queue_config(tasks_root, tasks_rel)`.
- `save_config_value(workspace, key, value)` — unchanged target `<workspace>/config.json`.
- Autosplit/spawn (`find_autosplit_sources`, `spawn_source`, `inspect_source`,
  `recover_matrix`, `main`): operate on `tasks_root` + a queue dir (default
  `main/`) instead of `<root>/.tasks/`. `--root` → `--workspace` on its CLI;
  derive `tasks_root` from `<workspace>/config.json` `tasks_repo`.
- `repo` is **per-queue, not inherited**: read a queue's default repo from
  `load_queue_config(tasks_root, tasks_rel).get("repo")` only.

## 3. Engine (`engine.py`) — two-root split

General rules:
- File/queue/brief functions take `tasks_root: Path` (rename from `root`) and keep
  `tasks_rel: str = "main"`. Paths become `tasks_root / tasks_rel / ...`.
- Git/worktree/landing functions that also place worktrees take
  `workspace: Path, repo: str` and derive `repo_root = workspace / repo`;
  worktrees live at `workspace/.worktrees/<repo>/...` (outside the target repo).
- Pure git-on-one-repo helpers take `repo_root: Path`.

### Worktrees / locks
- `_queue_slug(queue)` unchanged.
- `worktree_branch(task, queue=None)` unchanged → `task-local/<queue>/<task>`.
- `worktree_dir(workspace, repo, task, queue=None) -> Path` →
  `workspace/.worktrees/<repo>/task-local-<queueslug>-<task>`.
- `setup_worktree(workspace, repo, task, *, queue=None) -> Path` — runs
  `git -C (workspace/repo) worktree add <wt> -b <branch> HEAD`; symlink
  `SYMLINK_TARGETS` from `repo_root` into the worktree.
- `teardown_worktree(workspace, repo, task, *, queue=None)`,
  `cleanup_task_worktree(workspace, repo, task, *, queue=None) -> bool`,
  `_worktree_has_commits(workspace, repo, task, *, queue=None) -> bool`,
  `_ensure_worktree_for_branch(workspace, repo, task, *, queue=None)`.
- `acquire_lock(workspace) -> int` — file at `workspace/.worktrees/.nightshift-local.lock`.
- `landing_lock(workspace, repo)` — file at
  `workspace/.worktrees/<repo>/.nightshift-landing.lock`; keep the module-global
  in-process `_LANDING_LOCK` (serialises in-process lands; conservative & fine).
- `enough_free_disk(workspace, ...)`, `check_preconditions(workspace, repo)` —
  disk on `workspace`; `_landing_blockers`/preflight validate on `repo_root`.
- failure logs: `workspace/.worktrees/<repo>/failures/<task>.log`.

### Brief delivery (NO brief in the target repo, ever)
- `materialize_brief(workspace, repo, task, body, *, queue=None) -> Path` — NEW:
  writes the brief body to a run-scratch file **outside** the worktree, e.g.
  `workspace/.worktrees/<repo>/task-local-<queueslug>-<task>.taskfile.md`. Returns it.
- `build_prompt(task, *, task_file: str, validate_cmd: str) -> str` — REWORK:
  drop `tasks_root`/`tasks_rel`; takes the already-materialised scratch
  `task_file` path and the resolved `validate_cmd`. Reads the charter asset and
  formats the same `$TASK_FILE`/`$TASK`/`$VALIDATE` header. (Worker reuses this.)

### Queue commits → content store (`tasks_root`)
The pre-run snapshot **into the target repo is removed**. There is no need to
commit queue state before cutting a worktree (briefs are read live from
`tasks_root` and delivered via scratch). The content store gets local commits on
lifecycle events instead.
- `commit_tasks(tasks_root, message, *, pathspecs=(".",)) -> str | None` — NEW
  generic helper: `git -C tasks_root add <pathspecs>` then commit if staged;
  no-op (returns None) when `tasks_root` is not a git repo or nothing staged.
- `drop_completed_task(tasks_root, task, tasks_rel="main", *, queue=None) -> bool`
  — delete the brief from `tasks_root` and `commit_tasks` the removal.
- `_commit_dispatch(tasks_root, tasks_rel="main")` — commit autosplit dispatch in
  the content store.
- Remove/retire `commit_queue_state`'s target-repo role; if kept, it commits the
  queue dir churn **in `tasks_root`** (used by create/edit lifecycle). `run_queue`
  must NOT snapshot the target repo.

### Landing (target repo only receives the impl squash)
- `squash_to_main(workspace, repo, task, title, *, queue=None, autostash=True) -> tuple[str|None, str, bool]`
  — merge `worktree_branch` into `main` in `repo_root`. **Drop** the
  `commit_queue_state` call and the `.tasks/` blocker special-casing
  (`_is_queue_path`); `_landing_blockers(repo_root)` now returns all tracked
  operator code WIP (still stash/restore via autostash).
- `recover_task(workspace, repo, task, title, *, queue=None) -> TaskResult`.
- `compute_code_loc(repo_root, sha) -> int` (keep doc/build exclusions).
- `resolve_task(...)`, `_agent_resolve(...)`, `build_resolve_prompt(...)`: thread
  `workspace, repo, tasks_root` as needed; brief read from `tasks_root`; rebase in
  worktree of `repo_root`. `build_resolve_prompt` must not hardcode `.tasks/`.

### `run_task` / `run_queue`
- `run_task(workspace, tasks_root, task, *, repo=None, emit=_noop, abort_reason=None, backend_name=None, tasks_rel="main") -> TaskResult`:
  1. Read brief `tasks_root/tasks_rel/<task>.md`; split frontmatter.
  2. Resolve repo: `repo = repo or repos.resolve_repo(meta.get("repo"), load_queue_config(tasks_root, tasks_rel).get("repo"))`.
     - `RepoConfigError` → emit `TASK_RESULT` status `error` (authoring error) and return.
  3. If not `repos.repo_available(workspace, repo)` → emit `TASK_RESULT` status
     `"paused"` (reason `repo_unavailable`), return a `TaskResult(status="paused")`.
     Do not cut a worktree.
  4. `materialize_brief(...)` → scratch; `setup_worktree(workspace, repo, ...)`;
     `build_prompt(task, task_file=str(scratch), validate_cmd=...)`; run backend in
     the worktree; validate; `squash_to_main(workspace, repo, ...)`;
     `compute_code_loc(repo_root, sha)`; for regular tasks
     `drop_completed_task(tasks_root, task, tasks_rel, queue=queue)`.
  - Emit `repo` in `TASK_STARTED`/`TASK_RESULT` payloads.
- `run_queue(workspace, tasks_root, tasks, *, ..., tasks_rel="main", ...)`:
  thread `workspace, tasks_root`; live re-scan via `live_ordered_queue(tasks_root, tasks_rel)`;
  **remove** the per-iteration `commit_queue_state` target snapshot; call
  `run_task(workspace, tasks_root, task, ..., tasks_rel=tasks_rel)`.

### Blocked sentinel (file-free)
- The agent signals an honest block by emitting a final log line
  `NIGHTSHIFT_BLOCKED: <reason>` and making no commits. Provide
  `extract_blocked_reason(text) -> str | None` (scan for the last sentinel).
  The worker/engine use it to surface a `blocked` status. (No `.BLOCKED` file.)

## 4. Work order shape (manager → worker)

`_build_work_order(workspace, tasks_root, task, queue, repo, lease_id, run_id, base_ref, cfg) -> dict`:
```jsonc
{
  "lease_id", "run_id", "task", "queue",          // queue label "main"/<name>
  "priority", "title",
  "body": "<brief markdown, frontmatter stripped>",
  "repo": "<workspace-relative child name>",       // NEW
  "task_path": "nightshift-tasks/<queue>/<task>.md",// workspace-relative (NEW form)
  "base_ref": "<canonical_head(repo_root)>",
  "config": { ...resolve_config... }
}
```
No absolute paths. `base_ref = canonical_head(repo_root)`.

## 5. Manager (`manager/`)

### `__main__.py`
- `--root` → `--workspace` (default `Path.cwd()`); `create_app(workspace)`.

### `config.py`
- `load_manager_config(workspace)` reads `<workspace>/config.json` `manager` block
  (unchanged keys) + NEW `tasks_repo` (default `nightshift-tasks`, also overridable
  via `NIGHTSHIFT_TASKS_REPO`). Expose `cfg.tasks_repo`.

### `app.py` (`create_app(workspace, *, store=None)`)
- Compute `workspace = workspace.resolve()`, `tasks_repo = cfg.tasks_repo`,
  `tasks_root = workspace / tasks_repo`; store both on `app.state`.
- All task/queue ops use `tasks_root` (e.g. `create_task(tasks_root, ...)`,
  `read_task(tasks_root, ...)`, `list_queue(tasks_root, tasks_rel)`,
  `reorder_queue(tasks_root, ...)`, `playlists.list_playlists(tasks_root)`).
- **`worker_poll`**: build candidates per queue; for each candidate resolve repo
  (`resolve_repo(meta.repo, queue_repo)`):
  - `RepoConfigError` → `store.set_task_state(queue, task, "blocked", blocked_reason=str(err))`; exclude.
  - resolved but not `repo_available` → `store.set_task_state(queue, task, "repo_unavailable", repo=repo)`;
    exclude; **emit one warning per queue** (dedup via `app.state.repo_warnings: set`).
  - available → keep, remember resolved `repo`; on successful pick, clear any prior
    paused/blocked state, `base_ref = canonical_head(repo_root)`,
    `_build_work_order(... repo ...)`, `store.create_run(... repo=repo ...)`.
- On task create/edit/delete and queue-config edits, **commit the content store**
  (`commit_tasks(tasks_root, "...")`). On completion in `worker_submit`,
  `drop_completed_task(tasks_root, ...)` already commits removal.
- **`worker_submit`**: land in `repo_root = workspace / lease.repo` (carry `repo`
  on the lease/run); `land(repo_root_or(workspace,repo), ...)`. A `blocked` status
  from the worker → `store.set_task_state(queue, task, "blocked", reason)`.
- NEW endpoints:
  - `GET /api/repos` → `{ "workspace": "<abs display>", "tasks_repo": "...",
    "repos": [{"name","available":true}...], "queues":
    [{"queue","repo","available"}...], "warnings": [{"queue","repo"}...] }`
    (known set from `repos.known_repos`; per-queue repo from queue config).
  - `POST /api/repos/rescan` → recompute known set; for every task in state
    `repo_unavailable` whose repo is now available, `store.clear_task_state`;
    clear `app.state.repo_warnings`; emit `repos_changed`/`queue_changed`; return
    the `/api/repos` payload.
  - `repo` added to queue-config PUT and task create/edit payloads.

### `scheduler.py`
- `build_candidates(workspace, tasks_root, queue, *, default_model="auto") -> list[TaskCandidate]`;
  read `live_ordered_queue(tasks_root, tasks_rel)`; `TaskCandidate` gains
  `repo: str | None` (resolved, or None if authoring error). Keep repo-resolution
  errors/availability handling in the manager (`worker_poll`) per above, OR return
  enough on the candidate for the manager to act. Skipping rules unchanged plus:
  do not dispatch `repo_unavailable`/`blocked` tasks.

### `landing.py`
- `canonical_head(repo_root)`, `base_ref_drifted(repo_root, ...)`,
  `merge_tree_conflicts(repo_root, branch)`, `land(workspace, repo, task, title, *, queue, base_ref, landing_mode, ...)`
  → derive `repo_root = workspace / repo`; call `squash_to_main(workspace, repo, ...)`,
  `teardown_worktree(workspace, repo, ...)`.

### `store.py` (+ migration)
- `nightshift.tasks` gains `repo text`; `nightshift.runs` gains `repo text`.
- `set_task_state(queue, task, state, *, blocked_reason=None, repo=None)` — extend.
- States now include `repo_unavailable` (rendered "Paused"). `list_blocked`
  stays `state == 'blocked'`.
- `tasks_in_state(state) -> list[dict]` ({queue, task, repo, blocked_reason}) — NEW,
  for rescan auto-resume.
- `create_run(..., repo: str | None = None)`; persist + surface `repo` in `list_runs`.
- Implement on **both** `MemoryStore` and `PgStore`.

### Migration `src/nightshift/assets/migrations/20260730000003_nightshift_repo_column.sql`
```sql
-- migrate:up
ALTER TABLE nightshift.tasks ADD COLUMN IF NOT EXISTS repo text;
ALTER TABLE nightshift.runs  ADD COLUMN IF NOT EXISTS repo text;
-- migrate:down
ALTER TABLE nightshift.runs  DROP COLUMN IF EXISTS repo;
ALTER TABLE nightshift.tasks DROP COLUMN IF EXISTS repo;
```

## 6. Worker (`worker/`)

### `config.py` / `__main__.py`
- `WorkerConfig.root` → `WorkerConfig.workspace` (keep a `root` alias property
  returning `workspace` only if it materially reduces churn; prefer renaming).
- `load_worker_config(workspace)`; `.env`/`config.json.local`/`LocalStore` rooted
  at `workspace`. `--root` → `--workspace`. Startup: validate `workspace` exists
  (a dir); per-task repo availability is the manager's job (pause), not a startup error.

### `execute.py`
- `repo = order["repo"]`; defensively, if not `repos.repo_available(cfg.workspace, repo)`
  return `ExecuteOutcome(status="error"/"blocked", failure_kind="repo_unavailable", ...)`.
- `materialize_brief(cfg.workspace, repo, task, order["body"], queue=queue)` → scratch;
  `build_prompt(task, task_file=str(scratch), validate_cmd=...)` (reuse engine's).
- `setup_worktree(cfg.workspace, repo, task, queue=queue)` / `_worktree_has_commits` /
  `teardown_worktree` all `(workspace, repo, task, queue=queue)`.
- Capture logged lines; if `extract_blocked_reason(...)` matches and there are no
  commits → `ExecuteOutcome(status="blocked", failure_reason=reason, landable=False)`.
- `ExecuteOutcome` may gain `status="blocked"`.

### `loop.py` / `client.py`
- Submit payload may carry `status="blocked"` + reason; otherwise unchanged.

## 7. Legacy server (`server/app.py`, `run_local.py`, `server/player.py`)

Minimal adaptation to stay green + mirror new surfaces:
- `--root` → `--workspace`; `create_app(workspace)`; `Player(workspace)`.
- Derive `tasks_root` from `<workspace>/config.json` `tasks_repo`. Thread
  `workspace`+`tasks_root` into every engine call (`create_task`, `read_task`,
  `set_task_meta`, `list_queue`, `reorder_queue`, `import_task`, `run_queue`,
  `playlists.*`). Repo resolution happens inside `run_task` (paused on missing).
- Run/log artifacts stay local under the queue's gitignored `runs/`/`logs/` (or
  `workspace/.worktrees/...`); never committed to the content store.
- **Mirror** `GET /api/repos`, `POST /api/repos/rescan`, and `repo` on
  task create/edit (`POST/PATCH /api/tasks`) and queue config so the shared UI
  works under the server too. The "Paused" (`repo_unavailable`) state surfaces in
  run/queue status.

## 8. Operator UI (`assets/ui/`)

Talk to the backend API only. Add:
- **Repos page**: nav button `data-view="repos"`, `#screen-repos` section, CSS
  visibility rule, JS module to `GET /api/repos` (known set + per-queue binding +
  warnings) and a **Rescan** button → `POST /api/repos/rescan`. Workspace path
  shown read-only.
- **Queues**: per-queue default-repo selector populated from `/api/repos`; persist
  via queue-config PUT (`repo`).
- **Task create/edit**: optional repo override field (defaults to queue repo);
  include `repo` in create/edit payloads.
- **State**: render `repo_unavailable` as "Paused" via existing `.status.paused`;
  show target `repo` in run history.

## 9. Prompts (`assets/prompts/`)

- `nightshift-local.md`: `$TASK_FILE` is a **read-only scratch path** (the brief is
  NOT in the repo). Remove the `git rm "$TASK_FILE"` completion step (the manager
  removes the brief from `nightshift-tasks` after landing) and all `.tasks/`
  references. Honest failure: make **no commits** and emit a final line
  `NIGHTSHIFT_BLOCKED: <reason>` (no `.BLOCKED` file).
- `nightshift-resolve.md`: drop the `.tasks/` task-file deletion conflict note and
  the `.tasks/$TASK.BLOCKED` instruction; honest failure emits
  `NIGHTSHIFT_BLOCKED: <reason>`.

## 10. justfile

- Add `workspace := env_var_or_default("NIGHTSHIFT_WORKSPACE", justfile_directory())`.
- `manager`, `worker`, `worker-headless`, `server`, `slackd` pass
  `--workspace "{{workspace}}"` instead of `--root "{{root}}"`.

## 11. Tests (`tests/`)

- Use `tests/_workspace.build_workspace(...)` to construct workspaces; replace
  bespoke single-root `_seed`/`_seed_tree`/`_init_repo` seeds.
- Update existing tests for the new signatures/layout and the work-order shape
  (`repo`, `task_path` = `nightshift-tasks/<queue>/<task>.md`).
- Add coverage: resolution & validation (direct-child+`.git`; malformed rejected;
  task-over-queue precedence; `repo_unavailable` → paused, no failed run);
  two-root split (work order reads from `tasks_root`; git/worktree/base_ref/land in
  `repo_root`); availability lifecycle (pause → clone → rescan → `queued`;
  one-warning-per-queue dedupe); content-store lifecycle (create/complete/edit →
  local commits, no push).

## 12. Invariants (must hold)
1. One workspace; every touched repo is a direct child.
2. Briefs/config from `tasks_root`; git ops in `repo_root`; never conflated.
3. Queue bound to a repo; task overrides; order task→queue.
4. Workspace-relative paths only persisted/transmitted.
5. Missing repo pauses (`repo_unavailable`) + warns once/queue; auto-resumes on rescan.
6. Target repo stays clean: briefs never enter it; worktrees outside it; only the
   impl squash lands.
7. Content store is git-tracked, low-churn, local (no required remote/push).
