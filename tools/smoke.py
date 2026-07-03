"""Self-contained end-to-end smoke test: real manager + real worker.

Run via ``just smoke``. Builds an isolated temp workspace (its own clone of
this repo as the target, its own ``nightshift-tasks`` content store, its own
``.nightshift`` config), then launches ``python -m nightshift.manager`` and a
worker subprocess and drives the operator API through:

* playlist rescan — materialise a queue per workspace repo, hide it, rescan
  again (the hidden queue must stay hidden), unhide it;
* the main queue — a good task dispatched, paused mid-run, stopped (the
  attempt is aborted and the worker's late submit is fenced), started again,
  validated, and squash-landed onto the clone's main; a bad task that errors,
  is retried by the phase-B policy, fails again, and ends quarantined with
  the queue paused (``retry_failed``);
* the rescanned playlist queue — select it as the focused queue, create a
  simple task and run it to completion; edit/save a task (title + body);
  run/pause/stop it; disable/enable it while paused and mid-run;
* quarantine release — clear the bad task's quarantine, fix its brief via an
  edit, press play, and watch it land.

The only substituted seam is the agent backend: the worker subprocess (this
same script with ``--role worker``) registers a deterministic ``smoke``
backend that obeys ``SMOKE: [sleep=N] commit|fail`` directives in the brief.
Everything else — HTTP protocol, scheduler, leases, worktrees, validate,
landing — is production code.

Safe to run while a live manager/worker/UI is up on the same host: the
workspace is a fresh temp dir, ports are ephemeral, and every ``NIGHTSHIFT_*``
environment variable is scrubbed so the manager under test always uses the
in-memory store (never a shared Postgres) and the worker never targets a live
manager.

Full walkthrough: docs/topics/smoke-test.md.
"""

from __future__ import annotations

import argparse
import itertools
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from nightshift import backends
from nightshift.backends import WorkerResult, WorkerSpec
from nightshift.worker.client import ManagerClient
from nightshift.worker.config import load_worker_config
from nightshift.worker.local_store import LocalStore
from nightshift.worker.loop import WorkerLoop


REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE_MODEL = "smoke/deterministic"
TARGET_REPO = "nightshift"
ARTIFACT = "smoke-artifact.txt"
# How long the good task's backend sleeps — long enough to pause/stop it mid-run.
SLOW_SLEEP = 4.0
WAIT_TIMEOUT = 30.0


class SmokeFailure(AssertionError):
    """A smoke assertion failed."""


# --------------------------------------------------------------------------- #
# Worker role — the deterministic backend + the stock WorkerLoop
# --------------------------------------------------------------------------- #


class SmokeBackend:
    """Deterministic stand-in for an agent CLI.

    Reads the run-scratch brief (the sibling ``<worktree>.taskfile.md`` that
    ``materialize_brief`` writes) and obeys a single directive line:

        SMOKE: [sleep=N] commit|fail

    ``commit`` writes an artifact file and commits it on the task branch (the
    landable path); ``fail`` exits non-zero (the worker-error path).
    """

    name = "smoke"
    agentic = True
    description = "Deterministic smoke-test backend (SMOKE: directives)."

    def available(self, config: dict[str, Any] | None = None) -> bool:
        return True

    def run(
        self,
        spec: WorkerSpec,
        emit_log: Callable[[str], None],
        should_abort: Callable[[], str | None],
        on_worker_start: Callable[[int], None] | None = None,
    ) -> WorkerResult:
        sleep_s, action = self._directive(Path(f"{spec.cwd}.taskfile.md"))
        if action is None:
            return WorkerResult(returncode=1, error="no SMOKE: directive in brief")
        if sleep_s > 0:
            emit_log(f"smoke backend: sleeping {sleep_s}s\n")
            time.sleep(sleep_s)
        if action == "fail":
            emit_log("smoke backend: failing as directed\n")
            return WorkerResult(returncode=1, error="smoke: intentional failure")
        emit_log("smoke backend: committing artifact\n")
        (spec.cwd / ARTIFACT).write_text(f"smoke artifact for {spec.task}\n")
        for argv in (
            ["git", "add", "-A"],
            ["git", "commit", "-m", f"smoke: {spec.task}"],
        ):
            res = subprocess.run(
                argv, cwd=spec.cwd, env=spec.env, capture_output=True, text=True
            )
            if res.returncode != 0:
                return WorkerResult(
                    returncode=1,
                    error=f"{' '.join(argv)} failed: {res.stderr.strip()[:300]}",
                )
        return WorkerResult(returncode=0, turns=1)

    @staticmethod
    def _directive(brief: Path) -> tuple[float, str | None]:
        if not brief.is_file():
            return 0.0, None
        sleep_s, action = 0.0, None
        for line in brief.read_text().splitlines():
            if not line.strip().startswith("SMOKE:"):
                continue
            for token in line.split(":", 1)[1].split():
                if token.startswith("sleep="):
                    sleep_s = float(token.removeprefix("sleep="))
                elif token in ("commit", "fail"):
                    action = token
        return sleep_s, action


def worker_main(workspace: Path) -> int:
    """Run the stock worker loop with the smoke backend registered.

    ``_BACKENDS`` is the deliberate seam: the registry is rebuilt from it on
    every lookup, so appending here makes the ``smoke`` provider resolvable to
    ``require_backend``/``available()`` without touching production code.
    """
    backends._BACKENDS = (*backends._BACKENDS, SmokeBackend())
    cfg = load_worker_config(workspace)
    client = ManagerClient(cfg.manager_url, shared_secret=cfg.shared_secret)
    loop = WorkerLoop(cfg, client, LocalStore(cfg.workspace))
    print(f"[smoke-worker] id={cfg.worker_id} manager={cfg.manager_url}", flush=True)
    loop.run_forever()
    return 0


# --------------------------------------------------------------------------- #
# Orchestrator role — workspace build, subprocesses, API-driven lifecycle
# --------------------------------------------------------------------------- #


def scrub_env() -> None:
    """Drop every NIGHTSHIFT_* var so the run can't inherit a live deployment.

    ``just`` loads the repo ``.env`` (dotenv-load), which may carry
    ``NIGHTSHIFT_PG_DSN`` / ``NIGHTSHIFT_MANAGER_URL`` / ``NIGHTSHIFT_WORKSPACE``
    for the operator's real setup — any of which would point this smoke run at
    live state instead of the isolated temp workspace.
    """
    import os

    for key in [k for k in os.environ if k.startswith("NIGHTSHIFT_")]:
        del os.environ[key]
    os.environ["PYTHONUNBUFFERED"] = "1"


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return res.stdout.strip()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_workspace(workspace: Path, port: int) -> None:
    """Materialise the isolated workspace: target clone, content store, config."""
    workspace.mkdir(parents=True)

    # Target repo: a local clone of this repo, detached from its origin so no
    # smoke operation can ever reach back into the source checkout.
    clone = workspace / TARGET_REPO
    subprocess.run(
        ["git", "clone", "--quiet", str(REPO_ROOT), str(clone)],
        check=True, capture_output=True, text=True,
    )
    _git(clone, "remote", "remove", "origin")
    # The clone's default branch mirrors the source checkout's HEAD, which may
    # be a feature branch. Landing targets ``refs/heads/main`` (Phase 6: refs
    # are authoritative), so pin the branch name regardless of where smoke ran.
    _git(clone, "branch", "-M", "main")
    _git(clone, "config", "user.name", "nightshift-smoke")
    _git(clone, "config", "user.email", "smoke@nightshift.local")

    # Content store: main queue bound to the clone, with a cheap validate
    # command (proves the artifact landed on the branch) and preflight opted
    # out ("" disables; absent would inherit `uv sync --frozen`).
    tasks = workspace / "nightshift-tasks"
    (tasks / "main").mkdir(parents=True)
    (tasks / "main" / "config.json").write_text(json.dumps({
        "repo": TARGET_REPO,
        "validate": f"test -f {ARTIFACT}",
        "preflight": "",
        "order": [],
    }, indent=2) + "\n")
    # Workspace-level content-store config: queues materialised later by the
    # playlist rescan carry only repo+order in their own config, so they
    # inherit this layer's cheap validate/preflight instead of the engine
    # defaults (`just validate` / `uv sync --frozen`).
    (tasks / "config.json").write_text(json.dumps({
        "validate": f"test -f {ARTIFACT}",
        "preflight": "",
    }, indent=2) + "\n")
    (tasks / ".gitignore").write_text("*/runs/\n*/logs/\n")
    _git(tasks, "init", "--quiet")
    _git(tasks, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(tasks, "config", "user.name", "nightshift-smoke")
    _git(tasks, "config", "user.email", "smoke@nightshift.local")
    _git(tasks, "add", "-A")
    _git(tasks, "commit", "--quiet", "-m", "init nightshift-tasks")

    ns = workspace / ".nightshift"
    ns.mkdir()
    (ns / "manager.json").write_text(json.dumps({
        "host": "127.0.0.1",
        "port": port,
        "landing_mode": "none",
        "rendezvous_remote": None,
        "default_model": SMOKE_MODEL,
        # Production backoff is 60s * 2**(n-1); the failure round-trip here
        # must retry within the smoke timeout, so dial the base way down.
        "retry_backoff_seconds": 0.5,
        "cadences": {
            "poll_seconds": 0.5,
            "heartbeat_seconds": 2.0,
            "lease_ttl_seconds": 60.0,
        },
    }, indent=2) + "\n")
    (ns / "worker.json").write_text(json.dumps({
        "worker_id": "smoke-worker",
        "manager_url": f"http://127.0.0.1:{port}",
        "models": [SMOKE_MODEL],
        "auto_model": SMOKE_MODEL,
        "max_model": SMOKE_MODEL,
    }, indent=2) + "\n")
    (workspace / "logs").mkdir()


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def wait_for(
    describe: str,
    probe: Callable[[], bool],
    *,
    timeout: float = WAIT_TIMEOUT,
    interval: float = 0.2,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe():
            return
        time.sleep(interval)
    raise SmokeFailure(f"timed out after {timeout}s waiting for {describe}")


class Api:
    """Thin operator-API client for the smoke manager."""

    def __init__(self, port: int) -> None:
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0)

    def get(self, path: str, **params: Any) -> Any:
        resp = self.http.get(path, params=params or None)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        resp = self.http.post(path, json=body)
        resp.raise_for_status()
        return resp.json()

    def patch(self, path: str, body: dict[str, Any]) -> Any:
        resp = self.http.patch(path, json=body)
        resp.raise_for_status()
        return resp.json()

    def put(self, path: str, body: dict[str, Any]) -> Any:
        resp = self.http.put(path, json=body)
        resp.raise_for_status()
        return resp.json()

    def transport(self, action: str) -> Any:
        """Drive the focused queue's transport (mirrors the UI controls)."""
        return self.post("/api/transport", {"action": action})

    def focus(self, playlist: str | None) -> None:
        """Select a queue as the UI focus (``None`` = main); subsequent
        queue-less task/queue/transport calls target it, like the UI."""
        self.post("/api/active", {"playlist": playlist})

    def state(self) -> dict[str, Any]:
        return self.get("/api/state")

    def leases(self) -> list[dict[str, Any]]:
        return self.get("/api/leases")

    def leases_for(self, label: str) -> list[dict[str, Any]]:
        """Active leases on one queue (rows store main as '' — normalize)."""
        return [le for le in self.leases() if (le.get("queue") or "main") == label]

    def runs_for(self, task: str) -> list[dict[str, Any]]:
        return [r for r in self.get("/api/runs") if r.get("task") == task]

    def landed_run(self, task: str) -> dict[str, Any] | None:
        """The task's completed-and-landed run, if any."""
        return next(
            (r for r in self.runs_for(task)
             if r["status"] == "completed" and r.get("commit_sha")),
            None,
        )


def _spawn(argv: list[str], log_path: Path, cwd: Path) -> subprocess.Popen:
    log = log_path.open("w")
    return subprocess.Popen(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)


_STEPS = itertools.count(1)


def _step(text: str) -> None:
    print(f"[smoke] {next(_STEPS):>2}. {text}", flush=True)


# ----- scenario phases ------------------------------------------------------ #


def phase_rescan_and_hide(api: Api) -> None:
    """Playlists page: rescan materialises a queue per workspace repo; hiding
    one parks it, and a second rescan must not resurrect it."""
    _step("rescan: a playlist appears for each workspace repo")
    result = api.post("/api/playlists/rescan")
    check(TARGET_REPO in result["created"],
          f"rescan did not create '{TARGET_REPO}': {result}")
    names = {pl["name"] for pl in result["playlists"]}
    check(names == {TARGET_REPO},
          f"expected only '{TARGET_REPO}' (content store skipped), got {names}")
    info = api.get(f"/api/playlists/{TARGET_REPO}")
    check(info["repository"] == TARGET_REPO and not info["disabled"],
          f"bad playlist binding after rescan: {info}")

    _step("hide the repo's playlist; rescan again must not resurrect it")
    api.put(f"/api/playlists/{TARGET_REPO}", {"disabled": True})
    listed = {pl["name"]: pl for pl in api.get("/api/playlists")}
    check(listed[TARGET_REPO]["disabled"] is True, f"hide did not stick: {listed}")
    # Hidden = parked: dropped from the scheduler's queue set and /api/repos.
    queues = {q["queue"] for q in api.get("/api/repos")["queues"]}
    check(TARGET_REPO not in queues, f"hidden playlist still scheduled: {queues}")
    result = api.post("/api/playlists/rescan")
    check(TARGET_REPO in result["configured"] and TARGET_REPO not in result["created"],
          f"rescan should reconfigure, not recreate: {result}")
    listed = {pl["name"]: pl for pl in api.get("/api/playlists")}
    check(listed[TARGET_REPO]["disabled"] is True,
          f"rescan resurrected the hidden playlist: {listed}")
    # Unhide for the playlist phases below.
    api.put(f"/api/playlists/{TARGET_REPO}", {"disabled": False})
    queues = {q["queue"]: q for q in api.get("/api/repos")["queues"]}
    check(TARGET_REPO in queues and queues[TARGET_REPO]["available"],
          f"unhidden playlist missing from /api/repos: {queues}")


def phase_main_lifecycle(api: Api, clone: Path) -> str:
    """Main queue: create a good and a bad task, drive play/pause/stop/start,
    land the good one, quarantine the bad one. Returns the bad task's id
    (released and landed in the final phase)."""
    _step("create good + bad tasks via the operator API")
    good = api.post("/api/tasks", {
        "title": "Smoke slow good",
        "text": f"Deterministic smoke task.\n\nSMOKE: sleep={SLOW_SLEEP:g} commit\n",
    })["task"]
    bad = api.post("/api/tasks", {
        "title": "Smoke always fails",
        "text": "Deterministic smoke task.\n\nSMOKE: fail\n",
    })["task"]
    for task in (good, bad):
        api.patch(f"/api/tasks/{task}", {"model": SMOKE_MODEL})
    queue = [row["task"] for row in api.get("/api/queue")]
    check(queue == [good, bad], f"unexpected queue order: {queue}")

    _step("pause the queue, then start the worker: nothing may dispatch")
    state = api.transport("pause")
    check(state["state"] == "paused" and state["pause_reason"] == "operator",
          f"expected operator pause, got {state}")
    wait_for(
        "worker checkin",
        lambda: any(w.get("id") == "smoke-worker" for w in api.get("/api/workers")),
    )
    time.sleep(2.0)  # several poll cycles at poll_seconds=0.5
    check(api.leases() == [], "a lease was granted while the queue was paused")
    check(api.state()["state"] == "paused", "queue lost its pause")

    _step(f"play: '{good}' starts running")
    api.transport("play")
    wait_for(
        f"'{good}' to start",
        lambda: api.state()["state"] == "playing"
        and api.state()["now_playing"] == good,
    )
    first_run_id = api.state()["run_id"]
    check(bool(first_run_id), "playing state carries no run_id")

    _step("pause mid-run: state pauses, the in-flight lease survives")
    state = api.transport("pause")
    check(state["state"] == "paused" and state["pause_reason"] == "operator",
          f"expected operator pause, got {state}")
    check(len(api.leases()) == 1, "pause should not cancel the in-flight lease")

    _step("stop: lease cancelled; the worker's late submit must be fenced")
    api.transport("stop")
    wait_for("lease cancellation", lambda: api.leases() == [], timeout=5.0)
    api.transport("pause")  # hold the queue while the doomed run drains
    time.sleep(SLOW_SLEEP + 1.5)  # backend finishes sleeping, submits, gets 409

    _step(f"start again: '{good}' re-runs, validates, and lands")
    api.transport("play")
    wait_for(f"'{good}' to land", lambda: api.landed_run(good) is not None)
    landed = api.landed_run(good)
    check(landed["id"] != first_run_id,
          "the stopped run landed — the stale-submit fence did not hold")
    first_run = next(r for r in api.runs_for(good) if r["id"] == first_run_id)
    check(first_run["status"] != "completed",
          f"stopped run should not complete, got {first_run['status']}")
    check((clone / ARTIFACT).is_file(), "landed artifact missing from clone main")
    subject = _git(clone, "log", "-1", "--format=%s")
    check(subject == "task: Smoke slow good",
          f"unexpected squash commit on clone main: {subject!r}")
    resp = api.http.get(f"/api/tasks/{good}")
    check(resp.status_code == 404, "landed brief should be dropped from the queue")

    _step(f"error path: '{bad}' fails, retries, and ends quarantined")
    wait_for(
        f"'{bad}' to be quarantined after retry",
        lambda: api.get(f"/api/tasks/{bad}")["quarantined"],
    )
    state = api.state()
    check(state["state"] == "paused" and state["pause_reason"] == "retry_failed",
          f"expected retry_failed pause, got {state}")
    errors = [r for r in api.runs_for(bad) if r["status"] == "error"]
    check(len(errors) >= 2, f"expected >=2 error runs for '{bad}', got {len(errors)}")
    check(all(r.get("failure_kind") == "worker_error" for r in errors),
          f"unexpected failure kinds: {[r.get('failure_kind') for r in errors]}")
    blocked = api.get("/api/blocked")
    check(any(b.get("task") == bad and b.get("state") == "quarantined" for b in blocked),
          f"'{bad}' missing from /api/blocked: {blocked}")
    return bad


def phase_playlist_create_and_run(api: Api, clone: Path) -> None:
    """Select the rescanned queue, add a simple task, press run, and watch it
    complete. The task carries no explicit model — it must inherit the
    manager's ``default_model`` through the config layers."""
    _step(f"select the '{TARGET_REPO}' queue; create a simple task; run it")
    api.focus(TARGET_REPO)
    api.transport("pause")  # hold dispatch between 'create' and 'run'
    simple = api.post("/api/tasks", {
        "title": "Smoke playlist simple",
        "text": "Simple task on the rescanned queue.\n\nSMOKE: commit\n",
    })["task"]
    listed = [row["task"] for row in api.get("/api/queue")]
    check(listed == [simple], f"unexpected playlist queue: {listed}")
    api.transport("play")
    wait_for(f"'{simple}' to land", lambda: api.landed_run(simple) is not None)
    subject = _git(clone, "log", "-1", "--format=%s")
    check(subject == "task: Smoke playlist simple",
          f"unexpected squash commit on clone main: {subject!r}")
    resp = api.http.get(f"/api/tasks/{simple}")
    check(resp.status_code == 404, "landed brief should be dropped from the queue")


def phase_edit_task(api: Api) -> str:
    """Edit/save: patch a task's title and body from the detail pane and read
    both back. Returns the edited task's id (its slug must not change)."""
    _step("edit/save: patch title + body; the file reflects both, id is stable")
    api.transport("pause")
    task = api.post("/api/tasks", {
        "title": "Smoke before edit",
        "text": "Placeholder spec.\n\nSMOKE: fail\n",
    })["task"]
    api.patch(f"/api/tasks/{task}", {
        "title": "Smoke lifecycle",
        "body": f"Edited spec.\n\nSMOKE: sleep={SLOW_SLEEP:g} commit",
    })
    brief = api.get(f"/api/tasks/{task}")  # same id — edits never rename
    check(brief["title"] == "Smoke lifecycle",
          f"title edit not persisted: {brief['title']!r}")
    check("SMOKE: sleep=" in brief["body"] and "SMOKE: fail" not in brief["body"],
          f"body edit not persisted: {brief['body']!r}")
    return task


def phase_playlist_run_pause_stop(api: Api, task: str) -> None:
    """Run/pause/stop the edited task on the playlist queue (the same
    transport contract the main queue proved, now against a named queue)."""
    _step(f"playlist transport: run/pause/stop '{task}'")
    api.transport("play")
    wait_for(
        f"'{task}' to start",
        lambda: api.state()["state"] == "playing"
        and api.state()["now_playing"] == task,
    )
    state = api.transport("pause")
    check(state["state"] == "paused", f"expected paused, got {state}")
    check(len(api.leases_for(TARGET_REPO)) == 1,
          "pause should not cancel the in-flight playlist lease")
    api.transport("stop")
    wait_for("playlist lease cancellation",
             lambda: api.leases_for(TARGET_REPO) == [], timeout=5.0)
    api.transport("pause")  # hold the queue while the doomed run drains
    time.sleep(SLOW_SLEEP + 1.5)
    check(api.landed_run(task) is None,
          "the stopped playlist run landed — the fence did not hold")
    check(api.http.get(f"/api/tasks/{task}").status_code == 200,
          "stopped task's brief should stay in the queue")


def phase_disable_enable(api: Api, clone: Path, task: str) -> None:
    """Disable/enable the task while the queue is paused and mid-run: a
    disabled task never dispatches (even on a playing queue); disabling an
    in-flight run never kills it — it drains and lands."""
    _step("disable while paused: play dispatches nothing until re-enabled")
    api.patch(f"/api/tasks/{task}", {"disabled": True})
    row = next(r for r in api.get("/api/queue") if r["task"] == task)
    check(row["disabled"], f"disabled flag not reflected in queue row: {row}")
    api.transport("play")
    time.sleep(2.0)  # several poll cycles at poll_seconds=0.5
    check(api.leases_for(TARGET_REPO) == [],
          "a disabled task was dispatched on a playing queue")

    _step("enable on the live queue: the task starts; disable mid-run drains")
    api.patch(f"/api/tasks/{task}", {"disabled": False})
    wait_for(
        f"'{task}' to start after enable",
        lambda: api.state()["now_playing"] == task,
    )
    api.patch(f"/api/tasks/{task}", {"disabled": True})  # mid-run toggle
    check(len(api.leases_for(TARGET_REPO)) == 1,
          "disabling mid-run must not cancel the in-flight lease")
    wait_for(f"'{task}' to land", lambda: api.landed_run(task) is not None)
    subject = _git(clone, "log", "-1", "--format=%s")
    check(subject == "task: Smoke lifecycle",
          f"unexpected squash commit on clone main: {subject!r}")
    check(api.http.get(f"/api/tasks/{task}").status_code == 404,
          "landed brief should be dropped even when disabled mid-run")


def phase_clear_quarantine(api: Api, clone: Path, bad: str) -> None:
    """Release the quarantined task the way the detail pane does, fix its
    brief via an edit, press play, and watch the once-bad task land."""
    _step(f"clear quarantine: '{bad}' is released, fixed via edit, and lands")
    api.focus(None)  # back to the main queue
    api.patch(f"/api/tasks/{bad}", {"quarantined": False, "failed": False})
    brief = api.get(f"/api/tasks/{bad}")
    check(not brief["quarantined"] and not brief["failed"],
          f"quarantine release not persisted: {brief}")
    blocked = api.get("/api/blocked")
    check(not any(b.get("task") == bad for b in blocked),
          f"released task still in /api/blocked: {blocked}")
    # The queue stays paused (retry_failed) until the operator presses play —
    # which leaves room to fix the brief before it re-dispatches.
    check(api.state()["state"] == "paused", "release should not unpause the queue")
    api.patch(f"/api/tasks/{bad}", {"body": "Fixed spec.\n\nSMOKE: commit"})
    api.transport("play")
    wait_for(f"'{bad}' to land after release", lambda: api.landed_run(bad) is not None)
    subject = _git(clone, "log", "-1", "--format=%s")
    check(subject == "task: Smoke always fails",
          f"unexpected squash commit on clone main: {subject!r}")
    check(api.get("/api/queue") == [], "main queue should end empty")


def run_scenario(api: Api, workspace: Path) -> None:
    clone = workspace / TARGET_REPO

    _step("manager is up; workspace repos visible")
    repos = api.get("/api/repos")
    check(
        any(r["name"] == TARGET_REPO and r["available"] for r in repos["repos"]),
        f"clone missing from /api/repos: {repos}",
    )

    phase_rescan_and_hide(api)
    bad = phase_main_lifecycle(api, clone)
    phase_playlist_create_and_run(api, clone)
    task = phase_edit_task(api)
    phase_playlist_run_pause_stop(api, task)
    phase_disable_enable(api, clone, task)
    phase_clear_quarantine(api, clone, bad)


def orchestrate(keep: bool) -> int:
    started = time.monotonic()
    root = Path(tempfile.mkdtemp(prefix="nightshift-smoke-"))
    workspace = root / "workspace"
    port = _free_port()
    manager: subprocess.Popen | None = None
    worker: subprocess.Popen | None = None
    failed = False
    print(f"[smoke] workspace: {workspace}", flush=True)
    try:
        build_workspace(workspace, port)
        logs = workspace / "logs"

        _step(f"launch manager (:{port}) and wait for readiness")
        manager = _spawn(
            [sys.executable, "-m", "nightshift.manager",
             "--workspace", str(workspace), "--host", "127.0.0.1", "--port", str(port)],
            logs / "manager.log", workspace,
        )
        api = Api(port)

        def _manager_ready() -> bool:
            check(manager.poll() is None, "manager subprocess exited early")
            try:
                return api.http.get("/api/info").status_code == 200
            except httpx.HTTPError:
                return False

        wait_for("manager readiness", _manager_ready)

        worker = _spawn(
            [sys.executable, str(Path(__file__).resolve()),
             "--role", "worker", "--workspace", str(workspace)],
            logs / "worker.log", workspace,
        )
        run_scenario(api, workspace)
        print(f"[smoke] PASS ({time.monotonic() - started:.1f}s)", flush=True)
        return 0
    except (SmokeFailure, httpx.HTTPError, subprocess.CalledProcessError) as exc:
        failed = True
        print(f"[smoke] FAIL: {exc}", file=sys.stderr, flush=True)
        _dump_logs(workspace / "logs")
        return 1
    finally:
        for proc in (worker, manager):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if keep or failed:
            print(f"[smoke] workspace kept at {workspace}", flush=True)
        else:
            shutil.rmtree(root, ignore_errors=True)


def _dump_logs(logs: Path, tail: int = 40) -> None:
    for name in ("manager.log", "worker.log"):
        path = logs / name
        if not path.is_file():
            continue
        lines = path.read_text(errors="replace").splitlines()[-tail:]
        print(f"\n[smoke] ── tail of {name} ──", file=sys.stderr)
        for line in lines:
            print(f"  {line}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("orchestrator", "worker"),
                        default="orchestrator")
    parser.add_argument("--workspace", type=Path, default=None,
                        help="(worker role) the smoke workspace to serve")
    parser.add_argument("--keep", action="store_true",
                        help="keep the temp workspace after a passing run")
    args = parser.parse_args(argv)
    scrub_env()
    if args.role == "worker":
        if args.workspace is None:
            parser.error("--role worker requires --workspace")
        return worker_main(args.workspace.resolve())
    return orchestrate(keep=args.keep)


if __name__ == "__main__":
    raise SystemExit(main())
