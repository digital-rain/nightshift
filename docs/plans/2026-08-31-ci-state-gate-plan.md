---
status: validating
date: 2026-08-31
---

# CI-State Gate Implementation Plan

> **For agentic workers:** execute with the `implement` skill, task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nightshift watches each target repo's GitHub Actions state on `main`, holds task dispatch for a repo whose `main` is red, auto-spawns an async `/fix`-shaped task for the failure, and resumes dispatch when `main` goes green again.

**Architecture:** CI monitoring is a **per-queue-binding** setting, not one global switch: a queue that is bound to a repo can have `ci_monitoring` on, and only those repos are polled. A new `gh` seam (`src/nightshift/ci.py`) mirrors the `GitRunner` invariant — it is the only place `gh` is invoked as a subprocess — and maps one `gh run list` call into a `CiStatus`. The manager's existing reconciler gains one duty that refreshes each repo's CI state on a cadence and persists it. The dispatch gate rides the **existing** `repo_excluded` seam in `worker_poll` (the same set that already holds back `repo_unavailable` tasks), so a red repo simply contributes its tasks to `blocked` — no parallel pause table, no second dispatch path. Resume-on-green needs no code of its own: the gate reads current state each poll, so a green refresh re-admits the repo's tasks automatically.

## Global constraints

- **The `gh` CLI is the only CI source.** Status comes from `gh run list` executed with `cwd` set to the repo root, so `gh` resolves the repo from its own git remote. A repo with no remote, or a host without `gh` authenticated, resolves to `UNKNOWN` and never gates.
- **Monitoring is per queue binding.** The YES/NO switch lives in the queue's own `.tasks/<queue>/config.json` beside `validate`, and is exposed on both the Repos page's Queue-bindings list and the Playlist detail page. A repo counts as *monitored* when at least one queue bound to it has it on.
- **The gate is fail-open.** Only `RED` holds dispatch. `PENDING`, `UNKNOWN`, and `GREEN` all dispatch normally. Gating on `PENDING` would stall every queue behind CI latency, which is not what was asked for.
- **`gh` invocation is confined to `src/nightshift/ci.py`.** Mirrors the standing rule for `git` in `GitRunner` ("the ONLY place `subprocess` appears in the git layer"). No other module shells out to `gh`.
- **New store state needs both backends.** Postgres gets a migration under `src/nightshift/assets/migrations/` with `-- migrate:up` *and* `-- migrate:down`; SQLite gets the matching `CREATE TABLE` in the inline schema in `src/nightshift/manager/store_sqlite.py`. Shared SQL lives on `SqlStoreBase`.
- **The manager stays the sole git authority.** This plan adds no writer to `main`. It reads CI state and gates dispatch; it never lands, reverts, or pushes.
- **One fix task per failing commit.** Dedupe on `head_sha` so a repo that stays red across many refreshes spawns exactly one fix task, not one per tick.
- **Restart note:** changes under `src/nightshift/manager/` require a manager restart — leave that to the operator (root `AGENTS.md` rule 3).

## File structure

| File | Responsibility | Task |
|---|---|---|
| Create `src/nightshift/ci.py` | The `gh` seam: `GhRunner`, `CiState`, `CiStatus`, `check_repo_ci()`. Pure mapping from `gh` JSON to a status; no store, no config. | 1 |
| Create `tests/test_ci_seam.py` | `gh`-JSON → `CiStatus` mapping: every conclusion value, the no-runs case, gh failure. | 1 |
| Create `…/migrations/20260831000001_nightshift_repo_ci.sql` | Postgres `nightshift.repo_ci` table (up + down). | 2 |
| Create `…/migrations/20260831000002_nightshift_attempt_kind.sql` | Postgres `attempts.kind` column (up + down). | 10 |
| Modify `src/nightshift/manager/store_sqlite.py` | Matching SQLite `repo_ci` table (~236) and `attempts.kind` column (~150). | 2, 10 |
| Modify `src/nightshift/manager/store.py` | Protocol entries + `SqlStoreBase` bodies for `repo_ci` / `set_repo_ci` / `set_repo_ci_fix`; `kind` on the attempt row. | 2, 10 |
| Modify `src/nightshift/queue_config.py` | `ci_monitoring_enabled()` / `set_ci_monitoring()` — the per-queue switch, beside `resolve_validate_cmd`. | 3 |
| Modify `src/nightshift/config/manager.py` | `Cadences.ci_refresh_seconds` only — there is no global on/off. | 3 |
| Modify `src/nightshift/lifecycle.py` | `TaskHoldKind.CI_RED`. | 6 |
| Modify `src/nightshift/manager/reconciler.py` | `_monitored_repos`, the `_refresh_repo_ci` duty, `_spawn_ci_fix`, and the `ci_red` hold set/clear in `_reconcile_holds`; `import time`. | 4, 5, 6 |
| Modify `src/nightshift/manager/api_worker.py` | Read-only dispatch exclusion for red repos (~622); carry the brief's `kind` onto the attempt. | 6, 10 |
| Modify `src/nightshift/manager/api_playlists.py` | `monitored` + `ci` on `/api/repos`; `ci_monitoring` on queue bindings and playlist info; `ci_state` on the playlists list; `PUT /api/queue/ci-monitoring`. | 7, 8, 9 |
| Modify `src/nightshift/assets/ui/app.js` | `ci_red` in the status vocabulary; Monitoring/Hold badges on repo rows; the CI-monitoring segmented control on both screens; the playlist build-status dot; the History "CI fix" badge. | 6–10 |
| Modify `src/nightshift/assets/ui/analytics.js` | CI-resolution as its own Stats category. | 10 |
| Modify `src/nightshift/assets/ui/style.css` | The five build-status dot colours. | 9 |
| Modify `tests/test_nightshift_store.py` | Round-trip + transition-return coverage for the new store methods. | 2 |
| Create `tests/test_ci_gate.py` | The whole gate: refresh, spawn, hold/clear, every payload, the switch. | 4–10 |
| Modify `docs/user/configuration-reference.md` | `ci_monitoring` (per queue) and `cadences.ci_refresh_seconds`. | 3 |
| Modify `ARCHITECTURE.md` | §Task lifecycle — one paragraph on the gate. | 11 |

## Task 1: The `gh` seam

**Files:**
- Create: `src/nightshift/ci.py`
- Test: `tests/test_ci_seam.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces: `class CiState(StrEnum)` with members `GREEN`, `RED`, `PENDING`, `UNKNOWN`; `@dataclass(frozen=True) class CiStatus` with fields `state: CiState`, `head_sha: str | None`, `url: str | None`, `detail: str | None`; `class GhRunner` with `__init__(self, repo_root: Path)` and `run(self, *args: str) -> tuple[int, str, str]`; `def status_from_runs(payload: str) -> CiStatus`; `def check_repo_ci(repo_root: Path, *, branch: str = "main", runner: GhRunner | None = None) -> CiStatus`.

- [x] **Step 1: Write the failing test**

```python
"""Tests for the gh seam: gh JSON -> CiStatus."""

from __future__ import annotations

import json

from nightshift.ci import CiState, status_from_runs


def _runs(**over) -> str:
    run = {
        "status": "completed",
        "conclusion": "success",
        "headSha": "abc123",
        "url": "https://github.com/o/r/actions/runs/1",
        "workflowName": "pytest",
    }
    run.update(over)
    return json.dumps([run])


def test_completed_success_is_green():
    st = status_from_runs(_runs())
    assert st.state is CiState.GREEN
    assert st.head_sha == "abc123"


def test_completed_failure_is_red():
    st = status_from_runs(_runs(conclusion="failure"))
    assert st.state is CiState.RED
    assert st.url == "https://github.com/o/r/actions/runs/1"
    assert "pytest" in (st.detail or "")


def test_timed_out_and_action_required_are_red():
    for c in ("timed_out", "action_required"):
        assert status_from_runs(_runs(conclusion=c)).state is CiState.RED


def test_skipped_and_neutral_are_green():
    for c in ("skipped", "neutral"):
        assert status_from_runs(_runs(conclusion=c)).state is CiState.GREEN


def test_cancelled_is_unknown_not_red():
    # A cancelled run is not evidence main is broken; never gate on it.
    assert status_from_runs(_runs(conclusion="cancelled")).state is CiState.UNKNOWN


def test_in_progress_is_pending():
    st = status_from_runs(_runs(status="in_progress", conclusion=None))
    assert st.state is CiState.PENDING


def test_no_runs_is_unknown():
    assert status_from_runs("[]").state is CiState.UNKNOWN


def test_garbage_payload_is_unknown():
    assert status_from_runs("not json").state is CiState.UNKNOWN
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ci_seam.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nightshift.ci'`

- [x] **Step 3: Write minimal implementation**

```python
"""The CI seam: nightshift's only window onto a repo's GitHub Actions state.

``GhRunner`` is to ``gh`` what ``git/runner.GitRunner`` is to ``git`` — the ONLY
place ``gh`` is invoked as a subprocess. Everything above this module works with
:class:`CiStatus` values and never shells out.

The gate this feeds is deliberately fail-open: only :attr:`CiState.RED` holds
dispatch. A repo with no remote, a host without ``gh`` on PATH or authenticated,
an unparseable payload, or a cancelled run all resolve to
:attr:`CiState.UNKNOWN`, which dispatches normally.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

_log = logging.getLogger(__name__)

# gh conclusions that mean "main is broken".
_RED_CONCLUSIONS = frozenset({"failure", "timed_out", "action_required"})
# gh conclusions that are completed-and-fine.
_GREEN_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})

_DETAIL_LIMIT = 400
_GH_TIMEOUT_SECONDS = 30.0


class CiState(StrEnum):
    """The four states the gate distinguishes."""

    GREEN = "green"
    RED = "red"
    PENDING = "pending"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CiStatus:
    """The latest CI verdict for one repo's default branch."""

    state: CiState
    head_sha: str | None = None
    url: str | None = None
    detail: str | None = None


class GhRunner:
    """Runs ``gh`` in a repo. The ONLY place ``gh`` is spawned."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def run(self, *args: str) -> tuple[int, str, str]:
        """Run ``gh``, returning ``(returncode, stdout, stderr)``.

        Never raises: a missing ``gh`` binary, a timeout, or any OS error comes
        back as a non-zero return code so the caller degrades to UNKNOWN.
        """
        argv = ("gh", *args)
        try:
            proc = subprocess.run(  # noqa: S603 — fixed "gh" executable
                argv,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=_GH_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return 127, "", str(exc)
        if os.environ.get("NIGHTSHIFT_GH_TRACE"):
            _log.info("gh %s cwd=%s rc=%d", " ".join(args), self.repo_root, proc.returncode)
        return proc.returncode, proc.stdout, proc.stderr


def status_from_runs(payload: str) -> CiStatus:
    """Map one ``gh run list --json ...`` payload to a :class:`CiStatus`.

    Pure: this is the whole decision table, and it is what the tests pin.
    """
    try:
        runs = json.loads(payload)
    except (ValueError, TypeError):
        return CiStatus(CiState.UNKNOWN, detail="unparseable gh payload")
    if not isinstance(runs, list) or not runs:
        return CiStatus(CiState.UNKNOWN, detail="no workflow runs on branch")

    run = runs[0]
    if not isinstance(run, dict):
        return CiStatus(CiState.UNKNOWN, detail="unexpected gh payload shape")

    head_sha = run.get("headSha") or None
    url = run.get("url") or None
    workflow = run.get("workflowName") or "workflow"

    if run.get("status") != "completed":
        return CiStatus(CiState.PENDING, head_sha=head_sha, url=url,
                        detail=f"{workflow} {run.get('status') or 'running'}")

    conclusion = (run.get("conclusion") or "").strip().lower()
    if conclusion in _RED_CONCLUSIONS:
        return CiStatus(CiState.RED, head_sha=head_sha, url=url,
                        detail=f"{workflow}: {conclusion}"[:_DETAIL_LIMIT])
    if conclusion in _GREEN_CONCLUSIONS:
        return CiStatus(CiState.GREEN, head_sha=head_sha, url=url,
                        detail=f"{workflow}: {conclusion}")
    # cancelled, or anything gh adds later: not evidence of breakage.
    return CiStatus(CiState.UNKNOWN, head_sha=head_sha, url=url,
                    detail=f"{workflow}: {conclusion or 'no conclusion'}")


def check_repo_ci(
    repo_root: Path, *, branch: str = "main", runner: GhRunner | None = None
) -> CiStatus:
    """Latest CI verdict for ``branch`` in the repo at ``repo_root``."""
    gh = runner or GhRunner(repo_root)
    rc, out, err = gh.run(
        "run", "list",
        "--branch", branch,
        "--limit", "1",
        "--json", "status,conclusion,headSha,url,workflowName",
    )
    if rc != 0:
        return CiStatus(CiState.UNKNOWN, detail=(err or out).strip()[:_DETAIL_LIMIT])
    return status_from_runs(out)
```

- [x] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_ci_seam.py -v`
Expected: 8 passed.

- [x] **Step 5: Add a `check_repo_ci` test with a stub runner**

```python
from pathlib import Path

from nightshift.ci import CiState, check_repo_ci


class _StubGh:
    def __init__(self, rc: int, out: str, err: str = "") -> None:
        self._r = (rc, out, err)
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str) -> tuple[int, str, str]:
        self.calls.append(args)
        return self._r


def test_check_repo_ci_passes_branch_and_parses():
    gh = _StubGh(0, _runs(conclusion="failure"))
    st = check_repo_ci(Path("/nonexistent"), branch="main", runner=gh)
    assert st.state is CiState.RED
    assert "--branch" in gh.calls[0] and "main" in gh.calls[0]


def test_gh_failure_degrades_to_unknown():
    gh = _StubGh(127, "", "gh: command not found")
    st = check_repo_ci(Path("/nonexistent"), runner=gh)
    assert st.state is CiState.UNKNOWN
    assert "not found" in (st.detail or "")
```

- [x] **Step 6: Run to green, then lint**

Run: `.venv/bin/python -m pytest tests/test_ci_seam.py -v && .venv/bin/python -m ruff check src/nightshift/ci.py tests/test_ci_seam.py`
Expected: 10 passed, ruff clean.

- [x] **Step 7: Commit** — `ci seam: gh run list -> CiStatus, fail-open on every error path`

## Task 2: Persist per-repo CI state

**Files:**
- Create: `src/nightshift/assets/migrations/20260831000001_nightshift_repo_ci.sql`
- Modify: `src/nightshift/manager/store_sqlite.py` (inline schema, alongside the `queue_state` table at ~236)
- Modify: `src/nightshift/manager/store.py` (`NightshiftStore` protocol ~188; `SqlStoreBase` ~375)
- Test: `tests/test_nightshift_store.py`

**Interfaces:**
- Consumes: `CiState` from Task 1.
- Produces, on `NightshiftStore` (and implemented once on `SqlStoreBase`):
  - `async def repo_ci(self) -> dict[str, dict[str, Any]]` — repo → `{"state", "head_sha", "url", "detail", "fix_task", "fix_sha", "updated_at"}`.
  - `async def set_repo_ci(self, repo: str, *, state: str, head_sha: str | None, url: str | None, detail: str | None) -> str | None` — upserts and returns the **previous** state string (`None` on first sight), which is how the caller detects a transition.
  - `async def set_repo_ci_fix(self, repo: str, *, fix_task: str, fix_sha: str) -> None` — records the spawned fix task for dedupe.

- [x] **Step 1: Write the migration**

```sql
-- migrate:up

-- Latest CI verdict per target repo, refreshed by the reconciler's
-- "repo CI refresh" duty and read by the poll path's dispatch gate.
-- One row per repo; absent row == never checked (treated as unknown).
CREATE TABLE IF NOT EXISTS nightshift.repo_ci (
    repo        text PRIMARY KEY,
    state       text NOT NULL,
    head_sha    text,
    url         text,
    detail      text,
    -- The auto-spawned fix task for the current red, and the sha it was
    -- spawned for: together these dedupe one fix task per failing commit.
    fix_task    text,
    fix_sha     text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- migrate:down

DROP TABLE IF EXISTS nightshift.repo_ci;
```

- [x] **Step 2: Mirror the table in the SQLite inline schema**

Add beside the `nightshift.queue_state` block in `store_sqlite.py`:

```sql
CREATE TABLE nightshift.repo_ci (
    repo        TEXT PRIMARY KEY,
    state       TEXT NOT NULL,
    head_sha    TEXT,
    url         TEXT,
    detail      TEXT,
    fix_task    TEXT,
    fix_sha     TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
```

- [x] **Step 3: Write the failing store test**

Append to `tests/test_nightshift_store.py`:

```python
async def test_repo_ci_roundtrip_and_transition(store):
    # First write: no previous state.
    prev = await store.set_repo_ci(
        "longitude", state="green", head_sha="aaa", url="u1", detail="pytest: success"
    )
    assert prev is None

    rows = await store.repo_ci()
    assert rows["longitude"]["state"] == "green"
    assert rows["longitude"]["head_sha"] == "aaa"

    # Second write returns the state it replaced -- this is the transition edge.
    prev = await store.set_repo_ci(
        "longitude", state="red", head_sha="bbb", url="u2", detail="pytest: failure"
    )
    assert prev == "green"
    rows = await store.repo_ci()
    assert rows["longitude"]["state"] == "red"
    assert rows["longitude"]["fix_task"] is None


async def test_repo_ci_fix_marker(store):
    await store.set_repo_ci("longitude", state="red", head_sha="bbb", url=None, detail=None)
    await store.set_repo_ci_fix("longitude", fix_task="fix-longitude-ci-bbb", fix_sha="bbb")
    rows = await store.repo_ci()
    assert rows["longitude"]["fix_task"] == "fix-longitude-ci-bbb"
    assert rows["longitude"]["fix_sha"] == "bbb"
```

- [x] **Step 4: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_nightshift_store.py -k repo_ci -v`
Expected: FAIL — `AttributeError: 'SqliteStore' object has no attribute 'set_repo_ci'`

- [x] **Step 5: Add the protocol entries**

In `NightshiftStore` (`store.py`), beside `queue_pauses` / `set_queue_pause`:

```python
    # Repo CI state (the dispatch gate's input, refreshed by the reconciler).
    async def repo_ci(self) -> dict[str, dict[str, Any]]: ...
    async def set_repo_ci(
        self, repo: str, *, state: str, head_sha: str | None,
        url: str | None, detail: str | None,
    ) -> str | None: ...
    async def set_repo_ci_fix(self, repo: str, *, fix_task: str, fix_sha: str) -> None: ...
```

- [x] **Step 6: Implement once on `SqlStoreBase`**

```python
    async def repo_ci(self) -> dict[str, dict[str, Any]]:
        rows = await self._fetch(
            "SELECT repo, state, head_sha, url, detail, fix_task, fix_sha, updated_at "
            "FROM nightshift.repo_ci"
        )
        return {r["repo"]: dict(r) for r in rows}

    async def set_repo_ci(
        self, repo: str, *, state: str, head_sha: str | None,
        url: str | None, detail: str | None,
    ) -> str | None:
        """Upsert the repo's CI row, returning the state it replaced.

        The previous state is the transition edge every caller needs: green->red
        spawns a fix task, red->green resumes dispatch. Returns ``None`` the
        first time a repo is seen.
        """
        prior = await self._fetch(
            "SELECT state FROM nightshift.repo_ci WHERE repo = $1", repo
        )
        previous = prior[0]["state"] if prior else None
        # A state change clears the fix marker: the next red is a new failure
        # and deserves its own fix task.
        if previous != state:
            await self._execute(
                "INSERT INTO nightshift.repo_ci "
                "  (repo, state, head_sha, url, detail, fix_task, fix_sha, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, NULL, NULL, $6) "
                "ON CONFLICT (repo) DO UPDATE SET "
                "  state = EXCLUDED.state, head_sha = EXCLUDED.head_sha, "
                "  url = EXCLUDED.url, detail = EXCLUDED.detail, "
                "  fix_task = NULL, fix_sha = NULL, updated_at = EXCLUDED.updated_at",
                repo, state, head_sha, url, detail, self._now(),
            )
        else:
            await self._execute(
                "UPDATE nightshift.repo_ci SET head_sha = $2, url = $3, "
                "  detail = $4, updated_at = $5 WHERE repo = $1",
                repo, head_sha, url, detail, self._now(),
            )
        return previous

    async def set_repo_ci_fix(self, repo: str, *, fix_task: str, fix_sha: str) -> None:
        await self._execute(
            "UPDATE nightshift.repo_ci SET fix_task = $2, fix_sha = $3 WHERE repo = $1",
            repo, fix_task, fix_sha,
        )
```

> **Placement note:** `_fetch`, `_execute`, and `_now` are the existing `SqlStoreBase` helpers used by `queue_pauses` / `set_queue_pause` — match their exact names and parameter-placeholder style as they appear in the file; SQLite and Postgres share this body, which is why it lives on the base and not on `PgStore`.

- [x] **Step 7: Run to green**

Run: `.venv/bin/python -m pytest tests/test_nightshift_store.py -k repo_ci -v`
Expected: 2 passed.

- [x] **Step 8: Verify the Postgres migration round-trips**

Run: `just migrate && just rollback && just migrate`
Expected: all three succeed; `repo_ci` exists after the final migrate.

- [x] **Step 9: Commit** — `store: per-repo CI state with transition-returning upsert`

## Task 3: Per-queue CI monitoring config

**Files:**
- Modify: `src/nightshift/queue_config.py` (beside `resolve_validate_cmd`, ~41)
- Modify: `src/nightshift/config/manager.py` (`Cadences`, ~23)
- Modify: `docs/user/configuration-reference.md`
- Test: `tests/test_nightshift_config.py`

**Interfaces:**
- Produces: `queue_config.ci_monitoring_enabled(config: dict) -> bool` (absent key = `False`); `queue_config.set_ci_monitoring(tasks_root: Path, tasks_rel: str, enabled: bool) -> None`; `Cadences.ci_refresh_seconds: float = 120.0`.

**Why per-queue:** a queue is already bound to exactly one repo, and it already carries its own operator-editable settings (`validate`, `order`, `sort`) in `.tasks/<queue>/config.json`. The monitoring switch belongs in the same place, edited through the same screens.

- [x] **Step 1: Write the failing test**

```python
from nightshift.queue_config import ci_monitoring_enabled


def test_ci_monitoring_defaults_off():
    assert ci_monitoring_enabled({}) is False
    assert ci_monitoring_enabled({"validate": "just validate"}) is False


def test_ci_monitoring_reads_the_flag():
    assert ci_monitoring_enabled({"ci_monitoring": True}) is True
    assert ci_monitoring_enabled({"ci_monitoring": False}) is False


def test_ci_refresh_cadence_default():
    assert ManagerSettings().cadences.ci_refresh_seconds == 120.0
```

- [x] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_nightshift_config.py -k ci_ -v`
Expected: FAIL — `ImportError: cannot import name 'ci_monitoring_enabled'`

- [x] **Step 3: Implement the queue-config accessors**

In `queue_config.py`:

```python
def ci_monitoring_enabled(config: dict) -> bool:
    """Whether this queue watches its bound repo's CI on main.

    Absent key means off: monitoring is opt-in per queue, because it costs a
    ``gh`` call per refresh and only makes sense for a queue bound to a repo
    with GitHub Actions.
    """
    return bool(config.get("ci_monitoring", False))


def set_ci_monitoring(tasks_root: Path, tasks_rel: str, enabled: bool) -> None:
    """Persist the queue's monitoring switch into its own config.json."""
    path = _order_config_path(tasks_root, tasks_rel)
    config = json.loads(path.read_text()) if path.exists() else {}
    config["ci_monitoring"] = bool(enabled)
    path.write_text(json.dumps(config, indent=2) + "\n")
```

- [x] **Step 4: Add the cadence**

In `Cadences` (`config/manager.py`):

```python
    ci_refresh_seconds: float = field(default=120.0, metadata=meta(
        category="Cadences", label="CI refresh seconds",
        desc=(
            "Minimum interval between GitHub Actions status checks per "
            "monitored repo. Each check is one `gh run list` call. Repos are "
            "polled only while a queue bound to them has CI monitoring on."),
        apply="restart"))
```

- [x] **Step 5: Run to green, then document**

Run: `.venv/bin/python -m pytest tests/test_nightshift_config.py -k ci_ -v`

Add to `docs/user/configuration-reference.md`:

```markdown
| `cadences.ci_refresh_seconds` | `120` | Minimum seconds between `gh run list` checks per monitored repo. |
```

and in the per-queue settings section:

```markdown
| `ci_monitoring` | `false` | Watch this queue's bound repo for a failing GitHub Actions run on `main`: hold the queue's tasks while it is red and queue a CI-resolution task. Toggled from Repos → Queue bindings, or the playlist's detail page. |
```

- [x] **Step 6: Commit** — `config: per-queue ci_monitoring switch + ci_refresh_seconds cadence`

## Task 4: The reconciler refresh duty

**Files:**
- Modify: `src/nightshift/manager/reconciler.py` (`reconcile_once` ~191; new duty; `__init__` ~124)
- Create: `tests/test_ci_gate.py`

**Interfaces:**
- Consumes: `check_repo_ci`, `CiState`, `CiStatus` (Task 1); `store.repo_ci` / `set_repo_ci` (Task 2); `queue_config.ci_monitoring_enabled` (Task 3); the existing `playlists_mod.tasks_rel(q)`, `load_queue_config`, `repos.resolve_repo`, `repos.repo_available`, `repos.repo_root`.
- Produces: `Reconciler._refresh_repo_ci()`; `Reconciler._monitored_repos() -> dict[str, set[str | None]]` (repo → the queues watching it); `Reconciler._ci_checked_at: dict[str, float]`; the `repo_ci` event kind.

- [x] **Step 1: Write the shared test harness**

Create `tests/test_ci_gate.py`, mirroring `tests/test_reconciler.py`'s harness (`TestClient(create_app(...))`, duties via `client.app.state.reconciler`, sync tests, coroutines through `client.portal.call`).

```python
"""Tests for the CI gate: refresh duty, fix spawn, dispatch hold, UI payloads."""

from __future__ import annotations

from functools import partial
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from _workspace import build_workspace
from nightshift.ci import CiState, CiStatus
from nightshift.manager.app import create_app
from nightshift.manager.store_sqlite import SqliteStore


class _StubCi:
    """Stands in for reconciler.check_repo_ci; counts calls so we can assert
    the duty skipped the subprocess entirely."""

    def __init__(self, status: CiStatus) -> None:
        self.status = status
        self.calls = 0

    def __call__(self, repo_root: Path, *, branch: str = "main") -> CiStatus:
        self.calls += 1
        return self.status


def _build(tmp_path, monkeypatch, *, monitoring: bool):
    ws = build_workspace(
        tmp_path,
        tasks={"alpha": "do the thing"},
        queues={"main": {"ci_monitoring": monitoring}},
    )
    stub = _StubCi(CiStatus(CiState.GREEN, head_sha="aaa"))
    monkeypatch.setattr("nightshift.manager.reconciler.check_repo_ci", stub)
    store = SqliteStore()
    return ws, store, stub, TestClient(create_app(ws, store=store))


@pytest.fixture
def gate(tmp_path, monkeypatch):
    ws, store, stub, client = _build(tmp_path, monkeypatch, monitoring=True)
    with client:
        yield ws, store, stub, client


@pytest.fixture
def unmonitored(tmp_path, monkeypatch):
    ws, store, stub, client = _build(tmp_path, monkeypatch, monitoring=False)
    with client:
        yield ws, store, stub, client


def _call(client: TestClient, fn, *a, **kw):
    return client.portal.call(partial(fn, *a, **kw))


def _reconcile(client: TestClient) -> None:
    client.portal.call(client.app.state.reconciler.reconcile_once)


def _open_throttle(client: TestClient) -> None:
    client.app.state.reconciler._ci_checked_at.clear()


def _tasks_dir(ws: Path, queue: str = "main") -> Path:
    # tasks_repo defaults to "nightshift-tasks" (OperatorConfig.tasks_repo).
    return ws / "nightshift-tasks" / queue


def _checkin(client: TestClient, worker_id: str = "w1") -> None:
    client.post("/api/worker/checkin",
                json={"worker_id": worker_id, "backend": "claude-code"})


def _poll(client: TestClient, worker_id: str = "w1") -> dict | None:
    return client.post(
        "/api/worker/poll",
        json={"worker_id": worker_id, "backend": "claude-code", "models": ["auto"]},
    ).json()["work"]
```

> If `build_workspace` does not accept per-queue `config.json` keys through its `queues=` mapping, extend it there — `tests/_workspace.py` is the canonical fixture builder (root `AGENTS.md`, Working norms); do not build a second helper in this file.

- [x] **Step 2: Write the failing duty tests**

```python
def test_refresh_records_state_for_a_monitored_queue(gate):
    _ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", url="u", detail="pytest: failure")
    _reconcile(client)
    assert _call(client, store.repo_ci)["longitude"]["state"] == "red"


def test_unmonitored_queue_never_shells_out(unmonitored):
    _ws, store, stub, client = unmonitored
    _reconcile(client)
    assert stub.calls == 0
    assert _call(client, store.repo_ci) == {}


def test_transition_emits_a_repo_ci_event(gate):
    _ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="pytest: failure")
    _reconcile(client)
    assert [e for e in _call(client, store.events_since, 0)
            if e.get("type") == "repo_ci"]


def test_steady_state_emits_nothing_new(gate):
    """A repo that stays green must not churn the operator's event feed."""
    _ws, store, stub, client = gate
    _reconcile(client)
    cursor = _call(client, store.max_event_id)
    _open_throttle(client)
    _reconcile(client)
    assert not [e for e in _call(client, store.events_since, cursor)
                if e.get("type") == "repo_ci"]


def test_throttle_suppresses_an_immediate_recheck(gate):
    _ws, _store, stub, client = gate
    _reconcile(client)
    _reconcile(client)
    assert stub.calls == 1
```

- [x] **Step 3: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -v`
Expected: FAIL — `AttributeError: module 'nightshift.manager.reconciler' has no attribute 'check_repo_ci'`.

- [x] **Step 4: Add imports and the throttle map**

`asyncio` and `repos` are already imported in `reconciler.py`; `time` is **not**:

```python
import time

from nightshift.ci import CiState, CiStatus, check_repo_ci
from nightshift.queue_config import ci_monitoring_enabled
from nightshift.task_files import create_task
```

In `Reconciler.__init__`, beside the other `self._…` assignments:

```python
        # Per-repo wall-clock of the last gh check (cadences.ci_refresh_seconds).
        self._ci_checked_at: dict[str, float] = {}
```

- [x] **Step 5: Implement the duty**

```python
    def _monitored_repos(self) -> dict[str, set[str | None]]:
        """Repo -> the queues watching it. Two queues bound to one repo share a
        single gh check; a queue with monitoring off contributes nothing."""
        out: dict[str, set[str | None]] = {}
        for q in self._all_queues():
            tasks_rel = playlists_mod.tasks_rel(q)
            config = load_queue_config(self._tasks_root, tasks_rel)
            if not ci_monitoring_enabled(config):
                continue
            repo = config.get("repo")
            if not repo or not repos.repo_available(self._workspace, repo):
                continue
            out.setdefault(repo, set()).add(q)
        return out

    async def _refresh_repo_ci(self) -> None:
        """Refresh GitHub Actions state for every monitored repo.

        Throttled per repo by ``cadences.ci_refresh_seconds``. A workspace with
        no monitored queue makes no subprocess call at all, so a host without
        ``gh`` never pays for the feature. Only a *state change* emits an event.
        """
        monitored = self._monitored_repos()
        if not monitored:
            return
        store = self._store()
        interval = float(self._cfg.cadences.ci_refresh_seconds or 0)
        now = time.time()
        for repo in sorted(monitored):
            last = self._ci_checked_at.get(repo)
            if last is not None and interval > 0 and (now - last) < interval:
                continue
            self._ci_checked_at[repo] = now
            status = await asyncio.to_thread(
                check_repo_ci, repos.repo_root(self._workspace, repo)
            )
            previous = await store.set_repo_ci(
                repo,
                state=str(status.state),
                head_sha=status.head_sha,
                url=status.url,
                detail=status.detail,
            )
            if previous == str(status.state):
                continue
            await self._emit(
                "repo_ci",
                payload={
                    "repo": repo,
                    "state": str(status.state),
                    "previous": previous,
                    "head_sha": status.head_sha,
                    "url": status.url,
                    "detail": status.detail,
                },
            )
            if status.state is CiState.RED:
                await self._spawn_ci_fix(repo, status, queues=monitored[repo])
```

Register the duty in `reconcile_once` **above** the hold duty, so Task 7's holds are written against this tick's state:

```python
        await self._run_duty("repo CI refresh", self._refresh_repo_ci)
        await self._run_duty("hold set/clear", self._reconcile_holds)
```

Add the forward stub that Task 5 replaces:

```python
    async def _spawn_ci_fix(
        self, repo: str, status: CiStatus, *, queues: set[str | None]
    ) -> None:
        """Replaced by Task 5."""
        return None
```

- [x] **Step 6: Run to green**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -v`
Expected: 5 passed.

- [x] **Step 7: Commit** — `reconciler: refresh CI for monitored repos, throttled, event on transition`

## Task 5: Spawn the CI-resolution task

**Files:**
- Modify: `src/nightshift/manager/reconciler.py` (replace the `_spawn_ci_fix` stub)
- Test: `tests/test_ci_gate.py`

**Interfaces:**
- Consumes: `create_task(tasks_root, title, text, tasks_rel="main") -> dict`; `store.set_repo_ci_fix` (Task 2).
- Produces: `Reconciler._spawn_ci_fix(repo, status, *, queues)`; the `repo_ci_fix_spawned` event; a brief carrying `kind: ci_resolution` frontmatter — the tag Task 10 categorises History and Stats on.

- [x] **Step 1: Write the failing tests**

```python
def test_red_spawns_one_ci_resolution_task(gate):
    ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb",
                           url="https://gh/run/1", detail="pytest: failure")
    _reconcile(client)

    row = _call(client, store.repo_ci)["longitude"]
    assert row["fix_task"] and row["fix_sha"] == "bbb"

    brief = (_tasks_dir(ws) / f"{row['fix_task']}.md").read_text()
    assert "kind: ci_resolution" in brief      # the Stats/History category tag
    assert "/fix" in brief
    assert "pytest: failure" in brief
    assert "https://gh/run/1" in brief


def test_same_red_sha_does_not_respawn(gate):
    ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="fail")
    _reconcile(client)
    first = _call(client, store.repo_ci)["longitude"]["fix_task"]
    _open_throttle(client)
    _reconcile(client)
    assert _call(client, store.repo_ci)["longitude"]["fix_task"] == first
    assert len(list(_tasks_dir(ws).glob("fix-ci-*.md"))) == 1


def test_red_green_red_spawns_a_second_task(gate):
    ws, _store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="fail one")
    _reconcile(client)
    _open_throttle(client)
    stub.status = CiStatus(CiState.GREEN, head_sha="ccc")
    _reconcile(client)
    _open_throttle(client)
    stub.status = CiStatus(CiState.RED, head_sha="ddd", detail="fail two")
    _reconcile(client)
    assert len(list(_tasks_dir(ws).glob("fix-ci-*.md"))) == 2
```

- [x] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -k spawn -v`
Expected: FAIL — `fix_task` is `None`; the stub returns without spawning.

- [x] **Step 3: Replace the stub**

```python
    async def _spawn_ci_fix(
        self, repo: str, status: CiStatus, *, queues: set[str | None]
    ) -> None:
        """Queue one CI-resolution task for a red main, deduped on the sha.

        The task goes into the monitored queue itself — that queue is already
        bound to this repo, so its validate command and worker routing are the
        right ones. ``set_repo_ci`` clears the fix marker on any state change,
        so red -> green -> red gets a genuinely new task while a repo that
        simply stays red across refreshes gets exactly one.
        """
        store = self._store()
        row = (await store.repo_ci()).get(repo) or {}
        if row.get("fix_task") and row.get("fix_sha") == status.head_sha:
            return

        target = sorted(queues, key=lambda q: (q is not None, q or ""))[0]
        tasks_rel = playlists_mod.tasks_rel(target)
        short = (status.head_sha or "unknown")[:8]
        title = f"fix ci: {repo} main is red at {short}"
        body = (
            f"/fix CI is failing on `{repo}` `main`.\n\n"
            f"- **Failing commit:** `{status.head_sha or 'unknown'}`\n"
            f"- **Detail:** {status.detail or 'no detail reported'}\n"
            f"- **Run:** {status.url or 'no run URL reported'}\n\n"
            "Reproduce the failure locally, find the root cause, fix it, and "
            "verify against the same check that failed. Nightshift is holding "
            "this queue's other tasks until `main` is green again.\n"
        )
        try:
            created = await asyncio.to_thread(
                create_task, self._tasks_root, title, body, tasks_rel
            )
        except (FileExistsError, ValueError):
            return
        # Tag the brief so History and the Stats page can categorise its runs.
        await asyncio.to_thread(
            _tag_ci_resolution, self._tasks_root, tasks_rel, created["task"], repo
        )
        await store.set_repo_ci_fix(
            repo, fix_task=created["task"], fix_sha=status.head_sha or ""
        )
        await self._emit(
            "repo_ci_fix_spawned",
            queue=target,
            task=created["task"],
            payload={"repo": repo, "head_sha": status.head_sha},
        )
```

Module-level helper in `reconciler.py`:

```python
def _tag_ci_resolution(
    tasks_root: Path, tasks_rel: str, task: str, repo: str
) -> None:
    """Add ``kind: ci_resolution`` + ``repo:`` to a freshly created brief's
    frontmatter. This is the tag the attempt record carries into History and
    the Stats page's CI-resolution category (Task 10)."""
    path = tasks_root / tasks_rel / f"{task}.md"
    meta, body = split_frontmatter(path.read_text())
    meta["kind"] = "ci_resolution"
    meta["repo"] = repo
    path.write_text(join_frontmatter(meta, body))
```

> `split_frontmatter` is already exported from `nightshift.spawn_daily`; add the matching `join_frontmatter` there if it does not exist yet, next to its splitter — one frontmatter seam, not two.

- [x] **Step 4: Run to green**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -v`
Expected: 8 passed.

- [x] **Step 5: Commit** — `reconciler: spawn one tagged ci_resolution task per failing sha`

## Task 6: The dispatch hold

**Files:**
- Modify: `src/nightshift/lifecycle.py` (`TaskHoldKind`, ~127)
- Modify: `src/nightshift/manager/reconciler.py` (`_reconcile_holds`: the repo block ~513 and the clear loop ~556)
- Modify: `src/nightshift/manager/api_worker.py` (the `repo_excluded` loop, ~622)
- Test: `tests/test_ci_gate.py`

**Interfaces:**
- Produces: `TaskHoldKind.CI_RED = "ci_red"`, written and cleared by `_reconcile_holds`, excluded read-only in `worker_poll`.

**Why this shape:** `repo_unavailable` is the existing idiom for "a repo-level condition is holding these tasks" — a `TaskHoldKind` written and cleared by the reconciler, excluded read-only in `worker_poll` (which says so: *"the corresponding hold writes and warnings are the reconciler's"*), and rendered by `STATE_LABELS`/`statusClass` as a pill. A red repo is the same kind of condition, so it gets the same treatment rather than a parallel mechanism.

- [x] **Step 1: Write the failing tests**

```python
def test_red_holds_the_queues_tasks(gate):
    _ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="pytest: failure")
    _reconcile(client)
    assert _call(client, store.get_task_state, None, "alpha")["state"] == "ci_red"
    _checkin(client)
    assert _poll(client) is None


def test_green_clears_the_hold_and_resumes(gate):
    _ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="fail")
    _reconcile(client)
    _checkin(client)
    assert _poll(client) is None

    _open_throttle(client)
    stub.status = CiStatus(CiState.GREEN, head_sha="ccc")
    _reconcile(client)
    assert not _call(client, store.get_task_state, None, "alpha")
    assert _poll(client) is not None


@pytest.mark.parametrize("state", ["pending", "unknown", "green"])
def test_only_red_gates(gate, state):
    """Fail-open: CI latency and unknown state must never stall a queue."""
    _ws, store, _stub, client = gate
    _call(client, store.set_repo_ci, "longitude", state=state,
          head_sha="aaa", url=None, detail=None)
    _checkin(client)
    assert _poll(client) is not None


def test_unmonitored_queue_dispatches_through_red(unmonitored):
    _ws, store, _stub, client = unmonitored
    _call(client, store.set_repo_ci, "longitude", state="red",
          head_sha="bbb", url=None, detail=None)
    _checkin(client)
    assert _poll(client) is not None
```

- [x] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -k "hold or gates or resume" -v`
Expected: FAIL — `get_task_state` returns `None`; work is handed out.

- [x] **Step 3: Add the hold kind**

In `lifecycle.py`, beside `REPO_UNAVAILABLE`:

```python
    CI_RED = "ci_red"
```

- [x] **Step 4: Write and clear the hold in `_reconcile_holds`**

Read the red set once, near the duty's other reads:

```python
        monitored = self._monitored_repos()
        red_repos = {
            repo for repo, row in (await self._store().repo_ci()).items()
            if row.get("state") == "red" and repo in monitored
        }
```

Extend the candidate walk, after the `elif cand.repo and not repos.repo_available(...)` arm:

```python
                elif cand.repo and cand.repo in red_repos:
                    existing = await store.get_task_state(cand.queue, cand.task)
                    if not existing or existing.get("state") != TaskHoldKind.CI_RED:
                        await store.set_task_state(
                            cand.queue, cand.task, TaskHoldKind.CI_RED,
                            repo=cand.repo,
                        )
```

And the silent clear, beside the existing `REPO_UNAVAILABLE` clear loop:

```python
        for row in await store.tasks_in_state(TaskHoldKind.CI_RED):
            repo = row.get("repo")
            if not repo or repo not in red_repos:
                await store.clear_task_state(
                    self._queue_from_label(row.get("queue")), row["task"]
                )
```

> The CI-resolution task must not hold itself: exclude it by name in the candidate walk — `if cand.task == (ci_rows.get(cand.repo) or {}).get("fix_task"): continue` — otherwise the one task that can turn CI green is held out of dispatch by the very condition it exists to clear. This is the single most important line in the task; the test below pins it.

```python
def test_the_ci_resolution_task_is_not_held_by_its_own_gate(gate):
    ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="fail")
    _reconcile(client)
    fix = _call(client, store.repo_ci)["longitude"]["fix_task"]
    assert not _call(client, store.get_task_state, None, fix)
    _checkin(client)
    order = _poll(client)
    assert order and order["task"] == fix     # the fix is what dispatches
```

- [x] **Step 5: Extend the read-only dispatch exclusion**

In `worker_poll`, after the existing `repo_excluded` loop and **before** `blocked |= repo_excluded` — the DB hold already blocks these, so this is the belt-and-braces path that matches `repo_unavailable`'s:

```python
        # CI gate: tasks whose monitored repo has a red main are held out of
        # dispatch on the same seam as unavailable repos. Only RED gates, so
        # CI latency never stalls a queue, and the CI-resolution task itself is
        # never excluded -- it is the thing that turns the repo green.
        ci_rows = await store.repo_ci()
        red = {r for r, row in ci_rows.items() if row.get("state") == "red"}
        if red:
            for cands in candidates_by_queue.values():
                for cand in cands:
                    if cand.repo in red and cand.task != (
                        ci_rows.get(cand.repo) or {}
                    ).get("fix_task"):
                        repo_excluded.add((cand.queue, cand.task))
```

- [x] **Step 6: Run to green + full gate**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -v && just validate`
Expected: 14 passed, suite green, ruff clean.

- [x] **Step 7: Commit** — `gate: hold a red repo's tasks as ci_red, never the fix task itself`

## Task 7: Repos page — MONITORING and HOLD badges

**Files:**
- Modify: `src/nightshift/manager/api_playlists.py` (`_repos_payload`, ~281)
- Modify: `src/nightshift/assets/ui/app.js` (`repoRow` ~1460, `availabilityBadge` ~1409, `STATE_LABELS` ~185, `statusClass` ~207)
- Test: `tests/test_ci_gate.py`

**Interfaces:**
- Produces: each entry in the `/api/repos` `repos` array gains `monitored: bool` and `ci: {"state","head_sha","url","detail","fix_task"} | None`; the UI status vocabulary gains `ci_red`.

- [x] **Step 1: Write the failing test**

```python
def test_repos_payload_carries_monitoring_and_ci(gate):
    _ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb",
                           url="https://gh/run/1", detail="pytest: failure")
    _reconcile(client)
    repos = {r["name"]: r for r in client.get("/api/repos").json()["repos"]}
    assert repos["longitude"]["monitored"] is True
    assert repos["longitude"]["ci"]["state"] == "red"
    assert repos["longitude"]["ci"]["url"] == "https://gh/run/1"


def test_unmonitored_repo_reports_monitored_false(unmonitored):
    _ws, _store, _stub, client = unmonitored
    repos = {r["name"]: r for r in client.get("/api/repos").json()["repos"]}
    assert repos["longitude"]["monitored"] is False
    assert repos["longitude"]["ci"] is None
```

- [x] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -k repos_payload -v`
Expected: FAIL — `KeyError: 'monitored'`

- [x] **Step 3: Extend `_repos_payload`**

`_repos_payload` is currently sync and `/api/repos` is a sync endpoint. Read CI state through the app's portal-free path by making both async (the sibling `rescan_repos` is already `async def`, so the pattern is established):

```python
    @app.get("/api/repos")
    async def get_repos() -> JSONResponse:
        return JSONResponse(await _repos_payload_async())
```

```python
    async def _repos_payload_async() -> dict[str, Any]:
        payload = _repos_payload()
        ci_rows = await _store().repo_ci()
        monitored = _monitored_repo_names()      # queues with ci_monitoring on
        for entry in payload.get("repos", []):
            name = entry["name"]
            entry["monitored"] = name in monitored
            row = ci_rows.get(name)
            entry["ci"] = None if not entry["monitored"] or not row else {
                "state": row.get("state"),
                "head_sha": row.get("head_sha"),
                "url": row.get("url"),
                "detail": row.get("detail"),
                "fix_task": row.get("fix_task"),
            }
        return payload
```

- [x] **Step 4: Render the badges**

In `app.js`, extend `repoRow` so the row carries availability plus, when monitored, a monitoring badge and the hold badge:

```javascript
function monitoringBadge() {
  const span = document.createElement("span");
  span.className = "repo-tag repo-tag-monitoring";
  span.textContent = "Monitoring";
  span.title = "A queue bound to this repo is watching its CI on main";
  return span;
}

function ciBadge(ci) {
  // Only a red repo gets a badge here -- green/pending/unknown are carried by
  // the Playlists dot, and a badge per state would be noise on this screen.
  if (!ci || ci.state !== "red") return null;
  const span = document.createElement("span");
  span.className = "status paused";          // the shared warn treatment
  span.textContent = "Hold";
  span.title = ci.detail
    ? `CI red: ${ci.detail} — dispatch held for this repo`
    : "CI red — dispatch held for this repo";
  return span;
}
```

and in `repoRow`, after the tasks-store tag:

```javascript
  if (r.monitored) main.append(monitoringBadge());
  li.append(main, availabilityBadge(r.available));
  const hold = ciBadge(r.ci);
  if (hold) li.append(hold);
```

Teach the shared status vocabulary the hold kind, beside `repo_unavailable`:

```javascript
  // A task whose monitored repo has a failing CI run on main is paused
  // (auto-resumable when CI goes green), never failed. Distinct label from
  // repo_unavailable so the operator can tell the two pauses apart.
  ci_red: "CI red",
```

```javascript
  if (status === "ci_red") return "paused";
```

- [x] **Step 5: Run to green**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -v && just validate`

- [x] **Step 6: Commit** — `repos page: Monitoring badge + CI Hold badge; ci_red in the status vocabulary`

## Task 8: The CI MONITORING switch (queue bindings + playlist detail)

**Files:**
- Modify: `src/nightshift/manager/api_playlists.py` (new `PUT /api/queue/ci-monitoring`, beside `PUT /api/queue/repo` ~756)
- Modify: `src/nightshift/assets/ui/app.js` (`repoQueueRow` ~1478; playlist detail body ~3959 and its save path ~4074)
- Test: `tests/test_ci_gate.py`

**Interfaces:**
- Produces: `PUT /api/queue/ci-monitoring` accepting `{"queue": str|null, "enabled": bool}`, persisting via `queue_config.set_ci_monitoring` and returning the refreshed repos payload; a YES/NO segmented control on both screens.

- [x] **Step 1: Write the failing test**

```python
def test_toggling_monitoring_persists_and_takes_effect(unmonitored):
    ws, _store, stub, client = unmonitored
    assert client.put("/api/queue/ci-monitoring",
                      json={"queue": None, "enabled": True}).status_code == 200

    cfg = json.loads((_tasks_dir(ws) / "config.json").read_text())
    assert cfg["ci_monitoring"] is True

    _reconcile(client)
    assert stub.calls == 1        # now polled, where before it was not


def test_toggling_off_stops_polling_and_clears_holds(gate):
    _ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="fail")
    _reconcile(client)
    assert _call(client, store.get_task_state, None, "alpha")["state"] == "ci_red"

    client.put("/api/queue/ci-monitoring", json={"queue": None, "enabled": False})
    _open_throttle(client)
    _reconcile(client)
    assert not _call(client, store.get_task_state, None, "alpha")
    _checkin(client)
    assert _poll(client) is not None
```

- [x] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -k monitoring -v`
Expected: FAIL — 404/405 on the unknown route.

- [x] **Step 3: Add the endpoint**

Beside `PUT /api/queue/repo`, matching its body-model and error style:

```python
    @app.put("/api/queue/ci-monitoring")
    async def put_queue_ci_monitoring(body: QueueCiMonitoringBody) -> JSONResponse:
        """Turn CI monitoring on/off for one queue.

        Turning it off leaves any ci_red holds behind for exactly one tick;
        the reconciler's clear loop drops them on the next pass because the
        repo is no longer in the monitored set.
        """
        queue = _resolve_queue(body.queue)
        set_ci_monitoring(tasks_root, playlists_mod.tasks_rel(queue), body.enabled)
        await _emit("repos_changed", payload={
            "queue": queue_label(queue), "ci_monitoring": body.enabled,
        })
        return JSONResponse(await _repos_payload_async())
```

```python
class QueueCiMonitoringBody(BaseModel):
    queue: str | None = None
    enabled: bool
```

- [x] **Step 4: Add the segmented control to the queue-binding row**

`repoQueueRow` already builds a head with the queue label and a default-repo selector. Add the switch beside it, reusing the existing `seg-opt` segmented-control styling used elsewhere in this UI:

```javascript
function ciMonitoringControl(q) {
  const wrap = document.createElement("div");
  wrap.className = "seg ci-monitoring-seg";
  const label = document.createElement("span");
  label.className = "seg-label";
  label.textContent = "CI monitoring";
  wrap.append(label);
  for (const [text, value] of [["Yes", true], ["No", false]]) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "seg-opt" + (Boolean(q.ci_monitoring) === value ? " on" : "");
    btn.textContent = text;
    btn.addEventListener("click", async () => {
      await putJSON("/api/queue/ci-monitoring",
                    { queue: q.queue === "main" ? null : q.queue, enabled: value });
      await loadRepos();          // re-render from the returned payload
    });
    wrap.append(btn);
  }
  return wrap;
}
```

and append it in `repoQueueRow` after the repo selector. `_repos_payload` must include `ci_monitoring` on each queue entry — add it where the queue bindings are built.

- [x] **Step 5: Add the same control to the playlist detail page**

The detail body already renders a "Validate command" field via `playlistInfoField`. Add the switch directly beneath it so both operator paths carry the same control:

```javascript
  const ciField = document.createElement("div");
  ciField.className = "playlist-info-field";
  const ciLabel = document.createElement("label");
  ciLabel.textContent = "CI monitoring";
  ciField.append(ciLabel, ciMonitoringControl({
    queue: info.name, ci_monitoring: info.ci_monitoring,
  }));
  body.append(validateField, ciField);
```

The playlist-info payload must carry `ci_monitoring`; add it where `info.validate` is populated.

- [x] **Step 6: Run to green**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -v && just validate`
Expected: 18 passed.

- [x] **Step 7: Commit** — `ui: CI monitoring switch on queue bindings and playlist detail`

## Task 9: Playlists page — the build-status dot

**Files:**
- Modify: `src/nightshift/manager/api_playlists.py` (the playlists list payload)
- Modify: `src/nightshift/assets/ui/app.js` (`playlistRow`, ~3718)
- Modify: `src/nightshift/assets/ui/style.css`
- Test: `tests/test_ci_gate.py`

**Interfaces:**
- Consumes: `store.repo_ci()`; per-queue `ci_monitoring`.
- Produces: each playlist entry gains `ci_state: "green"|"red"|"pending"|"unknown"|null` (`null` = not monitored); `ciDot(ciState)` in the UI.

**The colour mapping, verbatim from the operator:** green and red for green/red, **amber** for `PENDING`, **white** for `UNKNOWN`, **gray** when the repo is not monitored.

- [x] **Step 1: Write the failing test**

```python
@pytest.mark.parametrize("state,expected", [
    ("green", "green"), ("red", "red"),
    ("pending", "pending"), ("unknown", "unknown"),
])
def test_playlist_carries_its_ci_state(gate, state, expected):
    _ws, store, _stub, client = gate
    _call(client, store.set_repo_ci, "longitude", state=state,
          head_sha="aaa", url=None, detail=None)
    pls = {p["name"]: p for p in client.get("/api/playlists").json()["playlists"]}
    assert pls["main"]["ci_state"] == expected


def test_unmonitored_playlist_has_no_ci_state(unmonitored):
    _ws, store, _stub, client = unmonitored
    _call(client, store.set_repo_ci, "longitude", state="red",
          head_sha="aaa", url=None, detail=None)
    pls = {p["name"]: p for p in client.get("/api/playlists").json()["playlists"]}
    assert pls["main"]["ci_state"] is None
```

- [x] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -k playlist_carries -v`
Expected: FAIL — `KeyError: 'ci_state'`

- [x] **Step 3: Add `ci_state` to the playlists payload**

Where each playlist entry is built, with the repo's row read once for the whole list:

```python
        entry["ci_state"] = (
            (ci_rows.get(repo) or {}).get("state")
            if ci_monitoring_enabled(config) and repo else None
        )
```

- [x] **Step 4: Render the dot**

```javascript
// Build-status dot for a playlist bound to a monitored repo.
//   green / red  = CI green / red
//   amber        = PENDING (a run is in flight)
//   white        = UNKNOWN (no runs, or gh could not answer)
//   gray         = repo not monitored
function ciDot(ciState) {
  const dot = document.createElement("span");
  const cls = {
    green: "ci-dot-green", red: "ci-dot-red",
    pending: "ci-dot-amber", unknown: "ci-dot-white",
  }[ciState] || "ci-dot-gray";
  dot.className = "ci-dot " + cls;
  dot.title = ciState
    ? `CI ${ciState}`
    : "CI not monitored for this playlist";
  return dot;
}
```

In `playlistRow`, prepend it so the dot leads the row:

```javascript
  li.append(ciDot(pl.ci_state));
```

- [x] **Step 5: Add the CSS**

```css
.ci-dot { width: .6rem; height: .6rem; border-radius: 50%; display: inline-block;
          margin-right: .5rem; flex: 0 0 auto; }
.ci-dot-green { background: var(--ok, #3fb950); }
.ci-dot-red   { background: var(--err, #f85149); }
.ci-dot-amber { background: var(--warn, #d29922); }
.ci-dot-white { background: #f0f6fc; }
.ci-dot-gray  { background: var(--muted, #6e7681); }
```

- [x] **Step 6: Run to green**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -v && just validate`
Expected: 24 passed.

- [x] **Step 7: Commit** — `playlists: build-status dot (green/red/amber/white/gray)`

## Task 10: CI resolution in History and Stats

**Files:**
- Create: `src/nightshift/assets/migrations/20260831000002_nightshift_attempt_kind.sql`
- Modify: `src/nightshift/manager/store_sqlite.py` (attempts inline schema, ~150)
- Modify: `src/nightshift/manager/store.py` (attempt insert path)
- Modify: `src/nightshift/manager/api_worker.py` (`_lease_and_build` — carry the brief's `kind` onto the attempt)
- Modify: `src/nightshift/assets/ui/app.js` (History row badge)
- Modify: `src/nightshift/assets/ui/analytics.js` (the Stats category)
- Test: `tests/test_ci_gate.py`

**Interfaces:**
- Produces: an `attempts.kind` column (`NULL` for ordinary tasks, `"ci_resolution"` for spawned CI fixes), surfaced on run rows and split out as its own Stats category.

**Why a column:** History and Stats read attempts, not briefs. `workflow` is the existing precedent for a classification that flows from task frontmatter onto the attempt record, so `kind` follows it rather than inventing a join back to the brief file.

- [x] **Step 1: Write the failing test**

```python
def test_ci_resolution_attempt_is_tagged(gate):
    _ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="fail")
    _reconcile(client)
    _checkin(client)
    order = _poll(client)
    attempt = _call(client, store.get_attempt, order["run_id"])
    assert attempt["kind"] == "ci_resolution"


def test_ordinary_attempt_has_no_kind(unmonitored):
    _ws, store, _stub, client = unmonitored
    _checkin(client)
    order = _poll(client)
    attempt = _call(client, store.get_attempt, order["run_id"])
    assert not attempt.get("kind")
```

- [x] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -k attempt -v`
Expected: FAIL — `KeyError: 'kind'`

- [x] **Step 3: Add the column**

Migration `20260831000002_nightshift_attempt_kind.sql`:

```sql
-- migrate:up

-- Task classification carried from the brief's `kind:` frontmatter onto the
-- attempt, so History and the Stats page can split CI-resolution runs out from
-- ordinary work. NULL for every ordinary task (and every pre-existing row).
ALTER TABLE nightshift.attempts
    ADD COLUMN IF NOT EXISTS kind text;

-- migrate:down

ALTER TABLE nightshift.attempts DROP COLUMN IF EXISTS kind;
```

And in the SQLite inline attempts schema, beside `workflow`:

```sql
    kind           text,
```

- [x] **Step 4: Carry the brief's `kind` onto the attempt**

In `_lease_and_build`, where the brief's frontmatter is already read for title/model, pass `kind=meta.get("kind")` through to the attempt insert, and add `kind` to the attempt row builder in `store.py` alongside `workflow`.

- [x] **Step 5: Badge it in History**

In the History row builder, beside the existing status pill:

```javascript
  if (run.kind === "ci_resolution") {
    const tag = document.createElement("span");
    tag.className = "repo-tag repo-tag-ci";
    tag.textContent = "CI fix";
    tag.title = "Auto-spawned to resolve a failing CI run on main";
    row.append(tag);
  }
```

- [x] **Step 6: Split the Stats category**

`analytics.js` already partitions runs (e.g. `failure_kind === "validation_error"`). Add the CI-resolution split beside it so the Stats page reports these separately — count, success rate, median duration, cost — rather than blending them into ordinary throughput:

```javascript
  // CI-resolution runs are a distinct workload: they are reactive, usually
  // short, and their success rate is the real health metric for the gate.
  // Blending them into ordinary task stats would flatter both numbers.
  const ciRuns = rows.filter((r) => r.kind === "ci_resolution");
  const taskRuns = rows.filter((r) => r.kind !== "ci_resolution");
```

Render a "CI resolution" card next to the existing summary cards, using the same card builder, reporting: runs, landed %, median wall-clock, total cost.

- [x] **Step 7: Run to green**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -v && just validate && just migrate && just rollback && just migrate`
Expected: 26 passed, suite green, migration round-trips.

- [x] **Step 8: Commit** — `history + stats: ci_resolution as its own run category`

## Task 11: Enable monitoring and resume queued jobs

**Files:**
- Modify: `.tasks/<queue>/config.json` (operator state, through the UI)

- [x] **Step 1: Confirm `gh` is authenticated on the manager host**

Run: `gh auth status && gh run list --branch main --limit 1 --json status,conclusion,headSha`
Expected: authenticated; a JSON array with one run.

- [x] **Step 2: Smoke with monitoring off**

Run: `just smoke`
Expected: pass — monitoring is opt-in per queue and off by default, so this proves the change is inert until switched on.

- [ ] **Step 3: Turn monitoring on for one queue**

In the operator UI: **Repos → Queue bindings → CI monitoring → Yes** for the queue bound to the repo you want watched. Ask the operator to restart the manager (`src/nightshift/manager/` changed — root `AGENTS.md` rule 3).

- [ ] **Step 4: Verify each surface**

- **Repos page:** the repo row shows **Monitoring**; when `main` is red it also shows **Hold**.
- **Playlists page:** the playlist's dot is green / red / amber / white to match, and gray for any unmonitored playlist.
- **Queue:** the queue's other tasks read **CI red**; the spawned `fix-ci-*` task does **not**, and is what dispatches.
- **History:** the CI-resolution run carries the **CI fix** badge.
- **Stats:** the CI-resolution card reports it separately from ordinary task throughput.

- [ ] **Step 5: Verify resume-on-green**

Land the fix (or push a green commit), wait one `ci_refresh_seconds`, and confirm the holds clear, the dot turns green, and dispatch resumes with no operator action.

- [ ] **Step 6: Resume the queued backlog**

With the gate green and dispatch flowing, resume normal queue processing. Note that the longitude no-db conversion sessions are **not** part of this backlog — they run via `/implement` from `longitude/docs/plans/2026-08-30-no-db-nightshift-sessions-plan.md`.

## Decisions

| Decision | Why |
|---|---|
| Gate on `RED` only; `PENDING`/`UNKNOWN` dispatch | Gating on `PENDING` stalls every queue behind CI latency. The operator asked for "hold dispatch + auto-spawn fix", not "block landing until green". |
| Reuse the `repo_unavailable` idiom end to end | It is already a `TaskHoldKind` written/cleared by the reconciler, excluded read-only in `worker_poll`, and rendered by `STATE_LABELS`/`statusClass`. The CI gate is the same kind of condition, so it reuses the whole chain rather than standing up a parallel one. |
| `gh` confined to `src/nightshift/ci.py` | Mirrors the standing `GitRunner` invariant. One seam to stub in tests, one place to change if the CI source ever moves off `gh`. |
| Fail-open on every `gh` error | A manager host without `gh`, or a repo with no remote, must not silently freeze the queue. Absence of evidence is not evidence of breakage. |
| Dedupe fix tasks on `head_sha`, cleared by any state change | A repo red for an hour spawns one fix task; a repo that goes red→green→red gets a genuinely new one. The clear falls out of `set_repo_ci` rather than needing separate bookkeeping. |
| Resume-on-green carries no code | The gate reads current state on every poll, so a green refresh re-admits the repo automatically. An explicit "resume" path would be a second mechanism that could disagree with the first. |
| Monitoring defaults **off**, per queue | It depends on host state (`gh` auth) and on the repo actually having Actions. Task 11 turns it on deliberately, after the smoke driver proves the change is inert while off. |
| The switch lives in the queue's `config.json` | A queue is already bound to one repo and already carries operator-editable settings (`validate`, `order`, `sort`) there, edited through the screens this plan extends. A separate store table would be a second place to look. |
| The CI-resolution task is exempt from its own gate | Holding it would deadlock: the one task that can turn the repo green would be held out by the red it exists to clear. |
| `attempts.kind`, not a join back to the brief | History and Stats read attempts. `workflow` is the existing precedent for a frontmatter classification riding onto the attempt row. |

## Non-goals

- **Blocking landing on CI.** The manager still lands validated branches immediately; this plan gates *dispatch* only. Adding a landing gate is a separate change with its own deadlock risk.
- **Non-GitHub CI.** `gh` is the only source. A local-validate fallback for `remote_policy: none` repos is a later plan if it is ever wanted; nothing here forecloses it, because `check_repo_ci` is the single seam it would slot behind.
- **Reconciling longitude's gaps-plan Task 2.** The no-db sessions plan no longer decomposes into `.tasks/longitude/` briefs, which supersedes that task. It is a longitude edit and is called out there, not fixed here.
