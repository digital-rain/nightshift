"""The worker poll loop: startup -> checkin -> poll -> do -> validate -> submit.

A worker is queue-agnostic by default: it polls and the manager hands back the
next leased work order (or nothing). The worker executes it with its own
backend, streams events + heartbeats to the manager (keeping the lease alive
across long runs), then submits exactly once. Landing is the manager's job.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from nightshift import playlists
from nightshift.git.worktrees import teardown_worktree, worktree_branch, worktree_dir
from nightshift.lifecycle import Outcome, RunStatus
from nightshift.model_id import provider_of
from nightshift.worker.client import ManagerClient
from nightshift.worker.config import WorkerConfig
from nightshift.worker.execute import execute_work_order
from nightshift.worker.local_store import LocalStore


_log = logging.getLogger("nightshift.worker.loop")

_LOG_FLUSH_LINES = 20

# The poll loop runs on a daemon thread while uvicorn serves the worker UI.
# An escaping exception used to kill that thread outright: the process stayed
# up, the UI kept answering, and the worker looked healthy while never polling
# again -- an invisible permanent stall that only a `ps` thread count revealed.
# Errors are retried with a capped backoff instead, and surfaced on the UI.
_LOOP_BACKOFF_START = 5.0
_LOOP_BACKOFF_CAP = 120.0

# Outcome fields that stay off the worker's local JSONL history rows: they are
# transport/diagnostic detail the local Now/History UI never shows. Keeps the
# on-disk record keys identical to the pre-Outcome finish dict.
_LOCAL_HISTORY_EXCLUDE = frozenset({
    "landable", "branch_ref", "head_sha", "failure_reason", "validate_cmd",
    # Workflow doc-step fields: the document can be large and neither is shown
    # in the local Now/History UI — keep them off the on-disk record.
    "document", "signal",
})


class WorkerLoop:
    def __init__(
        self,
        cfg: WorkerConfig,
        client: ManagerClient,
        local: LocalStore,
        *,
        poll_seconds: float = 5.0,
        heartbeat_seconds: float = 10.0,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.local = local
        self.poll_seconds = poll_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._stop = threading.Event()
        self._queue_failures: dict[str, int] = {}
        self._backoff_queues: set[str] = set()
        # Poll-loop health, surfaced on the worker UI (see `loop_health`).
        self._checked_in = False
        self.consecutive_errors = 0
        self.last_error: str | None = None
        self.last_error_at: float | None = None

    def checkin(self) -> None:
        checkin_meta: dict[str, Any] = {"pid": _safe_pid()}
        if self.cfg.worker_url:
            checkin_meta["worker_url"] = self.cfg.worker_url
        resp = self.client.checkin(
            self.cfg.worker_id,
            backend=",".join(sorted(self.cfg.providers())) or None,
            queues=self.cfg.queues,
            priorities=self.cfg.priorities,
            models=self.cfg.advertised_models(),
            mcps=self.cfg.mcps,
            meta=checkin_meta,
        )
        cad = resp.get("cadences", {})
        self.poll_seconds = float(cad.get("poll_seconds", self.poll_seconds))
        self.heartbeat_seconds = float(
            cad.get("heartbeat_seconds", self.heartbeat_seconds)
        )
        if cad.get("refresh_ms"):
            self.cfg.refresh_ms = int(cad["refresh_ms"])

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        """Poll until stopped, surviving errors rather than dying on them.

        Nothing supervises this thread, so an escaping exception is not a crash
        the operator can see -- it is a worker that answers its UI, reports its
        config, and silently never takes work again. Every iteration (the
        opening check-in included) is therefore retried with a capped backoff,
        and the last error is kept for the UI to show.
        """
        backoff = _LOOP_BACKOFF_START
        while not self._stop.is_set():
            try:
                if not self._checked_in:
                    self.checkin()
                    self._checked_in = True
                did_work = self.run_once()
            except Exception as exc:  # noqa: BLE001 -- the whole point is to not die
                # A failed check-in must be retried as a check-in, not skipped.
                self._checked_in = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.last_error_at = time.time()
                self.consecutive_errors += 1
                _log.exception(
                    "worker loop error (%d in a row); retrying in %.0fs",
                    self.consecutive_errors, backoff,
                )
                self._stop.wait(backoff)
                backoff = min(backoff * 2, _LOOP_BACKOFF_CAP)
                continue
            self.consecutive_errors = 0
            backoff = _LOOP_BACKOFF_START
            if not did_work:
                # Idle: heartbeat + wait one poll interval.
                self.client.heartbeat(self.cfg.worker_id)
                self._stop.wait(self.poll_seconds)

    def loop_health(self) -> dict[str, Any]:
        """What the UI shows about the poll loop: whether it is retrying, and
        why. A worker whose loop is wedged must never look healthy."""
        return {
            "checked_in": self._checked_in,
            "consecutive_errors": self.consecutive_errors,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
        }

    def run_once(self) -> bool:
        """Poll once; execute + submit if work was handed out. Returns True if a
        task was processed (so the caller can poll again immediately)."""
        resp = self.client.poll(
            self.cfg.worker_id,
            backend=",".join(sorted(self.cfg.providers())) or None,
            queues=self.cfg.queues,
            priorities=self.cfg.priorities,
            models=self.cfg.advertised_models(),
            mcps=self.cfg.mcps,
            exclude_queues=sorted(self._backoff_queues) or None,
        )
        self._sync_backoff_with_manager(resp.get("queue_pauses") or {})
        work = resp.get("work")
        if not work:
            return False
        # Phase 7 chaining (spec §7.4): a doc-step submit may hand back the
        # next step as ``next_order`` in its response. Process the chain inline
        # — same lease semantics, no re-poll — until the manager stops chaining.
        while work is not None:
            result = self._process(work)
            work = result.get("next_order")
        return True

    def _note_submit_outcome(self, queue_label: str, status: str) -> None:
        if status == RunStatus.COMPLETED:
            self._queue_failures[queue_label] = 0
            self._backoff_queues.discard(queue_label)
            return
        if status in (RunStatus.ERROR, RunStatus.BLOCKED):
            count = self._queue_failures.get(queue_label, 0) + 1
            self._queue_failures[queue_label] = count
            if count >= 2:
                self._backoff_queues.add(queue_label)

    def _sync_backoff_with_manager(self, queue_pauses: dict[str, str]) -> None:
        """Drop a local backoff once the manager reports that queue is no
        longer paused (operator pressed Play)."""
        for label in list(self._backoff_queues):
            if label not in queue_pauses:
                self._backoff_queues.discard(label)
                self._queue_failures[label] = 0

    # ------------------------------------------------------------------ #

    def _process(self, order: dict[str, Any]) -> dict[str, Any]:
        run_id = order["run_id"]
        lease_id = order["lease_id"]
        task = order["task"]
        queue = order.get("queue") or "main"
        title = order.get("title", task)
        repo = order.get("repo") or ""
        branch = worktree_branch(task, queue)
        queue_internal = playlists.queue_from_tasks_rel(queue)
        wt = str(worktree_dir(self.cfg.workspace, repo, task, queue_internal)) if repo else None

        # Session resume (spec §7.5): a chained step whose (task, role) matches
        # a session this worker just ran may reuse it — a worker-local hint
        # injected into the order's config. A hint, never a dependency: the
        # prompt still carries every declared input. Never across tasks.
        wf = order.get("config", {}).get("workflow") or {}
        role = wf.get("role")
        if role:
            prior = self.local.session_for(task, role)
            if prior:
                order["config"]["resume_session_id"] = prior

        order_model = str(order.get("config", {}).get("model", "auto"))
        self.local.begin(
            run_id=run_id,
            task=task,
            queue=queue,
            title=title,
            model=order_model,
            backend=provider_of(order_model) or "",
            repo=repo,
            branch=branch,
            worktree=wt,
        )

        buffer: list[dict[str, Any]] = []

        def flush() -> None:
            if buffer:
                self.client.post_events(run_id, list(buffer))
                buffer.clear()

        # Per-phase wall-clock (Outcome.timings): every phase change funnels
        # through on_phase, so timing the transitions here covers every
        # execute path (code, doc, split) without touching their returns.
        phase_seconds: dict[str, float] = {}
        current_phase: str | None = None
        phase_started = 0.0

        def close_phase(now: float) -> None:
            nonlocal current_phase
            if current_phase is not None:
                phase_seconds[current_phase] = (
                    phase_seconds.get(current_phase, 0.0) + (now - phase_started)
                )
                current_phase = None

        def on_phase(phase: str) -> None:
            nonlocal current_phase, phase_started
            close_phase(time.monotonic())
            current_phase, phase_started = phase, time.monotonic()
            self.local.set_phase(phase)
            buffer.append(
                {
                    "type": "task_status",
                    "task": task,
                    "phase": phase,
                    "status": "running",
                }
            )
            flush()

        def on_log(line: str) -> None:
            self.local.log(line)
            buffer.append({"type": "task_log", "task": task, "line": line})
            if len(buffer) >= _LOG_FLUSH_LINES:
                flush()

        # Keep the lease alive across a long backend run.
        hb_stop = threading.Event()
        hb = threading.Thread(
            target=self._heartbeat_loop, args=(lease_id, hb_stop), daemon=True
        )
        hb.start()

        def on_session(session_id: str) -> None:
            # Remember the session for a (task, role) so a chained same-role
            # step may resume it (spec §7.5). Worker-local, never on the wire.
            if role:
                self.local.remember_session(task, role, session_id)

        try:
            buffer.append({"type": "task_started", "task": task, "title": title})
            execute_started = time.monotonic()
            outcome = execute_work_order(
                self.cfg, order, on_phase=on_phase, on_log=on_log,
                on_session=on_session,
            )
            flush()
        finally:
            hb_stop.set()
            hb.join(timeout=1)

        # Stamp the phase split onto the outcome. A run that never entered a
        # phase (an environment failure before the worker phase) carries no
        # timings — None distinguishes "not measured" from a zero-second run.
        ended = time.monotonic()
        close_phase(ended)
        if phase_seconds:
            timings = {k: round(v, 3) for k, v in phase_seconds.items()}
            timings["total"] = round(ended - execute_started, 3)
            outcome = outcome.model_copy(update={"timings": timings})

        return self._submit(order, outcome)

    def _heartbeat_loop(self, lease_id: str, stop: threading.Event) -> None:
        while not stop.wait(self.heartbeat_seconds):
            self.client.heartbeat(self.cfg.worker_id, lease_id=lease_id)

    def _submit(self, order: dict[str, Any], outcome: Outcome) -> dict[str, Any]:
        run_id = order["run_id"]
        task = order["task"]
        # The wire body is the lease/task envelope plus the unified Outcome,
        # flat — the manager's SubmitBody embeds Outcome the same way.
        payload = {
            "worker_id": self.cfg.worker_id,
            "lease_id": order["lease_id"],
            "task": task,
            "queue": order.get("queue", "main"),
            "repo": order.get("repo"),
            "title": order.get("title", task),
            "quarantine": self.cfg.quarantine,
            **outcome.model_dump(),
        }
        result: dict[str, Any] = {}
        try:
            result = self.client.submit(run_id, payload)
        except Exception as exc:
            result = {"landed": False, "error": str(exc)}
        self._note_submit_outcome(order.get("queue") or "main", outcome.status)
        # Phase 7: a landable submit returns immediately with {"queued": true}
        # (the land runs async on the manager's repo executor). The published
        # WIP ref is the transport — once the manager accepted the submit, the
        # local worktree is consumed either way (a later land failure resolves
        # from the manager's fetched branch, never from this box).
        if outcome.branch_ref and (result.get("landed") or result.get("queued")):
            repo = order.get("repo")
            if repo:
                queue_internal = playlists.queue_from_tasks_rel(
                    order.get("queue") or "main"
                )
                teardown_worktree(self.cfg.workspace, repo, task, queue=queue_internal)
        # The local history row derives from the same Outcome (minus the
        # transport-only fields), plus the manager's land result.
        wf = (order.get("config") or {}).get("workflow") or {}
        wf_tag = {"name": wf["name"], "step": wf["step"]} if wf.get("name") else None
        self.local.finish(
            {
                "run_id": run_id,
                "task": task,
                "queue": order.get("queue", "main"),
                "title": order.get("title", task),
                "repo": order.get("repo", ""),
                **outcome.model_dump(exclude=_LOCAL_HISTORY_EXCLUDE),
                "commit_sha": result.get("sha"),
                "landed": bool(result.get("landed")),
                "quarantined": bool(result.get("quarantined")),
                **({"workflow": wf_tag} if wf_tag else {}),
            }
        )
        # Drop remembered sessions when the task reaches a terminal outcome
        # (spec §7.5 rule 3: within one task, dropped on task end). A landed
        # change, a quarantine, or any non-advancing completion ends the run;
        # an advancing chain keeps them (next_order present).
        if result.get("next_order") is None:
            self.local.drop_sessions(task)
        return result


def _safe_pid() -> int:
    return os.getpid()
