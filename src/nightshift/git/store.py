"""Content-store commits — the ``nightshift-tasks`` git lifecycle.

Moved verbatim from ``engine.py`` in Phase 3 of the rebuild-in-place migration
(``_commit_dispatch`` promoted to :func:`commit_dispatch`).
"""

from __future__ import annotations

from pathlib import Path

from nightshift.git import GitRunner


def commit_tasks(
    tasks_root: Path, message: str, *, pathspecs: tuple[str, ...] = (".",)
) -> str | None:
    """Stage ``pathspecs`` in the content store (``tasks_root``) and commit them.

    The generic content-store commit helper for the ``nightshift-tasks`` git
    lifecycle (briefs created/removed, queue config edited). Local commit only —
    no remote required, no push. Returns the new commit's short sha, or ``None``
    when ``tasks_root`` is not a git repo *or* nothing was staged (a no-op).

    ``git add`` exits 1 when a pathspec matches only ``.gitignore``-ignored files
    (a queue's runtime ``runs/``/``logs/`` are gitignored in the store); that case
    is tolerated — the wanted files are still staged — while any other failure is
    treated as "nothing to commit" so a lifecycle event can never crash a run.
    """
    if not (tasks_root / ".git").exists():
        return None
    git = GitRunner(tasks_root)
    add = git.run("-c", "advice.addIgnoredFile=false", "add", "--", *pathspecs)
    if not add.ok and (
        add.returncode != 1
        or "ignored by one of your .gitignore" not in add.stderr
    ):
        return None
    staged = git.run("diff", "--cached", "--name-only", "--", *pathspecs)
    if not staged.stdout.strip():
        return None
    commit = git.run("commit", "-m", message, "--", *pathspecs)
    if not commit.ok:
        return None
    return git.run("rev-parse", "--short", "HEAD").stdout.strip() or None


def commit_dispatch(tasks_root: Path, tasks_rel: str = "main") -> None:
    """Commit autosplit dispatch (spawned files + evergreen reset) in the content
    store, scoped to the dispatched queue dir."""
    commit_tasks(
        tasks_root,
        "nightshift-local: dispatch daily queues",
        pathspecs=(tasks_rel,),
    )


def commit_queue_state(tasks_root: Path, tasks_rel: str = "main") -> str | None:
    """Commit a queue's brief/config churn in the content store (``tasks_root``).

    The pre-run *target-repo* snapshot is gone: briefs are read live from
    ``tasks_root`` and delivered to the worker via a run-scratch file, so a run
    never needs to snapshot anything into the repo it lands in. This helper is
    retained for the create/edit lifecycle — it commits the queue dir
    (``<tasks_root>/<tasks_rel>``) churn locally, returning the new short sha or
    ``None`` when the store was already clean / not a git repo.
    """
    return commit_tasks(
        tasks_root,
        "nightshift: commit queue state",
        pathspecs=(tasks_rel,),
    )
