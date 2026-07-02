"""CLI: out-of-process conflict resolver for the Nightshift manager.

The manager spawns this as a *separate OS process* (``python -m
nightshift.manager.resolve_job``) so a long, agent-driven resolve never blocks
its event loop and never gates new dispatch: the player/API surface stays live
and fresh work can start off the last pre-conflict commit while this runs.

What it does, in order:

1. Integrate origin/main into the local clone so the rebase target is current.
2. Run :func:`nightshift.runner_legacy.resolve_task` in the task's *preserved* worktree
   (rebase onto main → agent resolves conflicts → re-validate → squash to local
   main), streaming the run's events back to the manager so the live log and
   ``resolve`` phase show in the UI.
3. Report the resolved squash SHA to the manager's resolve-result endpoint.
   Since Phase 7 the subprocess never pushes origin itself: the manager lands
   the reported SHA as a job on the repo's executor (``produce=cherry(sha)``),
   so push authority is single-threaded per repo and the last cross-process
   integrate-lock consumer is gone. The endpoint updates the run, clears/holds
   the task, and republishes queue state.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from nightshift import playlists as playlists_mod
from nightshift.events import RUN_FINISHED, RUN_STARTED, Event
from nightshift.git.sync import sync_main_to_origin
from nightshift.lifecycle import FailureKind, LandingMode, RunStatus
from nightshift.runner_legacy import resolve_task
from nightshift.spawn_daily import resolve_config
from nightshift.worker.client import ManagerClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--title", default="")
    parser.add_argument("--queue", default=None)
    parser.add_argument("--tasks-repo", default="nightshift-tasks")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--origin-run-id", default=None)
    parser.add_argument("--manager-url", required=True)
    parser.add_argument("--landing-mode", default="none")
    parser.add_argument("--rendezvous-remote", default=None)
    parser.add_argument("--max-push-retries", type=int, default=3)
    args = parser.parse_args(argv)

    workspace = args.workspace.expanduser().resolve()
    repo = args.repo
    task = args.task
    queue = args.queue or None
    title = args.title or task
    run_id = args.run_id
    remote = args.rendezvous_remote or None
    # Parsed once at the process boundary (the argv comes from the manager's
    # already-validated config); an unknown mode fails loudly here.
    landing_mode = LandingMode(args.landing_mode)

    secret = os.environ.get("NIGHTSHIFT_SHARED_SECRET") or None
    client = ManagerClient(args.manager_url, shared_secret=secret)

    def emit(event: Event) -> None:
        client.post_events(run_id, [event.to_dict()])

    emit(Event(RUN_STARTED, {"task": task, "resolve": True}))

    tasks_root = workspace / args.tasks_repo
    tasks_rel = playlists_mod.tasks_rel(queue)
    config = resolve_config(workspace, tasks_root, tasks_rel)

    # Integrate origin/main first so the agent rebases onto the freshest merged
    # state, not a stale local main (best-effort: the land re-syncs anyway).
    if remote and landing_mode.is_remote:
        try:
            sync_main_to_origin(workspace, repo, remote, reset_divergence=False)
        except Exception:  # noqa: BLE001 — a transient fetch failure is non-fatal
            pass

    result = resolve_task(
        workspace,
        repo,
        tasks_root,
        task,
        title,
        emit=emit,
        config=config,
        backend_name=config.get("resolve_backend"),
        queue=queue,
    )

    landed = bool(result.success)
    sha = result.commit_sha
    # ``resolve_task`` squashed onto *local* main. The origin push is the
    # manager's (Phase 7): ``pushed: None`` on a landed push-mode resolve tells
    # worker_resolve_result to land the SHA on the repo executor.
    payload = {
        "task": task,
        "queue": queue,
        "origin_run_id": args.origin_run_id,
        "status": RunStatus.COMPLETED if landed else RunStatus.ERROR,
        "landed": landed,
        "sha": sha if landed else None,
        "result_line": result.result_line
        or ("resolved: landed" if landed else "resolve failed"),
        "failure_kind": None if landed else (result.failure_kind or FailureKind.MERGE_CONFLICT),
        "failure_reason": None if landed else (result.error or None),
        "loc": result.loc if landed else None,
        "remote": None,
        "pushed": None,
    }
    client.resolve_result(run_id, payload)
    emit(Event(RUN_FINISHED, {"task": task}))
    client.close()
    return 0 if landed else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
