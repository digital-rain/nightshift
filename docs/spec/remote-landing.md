# Nightshift — cross-machine landing

How a worker on a different machine gets its validated work to the manager, while the manager stays the sole writer of each target repo's canonical `main` and the sole author of PRs.

## Status

Transport B (git rendezvous remote) — proposed for the multi-repo workspace engine; adds a transport to the existing `land()` path.
PR mode is made `origin/main`-authoritative so the manager's local `main` and `origin/main` cannot diverge (Proposal 1).
Transport A (bundle over the manager API) and active PR-state tracking (Proposal 2) are documented future work.
Where this doc and the code disagree once implemented, the code governs and this doc should be updated.

## Problem

The manager/worker split was built for a single box and never grew a cross-machine transport.
Today a worker commits to a local branch `task-local/<queue>/<task>` in a worktree cut from `repo_root = <workspace>/<repo>`, then submits metadata only over the HTTP API (`worker/loop.py` `_submit`, `worker/client.py`).
The manager squash-merges that local branch from its own clone (`manager/landing.py` `land()` calls `engine.squash_to_main(workspace, repo, ...)`).
That only works when worker and manager share one workspace, so the branch is already present in the manager's `repo_root`.
With manager and workers on different machines (each its own `--workspace` and its own clones of the target repos), the worker's commits never reach the manager: `squash_to_main` finds no `task-local/...` branch and the land fails.
The transport is unbuilt, not merely unconfigured.

## Goals and invariants

- The manager remains the only writer of each target repo's canonical `main` and the only component that pushes `origin` / opens PRs (`landing.py` `_push_main` / `_open_pr`, already `cwd=repo_root`).
  A worker never writes `main`.
- A worker's only outward actions: a metadata submit over the authenticated API, and (new) publishing its own task branch to a rendezvous remote it has scoped access to.
- Per repo: everything threads `(workspace, repo)` and runs git inside `repo_root = workspace/repo`, like every other engine git call.
  No new absolute paths are persisted or transmitted (multi-repo contract invariant 4).
- Backward compatible: a co-located worker sharing the manager's workspace keeps working with no transport configured.
- The existing drift / conflict / resolve semantics in `land()` (`base_ref` pin, `git merge-tree` preview, conflict preserves the branch and issues a resolve work-order) are preserved.

## Decision

v1 = transport B (git rendezvous remote): the worker pushes its validated task branch to a shared git remote; the manager fetches it and lands.
Simplest to stand up and troubleshoot (plain `git push`/`fetch` + prune), git-native durability and observability, minimal new code.
It accepts a worker -> remote -> manager round trip (about 2x bandwidth) and a scoped per-worker push credential.

future = transport A (bundle over the manager API): the worker uploads a `git bundle` straight to the manager (one hop, no double bandwidth, no per-worker git credential), at the cost of more code and lower in-flight durability/observability.
Both transports share the manager-side seam "obtain the branch into the manager's repo, then land", so A is an additive transport implemented when B's bandwidth cost bites.

## Rendezvous remote: a per-repo remote name

Each target repo (`workspace/repo`) is its own clone with its own `origin` (its own GitHub repo); the manager already pushes `main` and PR branches there with `cwd=repo_root`.
The rendezvous is therefore a remote *name* resolved inside each `repo_root` (default `origin`), not one global URL.
Worker and manager run `git -C <repo_root> push/fetch <remote-name> ...`; each target repo simply needs that remote configured (it already is, for the manager's push/PR path).
Config selects the name, so the choice (the repo's own `origin`, or a dedicated bare/rendezvous host added as another remote in every clone) stays a deployment decision.
GitHub as the `origin` rendezvous is the assumed v1 deployment; technically any git remote works.

## Branch namespace

- worker-published WIP ref (per repo, on that repo's remote): `refs/heads/nightshift-wip/<queue_slug>/<task>`.
- manager PR branch (unchanged, PR mode only): `task/<queue_slug>-<task>`.
- local worktree branch (unchanged): `task-local/<queue_slug>/<task>` (`engine.worktree_branch`).

Each repo has its own remote, so the WIP ref never collides across repos.
Keeping it distinct from the PR branch means a worker's scoped credential pushes only `nightshift-wip/*`, never `main` or `task/*`.
The WIP ref is transport-only: once the manager has consumed it (landed, or built the PR branch from it) the ref is pruned.

## Engine helpers (new, `engine.py`), threaded (workspace, repo)

- `publish_task_branch(workspace, repo, task, remote, *, queue=None) -> tuple[str, str]`: force-push local `worktree_branch(task, queue)` from `repo_root` to `<remote>` as `refs/heads/nightshift-wip/<queue_slug>/<task>`; return `(wip_ref, head_sha)` (full SHA).
- `fetch_rendezvous_branch(workspace, repo, remote, wip_ref, task, *, queue=None) -> str | None`: force-fetch `<remote> <wip_ref>:refs/heads/<worktree_branch>` into `repo_root`; return the fetched tip SHA, or `None` on a fetch error.
- `prune_rendezvous_branch(workspace, repo, remote, wip_ref) -> None`: best-effort `git -C repo_root push <remote> --delete <wip_ref>`.
- `sync_main_to_origin(workspace, repo, remote, *, autostash=True) -> str | None`: fetch `<remote> main` and bring local `main` to it (fast-forward, or `reset --hard` over an orphaned ephemeral pr-mode squash), guarding operator WIP with the same stash posture `squash_to_main` uses; return the new HEAD, or `None` when the remote has no `main` (the caller falls back to local HEAD).

These mirror the engine convention (`setup_worktree` / `teardown_worktree` / `squash_to_main` all take `(workspace, repo, task, *, queue=None)`).

## Worker side (`worker/execute.py`, `worker/loop.py`, `worker/config.py`)

The worker harness publishes; the agent does not (`assets/prompts/nightshift-local.md` stays "no commits to main / do not push"; the harness pushes only after its own validate gate passes, keeping validation the trust boundary).

- In cross-machine mode (`cfg.rendezvous_remote` set) the worker bases its worktree on the work order's `base_ref` before `setup_worktree` (fetch the remote, cut the worktree from `base_ref`), so the published branch is anchored to the same commit the manager will squash onto.
- `ExecuteOutcome` gains `branch_ref: str | None = None` and `head_sha: str | None = None`.
- A `_finish_landable(...)` helper both landable returns ("validation skipped" and "validated") route through: when `cfg.rendezvous_remote` is set it calls `publish_task_branch(cfg.workspace, repo, task, cfg.rendezvous_remote, queue=queue)` and stores `(branch_ref, head_sha)`; a push failure becomes `status="error"`, `failure_kind="publish_failed"`, `landable=False` (nothing lands).
  When unset it publishes nothing and leaves both fields `None` (co-located, today's behavior).
- `worker/loop.py` `_submit` adds `branch_ref` and `head_sha` to the payload; `worker/client.py` is unchanged (it forwards the dict).
- `worker/loop.py` tears down the worker's own worktree on a cross-machine `landed: true` response (the manager's `teardown_worktree` only touches the manager's workspace), and preserves it on a failed land so a re-fetch/resolve run can use it.
- `WorkerConfig` gains `rendezvous_remote: str | None = None`, loaded env-wins from `NIGHTSHIFT_RENDEZVOUS_REMOTE` then `config.json.local` `rendezvous_remote`; unset -> co-located.

## Manager side (`manager/landing.py`, `manager/app.py`, `manager/config.py`)

`land()` gains keyword-only `branch_ref: str | None = None`, `head_sha: str | None = None`, `rendezvous_remote: str | None = None`, and an obtain step before the existing drift/squash logic:

1. `repo_root = workspace/repo`, `branch = worktree_branch(task, queue)` (already computed).
2. Cross-machine detection by worktree dir, not branch existence (the correctness rule): if `not worktree_dir(workspace, repo, task, queue).exists()` and `branch_ref` is set, this is a cross-machine land, so fetch on every attempt:
   - require `rendezvous_remote` and `head_sha`; if either is missing, refuse (`LandingResult(landed=False, recoverable=False, ...)`) — fail-closed, never land unverified content.
   - `fetched = fetch_rendezvous_branch(...)`; a fetch error -> `LandingResult(landed=False, recoverable=True, ...)`.
   - if `fetched != head_sha` -> `LandingResult(landed=False, recoverable=False, detail="head_sha mismatch ...")`.
3. In PR mode, `sync_main_to_origin(...)` so the ephemeral squash sits on the latest `origin/main`.
4. Run the existing flow unchanged: `base_ref_drifted` + `merge_tree_conflicts` preview, `squash_to_main`, `teardown_worktree`, then the `landing_mode` (`none`/`push`/`pr`) policy.
5. On a successful cross-machine land: `prune_rendezvous_branch(...)` (best-effort).
6. On conflict/rejection: keep the WIP ref (the local branch is already preserved) so a resolve work-order can re-fetch.

Why gate on `worktree_dir(...).exists()` and re-fetch every attempt: if the obtain step were gated on "branch absent", a rejected first attempt would leave a stale local `task-local/...` branch and a retry would skip the re-fetch/re-verify and could squash stale or unverified content.
Gating on the worktree directory means co-located lands (worktree present) never fetch, and pure cross-machine lands (no worktree dir) always force-fetch + re-verify `head_sha`.
This also closes the null-`head_sha` bypass.

`manager/app.py`:

- `worker_poll` computes the task's effective landing mode (`make_pr` override below) and, when that mode is `pr` and the repo has the configured remote, calls `sync_main_to_origin(...)` before `base_ref = canonical_head(workspace / repo)`, so `base_ref` is the latest `origin/main`.
- `SubmitBody` gains `branch_ref: str | None = None` and `head_sha: str | None = None`.
- `worker_submit` resolves the effective landing mode and passes it plus `branch_ref` / `head_sha` / `cfg.rendezvous_remote` into the existing `land(...)` call; everything else there (run/lease/event bookkeeping, `merge_conflict` vs `merge_rejected` mapping) is unchanged.

`manager/config.py`: `ManagerConfig` gains `rendezvous_remote: str | None = "origin"` (env `NIGHTSHIFT_RENDEZVOUS_REMOTE` wins), consulted only when a submit carries `branch_ref` and the worktree dir is absent, or for the pr-mode `origin/main` sync.

## PR-mode `origin/main` authority (Proposal 1)

The divergence is `pr`-only.
`none` involves no remote; `push` sends nightshift's exact commit to `origin/main` (no re-squash, no divergence).
`pr` mode is the problem: GitHub re-squashes at merge, producing a different SHA than the manager's local squash, and optimistic local-main advance would let a later task stack on an unmerged PR.

The fix makes `origin/main` authoritative in `pr` mode.
At dispatch the manager resyncs local `main` to `origin/main` before pinning `base_ref`, so an orphaned ephemeral squash from a prior pr land is dropped and divergence cannot accumulate.
At land time the manager resyncs again before the squash, so the ephemeral squash sits on the latest `origin/main`; the existing `base_ref_drifted` + `merge_tree_conflicts` preview catches anything merged since dispatch and refuses cleanly.
The local squash is ephemeral — the next dispatch's resync replaces it with GitHub's merge commit.
`none` and `push` are untouched (local `main` stays the running ledger).

Proposal 2 (future): poll `gh pr view --json state,mergeCommit` on the scheduler tick to reconcile promptly on merge, flag a resolve on close, and optionally serialize same-repo dispatch — explicit in-flight tracking layered on top of Proposal 1's lazy resync.

## Task-level `make_pr` override

A task may set `make_pr: true` in its brief frontmatter to force PR mode regardless of the manager's `landing_mode`.
The rule is a single line, resolved at dispatch and at submit:

```
effective_mode = "pr" if meta.get("make_pr") else cfg.landing_mode
```

`make_pr: true` wins over the manager default; absent or `false` defers to `cfg.landing_mode` (it is not the inverse and never forces a squash or push).
"Manager only cuts PRs" is just `landing_mode: pr`.
Autopush (`push`) stays manager-level only; a task can force a PR, never a direct push.
v1 wires the landing decision and reads the frontmatter key; surfacing `make_pr` in the UI toggle, Slack intake tags, `spawn_daily` defaults, and `resolve_frontmatter` is a parity follow-up.

## Credentials and safety

- A worker needs push access to `nightshift-wip/*` only on each target repo's rendezvous; never `main`.
  On GitHub: a deploy key/token plus branch protection on `main`; on a bare repo: an `update`/`pre-receive` hook rejecting writes outside `refs/heads/nightshift-wip/*`.
- The manager keeps sole `origin` push / `gh` authority for `main` and PRs.
- The HTTP trust model is unchanged: the shared secret still guards every `/api/worker/*` call.

## Co-located backward compatibility

With no rendezvous remote configured (worker side), the worker publishes nothing and the manager finds the worktree + branch already in its workspace — today's behavior, untouched.
Cross-machine is opt-in.

## Relationship to the multi-repo workspace spec

Orthogonal to the `nightshift-tasks` content-store "no multi-machine sync now" non-goal (multi-repo-workspace.md section 12): that is about syncing briefs/queue config, which stay local to the manager.
This spec transports a validated implementation branch (code) for a target repo from worker to manager.
Briefs are delivered to the worker by value in the work order (`materialize_brief`); only the impl squash ever lands.

## Failure modes

- Push fails (worker): submit reports it, the run errors with a clear reason, nothing lands, the local worktree is preserved.
- Fetch fails (manager): land fails recoverable; the WIP ref is kept for inspection.
- `head_sha` missing or mismatched: land fails closed (`merge_rejected`); the WIP ref is kept.
- Conflict / base drift: unchanged — branch preserved, task blocked with a resolve reason.
- Worker dies after push: the WIP ref persists on the rendezvous, so the manager can still fetch and land.

## Config keys

- `NIGHTSHIFT_RENDEZVOUS_REMOTE` (env) / `rendezvous_remote` (manager: `<workspace>/config.json` `manager` block; worker: `config.json.local`) — a git remote name resolved inside each `repo_root`.
  Worker: unset disables publishing (co-located).
  Manager: default `origin`, used to fetch a cross-machine branch and to sync `origin/main` in pr mode.
- `make_pr` (task brief frontmatter, boolean) — force PR mode for that task.
- The WIP namespace prefix is fixed (`nightshift-wip/`) in v1.

## Files touched

- `src/nightshift/engine.py` — `publish_task_branch` / `fetch_rendezvous_branch` / `prune_rendezvous_branch` / `sync_main_to_origin`; an optional `base` for `setup_worktree`.
- `src/nightshift/worker/execute.py` — `_finish_landable` publish step; outcome fields; cross-machine base sync.
- `src/nightshift/worker/loop.py` — carry `branch_ref` / `head_sha` in `_submit`; teardown on a cross-machine land.
- `src/nightshift/worker/config.py` — `WorkerConfig.rendezvous_remote`.
- `src/nightshift/manager/landing.py` — obtain (fetch + verify) / pr-mode sync / prune / keep-on-conflict in `land()`.
- `src/nightshift/manager/app.py` — `SubmitBody` fields; dispatch sync; effective-mode resolution; pass into `land()`.
- `src/nightshift/manager/config.py` — `ManagerConfig.rendezvous_remote`.
- `docs/setup-guide.md`, `docs/configuration-reference.md` — the cross-machine workflow + env var + `make_pr`.
- Prompts unchanged.

## Tests (`tests/`, `just validate`)

Build a workspace + target repo with `tests/_workspace.build_workspace(...)`, plus a bare repo in `tmp_path` added as the target clone's `origin`/rendezvous remote:

- engine helpers: publish pushes the WIP ref + returns `(ref, sha)`; fetch lands it into `repo_root` as `task-local/...`; prune deletes it; `sync_main_to_origin` fast-forwards and resets over an orphan.
- worker execute: landable + `rendezvous_remote` set -> publishes + reports `branch_ref`/`head_sha`; publish failure -> `publish_failed` error; no-commit/blocked -> publishes nothing.
- worker config and manager config: env + file parse of `rendezvous_remote`.
- manager `land()`: cross-machine happy path (fetch + squash + prune); missing/mismatched `head_sha` fail-closed; fetch error recoverable; conflict keeps the WIP ref; re-land regression (rejected first attempt, then a corrected second attempt re-fetches and verifies the new `head_sha`).
- pr-mode `origin/main` authority: after a pr land + a simulated squash-merge on the bare origin, the next dispatch's `sync_main_to_origin` makes local `main == origin/main` (no accumulating divergence).
- `make_pr` override: a task with `make_pr: true` routes through pr mode even when the manager's `landing_mode` is `none`/`push`.
- co-located regression: no `rendezvous_remote`, worktree present -> manager lands exactly as today, no fetch/sync.

## Future design (transport A — bundle over API)

When B's double bandwidth bites, replace the transport (not the landing):

1. Worker `git bundle create <file> <base_ref>..<head>` for its task branch.
2. Worker POSTs the bundle (multipart) to a new `POST /api/worker/runs/{run_id}/bundle`, authenticated by the shared secret.
3. Manager writes it to a temp file, fetches `head_sha:refs/heads/<worktree_branch>` from the bundle into `repo_root`, verifies `head_sha`, then runs the same `land()` body.
4. Manager drops the temp ref/file after the land.

A removes the per-worker git credential and the second hop, at the cost of a multipart endpoint with a size cap, bundle create/verify code, and lower in-flight durability/observability.
A and B converge on the same "obtain the branch, then land()" seam, so A is additive.

## Open questions

- Rendezvous credential provisioning across many workers (deploy key vs token vs SSH) is deployment-specific; v1 documents the requirement rather than prescribing one.
- Whether to garbage-collect abandoned `nightshift-wip/*` refs on a schedule (a lease expiry could orphan one if a worker dies before submit).
- Whether to lift `make_pr` to full parity with `automerge`/`draft` (UI, Slack, `spawn_daily`, frontmatter defaults).
