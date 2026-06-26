"""The worker poll loop: startup -> checkin -> poll -> do -> validate -> submit.

A worker is queue-agnostic by default: it polls and the manager hands back the
next leased work order (or nothing). The worker executes it with its own
backend, streams events + heartbeats to the manager (keeping the lease alive
across long runs), then submits exactly once. Landing is the manager's job.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from nightshift import playlists
from nightshift.engine import teardown_worktree, worktree_branch, worktree_dir
from nightshift.model_id import provider_of
from nightshift.worker.client import ManagerClient
from nightshift.worker.config import WorkerConfig
from nightshift.worker.execute import ExecuteOutcome, execute_work_order
from nightshift.worker.local_store import LocalStore


_LOG_FLUSH_LINES = 20


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

    def checkin(self) -> None:
        resp = self.client.checkin(
            self.cfg.worker_id,
            backend=",".join(sorted(self.cfg.providers())) or None,
            queues=self.cfg.queues,
            priorities=self.cfg.priorities,
            models=self.cfg.advertised_models(),
            mcps=self.cfg.mcps,
            meta={"pid": _safe_pid()},
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
        self.checkin()
        while not self._stop.is_set():
            did_work = self.run_once()
            if not did_work:
                # Idle: heartbeat + wait one poll interval.
                self.client.heartbeat(self.cfg.worker_id)
                self._stop.wait(self.poll_seconds)

    def run_once(self) -> bool:
        """Poll once; execute + submit if work was handed out. Returns True if a
        task was processed (so the caller can poll again immediately)."""
        work = self.client.poll(
            self.cfg.worker_id,
            backend=",".join(sorted(self.cfg.providers())) or None,
            queues=self.cfg.queues,
            priorities=self.cfg.priorities,
            models=self.cfg.advertised_models(),
            mcps=self.cfg.mcps,
        )
        if not work:
            return False
        self._process(work)
        return True

    # ------------------------------------------------------------------ #

    def _process(self, order: dict[str, Any]) -> None:
        run_id = order["run_id"]
        lease_id = order["lease_id"]
        task = order["task"]
        queue = order.get("queue") or "main"
        title = order.get("title", task)
        repo = order.get("repo") or ""
        branch = worktree_branch(task, queue)
        queue_internal = playlists.queue_from_tasks_rel(queue)
        wt = str(worktree_dir(self.cfg.workspace, repo, task, queue_internal)) if repo else None

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

        def on_phase(phase: str) -> None:
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

        try:
            buffer.append({"type": "task_started", "task": task, "title": title})
            outcome = execute_work_order(
                self.cfg, order, on_phase=on_phase, on_log=on_log
            )
            flush()
        finally:
            hb_stop.set()
            hb.join(timeout=1)

        self._submit(order, outcome)

    def _heartbeat_loop(self, lease_id: str, stop: threading.Event) -> None:
        while not stop.wait(self.heartbeat_seconds):
            self.client.heartbeat(self.cfg.worker_id, lease_id=lease_id)

    def _submit(self, order: dict[str, Any], outcome: ExecuteOutcome) -> None:
        run_id = order["run_id"]
        task = order["task"]
        payload = {
            "worker_id": self.cfg.worker_id,
            "lease_id": order["lease_id"],
            "task": task,
            "queue": order.get("queue", "main"),
            "repo": order.get("repo"),
            "title": order.get("title", task),
            "status": outcome.status,
            "result_line": outcome.result_line,
            "backend": outcome.backend,
            "model": outcome.resolved_model,
            "landable": outcome.landable,
            "branch_ref": outcome.branch_ref,
            "head_sha": outcome.head_sha,
            "failure_kind": outcome.failure_kind,
            "failure_reason": outcome.failure_reason,
            "turns": outcome.turns,
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
            "cost_usd": outcome.cost_usd,
            "validate_cmd": outcome.validate_cmd,
            "worktree": outcome.worktree,
        }
        result: dict[str, Any] = {}
        try:
            result = self.client.submit(run_id, payload)
        except Exception as exc:
            result = {"landed": False, "error": str(exc)}
        if outcome.branch_ref and result.get("landed"):
            repo = order.get("repo")
            if repo:
                queue_internal = playlists.queue_from_tasks_rel(
                    order.get("queue") or "main"
                )
                teardown_worktree(self.cfg.workspace, repo, task, queue=queue_internal)
        self.local.finish(
            {
                "run_id": run_id,
                "task": task,
                "queue": order.get("queue", "main"),
                "title": order.get("title", task),
                "model": outcome.resolved_model,
                "backend": outcome.backend,
                "status": outcome.status,
                "result_line": outcome.result_line,
                "commit_sha": result.get("sha"),
                "landed": bool(result.get("landed")),
                "turns": outcome.turns,
                "input_tokens": outcome.input_tokens,
                "output_tokens": outcome.output_tokens,
                "cost_usd": outcome.cost_usd,
                "worktree": outcome.worktree,
            }
        )


def _safe_pid() -> int:
    return os.getpid()
