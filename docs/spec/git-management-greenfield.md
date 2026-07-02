# Git / Codebase Management — Greenfield Design

**Subject:** How Nightshift's git layer would be built from scratch, keeping every behavioral invariant of the current system while eliminating the structural debt catalogued in [`../analysis/git-management-review.md`](../analysis/git-management-review.md).
**Status:** Proposal — nothing here is implemented. Written as a target architecture; the review's "recommended sequence" is the incremental path toward it.
**Non-goals:** Changing the operator-visible model (workspace of child repos, squash-to-main, pause-on-missing-repo, resolve flow). Those survive unchanged.

---

## 0. The one idea

**Landing is a ref operation, not a working-tree operation.**

Every hard problem in the current implementation — autostash, orphan-squash bookkeeping, `reset --hard` rescue, half-merged-tree recovery, most of the locking subtlety — exists because `git merge --squash` mutates the index and working tree of the shared canonical clone that an operator might also be using.

Greenfield, the land pipeline runs entirely on git plumbing (git ≥ 2.38, already required):

```
merge-tree --write-tree <main> <branch>     # conflict check + merged tree OID, touches nothing
commit-tree <tree> -p <main> -m "task: …"   # squash commit object, touches nothing
update-ref refs/heads/main <new> <old>      # atomic, compare-and-swap on the old tip
reset --keep                                # advance the checkout; REFUSES to clobber local WIP
```

Consequences:

- A land can never leave the tree half-merged → `_reset_to_head` and its failure choreography disappear.
- Operator WIP never has to be moved out of the way for a merge → the entire stash-create/restore machinery reduces to one rare case: `reset --keep` refuses, the checkout is simply left behind `main` (refs are authoritative; a "checkout is N behind" notice surfaces in the UI).
- The conflict *preview* and the conflict *detection* are the same call → one conflict code path instead of two.
- `update-ref` with an expected old value gives compare-and-swap semantics on `main`, so a lost race is detected by git itself, not by lock discipline alone.

Everything else in this spec is arranged around that pipeline.

---

## 1. Package layout

The git layer is a package with one concern per module, each well under the 1k-line rule (target: none over ~300 lines):

```
nightshift/git/
  runner.py      GitRunner — the single subprocess seam
  errors.py      GitError, typed failure kinds
  refs.py        rev_parse, is_ancestor, branch_exists, update_ref CAS
  worktrees.py   worktree lifecycle (create/remove/reattach, artifact symlinks)
  landing.py     the plumbing land pipeline + LandOutcome
  sync.py        origin integration (fetch, fast-forward, divergence report)
  transport.py   rendezvous publish/fetch/prune (WIP refs)
  locks.py       RepoLock — one per-repo lock, reentrancy-checked
  store.py       content-store commits (commit_tasks equivalent)
nightshift/loc.py          LOC accounting (self-contained today; stays out of git/)
nightshift/repos.py        unchanged — it is already the model module
```

`engine.py` ceases to exist as a name. Its non-git residents move to `queue_config.py`, `task_files.py`, `preflight.py`, `prompts.py`, and a slim `runner/` for `run_task`/`run_queue`/`Controller`. Nothing outside `nightshift/git/` ever spawns a git subprocess, and nothing outside it imports a `_`-prefixed name from it.

---

## 2. The subprocess seam (`runner.py`)

One class, injected everywhere, faked in tests:

```python
@dataclass(frozen=True)
class GitResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool: ...
    @property
    def detail(self) -> str:        # the (stderr or stdout).strip()[:300] idiom, once
        ...

class GitRunner:
    """Runs git in a repo. The ONLY place `subprocess` appears in the git layer."""
    def __init__(self, repo_root: Path): ...
    def run(self, *args: str) -> GitResult: ...           # never raises on rc != 0
    def out(self, *args: str) -> str | None: ...          # stdout or None on failure
    def must(self, *args: str) -> GitResult: ...          # raises GitError(detail) on failure
```

Rules:

- **One error policy per call site, chosen explicitly**: `run` when the caller branches on the result, `out` for queries, `must` when failure is exceptional. No `check=True` accidents, no silently discarded returncodes — a discarded failure is written as `run(...)` with a comment saying why it is best-effort.
- Central place for tracing (`NIGHTSHIFT_GIT_TRACE=1` logs every argv + rc + duration), timeouts, and env scrubbing.
- Tests fake `GitRunner`, not `subprocess.run`. Integration tests keep using real repos via `tests/_workspace.py` — that pattern is good and stays.

---

## 3. Typed contracts (`errors.py`, result types)

No tuples, no stringly-typed modes. The vocabulary is defined once:

```python
class LandingMode(StrEnum):
    NONE = "none"
    PUSH = "push"
    PR = "pr"
    # parsed once at the config boundary; an unknown string raises there.

class LandKind(StrEnum):
    LANDED = "landed"
    NO_CHANGES = "no_changes"
    ADOPTED = "adopted"            # agent landed on main during the run
    CONFLICT = "conflict"          # content conflict → resolve work-order
    CHECKOUT_BEHIND = "checkout_behind"  # landed; operator checkout not advanced
    PUSH_REJECTED = "push_rejected"      # retries exhausted; branch preserved
    TRANSPORT_FAILED = "transport_failed" # rendezvous fetch/verify failed

@dataclass(frozen=True)
class LandOutcome:
    kind: LandKind
    sha: str | None = None
    detail: str = ""
    conflicts: tuple[str, ...] = ()       # paths, when kind is CONFLICT
    dropped_commits: tuple[str, ...] = () # rescue casualties — NEVER silent
    pr_url: str | None = None
```

Every consumer dispatches on `kind` with an exhaustive `match`/`switch` (a `never` default per workspace rule), so adding a kind breaks compilation-time checks (`ruff`/`ty` exhaustiveness), not production nights. The `failure_kind` strings the manager stores are derived from `LandKind` in one mapping function, not scattered literals.

Anything the pipeline *chose to discard* (a conflicting operator commit during a divergence rescue) is carried in `dropped_commits` and surfaced to the run record and the UI. The reflog is a recovery mechanism, not a notification channel.

---

## 4. Locking (`locks.py`)

One lock, one rule.

```python
class RepoLock:
    """Per-(workspace, repo) mutual exclusion for canonical-repo mutation.

    Two layers: an in-process lock from a registry keyed by (workspace, repo) —
    NOT a module-global shared across repos — and a flock at
    <workspace>/.worktrees/<repo>/.lock for cross-process exclusion.
    Re-entry raises RuntimeError immediately instead of deadlocking.
    """
```

- **Held only at orchestration boundaries**: `land()`, `push_resolved()`, `recover()`, the CLI's land path. Each wraps its whole critical section (sync → preview → commit → push) in one `with repo_lock(workspace, repo):`.
- **Primitives are lock-free.** `sync.py`, `landing.py`, `transport.py` functions document "caller holds the repo lock" and *assert* it (the lock object exposes `is_held_by_current_thread`). There is no second lock, so there is no nesting contract to document or violate.
- Lands on different repos proceed concurrently (the registry is per-repo; the current design's global `threading.Lock` serialization across repos disappears).
- Long agent work (resolve) still runs **outside** the lock; only the integrate-and-push section takes it — same as today's intent, now structurally guaranteed rather than comment-guaranteed.

---

## 5. The land pipeline (`landing.py`)

One function owns the sequencing that currently exists in three copies (`land()`'s loop, `push_resolved_main`, `resolve_job`'s preamble):

```python
def integrate_and_push(
    ctx: RepoContext,                 # workspace, repo, GitRunner, RepoLock, remote config
    produce: Callable[[str], ProduceResult],  # base_sha -> commit built on it (plumbing)
    *,
    mode: LandingMode,
    max_retries: int = 3,
) -> LandOutcome:
    """sync origin → produce commit on fresh tip → CAS update-ref → push →
    on rejection, drop our own commit and retry. Holds the repo lock throughout."""
```

- The normal land passes `produce = squash(branch)`; a resolve passes `produce = cherry(resolved_sha)`. The orphan-squash / `drop_shas` bookkeeping is *internal* to this function — the greenfield version doesn't need it at all, because the produced commit is never on `main` until the CAS succeeds; a rejected push means we simply re-produce from the new tip. **The orphan problem is designed out, not managed.**
- Squash production is pure plumbing (`merge-tree --write-tree` → `commit-tree`), so a conflict is detected exactly once and reported as `LandKind.CONFLICT` with paths. No second detection path, no tree cleanup.
- After a successful ref update, the checkout advance (`reset --keep`) is best-effort: refusal → `LandKind.CHECKOUT_BEHIND` (still a success).
- Remote policy is one exhaustive dispatch on `LandingMode` — `NONE` stops after the ref update, `PUSH` pushes `main` (CAS-style, non-force), `PR` pushes `task/<queue>-<task>` and drives `gh`. There is exactly one "push main" helper.
- Cross-machine landing keeps today's exact invariants: gate on absent worktree dir (forces re-fetch + re-verify on every retry), fetch the WIP ref, **fail closed** on `head_sha` mismatch, prune the transport ref only after a confirmed land.

`land()` proper becomes a thin composition: adopt-check → `integrate_and_push(squash)` → worktree teardown. Target size for the whole module: under 250 lines including docstrings.

## 5.1 What autostash becomes

Nothing needs stashing to *land* (plumbing doesn't touch the tree). The only tree mutation is the optional checkout advance, and `reset --keep` natively refuses rather than clobbers. The origin-sync fast-forward path uses the same `reset --keep`. Therefore:

- `_stash_operator_work` / `_restore_operator_work` / `AUTOSTASH_MESSAGE` — deleted.
- The `autostash_operator_work` config knob — deleted (nothing destructive remains to opt out of).
- The data-loss class found in the review (`stash create` failure → `reset --hard`) — structurally impossible: **`reset --hard` does not appear in the codebase.** Divergence rescue replays operator commits with cherry-picks onto a temp branch and CAS-swaps the ref; casualties are reported in `dropped_commits`.

---

## 6. Worktrees (`worktrees.py`) and transport (`transport.py`)

Largely today's logic, relocated and normalized:

- Deterministic naming (`task-local/<queue>/<task>` branches, `.worktrees/<repo>/…` dirs outside the target repo) is kept verbatim — it is correct.
- `create()` uses `runner.must` (a failed `worktree add` is a task-fatal, *typed* error the worker maps to `failure_kind=worktree_failed`, never a raw traceback).
- `remove()` is explicitly best-effort (`runner.run` + comment); `reattach()` replaces `_ensure_worktree_for_branch`.
- Transport keeps the WIP-ref namespace and scoped-credential story unchanged. `publish()` returns `(ref, sha)` or raises a typed `TransportError`; `fetch_verified()` fuses fetch + `head_sha` comparison so an unverified branch can never exist locally under the task's branch name.

---

## 7. Origin sync (`sync.py`)

- `Throttle` is an injected object owned by the manager app (per-repo monotonic timestamps), not module-global state with a tests-only reset.
- `sync(ctx, *, allow_divergence_rescue: bool) -> SyncOutcome` reports what happened (`CURRENT`, `FAST_FORWARDED`, `DIVERGED_KEPT`, `RESCUED(dropped_commits=…)`) instead of returning a bare SHA.
- Fast-forward advances the checkout with `reset --keep`; the lockfile-changed → invalidate-preflight-marker hook is kept but lives behind an event/callback so `sync.py` doesn't import preflight.

---

## 8. Content store (`store.py`)

`commit_tasks` survives nearly as-is (it is decent code) but:

- goes through `GitRunner`,
- returns a typed `StoreCommit | None` instead of a bare sha-or-None with a docstring explaining three different `None` meanings,
- the 24 scattered `commit_tasks(tasks_root, f"nightshift: …")` call sites in `app.py` collapse behind intent-named wrappers (`store.record_task_created(...)` etc.), giving one place for message conventions.

---

## 9. State machine at the manager boundary

The manager's submit handler currently interleaves store updates, SSE emits, quarantine checks, and landing in a ~200-line endpoint. Greenfield, the landing side is a single call returning `LandOutcome`, and the handler is a table:

| `LandOutcome.kind` | run status | task state | side effects |
|---|---|---|---|
| `LANDED` / `CHECKOUT_BEHIND` | completed | cleared | drop brief, LOC, teardown |
| `ADOPTED` | completed | cleared | drop brief |
| `NO_CHANGES` | completed | cleared (or quarantine) | — |
| `CONFLICT` | error | blocked ("needs resolve") | keep branch, auto-resolve spawn |
| `PUSH_REJECTED` | error | blocked | keep branch, auto-resolve spawn |
| `TRANSPORT_FAILED` | error | blocked | keep WIP ref for retry |

Exhaustive dispatch; a new kind cannot be silently mishandled.

---

## 10. Testing strategy

- **Unit**: every `git/` module tested against a `FakeGitRunner` (scripted results) — fast, no repos, covers error branches that are impractical to provoke with real git (fetch failures, push rejections mid-loop).
- **Integration**: keep `tests/_workspace.py` real-repo tests for the pipeline end-to-end (they are the current suite's strength). Add the cases the review found untested: rescue with a conflicting operator commit (asserting `dropped_commits` is surfaced), checkout-behind land, CAS race (two concurrent `integrate_and_push` on one repo).
- **Property**: one invariant test per module docstring claim — e.g. "after any `land()` outcome, `git status --porcelain` in `repo_root` is unchanged from before the call" (trivially true now that landing is plumbing-only; this test *prevents regression to working-tree merges*).

---

## 11. Invariants (unchanged from today, restated as the contract)

1. The manager is the sole writer of each target repo's `main`; workers only produce task branches.
2. `main` moves only via compare-and-swap ref updates under the per-repo lock.
3. A task branch (and its WIP ref) is destroyed only after a confirmed land.
4. Cross-machine content lands only after fail-closed `head_sha` verification, re-verified on every retry.
5. Operator working-tree state is never destroyed by Nightshift — at worst the checkout lags `main`, and anything the rescue path drops from `main`'s history is reported, never silent.
6. Briefs never enter a target repo; only the implementation squash lands.
7. All persisted repo references are workspace-relative bare slugs (`repos.py`, unchanged).

## 12. What is deliberately *not* rebuilt

- `repos.py` — already the model module; ships as-is.
- The scheduler, store, worker protocol, resolve-agent prompting — out of scope; they consume `LandOutcome`/`FailureKind` but their logic is untouched.
- The behavioral surface (config keys other than the deleted `autostash_operator_work`, HTTP API shapes, UI states) — operators should not notice the rewrite, except that "your uncommitted changes were stashed" messages disappear and a "checkout behind main" notice appears.
