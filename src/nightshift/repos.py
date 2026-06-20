"""Workspace repo addressing, resolution & availability.

A Nightshift *workspace* (``--workspace``) parents every git repo Nightshift may
touch. A *repo* is referenced by a **bare child slug** (e.g. ``longitude``) and
resolved against the workspace as ``<workspace>/<repo>``.

Two distinct failure classes (see ``docs/spec/multi-repo-workspace.md`` §2):

* **Malformed / unsafe reference** — anything that is not a bare child slug
  (a path, ``..``, ``/``, or an absolute path). This is an *authoring-time*
  config error (:class:`RepoConfigError`); it is the path-traversal guard and is
  never dispatched. It reuses the :func:`nightshift.playlists.is_valid_name`
  slug guard.
* **Well-formed name, repo absent or missing ``.git``** — *not* an error: the
  task is paused (``repo_unavailable``) until the repo appears (§4). Callers
  test this with :func:`repo_available` after a successful :func:`resolve_repo`.

All references handled here are **workspace-relative** — never absolute paths.
Absolute paths are materialised only transiently at a filesystem/git call via
:func:`repo_root`.
"""

from __future__ import annotations

from pathlib import Path

from nightshift.playlists import is_valid_name


# Default name of the dedicated content-store repo holding briefs + queue config.
DEFAULT_TASKS_REPO = "nightshift-tasks"


class RepoConfigError(ValueError):
    """A repo reference that is malformed/unsafe, or a queue/task with no repo
    set at all. An authoring-time error surfaced where the queue/task is edited;
    it is never dispatched to a worker."""


def is_valid_repo_ref(ref: str | None) -> bool:
    """True when ``ref`` is a bare workspace child slug (``^[a-z0-9][a-z0-9-]*$``).

    Rejects paths, ``..``, ``/``, and absolute paths — this is the
    path-traversal guard for every persisted/transmitted repo reference.
    """
    return bool(ref) and is_valid_name(ref)


def repo_root(workspace: Path, repo: str) -> Path:
    """Materialise the absolute target-repo path. Transient only — never stored."""
    return workspace / repo


def repo_available(workspace: Path, repo: str) -> bool:
    """True iff ``<workspace>/<repo>`` is a direct child of the workspace that
    contains a ``.git`` (a well-formed reference is required). A well-formed but
    absent/``.git``-less repo returns ``False`` → the task is paused, not failed.
    """
    if not is_valid_repo_ref(repo):
        return False
    target = workspace / repo
    return target.is_dir() and (target / ".git").exists()


def known_repos(workspace: Path) -> list[str]:
    """The known-repos set: direct children of ``workspace`` that contain a
    ``.git``, as bare names sorted lexicographically. Includes the tasks-store
    repo if present (it is a workspace child like any other)."""
    if not workspace.is_dir():
        return []
    out: list[str] = []
    for child in sorted(workspace.iterdir()):
        if not child.is_dir() or not is_valid_repo_ref(child.name):
            continue
        if (child / ".git").exists():
            out.append(child.name)
    return out


def resolve_repo(task_repo: str | None, queue_repo: str | None) -> str:
    """Resolve a task's target repo by precedence: task frontmatter ``repo:`` →
    queue ``config.json`` ``repo``. Returns the workspace-relative child name.

    Raises :class:`RepoConfigError` when neither is set (authoring error on the
    queue) or the chosen reference is malformed/unsafe (the path-traversal
    guard). Availability (repo present + ``.git``) is a *separate* check
    (:func:`repo_available`) that pauses rather than fails.
    """
    ref = (task_repo or "").strip() or (queue_repo or "").strip()
    if not ref:
        raise RepoConfigError(
            "no target repo set: the queue's config.json must set a default "
            "`repo` (or the task frontmatter must override it)"
        )
    if not is_valid_repo_ref(ref):
        raise RepoConfigError(
            f"invalid repo reference {ref!r}: a repo must be a bare workspace "
            "child name matching [a-z0-9][a-z0-9-]* (no paths, '..', '/', or "
            "absolute paths)"
        )
    return ref
