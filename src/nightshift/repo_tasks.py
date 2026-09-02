"""Repo task import — draining a target repo's publishing inboxes.

A target repo may carry two inbox roots that external tooling publishes task
briefs into — ``.tasks/`` and ``docs/tasks/`` — each in a flat layout
(``*.md`` briefs directly in the root) or a queue-dir layout
(``<queue>/*.md`` subdirectories), or both at once. ``.tasks/`` is the legacy
root, where a local ``config.json`` may also carry the publication ``order``;
``docs/tasks/`` briefs are plain markdown with no json control file, so they
publish in filename order and carry all their Nightshift metadata in
frontmatter.

Import is a *move* with git authority on both sides: the brief becomes
canonical in the content store (``nightshift-tasks/<queue>/``) and the source
file — with its entry in the inbox's ``config.json`` order — is removed from
the repo's ``main`` by the manager (the sole writer to ``main``), so a brief
exists in exactly one place, can never run twice, and is never lost. The scan reads the inboxes from the same authority the removal
writes to — the ``main`` *tree*, never the operator checkout, which may be
parked on any branch. See ``docs/spec/2026-07-04-repo-task-import.md``.

An import drains the briefs the operator picked (the whole scanned set when
they pick nothing in particular); anything left out stays published in the
inbox and is offered again next time.

This module is shared-core: read-only scan/select/copy plus the lock-held
removal orchestration; the HTTP surface lives in
:mod:`nightshift.manager.api_repo_tasks`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from nightshift.git import GitRunner
from nightshift.git.landing import (
    RepoContext,
    delete_produce,
    integrate_and_push_locked,
    push_main,
)
from nightshift.git.refs import main_sha
from nightshift.git.sync import sync_main_locked
from nightshift.lifecycle import LAND_SUCCESS_KINDS, LandingMode
from nightshift.playlists import is_valid_name
from nightshift.queue_config import load_order, save_order
from nightshift.spawn_daily import (
    is_disabled,
    is_quarantined,
    split_frontmatter,
    task_priority,
)
from nightshift.task_files import resolve_title


# The inbox directories external tooling publishes briefs into, relative to a
# target repo's root. Only ``.tasks`` carries a ``config.json`` order — see
# :func:`_inboxes`.
REPO_TASKS_DIR = ".tasks"
DOCS_TASKS_DIR = "docs/tasks"


@dataclass(frozen=True)
class _Inbox:
    """One inbox tree to scan: its repo-relative path plus whether a local
    ``config.json`` may order it (``.tasks`` only — ``docs/tasks`` briefs are
    plain markdown with no json control file)."""

    path: str
    ordered: bool


@dataclass(frozen=True)
class RepoTask:
    """One importable brief discovered in a repo's ``.tasks`` inbox."""

    name: str
    title: str
    # Repo-relative source path (``.tasks/….md``) — what the removal commit
    # deletes from ``main``.
    source: str
    priority: int
    disabled: bool
    # Published ``quarantined: true`` — the publisher's "do not run this". The
    # auto-importer refuses these outright (skipped, never removed); the manual
    # picker still offers them, tagged and unticked, so importing one is a
    # deliberate override of the publisher's hold.
    quarantined: bool
    # Exact text already present in the destination queue: import removes the
    # source without writing a second copy (the idempotent-replay/crash-
    # recovery path).
    duplicate: bool
    text: str


def _inboxes(queue_name: str) -> list[_Inbox]:
    """The inbox trees a queue drains, in the order they publish: each root's
    flat briefs, then the subdir matching ``queue_name`` (the queue's label;
    other subdirs belong to other queues and stay untouched). ``.tasks`` comes
    before ``docs/tasks`` so a repo publishing into both keeps the legacy
    inbox's precedence in the destination queue's execution order."""
    return [
        _Inbox(path, ordered=root == REPO_TASKS_DIR)
        for root in (REPO_TASKS_DIR, DOCS_TASKS_DIR)
        for path in (root, f"{root}/{queue_name}")
    ]


def host_inbox(host_queue: str) -> str:
    """The repo-relative inbox path of a named ``.tasks/`` host queue — the
    single tree the auto-importer drains (see :mod:`nightshift.auto_import`)."""
    return f"{REPO_TASKS_DIR}/{host_queue}"


def _local_order(git: GitRunner, treeish: str) -> list[str]:
    """The inbox-local ``config.json`` ``order`` list read from the inbox tree
    (best-effort: ``[]`` on a missing/malformed file — callers fall back to
    filename order)."""
    raw = git.run("cat-file", "blob", f"{treeish}/config.json")
    if not raw.ok:
        return []
    try:
        data = json.loads(raw.stdout)
    except ValueError:
        return []
    order = data.get("order") if isinstance(data, dict) else None
    return [str(name) for name in order] if isinstance(order, list) else []


def _md_blobs(git: GitRunner, treeish: str) -> dict[str, str]:
    """``{stem: filename}`` for the ``*.md`` blobs of one inbox tree
    (``<sha>:<inbox>``) — ``{}`` when the tree does not exist. Subdirectories
    are not blobs, so a queue-dir layout's briefs belong to their own inbox,
    never the root's."""
    listing = git.run("ls-tree", "-z", treeish)
    if not listing.ok:
        return {}
    by_stem: dict[str, str] = {}
    for entry in listing.stdout.split("\0"):
        meta, _, name = entry.partition("\t")
        if meta.split()[1:2] == ["blob"] and name.endswith(".md"):
            by_stem[name[: -len(".md")]] = name
    return by_stem


def _scan_tree(git: GitRunner, treeish: str, *, ordered: bool = True) -> list[str]:
    """The ``*.md`` blob names of one inbox tree (``<sha>:<inbox>``) in its
    published order: stems listed in the tree's ``config.json`` ``order``
    first, the rest by filename. ``[]`` when the tree does not exist.

    ``ordered=False`` skips the ``config.json`` read entirely — a json-free
    inbox publishes in filename order, so an unrelated ``config.json`` sitting
    in it never reorders the briefs.
    """
    by_stem = _md_blobs(git, treeish)
    local = _local_order(git, treeish) if ordered else []
    rank = {name: i for i, name in enumerate(local)}
    listed = sorted((s for s in by_stem if s in rank), key=lambda s: rank[s])
    unlisted = sorted(s for s in by_stem if s not in rank)
    return [by_stem[s] for s in (*listed, *unlisted)]


def _parse_brief(git: GitRunner, treeish: str, name: str) -> tuple[dict, str] | None:
    """Read one inbox blob into ``(frontmatter, text)`` — ``None`` when it is
    not an importable brief. Stems starting with ``_`` or ``.`` (templates,
    evergreen inboxes like ``_todo.md``) and ``autosplit: true`` sources
    (recurring, tooling appends to them in place) stay in the repo."""
    if name.startswith(("_", ".")):
        return None
    raw = git.run("cat-file", "blob", f"{treeish}/{name}")
    if not raw.ok:
        return None
    text = raw.stdout
    meta = split_frontmatter(text)[0] if text.startswith("---") else {}
    if meta.get("autosplit"):
        return None
    return meta, text


def list_repo_task_queues(workspace: Path, repo: str) -> list[str]:
    """The ``.tasks/<name>/`` host-queue subdirs published on the repo's
    canonical ``main``, sorted.

    These are the queues the Repos page offers as a queue's *host task queue*
    binding. Read from the ``main`` tree for the same reason the brief scan is
    (:func:`scan_repo_tasks`): the operator checkout may be parked anywhere. A
    subdir whose name is not a valid slug is skipped — the name is
    concatenated into an inbox path, so this is the traversal guard on the
    discovery side.
    """
    repo_root = workspace / repo
    base = main_sha(repo_root)
    if base is None:
        return []
    listing = GitRunner(repo_root).run("ls-tree", "-z", f"{base}:{REPO_TASKS_DIR}")
    if not listing.ok:
        return []
    out: list[str] = []
    for entry in listing.stdout.split("\0"):
        meta, _, name = entry.partition("\t")
        if meta.split()[1:2] == ["tree"] and is_valid_name(name):
            out.append(name)
    return sorted(out)


def _scan_inboxes(
    workspace: Path,
    repo: str,
    inboxes: list[_Inbox],
    tasks_root: Path,
    dest_rel: str,
) -> list[RepoTask]:
    """Scan the given inbox trees of a repo's canonical ``main`` into
    importable briefs — the shared core of :func:`scan_repo_tasks` (every
    inbox a queue drains) and :func:`scan_repo_inbox` (one named host queue).
    """
    repo_root = workspace / repo
    git = GitRunner(repo_root)
    base = main_sha(repo_root)
    if base is None:
        return []

    dest_dir = tasks_root / dest_rel
    existing = (
        {p.read_text(errors="replace") for p in dest_dir.glob("*.md")}
        if dest_dir.is_dir()
        else set()
    )

    out: list[RepoTask] = []
    for inbox in inboxes:
        treeish = f"{base}:{inbox.path}"
        for name in _scan_tree(git, treeish, ordered=inbox.ordered):
            parsed = _parse_brief(git, treeish, name)
            if parsed is None:
                continue
            meta, text = parsed
            stem = name[: -len(".md")]
            out.append(RepoTask(
                name=stem,
                title=resolve_title(stem, meta),
                source=f"{inbox.path}/{name}",
                priority=task_priority(meta),
                disabled=is_disabled(meta),
                quarantined=is_quarantined(meta),
                duplicate=text in existing,
                text=text,
            ))
    return out


def scan_repo_tasks(
    workspace: Path,
    repo: str,
    queue_name: str,
    tasks_root: Path,
    dest_rel: str,
) -> list[RepoTask]:
    """Scan the repo's canonical ``main`` for inbox briefs importable into a
    queue.

    The scan reads the ``main`` *tree* — the same authority the removal
    commits to — never the operator checkout, which may be parked on any
    branch (a drained brief must disappear from the preview even while some
    feature branch still carries the file). Every inbox tree of every root
    (:func:`_inboxes`) is scanned in turn, each group in its published order
    (:func:`_scan_tree`); a root that does not exist simply contributes
    nothing. Read-only — the import itself is :func:`copy_repo_tasks` +
    :func:`remove_repo_tasks_locked`.
    """
    return _scan_inboxes(
        workspace, repo, _inboxes(queue_name), tasks_root, dest_rel
    )


def scan_repo_inbox(
    workspace: Path,
    repo: str,
    inbox: str,
    tasks_root: Path,
    dest_rel: str,
) -> list[RepoTask]:
    """Scan exactly one inbox tree, in its published order.

    The auto-importer's read side: it drains a single named host queue
    (``.tasks/<host>``, see :func:`host_inbox`) rather than every inbox a
    manual import offers, so a repo's other host queues are never pulled into
    a queue that did not bind them.
    """
    return _scan_inboxes(
        workspace, repo, [_Inbox(inbox, ordered=True)], tasks_root, dest_rel
    )


def select_repo_tasks(
    entries: list[RepoTask], sources: list[str] | None
) -> tuple[list[RepoTask], list[str]]:
    """Narrow a scan to the operator's picked ``sources`` (repo-relative paths;
    ``None`` = the whole set, ``[]`` = nothing), keeping scan order.

    Selection keys on ``source`` rather than the task name because the name is
    ambiguous — the same stem can be published under either inbox root, and
    both flat and under the queue's subdir; those are all distinct briefs.

    Returns ``(picked, missing)``, where ``missing`` are picked sources the scan
    no longer offers. A selection made against a stale preview (a concurrent
    import drained a brief, or tooling rewrote it) is not a batch failure: what
    is still published imports, and the caller reports the rest rather than
    claiming they moved.
    """
    if sources is None:
        return entries, []
    wanted = set(sources)
    picked = [e for e in entries if e.source in wanted]
    return picked, sorted(wanted - {e.source for e in picked})


def copy_repo_tasks(
    tasks_root: Path, dest_rel: str, entries: list[RepoTask]
) -> list[dict]:
    """Copy the non-duplicate scanned briefs into the destination queue dir,
    appending them to its execution order. A name collision with *different*
    content gets a ``-2`` suffix (the existing cross-queue copy policy).

    Returns ``{task, title}`` per brief written. This is the durable half of
    an import — the caller commits the content store, and only then removes
    the sources from the repo.
    """
    dest_dir = tasks_root / dest_rel
    dest_dir.mkdir(parents=True, exist_ok=True)
    imported: list[dict] = []
    for entry in entries:
        if entry.duplicate:
            continue
        name = entry.name
        n = 2
        while (dest_dir / f"{name}.md").exists():
            name = f"{entry.name}-{n}"
            n += 1
        (dest_dir / f"{name}.md").write_text(entry.text)
        imported.append({"task": name, "title": entry.title})
    if imported:
        save_order(
            tasks_root,
            [*load_order(tasks_root, dest_rel), *(t["task"] for t in imported)],
            dest_rel,
        )
    return imported


def _json_indent(text: str) -> int | str:
    """The indent of the first indented line of a json document (so a rewrite
    keeps the publisher's own formatting instead of churning the whole file).
    Two spaces when nothing indented is found."""
    for line in text.splitlines():
        lead = line[: len(line) - len(line.lstrip())]
        if lead and line.strip():
            return lead
    return 2


def prune_inbox_orders(
    repo_root: Path, sources: Sequence[str]
) -> Callable[[str, Sequence[str]], dict[str, str]]:
    """The removal commit's companion edit (:func:`delete_produce`'s
    ``rewrite``): every drained ``.tasks`` inbox's ``config.json`` with the
    stems it no longer has a brief for dropped from ``order``.

    Deleting the ``*.md`` files alone leaves the publisher's ``order`` naming
    briefs that no longer exist — dead weight that grows with every import and
    that anything walking the list positionally has to step over. The rule is
    the resulting tree: a stem stays only while its ``<stem>.md`` survives in
    the inbox. That prunes what this commit drains *and* heals entries an
    earlier import (which only ever deleted files) left behind, in the one
    commit that is already touching the inbox.

    Conservative everywhere else: only ``.tasks`` inboxes carry an order at
    all (``docs/tasks`` is plain markdown, so those sources rewrite nothing),
    a missing or unparsable ``config.json`` is left exactly as it is, other
    keys and the surviving entries' relative order are untouched, and a config
    that needs no change is not rewritten.
    """

    def rewrite(base: str, deleted: Sequence[str]) -> dict[str, str]:
        git = GitRunner(repo_root)
        gone = set(deleted)
        edits: dict[str, str] = {}
        for inbox in sorted({s.rpartition("/")[0] for s in sources}):
            if inbox != REPO_TASKS_DIR and not inbox.startswith(f"{REPO_TASKS_DIR}/"):
                continue
            path = f"{inbox}/config.json"
            raw = git.run("cat-file", "blob", f"{base}:{path}")
            if not raw.ok:
                continue
            try:
                data = json.loads(raw.stdout)
            except ValueError:
                continue
            order = data.get("order") if isinstance(data, dict) else None
            if not isinstance(order, list):
                continue
            surviving = {
                stem
                for stem, name in _md_blobs(git, f"{base}:{inbox}").items()
                if f"{inbox}/{name}" not in gone
            }
            kept = [stem for stem in order if str(stem) in surviving]
            if kept == order:
                continue
            data["order"] = kept
            body = json.dumps(data, indent=_json_indent(raw.stdout), ensure_ascii=False)
            edits[path] = f"{body}\n" if raw.stdout.endswith("\n") else body
        return edits

    return rewrite


def remove_repo_tasks_locked(
    workspace: Path,
    repo: str,
    sources: list[str],
    message: str,
    *,
    remote: str = "origin",
) -> dict:
    """Remove drained inbox files from the repo's canonical ``main`` — one
    commit built and CAS'd through the landing pipeline. The caller must hold
    the RepoLock; in the manager this runs as a repo-executor job, so it can
    never interleave with a land or sync on the same repo.

    Posture around the commit: sync ``origin/main`` first (best-effort) so the
    removal lands on the fresh tip, and push ``main`` afterwards so the
    removal is never lost to a later divergence-rescuing sync. A failed
    removal or push is surfaced as ``warning`` and never unwound — the import
    already made the briefs durable in the content store, and a replayed
    import dedupes instead of duplicating.

    The commit deletes the brief files *and* prunes the stems they left behind
    in the inbox's ``config.json`` order (:func:`prune_inbox_orders`), so a
    drained inbox is left consistent rather than listing briefs that no longer
    exist.
    """
    repo_root = workspace / repo
    has_remote = GitRunner(repo_root).run("remote", "get-url", remote).ok
    if has_remote:
        sync_main_locked(workspace, repo, remote)
    outcome = integrate_and_push_locked(
        RepoContext(workspace=workspace, repo=repo),
        delete_produce(
            repo_root, sources, message,
            rewrite=prune_inbox_orders(repo_root, sources),
        ),
        mode=LandingMode.NONE,
    )
    removed = outcome.kind in LAND_SUCCESS_KINDS
    warning: str | None = None
    if not removed:
        warning = f"could not remove imported briefs from {repo}: {outcome.detail}"
    elif has_remote:
        sha = main_sha(repo_root)
        push = push_main(workspace, repo, remote, sha) if sha else None
        if push is not None and not push.ok:
            warning = (
                f"imported-brief removal committed locally but the push to "
                f"{remote} failed: {push.detail}"
            )
    return {"removed": removed, "warning": warning}
