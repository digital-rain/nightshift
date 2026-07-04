# Git / Codebase Management — Thermo-Nuclear Code Quality Review

**Subject:** The git-management subsystem: worktree lifecycle, landing, locking, origin sync, rendezvous transport, and content-store commits.
**Scope:** `src/nightshift/engine.py` (git region ~1443–2531 + content-store helpers 899–975), `src/nightshift/manager/landing.py`, `src/nightshift/repos.py`, `src/nightshift/worker/execute.py`, call sites in `manager/app.py` and `manager/resolve_job.py`.
**Date:** 2026-07-02
**Verdict:** The *design* — invariants, locking intent, failure taxonomy — is unusually well thought out and well documented. The *implementation* does not meet the bar: a 3,634-line god module, no git abstraction layer, prose-enforced lock contracts, tuple-encoded error signaling, three near-copies of the integrate-and-push loop, and one concrete data-loss hazard.

See the companion greenfield design: [`../spec/git-management-greenfield.md`](../spec/git-management-greenfield.md).

---

## Severity 1 — Structural

### 1.1 `engine.py` is a god module at 3.6× the 1k-line rule — and the seams are already drawn

`engine.py` contains at least nine unrelated concerns; the module's own section-divider comments mark the boundaries:

| Lines | Concern |
|---|---|
| 103–350 | validate/preflight command resolution + venv sync |
| 454–544 | CLI preconditions, instance lock |
| 574–896 | queue config CRUD: order, sort, priorities, play filter |
| 899–975 | content-store commits (`commit_tasks`, `commit_queue_state`) |
| 981–1305 | task-file CRUD + hand-rolled frontmatter editor |
| 1313–1440 | prompt building, `claude` argv, `worker_env` |
| **1443–2531** | **the entire git layer**: worktrees, both locks, autostash, squash, LOC accounting, rendezvous transport, origin sync/rescue, recover |
| 2534–3000 | resolve agent + repair |
| 3043–3634 | `Controller`, `run_task`, `run_queue` |

The proof that the git layer has no home: other modules import engine's *private* helpers across the boundary (`landing.py` imports `_queue_slug` and `_worktree_has_commits`; `app.py` and `execute.py` import `_worktree_has_commits`) — and `landing.py` **re-implements `_rev_parse` verbatim** (`landing.py:53` vs `engine.py:2119`) rather than import a fourth underscore name. When consumers either import privates or copy them, the module boundary is fictional.

### 1.2 No git-command seam: ~57 raw `subprocess.run(["git", …])` calls with four different error idioms

The same six-line incantation is repeated 47 times in `engine.py` and 10 in `landing.py`, with four inconsistent failure policies:

1. `check=True` **exactly once** — `setup_worktree` (`engine.py:1583`). It is the only git call that can raise, no caller catches `CalledProcessError`, so a failed `worktree add` crashes `run_task` with a raw traceback while every other git failure returns a polite tuple. Exactly backwards for the call that starts every task.
2. returncode → `(None, detail, recoverable)` tuples (`squash_to_main`, sync).
3. returncode silently discarded (`teardown_worktree`, `prune_rendezvous_branch`, `_reset_to_head`, the cherry-pick `--skip`/`--abort` chains).
4. `raise RuntimeError` (`publish_task_branch`).

A ~30-line helper trio (`git(repo_root, *args)`, `git_ok(...)`, `git_out(...)`) would delete roughly 250–300 lines of ceremony, unify the stderr-trim idiom (`(res.stderr or res.stdout).strip()[:300]` appears 6+ times), give one place to add tracing, and create the missing **test seam**. Today `test_remote_landing.py:388` monkeypatches `nightshift.engine.subprocess.run` *module-wide* to count fetches — intercepting every subprocess in a 3,634-line module to observe one call. That test is one refactor away from silently measuring nothing.

### 1.3 The two-lock design is enforced entirely by docstrings

The contract: `integrate_lock` wraps a whole sync→preview→squash→push section; `landing_lock` guards each primitive; neither is reentrant; nesting deadlocks. Documented three times (`engine.py:1506–1548`, `landing.py:444–448`), enforced zero times. `push_resolved_main` only works because a comment reminds the author that `sync_main_to_origin` "takes landing_lock itself, so it must run OUTSIDE the landing_lock block below." One innocent reorder and the manager hangs forever with no diagnostic.

Two further design flaws:

- `_LANDING_LOCK` / `_INTEGRATE_LOCK` (`engine.py:1502–1503`) are **module-global across all repos**, while the flock underneath is keyed per workspace+repo. Two lands on different repos serialize needlessly in-process — and holding the global thread lock *while blocking on another process's flock* stalls every repo in the manager.
- The reason two locks exist at all is that the primitives each grab `landing_lock` internally, so the orchestrator needed a *different* outer lock. Make the primitives lock-free and move locking to the orchestration boundary, and the second lock, the nesting rules, and the three warning docstrings all disappear.

### 1.4 Canonical-clone working-tree mutation is the root complexity generator — and it has a real bug

`squash_to_main` runs `git merge --squash` in the shared `repo_root`, whose working tree the operator may be using. That one decision transitively requires: `_landing_blockers`, `_stash_operator_work` (`stash create` + `checkout HEAD -- paths`), `_restore_operator_work` (with the conflict → `stash store` fallback), `_reset_to_head`, and in the sync path the `reset --hard` + `_unpushed_commits` + `_replay_commits` operator-rescue machinery, plus the `orphan_squash` / `drop_shas` bookkeeping threaded through `land()` → `sync_main_to_origin` → `_sync_main_to_origin_impl`. It is the most intricate code in the repository, guarding an edge case (operator WIP in a repo an overnight bot owns) the design docs themselves treat as unusual.

Concrete hazards:

- **Data-loss bug: inconsistent stash-failure handling.** `squash_to_main` handles a failed `stash create` correctly (`engine.py:2008`: refuse the land when `wip_sha is None`). The same pattern in `_sync_main_to_origin_impl` (`engine.py:2455–2470`) does not — if `stash create` fails it proceeds straight to `reset --hard` and the operator's uncommitted work is **gone**. Duplicated logic diverging is the predictable cost of finding 1.2.
- **`_replay_commits` silently drops a conflicting operator commit** (`engine.py:2363–2396`). The docstring is honest ("preserved unreachable in the reflog for manual recovery"), but the function returns `None` — no caller, log line, or `LandingResult.detail` ever tells the operator their commit vanished. The reflog is not a UI.
- **Preview/execute duplication.** `land()` previews the merge with `merge_tree_conflicts` (plumbing, tree-safe), then `squash_to_main` performs the *same merge again* with `merge --squash` and a second, independent conflict-detection path (`_conflicted_paths` + `_reset_to_head`). Two conflict branches in `land()` represent one event. Since git ≥ 2.38 is already required, `merge-tree --write-tree` could be the *only* merge: take its tree OID, `commit-tree` + `update-ref`, then advance the working tree with `reset --keep`. That deletes the duplication, most of `_reset_to_head`, and shrinks autostash to the rare `reset --keep`-refused case.

---

## Severity 2 — Duplication / missed abstraction

### 2.1 Three copies of the integrate-and-push loop

`land()`'s retry loop (`landing.py:203–303`), `push_resolved_main` (`landing.py:441–487`), and (to a lesser degree) the `resolve_job` preamble all implement: *sync origin (dropping my own orphan) → produce/replay commit → push → on rejection, loop*. Same `drop_shas` trick, same lock choreography, same stderr trim, same bounded retry. One `integrate_and_push(workspace, repo, remote, produce_commit, *, max_retries)` engine collapses them, so the subtle correctness knowledge (orphan bookkeeping, lock ordering) lives in exactly one place.

### 2.2 `_apply_remote_policy` is a half-abstraction; two push helpers differ only cosmetically

`_apply_remote_policy` (`landing.py:318`) is called **only** from the adopt path; `land()` inlines its own push branch using a *different* helper. `_push_main` (`landing.py:375`) and `_push_head_to_main` (`landing.py:397`) run the identical `git push <remote> HEAD:main` under the identical lock — one mutates a `LandingResult`, the other returns a tuple, and they disagree on whether the remote is configurable. Two entry points for "push main" is how the adopt path and the normal path drift apart.

### 2.3 The land-success finalization block appears three times in `engine.py`

"`sha` obtained → `compute_code_loc` → `teardown_worktree` → `drop_completed_task` (unless evergreen) → emit `TASK_RESULT` → return `TaskResult`" is copy-pasted at `engine.py:2697–2711` (resolve cheap path), `2879–2893` (`_agent_resolve`), and `3476–3495` (`run_task`) — with a fourth near-variant in `app.py:1150–1170`. Extract `finalize_land(...)`.

---

## Severity 3 — Boundary / type-contract problems

### 3.1 `(sha | None, detail, recoverable)` encodes four outcomes in a shape that expresses two

`squash_to_main`'s tuple has a hidden fourth state: `sha is not None and detail` means "landed, but your stash restore conflicted" — a convention callers must simply know (`run_task` re-documents it in a comment at `engine.py:3421–3424`; `landing.py:247` re-explains `recoverable=False → conflict`). Replace with a `SquashOutcome` carrying an explicit kind enum (`LANDED`, `LANDED_STASH_CONFLICT`, `BLOCKED_DIRTY`, `CONFLICT`, `FAILED`).

### 3.2 Stringly-typed `landing_mode` with a non-exhaustive branch

`landing_mode in ("push", "pr")` is compared at six-plus sites across `landing.py`, `app.py` (×2), and `resolve_job.py`; `land()` dispatches with `if/elif/else` where the `else` silently means `"none"` — a typo'd mode lands locally and pushes nothing, with no error. (Also violates the workspace exhaustive-switch rule.) A `LandingMode` enum parsed once at the config boundary turns the silent fallback into a loud one. Same treatment for the `failure_kind` string vocabulary (`merge_conflict`, `merge_rejected`, `publish_failed`, `worker_error`, …) scattered as literals across `engine.py`, `execute.py`, and `app.py`.

### 3.3 `LandingResult` / `ExecuteOutcome` optional-field sprawl

`LandingResult` has a three-state `pushed: bool | None` plus `remote`, `conflict`, `recoverable` whose valid combinations are constrained only by comments. `ExecuteOutcome` (`execute.py:53–78`) has 15 fields and is constructed **12 times** in `execute_work_order` with 8+ keywords each — over a third of the function is outcome-constructor plumbing. A local `_fail(kind, reason)` closure capturing `model/backend/wt_path/tele` would roughly halve the function.

### 3.4 Module-global throttle state with a tests-only reset

`_LAST_ORIGIN_SYNC_CHECK` (`engine.py:2242`) plus `reset_origin_sync_throttle` ("tests only") is the classic sign the state wants to be an object. A shipping function whose only companion API exists for tests should be an injected throttle owned by the manager app — the only place pacing matters, and the throttle is per-process anyway.

---

## What is genuinely good

The *design* layer should be preserved verbatim through any refactor:

- **The invariants are explicit and correct-by-argument.** "The manager is the only writer to canonical main"; fail-closed `head_sha` verification on cross-machine fetch; "gate the re-fetch on the worktree dir, not branch presence, so a retry never lands stale content" (`landing.py:149–153`); branch-preserved-until-confirmed-push; WIP refs namespaced so worker credentials can be scoped. These decisions make or break a system like this, and they're right.
- **The stack-free autostash choice** (`git stash create`, `engine.py:1699–1726`) is genuinely clever: a human running `git stash` mid-land cannot perturb it, and conflicted restores are preserved via `stash store`, never dropped.
- **The surgical divergence rescue** (drop my own orphan squash, replay everything else) is the right semantic, and the docstrings explain *why* at every step.
- **`repos.py` is a model module**: 101 lines, one concern, a crisp two-failure-class contract.
- **Test quality where tests exist is high.** `test_remote_landing.py` covers the scary cases (orphan drop vs. operator-commit rescue, throttle behavior, fail-closed verification, re-fetch-on-retry); `test_run_local.py` covers autostash land/refuse/restore-conflict.

Coverage gaps mapping to the bugs above: the `_sync_main_to_origin_impl` stash-create-failure → `reset --hard` path is untested; `_replay_commits` dropping a conflicting commit is untested; nothing pins `_apply_remote_policy` and the inline push branch together.

---

## Recommended sequence

1. **Introduce the `git()` helper and mechanically convert all ~57 call sites** (pure refactor; fixes the `check=True` outlier by choice rather than accident). This unlocks everything else.
2. **Fix the two correctness hazards now**, independent of restructuring: guard `wip_sha is None` before `reset --hard` in `_sync_main_to_origin_impl`; make `_replay_commits` report dropped SHAs into the sync result/detail.
3. **Extract `nightshift/gitrepo.py`** (locks + worktree + squash + sync + transport) and the low-risk satellites (`loc.py`, `queue_config.py`, `task_files.py`). Engine drops to well under 1k lines without touching logic.
4. **Collapse the locking to one per-repo lock at the orchestration boundary**, with a reentrancy assertion; delete `integrate_lock`.
5. **Unify the retry loops** behind `integrate_and_push(produce_commit)`; delete `_push_main`; fold `_apply_remote_policy` into a single exhaustive `LandingMode` dispatch.
6. **Type the contracts**: `LandingMode`/`FailureKind` enums, `SquashOutcome`, the `_fail` helper in `execute.py`.
7. Longer term, evaluate the `merge-tree --write-tree` + `commit-tree` + `reset --keep` land, which deletes the preview/execute duplication and most of the autostash surface.

Every finding compounds: the missing git seam caused the idiom divergence, the divergence caused the copy-paste, and the copy-paste has now produced its first data-loss bug. Restructure before the next feature lands on this subsystem.
