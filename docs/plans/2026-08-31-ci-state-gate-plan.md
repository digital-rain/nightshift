---
status: draft
date: 2026-08-31
---

# CI-State Gate Implementation Plan

> **For agentic workers:** execute with the `implement` skill, task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nightshift watches each target repo's GitHub Actions state on `main`, holds task dispatch for a repo whose `main` is red, auto-spawns an async `/fix`-shaped task for the failure, and resumes dispatch when `main` goes green again.

**Architecture:** A new `gh` seam (`src/nightshift/ci.py`) mirrors the `GitRunner` invariant — it is the only place `gh` is invoked as a subprocess — and maps one `gh run list` call into a `CiStatus`. The manager's existing reconciler gains one duty that refreshes each repo's CI state on a cadence and persists it. The dispatch gate rides the **existing** `repo_excluded` seam in `worker_poll` (the same set that already holds back `repo_unavailable` tasks), so a red repo simply contributes its tasks to `blocked` — no parallel pause table, no second dispatch path. Resume-on-green needs no code of its own: the gate reads current state each poll, so a green refresh re-admits the repo's tasks automatically.

## Global constraints

- **The `gh` CLI is the only CI source.** Status comes from `gh run list` executed with `cwd` set to the repo root, so `gh` resolves the repo from its own git remote. A repo with no remote, or a host without `gh` authenticated, resolves to `UNKNOWN` and never gates.
- **The gate is fail-open.** Only `RED` holds dispatch. `PENDING`, `UNKNOWN`, and `GREEN` all dispatch normally. Gating on `PENDING` would stall every queue behind CI latency, which is not what was asked for.
- **`gh` invocation is confined to `src/nightshift/ci.py`.** Mirrors the standing rule for `git` in `GitRunner` ("the ONLY place `subprocess` appears in the git layer"). No other module shells out to `gh`.
- **New store state needs both backends.** Postgres gets a migration under `src/nightshift/assets/migrations/` with `-- migrate:up` *and* `-- migrate:down`; SQLite gets the matching `CREATE TABLE` in the inline schema in `src/nightshift/manager/store_sqlite.py`. Shared SQL lives on `SqlStoreBase`.
- **The manager stays the sole git authority.** This plan adds no writer to `main`. It reads CI state and gates dispatch; it never lands, reverts, or pushes.
- **One fix task per failing commit.** Dedupe on `head_sha` so a repo that stays red across many refreshes spawns exactly one fix task, not one per tick.
- **Restart note:** changes under `src/nightshift/manager/` require a manager restart — leave that to the operator (root `AGENTS.md` rule 3).

## File structure

| File | Responsibility |
|---|---|
| Create `src/nightshift/ci.py` | The `gh` seam: `GhRunner`, `CiState`, `CiStatus`, `check_repo_ci()`. Pure mapping from `gh` JSON to a status; no store, no config. |
| Create `tests/test_ci_seam.py` | `gh`-JSON → `CiStatus` mapping, including every conclusion value and the no-runs case. |
| Create `src/nightshift/assets/migrations/20260831000001_nightshift_repo_ci.sql` | Postgres `nightshift.repo_ci` table (up + down). |
| Modify `src/nightshift/manager/store_sqlite.py` (inline schema, near the `queue_state` block at ~236) | Matching SQLite `CREATE TABLE nightshift.repo_ci`. |
| Modify `src/nightshift/manager/store.py` | `NightshiftStore` protocol entries + `SqlStoreBase` implementations of `repo_ci()` / `set_repo_ci()` / `set_repo_ci_fix()`. |
| Modify `src/nightshift/config/manager.py` | `OperatorConfig` CI knobs: `ci_gate`, `ci_fix_queue`; `Cadences.ci_refresh_seconds`. |
| Modify `src/nightshift/manager/reconciler.py` | One new duty, `_refresh_repo_ci`, wired into `reconcile_once()`; `import time` for the per-repo throttle. |
| Modify `src/nightshift/manager/api_worker.py` (~line 622, the `repo_excluded` loop) | Extend the existing dispatch exclusion with CI-red repos. |
| Modify `tests/test_nightshift_store.py` | Round-trip + transition-return coverage for the new store methods. |
| Create `tests/test_ci_gate.py` | Dispatch exclusion, fix-task spawn + dedupe, resume-on-green. |
| Modify `docs/user/configuration-reference.md` | Document the three new settings. |

## Task 1: The `gh` seam

**Files:**
- Create: `src/nightshift/ci.py`
- Test: `tests/test_ci_seam.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces: `class CiState(StrEnum)` with members `GREEN`, `RED`, `PENDING`, `UNKNOWN`; `@dataclass(frozen=True) class CiStatus` with fields `state: CiState`, `head_sha: str | None`, `url: str | None`, `detail: str | None`; `class GhRunner` with `__init__(self, repo_root: Path)` and `run(self, *args: str) -> tuple[int, str, str]`; `def status_from_runs(payload: str) -> CiStatus`; `def check_repo_ci(repo_root: Path, *, branch: str = "main", runner: GhRunner | None = None) -> CiStatus`.

- [ ] **Step 1: Write the failing test**

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

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ci_seam.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nightshift.ci'`

- [ ] **Step 3: Write minimal implementation**

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

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_ci_seam.py -v`
Expected: 8 passed.

- [ ] **Step 5: Add a `check_repo_ci` test with a stub runner**

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

- [ ] **Step 6: Run to green, then lint**

Run: `.venv/bin/python -m pytest tests/test_ci_seam.py -v && .venv/bin/python -m ruff check src/nightshift/ci.py tests/test_ci_seam.py`
Expected: 10 passed, ruff clean.

- [ ] **Step 7: Commit** — `ci seam: gh run list -> CiStatus, fail-open on every error path`

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

- [ ] **Step 1: Write the migration**

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

- [ ] **Step 2: Mirror the table in the SQLite inline schema**

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

- [ ] **Step 3: Write the failing store test**

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

- [ ] **Step 4: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_nightshift_store.py -k repo_ci -v`
Expected: FAIL — `AttributeError: 'SqliteStore' object has no attribute 'set_repo_ci'`

- [ ] **Step 5: Add the protocol entries**

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

- [ ] **Step 6: Implement once on `SqlStoreBase`**

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

- [ ] **Step 7: Run to green**

Run: `.venv/bin/python -m pytest tests/test_nightshift_store.py -k repo_ci -v`
Expected: 2 passed.

- [ ] **Step 8: Verify the Postgres migration round-trips**

Run: `just migrate && just rollback && just migrate`
Expected: all three succeed; `repo_ci` exists after the final migrate.

- [ ] **Step 9: Commit** — `store: per-repo CI state with transition-returning upsert`

## Task 3: Configuration knobs

**Files:**
- Modify: `src/nightshift/config/manager.py` (`Cadences` ~23, `OperatorConfig` ~54)
- Modify: `docs/user/configuration-reference.md`
- Test: `tests/test_nightshift_config.py`

**Interfaces:**
- Produces: `Cadences.ci_refresh_seconds: float = 120.0`; `OperatorConfig.ci_gate: bool = False`; `OperatorConfig.ci_fix_queue: str = ""`.

- [ ] **Step 1: Write the failing test**

```python
def test_ci_gate_defaults_off():
    cfg = ManagerSettings()
    assert cfg.ci_gate is False
    assert cfg.ci_fix_queue == ""
    assert cfg.cadences.ci_refresh_seconds == 120.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_nightshift_config.py -k ci_gate -v`
Expected: FAIL — `AttributeError: 'ManagerSettings' object has no attribute 'ci_gate'`

- [ ] **Step 3: Add the fields**

In `Cadences`:

```python
    ci_refresh_seconds: float = field(default=120.0, metadata=meta(
        category="Cadences", label="CI refresh seconds",
        desc=(
            "Minimum interval between GitHub Actions status checks per target "
            "repo. Each check is one `gh run list` call against the repo's "
            "default branch. Only applies when the CI gate is enabled."),
        apply="restart"))
```

In `OperatorConfig`:

```python
    ci_gate: bool = field(default=False, metadata=meta(
        category="Operator", label="CI gate",
        desc=(
            "Hold task dispatch for a repo whose main branch has a failing "
            "GitHub Actions run, and auto-spawn a fix task for the failure. "
            "Dispatch resumes automatically when main goes green. Requires an "
            "authenticated `gh` CLI on the manager host; repos with no remote "
            "are never gated."),
        apply="restart"))
    ci_fix_queue: str = field(default="", metadata=meta(
        category="Operator", label="CI fix queue",
        desc=(
            "Queue that receives auto-spawned CI fix tasks. Empty means the "
            "main queue. Only used when the CI gate is enabled."),
        apply="restart"))
```

- [ ] **Step 4: Run to green**

Run: `.venv/bin/python -m pytest tests/test_nightshift_config.py -k ci_gate -v`

- [ ] **Step 5: Document the three settings in `docs/user/configuration-reference.md`**

Add to the settings reference, matching the surrounding row format:

```markdown
| `ci_gate` | `false` | Hold dispatch for a repo whose `main` is red on GitHub Actions, auto-spawn a fix task, resume on green. Needs an authenticated `gh` on the manager host. |
| `ci_fix_queue` | `""` | Queue for auto-spawned CI fix tasks; empty = main queue. |
| `cadences.ci_refresh_seconds` | `120` | Minimum seconds between `gh run list` checks per repo. |
```

- [ ] **Step 6: Commit** — `config: ci_gate, ci_fix_queue, cadences.ci_refresh_seconds (gate default off)`

## Task 4: The reconciler refresh duty

**Files:**
- Modify: `src/nightshift/manager/reconciler.py` (`reconcile_once` ~191; new duty beside the others; `Reconciler.__init__` ~124)
- Create: `tests/test_ci_gate.py`

**Interfaces:**
- Consumes: `check_repo_ci`, `CiState`, `CiStatus` (Task 1); `store.repo_ci` / `store.set_repo_ci` (Task 2); `cfg.ci_gate`, `cfg.cadences.ci_refresh_seconds` (Task 3); the existing `repos.known_repos(workspace)`, `repos.repo_available(workspace, repo)`, `repos.repo_root(workspace, repo)`.
- Produces: `Reconciler._refresh_repo_ci()` registered as a duty; `Reconciler._ci_checked_at: dict[str, float]`; the `repo_ci` event kind, emitted only on a state transition.

- [ ] **Step 1: Write the shared test harness**

Create `tests/test_ci_gate.py`. This mirrors the harness already used by `tests/test_reconciler.py` — `TestClient(create_app(...))`, duties driven through `client.app.state.reconciler`, and `build_workspace` for the fixture. Tests are **synchronous**; coroutines run on the app loop via `client.portal.call`.

```python
"""Tests for the CI gate: refresh duty, fix spawn, dispatch exclusion, resume."""

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


def _build(tmp_path, monkeypatch, *, ci_gate: bool):
    ws = build_workspace(
        tmp_path,
        tasks={"alpha": "do the thing"},
        config={"ci_gate": ci_gate},
    )
    stub = _StubCi(CiStatus(CiState.GREEN, head_sha="aaa"))
    monkeypatch.setattr("nightshift.manager.reconciler.check_repo_ci", stub)
    store = SqliteStore()
    return ws, store, stub, TestClient(create_app(ws, store=store))


@pytest.fixture
def gate(tmp_path, monkeypatch):
    ws, store, stub, client = _build(tmp_path, monkeypatch, ci_gate=True)
    with client:
        yield ws, store, stub, client


@pytest.fixture
def gate_off(tmp_path, monkeypatch):
    ws, store, stub, client = _build(tmp_path, monkeypatch, ci_gate=False)
    with client:
        yield ws, store, stub, client


def _call(client: TestClient, fn, *a, **kw):
    """Run one store coroutine on the app's event loop."""
    return client.portal.call(partial(fn, *a, **kw))


def _reconcile(client: TestClient) -> None:
    client.portal.call(client.app.state.reconciler.reconcile_once)


def _open_throttle(client: TestClient) -> None:
    """Clear the per-repo ci_refresh_seconds throttle so the next reconcile
    actually re-checks."""
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

- [ ] **Step 2: Write the failing duty tests**

```python
def test_refresh_records_state(gate):
    _ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", url="u", detail="pytest: failure")
    _reconcile(client)
    assert _call(client, store.repo_ci)["longitude"]["state"] == "red"


def test_transition_emits_a_repo_ci_event(gate):
    _ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="pytest: failure")
    _reconcile(client)
    events = _call(client, store.events_since, 0)
    assert [e for e in events if e.get("type") == "repo_ci"]


def test_steady_state_emits_nothing_new(gate):
    """A repo that stays green must not churn the operator's event feed."""
    _ws, store, stub, client = gate
    stub.status = CiStatus(CiState.GREEN, head_sha="aaa")
    _reconcile(client)
    cursor = _call(client, store.max_event_id)
    _open_throttle(client)
    _reconcile(client)
    fresh = _call(client, store.events_since, cursor)
    assert not [e for e in fresh if e.get("type") == "repo_ci"]


def test_throttle_suppresses_an_immediate_recheck(gate):
    _ws, _store, stub, client = gate
    _reconcile(client)
    _reconcile(client)          # still inside ci_refresh_seconds
    assert stub.calls == 1


def test_gate_off_never_shells_out(gate_off):
    _ws, store, stub, client = gate_off
    _reconcile(client)
    assert stub.calls == 0
    assert _call(client, store.repo_ci) == {}
```

- [ ] **Step 3: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -v`
Expected: FAIL — `AttributeError: module 'nightshift.manager.reconciler' has no attribute 'check_repo_ci'` (monkeypatch target does not exist yet).

- [ ] **Step 4: Add the imports**

Add to the imports at the top of `reconciler.py`. `asyncio` and `repos` are already there; `time` is **not**:

```python
import time

from nightshift.ci import CiState, CiStatus, check_repo_ci
```

- [ ] **Step 5: Initialise the throttle map in `Reconciler.__init__`**

Beside the other `self._…` assignments:

```python
        # Per-repo wall-clock of the last gh check (cadences.ci_refresh_seconds).
        self._ci_checked_at: dict[str, float] = {}
```

- [ ] **Step 6: Implement the duty**

```python
    async def _refresh_repo_ci(self) -> None:
        """Refresh each known repo's GitHub Actions state on its default branch.

        Throttled per repo by ``cadences.ci_refresh_seconds``, and a complete
        no-op when ``ci_gate`` is off — a host without ``gh`` never pays for the
        feature. Only a *state change* emits an event, so a steady repo stays
        off the operator's feed entirely.
        """
        if not self._cfg.ci_gate:
            return
        store = self._store()
        interval = float(self._cfg.cadences.ci_refresh_seconds or 0)
        now = time.time()
        for repo in repos.known_repos(self._workspace):
            if not repos.repo_available(self._workspace, repo):
                continue
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
                type="repo_ci",
                repo=repo,
                state=str(status.state),
                previous=previous,
                head_sha=status.head_sha,
                url=status.url,
                detail=status.detail,
            )
            if status.state is CiState.RED:
                await self._spawn_ci_fix(repo, status)
```

Register it in `reconcile_once`, after the existing duties:

```python
        await self._run_duty("repo CI refresh", self._refresh_repo_ci)
```

Add the forward stub that Task 5 replaces (this keeps Task 4 independently green; its replacement is Task 5 Step 3, inside this plan):

```python
    async def _spawn_ci_fix(self, repo: str, status: CiStatus) -> None:
        """Replaced by Task 5."""
        return None
```

- [ ] **Step 7: Run to green**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -v`
Expected: 5 passed.

- [ ] **Step 8: Commit** — `reconciler: repo CI refresh duty, throttled, event on transition only`

## Task 5: Auto-spawn the fix task

**Files:**
- Modify: `src/nightshift/manager/reconciler.py` (replace the `_spawn_ci_fix` stub)
- Test: `tests/test_ci_gate.py`

**Interfaces:**
- Consumes: `task_files.create_task(tasks_root, title, text, tasks_rel="main") -> dict`; `store.set_repo_ci_fix` (Task 2); `cfg.ci_fix_queue` (Task 3).
- Produces: `Reconciler._spawn_ci_fix(repo: str, status: CiStatus) -> None`; the `repo_ci_fix_spawned` event kind.

- [ ] **Step 1: Write the failing tests**

```python
def test_red_spawns_one_fix_task(gate):
    ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb",
                           url="https://gh/run/1", detail="pytest: failure")
    _reconcile(client)

    row = _call(client, store.repo_ci)["longitude"]
    assert row["fix_task"] and row["fix_sha"] == "bbb"

    brief = (_tasks_dir(ws) / f"{row['fix_task']}.md").read_text()
    assert "/fix" in brief
    assert "pytest: failure" in brief
    assert "https://gh/run/1" in brief


def test_same_red_sha_does_not_respawn(gate):
    ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="fail")
    _reconcile(client)
    first = _call(client, store.repo_ci)["longitude"]["fix_task"]

    _open_throttle(client)
    _reconcile(client)          # same failing sha
    assert _call(client, store.repo_ci)["longitude"]["fix_task"] == first
    assert len(list(_tasks_dir(ws).glob("fix-ci-*.md"))) == 1


def test_red_green_red_spawns_a_second_fix(gate):
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

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -k fix -v`
Expected: FAIL — `fix_task` is `None`; the stub returns without spawning.

- [ ] **Step 3: Replace the stub**

```python
    async def _spawn_ci_fix(self, repo: str, status: CiStatus) -> None:
        """Queue one `/fix` task for a red main, deduped on the failing sha.

        ``set_repo_ci`` clears the fix marker on any state change, so red ->
        green -> red gets a genuinely new fix task while a repo that simply
        stays red across refreshes gets exactly one.
        """
        store = self._store()
        row = (await store.repo_ci()).get(repo) or {}
        if row.get("fix_task") and row.get("fix_sha") == status.head_sha:
            return  # already queued for this failing commit

        short = (status.head_sha or "unknown")[:8]
        title = f"fix ci: {repo} main is red at {short}"
        body = (
            f"/fix CI is failing on `{repo}` `main`.\n\n"
            f"- **Failing commit:** `{status.head_sha or 'unknown'}`\n"
            f"- **Detail:** {status.detail or 'no detail reported'}\n"
            f"- **Run:** {status.url or 'no run URL reported'}\n\n"
            "Reproduce the failure locally, find the root cause, fix it, and "
            "verify against the same check that failed. Nightshift is holding "
            "dispatch for this repo until `main` is green again.\n"
        )
        tasks_rel = self._cfg.ci_fix_queue or "main"
        try:
            created = await asyncio.to_thread(
                create_task, self._tasks_root, title, body, tasks_rel
            )
        except (FileExistsError, ValueError):
            return  # slug already taken, or an empty title: nothing to queue
        await store.set_repo_ci_fix(
            repo, fix_task=created["task"], fix_sha=status.head_sha or ""
        )
        await self._emit(
            type="repo_ci_fix_spawned",
            repo=repo,
            task=created["task"],
            queue=tasks_rel,
            head_sha=status.head_sha,
        )
```

Add the import at the top of `reconciler.py`:

```python
from nightshift.task_files import create_task
```

> The title slugifies to `fix-ci-<repo>-main-is-red-at-<short>`, which is what the tests glob for. `create_task` appends the new task to the queue's `config.json` order, so it lands at the queue tail where the operator can drag it forward.

- [ ] **Step 4: Run to green**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit** — `reconciler: auto-spawn one /fix task per failing main sha`

## Task 6: The dispatch gate

**Files:**
- Modify: `src/nightshift/manager/api_worker.py` (the `repo_excluded` loop at ~622)
- Test: `tests/test_ci_gate.py`

**Interfaces:**
- Consumes: `store.repo_ci()` (Task 2); `cfg.ci_gate` (Task 3); the existing `repo_excluded` set and `TaskCandidate.repo`.
- Produces: no new symbols. The gate extends the exclusion set that already holds back `repo_unavailable` tasks, so a held task reaches the worker exactly as those already do.

- [ ] **Step 1: Write the failing tests**

```python
def test_red_repo_is_not_dispatched(gate):
    _ws, store, _stub, client = gate
    _call(client, store.set_repo_ci, "longitude", state="red",
          head_sha="bbb", url=None, detail="pytest: failure")
    _checkin(client)
    assert _poll(client) is None


def test_green_repo_dispatches(gate):
    _ws, store, _stub, client = gate
    _call(client, store.set_repo_ci, "longitude", state="green",
          head_sha="aaa", url=None, detail=None)
    _checkin(client)
    assert _poll(client) is not None


@pytest.mark.parametrize("state", ["pending", "unknown"])
def test_pending_and_unknown_do_not_gate(gate, state):
    """Fail-open: CI latency must never stall a queue."""
    _ws, store, _stub, client = gate
    _call(client, store.set_repo_ci, "longitude", state=state,
          head_sha="aaa", url=None, detail=None)
    _checkin(client)
    assert _poll(client) is not None


def test_gate_off_dispatches_through_red(gate_off):
    _ws, store, _stub, client = gate_off
    _call(client, store.set_repo_ci, "longitude", state="red",
          head_sha="bbb", url=None, detail=None)
    _checkin(client)
    assert _poll(client) is not None


def test_resume_on_green_needs_no_resume_path(gate):
    """The gate reads live state on every poll, so green re-admits the repo
    with no explicit resume step anywhere in the system."""
    _ws, store, _stub, client = gate
    _call(client, store.set_repo_ci, "longitude", state="red",
          head_sha="bbb", url=None, detail=None)
    _checkin(client)
    assert _poll(client) is None
    _call(client, store.set_repo_ci, "longitude", state="green",
          head_sha="ccc", url=None, detail=None)
    assert _poll(client) is not None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -k "dispatch or gate or resume" -v`
Expected: FAIL — `test_red_repo_is_not_dispatched` receives work; the gate does not exist yet.

- [ ] **Step 3: Extend the existing exclusion**

In `worker_poll`, immediately after the existing `repo_excluded` loop (the one already holding back `repo_error`, unavailable repos, and `workflow_error`) and **before** the existing `blocked |= repo_excluded` line:

```python
        # CI gate: a repo whose main is red holds its tasks out of dispatch on
        # the same seam that already holds back unavailable repos. Only RED
        # gates -- pending/unknown/green all dispatch, so CI latency never
        # stalls a queue. Resume-on-green needs no code of its own: the next
        # poll reads refreshed state and these tasks are simply not excluded.
        if cfg.ci_gate:
            red_repos = {
                repo for repo, row in (await store.repo_ci()).items()
                if row.get("state") == "red"
            }
            if red_repos:
                for cands in candidates_by_queue.values():
                    for cand in cands:
                        if cand.repo in red_repos:
                            repo_excluded.add((cand.queue, cand.task))
```

- [ ] **Step 4: Run to green**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -v`
Expected: 14 passed.

- [ ] **Step 5: Full suite + lint**

Run: `just validate`
Expected: ruff clean, whole suite green.

- [ ] **Step 6: Commit** — `poll: hold dispatch for repos whose main is red (rides repo_excluded)`

## Task 7: Operator surfacing

**Files:**
- Modify: `src/nightshift/lifecycle.py` (`TaskHoldKind`, ~127)
- Modify: `src/nightshift/manager/reconciler.py` (`_reconcile_holds`, the repo-availability block ~513 and the hold-clear loop ~556)
- Modify: `src/nightshift/manager/api_operator.py` (`_state_payload`, ~837)
- Modify: `src/nightshift/assets/ui/app.js` (`STATE_LABELS` ~185, `statusClass` ~207)
- Modify: `ARCHITECTURE.md` (§Task lifecycle)
- Test: `tests/test_ci_gate.py`

**Interfaces:**
- Consumes: `store.repo_ci()` (Task 2); the existing `store.set_task_state` / `store.clear_task_state` / `store.tasks_in_state`.
- Produces: `TaskHoldKind.CI_RED = "ci_red"`; a `repo_ci` key on `/api/state`; the `ci_red` entries in the UI's status vocabulary.

**Why this shape:** the codebase already has a complete idiom for "a repo-level condition is holding these tasks" — `repo_unavailable`. It is a `TaskHoldKind` written by the reconciler's `_reconcile_holds`, cleared by the same duty when the condition lifts, excluded from dispatch read-only in `worker_poll`, and rendered in the UI as a status pill via `STATE_LABELS` / `statusClass`. The CI gate is the same kind of condition and gets the same treatment — **not** a bespoke banner. Task 6 already supplied the read-only dispatch exclusion, which is the half that matches `worker_poll`'s stated contract ("the corresponding hold writes and warnings are the reconciler's"). This task supplies the other half.

- [ ] **Step 1: Write the failing tests**

```python
def test_red_repo_marks_its_tasks_ci_red(gate):
    _ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="pytest: failure")
    _reconcile(client)
    row = _call(client, store.get_task_state, None, "alpha")
    assert row["state"] == "ci_red"


def test_green_clears_the_ci_red_hold(gate):
    _ws, store, stub, client = gate
    stub.status = CiStatus(CiState.RED, head_sha="bbb", detail="fail")
    _reconcile(client)
    assert _call(client, store.get_task_state, None, "alpha")["state"] == "ci_red"

    _open_throttle(client)
    stub.status = CiStatus(CiState.GREEN, head_sha="ccc")
    _reconcile(client)
    assert not _call(client, store.get_task_state, None, "alpha")


def test_state_payload_carries_repo_ci(gate):
    _ws, store, _stub, client = gate
    _call(client, store.set_repo_ci, "longitude", state="red",
          head_sha="bbb", url="https://gh/run/1", detail="pytest: failure")
    payload = client.get("/api/state").json()
    assert payload["repo_ci"]["longitude"]["state"] == "red"
    assert payload["repo_ci"]["longitude"]["url"] == "https://gh/run/1"
    assert payload["repo_ci"]["longitude"]["head_sha"] == "bbb"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -k "ci_red or state_payload" -v`
Expected: FAIL — `get_task_state` returns `None` (no hold is written), and `KeyError: 'repo_ci'`.

- [ ] **Step 3: Add the hold kind**

In `lifecycle.py`, beside `REPO_UNAVAILABLE`:

```python
    CI_RED = "ci_red"
```

- [ ] **Step 4: Write and clear the hold in `_reconcile_holds`**

The repo-availability block already walks every candidate. Extend it — after the `elif cand.repo and not repos.repo_available(...)` arm — with the CI arm:

```python
                elif cand.repo and cand.repo in red_repos:
                    existing = await store.get_task_state(cand.queue, cand.task)
                    if not existing or existing.get("state") != TaskHoldKind.CI_RED:
                        await store.set_task_state(
                            cand.queue, cand.task, TaskHoldKind.CI_RED,
                            repo=cand.repo,
                        )
```

`red_repos` is read once at the top of the same duty, beside the existing reads:

```python
        red_repos: set[str] = set()
        if self._cfg.ci_gate:
            red_repos = {
                repo for repo, row in (await store.repo_ci()).items()
                if row.get("state") == "red"
            }
```

And the clear, beside the existing `REPO_UNAVAILABLE` clear loop (same silent-clear rule):

```python
        for row in await store.tasks_in_state(TaskHoldKind.CI_RED):
            repo = row.get("repo")
            if not repo or repo not in red_repos:
                await store.clear_task_state(
                    self._queue_from_label(row.get("queue")), row["task"]
                )
```

> Ordering note: `reconcile_once` must run `repo CI refresh` **before** `hold set/clear`, so the holds are written against the state this tick fetched. Move the duty registration from Task 4 Step 6 to sit above `self._reconcile_holds` in `reconcile_once`.

- [ ] **Step 5: Add `repo_ci` to `_state_payload`**

`_state_payload` already reads `pauses = await store.queue_pauses()` at ~841. Add beside it:

```python
        ci_rows = await store.repo_ci()
```

and the key on the returned dict (which currently returns `{**focused_state, "active_playlist": focused, "queues": queues}`):

```python
            "repo_ci": {
                repo: {
                    "state": row.get("state"),
                    "head_sha": row.get("head_sha"),
                    "url": row.get("url"),
                    "detail": row.get("detail"),
                    "fix_task": row.get("fix_task"),
                }
                for repo, row in ci_rows.items()
            },
```

- [ ] **Step 6: Teach the UI vocabulary the new status**

In `app.js`, `STATE_LABELS` (~185) — beside the `repo_unavailable` entry and its comment:

```javascript
  // A task whose target repo has a failing CI run on main is paused
  // (auto-resumable when CI goes green), never failed. Distinct label from
  // repo_unavailable so the operator can tell the two pauses apart at a glance.
  ci_red: "CI red",
```

In `statusClass` (~207), reuse the same warn treatment:

```javascript
  if (status === "ci_red") return "paused";
```

> No new CSS: `ci_red` deliberately reuses the existing `.status.paused` warn pill, exactly as `repo_unavailable` does. No new DOM, no banner — the queue rows the operator already reads carry the state.

- [ ] **Step 7: Run to green**

Run: `.venv/bin/python -m pytest tests/test_ci_gate.py -v && just validate`
Expected: 17 passed, suite green, ruff clean.

- [ ] **Step 8: Add the paragraph to `ARCHITECTURE.md` §Task lifecycle**

```markdown
When `ci_gate` is enabled, the reconciler refreshes each repo's GitHub Actions
state on `main` (`cadences.ci_refresh_seconds`) and holds that repo's tasks
`ci_red` — the same hold-and-clear shape as `repo_unavailable`, rendered as a
"CI red" pill in the queue. `worker_poll` excludes held tasks read-only, one
`/fix` task is spawned per failing commit, and the hold clears itself once
`main` is green, so dispatch resumes with no operator action.
```

- [ ] **Step 9: Commit** — `ui: surface the CI hold as a ci_red pill, matching repo_unavailable`

## Task 8: Enable the gate and resume queued jobs

**Files:**
- Modify: `.nightshift/manager.json` (operator state — not the package)
- Test: manual, with the smoke driver as the automated half.

- [ ] **Step 1: Confirm `gh` is authenticated on the manager host**

Run: `gh auth status && gh run list --branch main --limit 1 --json status,conclusion,headSha --repo <owner>/<repo>`
Expected: authenticated; a JSON array with one run.

- [ ] **Step 2: Run the end-to-end smoke driver with the gate off**

Run: `just smoke`
Expected: pass — the gate defaults off, so this proves the change is inert until enabled.

- [ ] **Step 3: Enable the gate**

Set in `.nightshift/manager.json`:

```json
{
  "ci_gate": true,
  "ci_fix_queue": "",
  "cadences": { "ci_refresh_seconds": 120 }
}
```

Then ask the operator to restart the manager (`src/nightshift/manager/` changed — root `AGENTS.md` rule 3).

- [ ] **Step 4: Verify the gate holds a red repo**

With a repo whose `main` is genuinely red, confirm in the operator UI:
- the red banner names the repo and links the failing run;
- a `fix-ci-*` task appears in the configured queue;
- workers polling for that repo's queue receive `{"work": null}` while other repos' queues keep dispatching.

- [ ] **Step 5: Verify resume-on-green**

Land the fix (or push a green commit), wait one `ci_refresh_seconds` interval, and confirm the banner clears and dispatch resumes with no operator action.

- [ ] **Step 6: Resume the queued backlog**

With the gate green and dispatch flowing, resume the queued jobs. The longitude no-db conversion sessions (`longitude/docs/plans/2026-08-30-no-db-nightshift-sessions-plan.md`) are the first workload to run under the new gate.

- [ ] **Step 7: Commit** — `enable ci gate` (config only; no package change)

## Decisions

| Decision | Why |
|---|---|
| Gate on `RED` only; `PENDING`/`UNKNOWN` dispatch | Gating on `PENDING` stalls every queue behind CI latency. The operator asked for "hold dispatch + auto-spawn fix", not "block landing until green". |
| Ride `repo_excluded`, don't add a repo-pause table | `worker_poll` already has one repo-level dispatch exclusion (`repo_unavailable`). Deepening it keeps one dispatch gate; a parallel pause table would be a second one to keep in sync. |
| `gh` confined to `src/nightshift/ci.py` | Mirrors the standing `GitRunner` invariant. One seam to stub in tests, one place to change if the CI source ever moves off `gh`. |
| Fail-open on every `gh` error | A manager host without `gh`, or a repo with no remote, must not silently freeze the queue. Absence of evidence is not evidence of breakage. |
| Dedupe fix tasks on `head_sha`, cleared by any state change | A repo red for an hour spawns one fix task; a repo that goes red→green→red gets a genuinely new one. The clear falls out of `set_repo_ci` rather than needing separate bookkeeping. |
| Resume-on-green carries no code | The gate reads current state on every poll, so a green refresh re-admits the repo automatically. An explicit "resume" path would be a second mechanism that could disagree with the first. |
| Gate defaults **off** | It depends on host state (`gh` auth) that nightshift cannot assume. Task 8 turns it on deliberately, after the smoke driver proves the change is inert while disabled. |

## Non-goals

- **Blocking landing on CI.** The manager still lands validated branches immediately; this plan gates *dispatch* only. Adding a landing gate is a separate change with its own deadlock risk.
- **Non-GitHub CI.** `gh` is the only source. A local-validate fallback for `remote_policy: none` repos is a later plan if it is ever wanted; nothing here forecloses it, because `check_repo_ci` is the single seam it would slot behind.
- **Reconciling longitude's gaps-plan Task 2.** The no-db sessions plan no longer decomposes into `.tasks/longitude/` briefs, which supersedes that task. It is a longitude edit and is called out there, not fixed here.
