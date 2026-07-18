"""In-app restart affordance: shared re_exec helper + manager/worker endpoints.

Behaviour, not implementation: ``re_exec`` builds the right ``execv`` argv
(mocking ``os.execv`` so the test process survives); the manager
``/api/control/restart`` endpoint sets the restart flag and schedules the
graceful shutdown; the worker ``/api/restart`` endpoint drains — restarting
immediately when idle and deferring (pending) while a run is in flight.
"""

from __future__ import annotations

import sys
from pathlib import Path

from starlette.testclient import TestClient

from _workspace import build_workspace
from nightshift import restart as restart_mod
from nightshift.manager.app import create_app
from nightshift.manager.store_sqlite import SqliteStore
from nightshift.worker.config import WorkerConfig
from nightshift.worker.local_store import LocalStore
from nightshift.worker.ui_app import create_worker_app


# --------------------------------------------------------------------------- #
# re_exec
# --------------------------------------------------------------------------- #


def test_re_exec_builds_module_argv(monkeypatch) -> None:
    calls: list[tuple] = []
    flushed: list[str] = []
    monkeypatch.setattr(restart_mod.os, "execv", lambda a, b: calls.append((a, b)))
    monkeypatch.setattr(restart_mod.sys.stdout, "flush", lambda: flushed.append("out"))
    monkeypatch.setattr(restart_mod.sys.stderr, "flush", lambda: flushed.append("err"))

    restart_mod.re_exec("nightshift.worker", ["--workspace", "/w", "--ui-port", "1"])

    assert calls == [
        (sys.executable, [sys.executable, "-m", "nightshift.worker",
                          "--workspace", "/w", "--ui-port", "1"])
    ]
    # stdout/stderr are flushed before the (mocked) execv so no log line is lost.
    assert set(flushed) == {"out", "err"}


def test_re_exec_empty_argv(monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(restart_mod.os, "execv", lambda a, b: calls.append((a, b)))
    monkeypatch.setattr(restart_mod.sys.stdout, "flush", lambda: None)
    monkeypatch.setattr(restart_mod.sys.stderr, "flush", lambda: None)

    restart_mod.re_exec("nightshift.manager", [])

    assert calls == [(sys.executable, [sys.executable, "-m", "nightshift.manager"])]


# --------------------------------------------------------------------------- #
# manager: POST /api/control/restart
# --------------------------------------------------------------------------- #


def test_manager_restart_sets_flag_and_schedules_shutdown(tmp_path: Path) -> None:
    root = build_workspace(tmp_path, tasks={"10.hi": "Do a thing."})
    app = create_app(root, store=SqliteStore())

    # A stub server standing in for the uvicorn.Server the __main__ sets up; the
    # deferred callback flips ``should_exit`` (asserted after the call runs).
    class _StubServer:
        should_exit = False

    scheduled: list[tuple[float, object]] = []

    with TestClient(app) as client:
        app.state.uvicorn_server = _StubServer()

        # Capture the deferred call_later so we can run it deterministically
        # instead of waiting real wall-clock time.
        import asyncio

        real_get_loop = asyncio.get_running_loop

        class _LoopProxy:
            def __init__(self, loop):
                self._loop = loop

            def call_later(self, delay, fn):
                scheduled.append((delay, fn))

            def __getattr__(self, name):
                return getattr(self._loop, name)

        def _patched():
            return _LoopProxy(real_get_loop())

        asyncio.get_running_loop = _patched  # type: ignore[assignment]
        try:
            resp = client.post("/api/control/restart")
        finally:
            asyncio.get_running_loop = real_get_loop  # type: ignore[assignment]

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "restarting": True}
        # Flag is set immediately; shutdown is deferred (not yet fired).
        assert app.state.restart_requested is True
        assert app.state.uvicorn_server.should_exit is False
        assert len(scheduled) == 1
        delay, fn = scheduled[0]
        assert delay > 0
        # Running the deferred callback flips should_exit → uvicorn stops → the
        # __main__ re-exec path runs.
        fn()
        assert app.state.uvicorn_server.should_exit is True


# --------------------------------------------------------------------------- #
# worker: POST /api/restart (drain semantics)
# --------------------------------------------------------------------------- #


def _worker_app(tmp_path: Path) -> tuple[object, LocalStore]:
    build_workspace(tmp_path, tasks={})
    cfg = WorkerConfig(workspace=tmp_path, worker_id="w1", manager_url="http://x")
    local = LocalStore(tmp_path)
    app = create_worker_app(cfg, local)
    return app, local


def test_worker_restart_idle_restarts_immediately(tmp_path: Path) -> None:
    app, local = _worker_app(tmp_path)

    class _StubServer:
        should_exit = False

    with TestClient(app) as client:
        app.state.uvicorn_server = _StubServer()
        assert local.now() is None  # idle
        resp = client.post("/api/restart")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "restarting": True, "pending": False}
        assert app.state.restart_pending is False
        # /api/info reports no pending restart while idle.
        assert client.get("/api/info").json()["restart_pending"] is False


def test_worker_restart_busy_is_pending(tmp_path: Path) -> None:
    app, local = _worker_app(tmp_path)

    class _StubServer:
        should_exit = False

    with TestClient(app) as client:
        app.state.uvicorn_server = _StubServer()
        # Simulate an active run so the loop is not idle.
        local.begin(
            run_id="r1", task="10.hi", queue="main", title="hi",
            model="auto", backend="claude-code",
        )
        assert local.now() is not None

        resp = client.post("/api/restart")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "restarting": True, "pending": True}
        # Restart is deferred: flag pending, server not yet told to exit.
        assert app.state.restart_pending is True
        assert app.state.uvicorn_server.should_exit is False
        # A strong reference to the drain poller lives on app.state (asyncio
        # only weak-refs tasks — without this the poller could be GC'd
        # mid-drain and the restart would never fire).
        first_task = app.state.drain_task
        assert first_task is not None
        # The pending state is surfaced to the UI via /api/info.
        assert client.get("/api/info").json()["restart_pending"] is True

        # A second Restart! click during the drain is acknowledged without
        # spawning a duplicate poller.
        resp2 = client.post("/api/restart")
        assert resp2.status_code == 200
        assert resp2.json() == {"ok": True, "restarting": True, "pending": True}
        assert app.state.drain_task is first_task
