"""CLI: out-of-process conflict resolver for the Nightshift manager.

The manager spawns this as a *separate OS process* (``python -m
nightshift.manager.resolve_job``) so a long, agent-driven resolve never blocks
its event loop and never gates new dispatch: the player/API surface stays live
and fresh work can start off the last pre-conflict commit while this runs.

What it does, in order:

1. Integrate origin/main into the local clone so the rebase target is current.
2. Run :func:`nightshift.engine.resolve_task` in the task's *preserved* worktree
   (rebase onto main → agent resolves conflicts → re-validate → squash to local
   main), streaming the run's events back to the manager so the live log and
   ``resolve`` phase show in the UI.
3. Push the resolved commit to origin/main under the integrate lock (bounded
   retry that replays onto a freshly-advanced origin), so the merge is strictly
   serialized against every other land while the agent work above ran unlocked.
4. Report the final outcome to the manager's resolve-result endpoint, which
   updates the run, clears/holds the task, and republishes queue state.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from nightshift import playlists as playlists_mod
from nightshift.engine import resolve_task, sync_main_to_origin
from nightshift.events import RUN_FINISHED, RUN_STARTED, Event
from nightshift.manager.landing import push_resolved_main
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
    landing_mode = args.landing_mode

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
    if remote and landing_mode in ("push", "pr"):
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
    push_detail = ""
    # ``resolve_task`` squashed onto *local* main; for push mode we still have to
    # land that commit on origin/main (serialized, replaying past any origin
    # advance that happened during the agent run).
    if landed and landing_mode == "push" and remote:
        ok, info = push_resolved_main(
            workspace,
            repo,
            remote,
            sha or "",
            max_retries=args.max_push_retries,
            autostash=bool(config.get("autostash_operator_work", True)),
        )
        if ok:
            sha = info
        else:
            landed = False
            push_detail = info

    remote_kind = "push" if (landing_mode == "push" and remote) else None
    payload = {
        "task": task,
        "queue": queue,
        "origin_run_id": args.origin_run_id,
        "status": "completed" if landed else "error",
        "landed": landed,
        "sha": sha if landed else None,
        "result_line": result.result_line
        or ("resolved: landed" if landed else "resolve failed"),
        "failure_kind": None if landed else (result.failure_kind or "merge_conflict"),
        "failure_reason": None if landed else (result.error or push_detail or None),
        "loc": result.loc if landed else None,
        "remote": remote_kind if landed else None,
        "pushed": True if (landed and remote_kind) else None,
    }
    client.resolve_result(run_id, payload)
    emit(Event(RUN_FINISHED, {"task": task}))
    client.close()
    return 0 if landed else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
