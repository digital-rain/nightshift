# Nightshift — Pre-validation (`pre_validate: off | on | auto`)

**Subject:** A manager setting that runs a queue's validate command against the target repo's `main` — on the manager, in its own checkout, before any worktree is cut, any lease is created, or any agent API is invoked — so tasks that would fail validation because `main` itself is broken fail *before* spending agent budget.
**Status:** Proposed — for implementation. Where this doc and the code disagree once implemented, the code governs and this doc should be updated.
**Default:** `off`. Pre-validation doubles validate time per change to main, so it is opt-in.

---

## 0. The one idea

Post-run validation catches two very different failures: the agent broke something, or `main` was already broken (in the non-DB path) before the agent started. The second kind wastes an entire agent run per task until an operator notices. Pre-validation moves that check to the front: the manager — which already owns the repo checkout, the origin sync, and the landing lock — runs `just validate` (or the queue's configured command) on `main` itself. No branch, no worktree: the test is simply *main passes validation*.

- **Pass** → cached against the validated commit; dispatch proceeds. No re-run until `main`'s head moves.
- **Fail** → the affected queues are paused with a new reason `pre_validation`, the validate output is stored as the queue's **blocked reason**, an event + Slack notification fires, and no lease is ever created. The operator fixes main, sets the queue back to READY (or presses Play), and dispatch resumes.

`auto` mode keeps the time cost near zero: run post-validation only, until a task on a queue fails post-validation — then that queue escalates to pre+post validation until a pre-validation run passes (or the operator clears the blockage), at which point it drops back to post-only.

## 1. Today's state (what this spec changes)

- Validation runs **only post-agent**, on the worker, in the task worktree (`worker/execute.py` — `run_interruptible(validate_argv, cwd=wt_dir, ...)`). A validation failure becomes `FailureKind.VALIDATION_ERROR` → `RetryAction.HOLD` → `TaskHold(TaskHoldKind.BLOCKED, "validation failed: ...")` on the *task*.
- The manager already refreshes `main` before assignment (poll path in `manager/api_worker.py`: throttled `sync_main_locked` via the per-repo `ExecutorPool`, then `base_ref = canonical_head(...)`), and is the sole mutator of canonical main (`manager/landing.py`). It runs `gh` subprocesses in the repo already; it runs no validate command today (`validate_on_integrate` exists in config but is wired to nothing — untouched by this spec).
- Queue transport state is durable in `nightshift.queue_state` (`paused_reason`: `operator | consecutive_failures | retry_failed`, `mode`), with pause/play via `POST /api/transport`. There is no queue-level blocked state and no queue-level blocked reason.
- The operator UI has segmented controls (task Status, transport mode) but no queue READY | PAUSED | BLOCKED control. The worker UI shows nothing about queue pauses.

## 2. The setting

One manager-level setting; **no per-queue override** (the decision is the manager's, made before assigning work).

| Key | Values | Default | Where |
|---|---|---|---|
| `pre_validate` | `"off"` \| `"on"` \| `"auto"` | `"off"` | `OperatorConfig` in `src/nightshift/config/manager.py`, flattened into `.nightshift/manager.json`; env override `NIGHTSHIFT_PRE_VALIDATE`; surfaced automatically on the manager settings page via the existing registry (`options=[...]` renders the enum control, like `landing_mode`). `apply="restart"` — the poll path reads it from the resolved `ManagerConfig`. |

Field metadata (category `Worker execution policy`, next to `validate_cmd`):

```python
pre_validate: str = field(default="off", metadata=meta(
    category="Worker execution policy", label="Pre-validate",
    desc=(
        "Run the queue's validate command on the target repo's main "
        "(on the manager, before any worktree or agent run) and refuse to "
        "dispatch while it fails. off = never; on = always; auto = only "
        "after a task on that queue fails post-validation, until a "
        "pre-validation run passes again."),
    apply="restart", env="NIGHTSHIFT_PRE_VALIDATE",
    options=["off", "on", "auto"]))
```

Plumb through all four places that pattern already exists for `landing_mode`: the `OperatorConfig` field, `load_manager_settings` (env > file > default, unknown value → fail the load loudly with `ValueError`), `save_manager_settings`, and the flat `ManagerConfig` projection + `load_manager_config`.

The *command* is not a new setting: pre-validation resolves the queue's validate command exactly as work orders do — per-queue `config.json` `"validate"` key, else the manager default (`just validate`), via the existing `queue_config.resolve_validate_cmd`. A queue whose validation is explicitly disabled (empty string) is **never pre-validated** (there is nothing to check that the queue would check later).

## 3. Gate semantics — when does a queue require pre-validation?

Pure function, new module `src/nightshift/prevalidate.py`:

```python
def gate(mode: str, queue_label: str, escalated: frozenset[str],
         validate_argv: list[str] | None) -> bool:
    """True when dispatch from this queue must be preceded by a passing
    pre-validation of its target repo's main."""
    if validate_argv is None:      # queue opted out of validation entirely
        return False
    match mode:
        case "on":
            return True
        case "auto":
            return queue_label in escalated
        case "off":
            return False
        case _:
            raise ValueError(f"unknown pre_validate mode: {mode}")
```

**Escalation lifecycle (`auto` mode)** — durable per-queue flag `pre_validate_escalated` in `nightshift.queue_state`:

- **Set** when a worker submit reports `failure_kind == FailureKind.VALIDATION_ERROR` for a task on that queue (the hook lives in the submit handler in `manager/api_worker.py`, after the transition is applied — transitions stay pure; they don't know the setting).
- **Cleared** when either (a) a pre-validation run covering that queue **passes**, or (b) the operator clears the blockage (READY + Save, or transport Play) on that queue.
- Ignored (never read) in `off` and `on` modes; stale flags are harmless.

## 4. Execution mechanics

### 4.1 Where it runs

In the target repo's base checkout, `<workspace>/<repo>`, on whatever `main` currently is. **No branch, no worktree.** The job is submitted to the existing per-repo `ExecutorPool` (`app.state.git_executors`), whose worker thread already wraps every job in the repo's `RepoLock` — so pre-validation is automatically serialized with syncs and landings and can never race a checkout mutation.

```python
PRE_VALIDATE_TIMEOUT_SECONDS = 1800  # a hung validate must not wedge the repo executor
BLOCKED_DETAIL_MAX_CHARS = 2000

@dataclass(frozen=True)
class PrevalidateOutcome:
    repo: str
    head: str            # the commit actually validated (read under the lock)
    cmd: str             # display string, e.g. "just validate"
    ok: bool
    detail: str          # tail of combined output (or the timeout message)

def run_prevalidate_job(workspace: Path, repo: str, argv: list[str]) -> PrevalidateOutcome:
    """Executor job (runs on the repo executor thread, RepoLock held)."""
    repo_root = workspace / repo
    head = canonical_head(repo_root)
    try:
        proc = subprocess.run(
            argv, cwd=repo_root, capture_output=True, text=True,
            timeout=PRE_VALIDATE_TIMEOUT_SECONDS,
        )
        ok = proc.returncode == 0
        detail = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-BLOCKED_DETAIL_MAX_CHARS:]
    except subprocess.TimeoutExpired:
        ok, detail = False, f"pre-validation timed out after {PRE_VALIDATE_TIMEOUT_SECONDS}s"
    return PrevalidateOutcome(repo=repo, head=head, cmd=shlex.join(argv), ok=ok, detail=detail)
```

### 4.2 Cache + in-flight tracking — `PreValidator`

One instance on `app.state.prevalidator`, created in `create_app`. Deliberately in-memory (a manager restart re-runs one validation — harmless; the durable pieces are the queue pause + blocked reason in the store).

```python
class PreValidator:
    """Cache of pre-validation verdicts + in-flight job dedup, per repo."""
    # verdicts: dict[str, PrevalidateOutcome]   — latest outcome per repo
    # inflight: set[str]                        — repos with a job running

    def verdict(self, repo: str, head: str, cmd: str) -> PrevalidateOutcome | None:
        """The cached outcome, if it still applies.

        A PASS applies only to the exact (head, cmd) it validated — main
        moving invalidates it. A FAIL applies to the cmd regardless of head:
        it stands until the operator clears it (clear()), so a push to main
        alone does not silently unblock a failed repo. Anything else -> None.
        """

    def start(self, repo: str, argv: list[str], on_done) -> None:
        """Submit run_prevalidate_job to the repo executor unless already
        in flight; schedule an asyncio completion task that stores the
        outcome and awaits on_done(outcome)."""

    def clear(self, repo: str) -> None:
        """Operator reset: drop the cached verdict so the next poll revalidates."""
```

The completion-task pattern (executor future → `asyncio.wrap_future` → tracked task applying async effects) mirrors the deferred-land path; register the pending tasks the same way so shutdown drains them.

**FAIL is sticky by design**: recovery is an explicit operator act (§6), not an incidental push. This also means the *operator's* fix lands, they press READY, and revalidation confirms — the state machine never self-clears on hope.

### 4.3 Poll-path integration (`manager/api_worker.py`)

Pre-validation gating is an **exclusion pass over candidate queues before `pick_next`**, exactly like the existing `repo_excluded` pass — so a repo mid-validation (or blocked) never starves unrelated repos/queues:

```python
# after repo_excluded is built, before pick_next:
pv: PreValidator = app.state.prevalidator
if cfg.pre_validate != "off":
    escalated = frozenset(await store.prevalidate_escalations())
    for q in list(candidates_by_queue):
        label = queue_label(q)
        q_repo = next((c.repo for c in candidates_by_queue[q] if c.repo), None)
        if q_repo is None:
            continue
        argv = resolve_validate_cmd(load_queue_config(tasks_root, playlists_mod.tasks_rel(q)))
        if not prevalidate.gate(cfg.pre_validate, label, escalated, argv):
            continue
        head = canonical_head(workspace / q_repo)
        v = pv.verdict(q_repo, head, shlex.join(argv))
        if v is not None and v.ok:
            continue                          # validated at this head — dispatch freely
        if v is None:
            pv.start(q_repo, argv, on_done=partial(_on_prevalidate_done, ...))
        elif not v.ok and label not in queue_pauses:
            # A standing FAIL, and this queue became gated after the failure
            # (e.g. auto-escalated later): apply the same blocking effects so
            # it shows BLOCKED instead of silently never dispatching.
            await _apply_prevalidate_failure(q, v)
        del candidates_by_queue[q]            # pending or failed: not dispatchable now
```

(`_apply_prevalidate_failure` is the per-queue half of `_on_prevalidate_done`'s failure branch — pause, blocked reason, emit, Slack — factored so both call sites share it.)

Notes:

- While a validation is pending, gated queues simply yield no work; workers keep polling on their normal cadence — no new wait mechanics.
- Queues already paused (`pre_validation` or otherwise) never reach this pass — the existing `queue_pauses` filter drops them earlier.
- **Accepted race:** the origin sync for the *chosen* candidate happens after `pick_next` and may advance main past the validated head within one poll. The check asserts "main is healthy," not "this exact sha is healthy"; the window is one `git_refresh_seconds` sync and the next poll revalidates the new head. Not worth double-checking post-sync.
- The task that triggered the gate is **never touched** — no lease exists, no task state is written. When the queue unblocks, the same task dispatches normally.

## 5. Failure → queue BLOCKED

`_on_prevalidate_done(outcome)` — the async completion effect, in `api_worker.py` (wired with the same injected seams the handlers already use: `_store`, `_emit`, `_broadcast`):

**On `ok == False`,** for every *gated* queue targeting `outcome.repo` (gated = `gate(...)` true for that queue at this moment — all validation-enabled queues on the repo in `on` mode; the escalated ones in `auto` mode):

1. `await store.set_queue_pause(label, "pre_validation")`
2. `await store.set_queue_blocked(label, reason)` where `reason = f"pre-validation failed on {repo}@{outcome.head[:12]} ({outcome.cmd}): {outcome.detail}"`
3. `await _emit("queue_paused", queue=q, payload={"reason": "pre_validation", "repo": repo, "detail": ...})` — persisted to the event log and broadcast over SSE so the UI converges live.
4. Slack, best-effort: `await asyncio.to_thread(post_queue_blocked, workspace, tasks_root, q, repo, reason)` (§7.3).

**On `ok == True`:** cache the pass (done in `PreValidator`); in `auto` mode, clear `pre_validate_escalated` for every queue targeting that repo (`await store.set_prevalidate_escalation(label, False)`) — the queue drops back to post-only validation.

Tasks are never marked; the blockage lives entirely on the queue. Because `pre_validation` is a `paused_reason`, everything downstream already works: the poll path's pause filter skips the queue, poll responses carry it in `queue_pauses` (workers back off exactly as for operator pauses), and a manager restart preserves it (durable store).

### 5.1 Store + schema changes

New migration `assets/migrations/20260802000001_nightshift_queue_blocked.sql` (numbering must sort after the latest existing migration):

```sql
-- Queue-level blocked reason + auto-mode pre-validation escalation.
ALTER TABLE nightshift.queue_state ADD COLUMN IF NOT EXISTS blocked_reason text;
ALTER TABLE nightshift.queue_state
    ADD COLUMN IF NOT EXISTS pre_validate_escalated boolean NOT NULL DEFAULT false;
```

Mirror both columns in the sqlite schema (`manager/store_sqlite.py`, `CREATE TABLE nightshift.queue_state`).

Store protocol + both implementations (`manager/store.py`), following the exact shape of `queue_pauses`/`set_queue_pause` (UPSERT keyed by queue label):

```python
async def queue_blocked_reasons(self) -> dict[str, str]: ...
async def set_queue_blocked(self, queue_label: str, reason: str | None) -> None: ...
async def prevalidate_escalations(self) -> set[str]: ...
async def set_prevalidate_escalation(self, queue_label: str, escalated: bool) -> None: ...
```

Extend the prune predicate — a `queue_state` row now exists while it carries *any* non-default state:

```sql
DELETE FROM nightshift.queue_state
WHERE queue = $1 AND paused_reason IS NULL AND mode IS NULL
  AND blocked_reason IS NULL AND NOT pre_validate_escalated
```

`rename_queue` already migrates `queue_state` rows wholesale; no change needed.

### 5.2 Escalation hook (submit path)

In the submit handler in `api_worker.py`, immediately after a successful `_finish(...)` (both the synchronous and the deferred-land completion paths reach a transition; only the failure path matters):

```python
if (cfg.pre_validate == "auto"
        and body.failure_kind == FailureKind.VALIDATION_ERROR):
    await store.set_prevalidate_escalation(label, True)
```

Scope note: only the worker submit path escalates. Resolve-runner validation failures re-enter as resolve outcomes and are out of scope here.

## 6. Operator recovery — READY | PAUSED | BLOCKED

### 6.1 Wire state

`_queue_state` in `manager/api_operator.py` keeps its `state` values (`idle | playing | paused` — wire compat with the worker backoff logic) and gains one field:

```python
"blocked_reason": blocked.get(key),   # from store.queue_blocked_reasons(); None when clear
```

The UI derives the three-way display state: **BLOCKED** when `pause_reason == "pre_validation"`, **PAUSED** for any other pause reason, **READY** otherwise (idle/playing).

### 6.2 Transport semantics (the clearing act)

`POST /api/transport` is the single mutation path; the segmented control maps onto it at Save time. Extend the existing actions:

- **`play`** (READY + Save, or the transport Play button) — in addition to today's behavior (clear pause, reset failure watch, release failed tasks), also:

```python
await store.set_queue_blocked(key, None)
await store.set_prevalidate_escalation(key, False)
repo = _queue_repo(target)               # already-injected helper in api_operator
if repo:
    app.state.prevalidator.clear(repo)   # next gated poll revalidates fresh
```

  Clearing is per-queue but the verdict cache is per-repo: READY on one blocked queue revalidates the repo; sibling queues stay paused until individually resumed (or until they're resumed and the fresh validation passes). This is the agreed contract — READY + Save clears *any* blockage on that queue.

- **`pause`** (PAUSED + Save, or Pause button) — unchanged (`paused_reason = "operator"`). Pausing a BLOCKED queue overwrites `paused_reason` but keeps `blocked_reason` visible until a Play clears it.

BLOCKED is **not** operator-settable; the segmented control renders it as the active-but-disabled segment when the system set it.

## 7. Surfacing

### 7.1 Manager operator UI (`assets/ui/app.js`, `index.html`, `style.css`)

- **Playlist/queue detail (`buildPlaylistInfoContent`)** — a labeled segmented control `Status: READY | PAUSED | BLOCKED` above the existing Name/Repository/Validate fields, rendered with the `.segmented`/`.seg-opt` pattern (same look as the task Status control). Current segment derives from `state.players[name]` per §6.1. READY and PAUSED are clickable (staged into the draft); BLOCKED is display-only (rendered disabled unless active). When `blocked_reason` is set, show it beneath the control in a monospace error block (the validate output tail is the payload — it must be readable). **Save**: if the staged segment differs from current, call `POST /api/transport` with `{action: "play"|"pause", queue}` before the existing `PUT /api/playlists/...` field save. Works identically for the library (`main`) info pane.
- **Playlist rows (`playlistRow`/`libraryRow`)** — the existing paused badge logic gains a distinction: `pause_reason === "pre_validation"` renders a red **blocked** badge (error styling) instead of the amber paused badge.
- **Pause banner** — add to `PAUSE_REASON_COPY`:

```js
pre_validation: "Blocked: pre-validation failed — main is broken for this queue's repo. Fix main, then set the queue to READY (or press Play) to revalidate and resume.",
```

  The banner component already renders for any known reason; when `blocked_reason` is present append its first line.

### 7.2 Worker UI (`worker/loop.py`, `worker/local_store.py`, `worker/ui_app.py`, `assets/ui-worker/`)

The worker already receives `queue_pauses` (label → reason) on every poll response. Surface it:

- `WorkerLoop._sync_backoff_with_manager` already consumes the map — additionally store it: `self.local.set_queue_pauses(queue_pauses)` (new `LocalStore` field + method, in-memory like `now`).
- `ui_app.py`: new `GET /api/queue-pauses` returning the stored map.
- `ui-worker/app.js`: poll it alongside `/api/now`; when any queue this worker serves has reason `pre_validation`, render a red banner ("queue *X* blocked: pre-validation failed on main — see manager"); other reasons render the existing amber paused treatment. Full blocked detail intentionally lives only in the manager UI.

### 7.3 Slack

New module-level helper in `slack/notify.py`, callable from the manager (which has no event-listener notifier wired today):

```python
def post_queue_blocked(workspace: Path, tasks_root: Path,
                       queue: str | None, repo: str, reason: str) -> None:
    """Post a plain (non-threaded) activity-channel message when a queue is
    blocked by pre-validation. Resolves the layered slack config for the
    queue exactly like notifier_for_queue; a disabled/half-configured Slack
    collapses to a no-op; never raises."""
```

Message shape: `":no_entry: queue *<label>* blocked — pre-validation failed on <repo> (main)"` with the first ~10 lines of the reason in a code block. Called best-effort from `_on_prevalidate_done` (§5) via `asyncio.to_thread`.

## 8. Invariants & edge cases

1. `pre_validate: off` (the default) changes **nothing** — no gate pass, no executor jobs, no new columns written. Existing deployments upgrade inert.
2. A pre-validation failure never creates a lease, run row, worktree, or agent call, and never writes task state. The queue is the unit of blockage.
3. A queue with validation explicitly disabled (`"validate": ""`) is never gated, in any mode.
4. A pre-validation PASS is valid only for the exact (repo, head, cmd) validated; a FAIL stands until an operator clears it (§4.2).
5. Pre-validation jobs serialize with syncs/landings via the existing per-repo executor + `RepoLock`; the 30-minute timeout bounds how long a hung validate can occupy that lane.
6. Escalation flags, pauses, and blocked reasons are durable (store); the verdict cache is not (restart ⇒ at most one redundant validate).
7. Multi-queue repos: a failure blocks every *gated* queue on that repo at once (main is broken — nothing should spend money on it); non-gated queues on the same repo are unaffected.
8. Workers need no changes to their execution path — they see `pre_validation` as just another pause reason in `queue_pauses`.

## 9. Implementation plan

Ordered, each task independently testable and committed. Run tests with `uv run pytest tests/<file> -x -q` from the nightshift repo root; run `just validate` before the final commit of the series.

### Task 1 — config: the `pre_validate` setting

**Files:** `src/nightshift/config/manager.py`; tests in `tests/test_config_model.py` (or `tests/test_nightshift_config.py`, matching where `landing_mode` load/save is tested) and `tests/test_settings_api.py`.

1. Add the `OperatorConfig.pre_validate` field with the metadata from §2.
2. `load_manager_settings`: resolve `os.environ.get("NIGHTSHIFT_PRE_VALIDATE") or data.get("pre_validate") or "off"`; reject values outside `{"off","on","auto"}` with `ValueError` (match the `landing_mode` loud-failure pattern).
3. `save_manager_settings`: write `"pre_validate"`.
4. `ManagerConfig` + `load_manager_config`: add the flat projection field.
5. Tests: default is `off`; file value round-trips; env wins over file; invalid value raises; the settings API GET exposes the field with its options and PUT validates it.

### Task 2 — store: blocked reasons + escalation flags

**Files:** new `src/nightshift/assets/migrations/20260802000001_nightshift_queue_blocked.sql`; `src/nightshift/manager/store.py`; `src/nightshift/manager/store_sqlite.py`; tests in `tests/test_nightshift_store.py`.

1. Migration + sqlite schema per §5.1.
2. The four store methods (§5.1) on the protocol and both implementations, and the widened prune predicate.
3. Tests (run against the in-memory store like the existing queue_state tests): set/read/clear blocked reason; set/read/clear escalation; a row with only a blocked reason (or only an escalation) survives pruning; clearing everything prunes the row; `rename_queue` carries both columns.

### Task 3 — `prevalidate.py`: gate + PreValidator

**Files:** new `src/nightshift/prevalidate.py`; new `tests/test_prevalidate.py`.

1. `gate()` per §3 (exhaustive over the three modes; unknown mode raises).
2. `run_prevalidate_job` per §4.1 (import `canonical_head` from the git layer; a repo-executor job, so it may assume the lock).
3. `PreValidator` per §4.2: verdict lookup semantics (pass keyed to head+cmd, fail sticky per cmd), in-flight dedup, `start` wiring an executor submit + tracked asyncio completion task, `clear`.
4. Tests, no real git needed beyond a tmp repo or a monkeypatched `canonical_head`: gate truth table (3 modes × escalated/not × validate-disabled); pass verdict invalidated by head move but not returned for a different cmd; fail verdict survives head move; `clear` drops it; `start` dedups an in-flight repo; job failure captures output tail; timeout produces a failed outcome (monkeypatch `PRE_VALIDATE_TIMEOUT_SECONDS` small, command `sleep`).

### Task 4 — manager integration: poll gate, failure effects, escalation hook, transport clear, wire state

**Files:** `src/nightshift/manager/api_worker.py`, `src/nightshift/manager/api_operator.py`, `src/nightshift/manager/app.py`; tests in `tests/test_nightshift_manager.py`.

1. `create_app`: construct `app.state.prevalidator = PreValidator(...)` wired to `executors`; track its completion tasks like the pending-land tasks for drain/shutdown.
2. Poll path: the exclusion pass per §4.3 (skeleton given there — keep it a small helper so the poll handler stays readable).
3. `_on_prevalidate_done` per §5: pause + blocked reason + `queue_paused` emit/broadcast + Slack thread-off + auto de-escalation on pass. Gated-queue enumeration needs queue→repo resolution; reuse the same helpers the candidates already carry (`Candidate.repo`) or the `_queue_repo` seam.
4. Submit hook per §5.2.
5. Transport `play` additions per §6.2; `_queue_state` gains `blocked_reason` per §6.1.
6. Tests (in-memory store, stub executor or a real `ExecutorPool` on a tmp workspace, validate cmd = `sh -c 'exit 1'` / `exit 0`):
   - `on` + failing cmd: first poll returns no work for the gated queue and starts one job; after completion the queue is paused `pre_validation` with a blocked reason; **no attempt row exists**; an unrelated queue on a healthy repo still dispatches in the same poll.
   - `on` + passing cmd: first poll no-work (pending), post-completion poll dispatches; no re-run while head unchanged; head move ⇒ one new job.
   - `auto`: no gating initially; a submit with `failure_kind=VALIDATION_ERROR` sets escalation; next poll gates; a pass clears escalation; alternatively transport `play` clears escalation + blocked reason + pause and the next poll revalidates (verdict cache cleared).
   - `off`: zero interaction (no jobs, no store writes).
   - `/api/state` exposes `blocked_reason`; transport `pause` on a blocked queue keeps the reason, `play` clears it.

### Task 5 — Slack `post_queue_blocked`

**Files:** `src/nightshift/slack/notify.py`; tests in `tests/test_slack_notify.py`.

1. The helper per §7.3, resolving config like `notifier_for_queue` and posting via the injectable client; disabled config ⇒ no call; any exception swallowed (match the notifier's never-raise discipline).
2. Tests with the existing fake client: enabled config posts once with queue/repo/reason in the payload; disabled config posts nothing; a raising client does not propagate.

### Task 6 — manager UI

**Files:** `src/nightshift/assets/ui/app.js` (+ `style.css` if the blocked badge/error block need styles).

1. Playlist-info Status segmented control + blocked-reason block + Save→transport mapping per §7.1.
2. Blocked badge on playlist/library rows; `PAUSE_REASON_COPY.pre_validation` entry.
3. Manual check: with a deliberately failing validate on a test queue (`"validate": "false"`), the row shows blocked, the info pane shows BLOCKED + reason, READY + Save resumes dispatch.

### Task 7 — worker UI

**Files:** `src/nightshift/worker/local_store.py`, `src/nightshift/worker/loop.py`, `src/nightshift/worker/ui_app.py`, `src/nightshift/assets/ui-worker/app.js` (+ `worker.css`); tests in `tests/test_nightshift_worker.py` for the store/endpoint pieces.

1. `LocalStore.set_queue_pauses` / `queue_pauses()`; loop stores the map each poll; `GET /api/queue-pauses`; banner rendering per §7.2.
2. Tests: the loop stores what the poll returned; the endpoint serves it.

### Task 8 — docs

Update `ARCHITECTURE.md`'s lifecycle sequence (pre-validation slot in the poll path) and this spec's Status line to Implemented. Update this doc wherever the implementation diverged.
