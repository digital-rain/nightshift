"""Shared test fixture: build a fake multi-repo workspace.

A Nightshift workspace parents many git repos. This builder creates, under a
``tmp_path``:

* ``<workspace>/config.json`` — operator/manager config.
* ``<workspace>/nightshift-tasks/`` — the content-store repo (a real git repo),
  with queues hoisted to its root (``main/`` is the default queue; alternates
  are sibling dirs). Each queue dir holds ``*.md`` briefs + ``config.json``.
* ``<workspace>/<repo>/`` — one or more target repos (real git repos, branch
  ``main``, one initial commit) that git operations run against.

Tests can also reference an *absent* repo name (one not created here) to
exercise the ``repo_unavailable`` pause/rescan lifecycle.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path

from nightshift.repos import DEFAULT_TASKS_REPO


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def git_init(repo: Path) -> None:
    """Init a repo on branch ``main`` with a repo-local identity (host-independent,
    matching the engine's expectations: it lands onto ``main`` by name)."""
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init")
    git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    git(repo, "config", "user.email", "test@test")
    git(repo, "config", "user.name", "test")


def git_commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "--short", "HEAD")


def make_target_repo(workspace: Path, name: str, *, files: Mapping[str, str] | None = None) -> Path:
    """Create ``<workspace>/<name>/`` as a real git repo with an initial commit."""
    repo = workspace / name
    git_init(repo)
    (repo / "README.md").write_text(f"# {name}\n")
    for rel, content in (files or {}).items():
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    git_commit_all(repo, "init")
    return repo


def make_bare_remote(path: Path) -> Path:
    """Init a bare git repo at ``path`` (a stand-in for the rendezvous remote /
    GitHub ``origin`` used by the cross-machine landing tests). Returns it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(path)],
        check=True,
        capture_output=True,
    )
    return path


def add_remote(repo_root: Path, name: str, url: Path, *, push_main: bool = True) -> None:
    """Register ``<name> -> url`` on ``repo_root`` and (by default) push ``main``
    so the remote's ``main`` matches the local clone."""
    git(repo_root, "remote", "add", name, str(url))
    if push_main:
        git(repo_root, "push", name, "main")


def _write_queue(
    tasks_root: Path,
    queue: str,
    *,
    tasks: Mapping[str, str] | None = None,
    config: Mapping[str, object] | None = None,
) -> None:
    qdir = tasks_root / queue
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / "config.json").write_text(json.dumps(dict(config or {}), indent=2) + "\n")
    for name, content in (tasks or {}).items():
        (qdir / f"{name}.md").write_text(content)


def build_workspace(
    workspace: Path,
    *,
    tasks: Mapping[str, str] | None = None,
    main_repo: str | None = "longitude",
    repos: Iterable[str] = ("longitude",),
    queues: Mapping[str, Mapping[str, object]] | None = None,
    config: Mapping[str, object] | None = None,
    tasks_repo: str = DEFAULT_TASKS_REPO,
    commit_tasks: bool = True,
) -> Path:
    """Build a fake workspace and return its path.

    * ``tasks`` — briefs for the default ``main`` queue.
    * ``main_repo`` — the ``repo`` bound to the ``main`` queue's config (or
      ``None`` to leave it unset → authoring error on dispatch).
    * ``repos`` — target repos to create as real git repos. A name in
      ``main_repo``/queue configs but *omitted* here stays absent (for pause).
    * ``queues`` — alternate queues, ``{name: {"tasks": {...}, "config": {...}}}``.
    * ``config`` — operator ``config.json`` overrides (merged onto a sane base).
    * ``commit_tasks`` — git-init + commit the ``nightshift-tasks`` store.
    """
    workspace.mkdir(parents=True, exist_ok=True)

    base_config: dict[str, object] = {
        "model": "auto",
        "validate": "true",
        "default_model": "auto",
    }
    base_config.update(config or {})
    (workspace / "config.json").write_text(json.dumps(base_config, indent=2) + "\n")

    for name in repos:
        make_target_repo(workspace, name)

    tasks_root = workspace / tasks_repo
    main_config: dict[str, object] = {"order": []}
    if main_repo is not None:
        main_config["repo"] = main_repo
    _write_queue(tasks_root, "main", tasks=tasks, config=main_config)

    for qname, spec in (queues or {}).items():
        _write_queue(
            tasks_root,
            qname,
            tasks=spec.get("tasks"),  # type: ignore[arg-type]
            config=spec.get("config"),  # type: ignore[arg-type]
        )

    if commit_tasks:
        git_init(tasks_root)
        (tasks_root / ".gitignore").write_text("*/runs/\n*/logs/\n")
        git_commit_all(tasks_root, "init nightshift-tasks")

    return workspace
