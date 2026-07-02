"""Cross-machine landing (transport B — git rendezvous remote) tests.

A worker on another box publishes its validated task branch to a shared git
remote (here a bare repo standing in for GitHub ``origin``); the manager fetches
it into its own clone, verifies ``head_sha``, and lands. PR mode is made
``origin/main``-authoritative so local/origin ``main`` cannot diverge. These
tests cover the engine transport helpers, the manager ``land()`` obtain/verify/
prune path, the PR-mode resync, the worker publish step, config parsing, the
``make_pr`` override, and the co-located regression. See
docs/spec/remote-landing.md.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

import nightshift.backends as backends_mod
from _workspace import (
    add_remote,
    build_workspace,
    git,
    git_commit_all,
    make_bare_remote,
)
from nightshift.backends import WorkerResult
from nightshift.engine import (
    _is_ancestor,
    fetch_rendezvous_branch,
    maybe_sync_main_to_origin,
    normalize_wip_prefix,
    prune_rendezvous_branch,
    publish_task_branch,
    reset_origin_sync_throttle,
    setup_worktree,
    sync_main_to_origin,
    worktree_branch,
    worktree_dir,
)
from nightshift.git import GitRunner
from nightshift.manager.app import create_app
from nightshift.manager.config import load_manager_config
from nightshift.manager.landing import LandingResult, canonical_head, land
from nightshift.manager.store import MemoryStore
from nightshift.worker.config import WorkerConfig, load_worker_config
from nightshift.worker.execute import execute_work_order
from nightshift.worker.local_store import LocalStore
from nightshift.worker.loop import WorkerLoop


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _setup_manager_repo(tmp_path: Path) -> tuple[Path, str, Path, Path]:
    """A manager workspace whose target repo has a bare ``origin`` rendezvous.

    Returns ``(workspace, repo, repo_root, bare)``. The bare's ``main`` matches
    the manager's local ``main`` (add_remote pushes it).
    """
    workspace = build_workspace(tmp_path, repos=("longitude",), main_repo="longitude")
    repo = "longitude"
    repo_root = workspace / repo
    bare = make_bare_remote(tmp_path / "remotes" / "longitude.git")
    add_remote(repo_root, "origin", bare)
    return workspace, repo, repo_root, bare


def _clone(bare: Path, dest: Path) -> Path:
    subprocess.run(["git", "clone", str(bare), str(dest)], check=True, capture_output=True)
    git(dest, "config", "user.email", "w@w")
    git(dest, "config", "user.name", "worker")
    return dest


def _publish_from_worker(
    bare: Path,
    work_root: Path,
    *,
    task: str,
    path: str,
    content: str,
    queue_slug: str = "main",
    base: str = "main",
) -> tuple[str, str]:
    """Simulate a cross-machine worker: clone the bare, branch from ``origin/<base>``,
    commit a change, and force-push it as the WIP ref. Returns ``(wip_ref, head)``.
    """
    worker = _clone(bare, work_root / f"worker-{task}")
    branch = f"task-local/{queue_slug}/{task}"
    git(worker, "checkout", "-b", branch, f"origin/{base}")
    (worker / path).write_text(content)
    git(worker, "add", "-A")
    git(worker, "commit", "-m", f"work {task}")
    wip_ref = f"refs/heads/nightshift-wip/{queue_slug}/{task}"
    git(worker, "push", "-f", "origin", f"HEAD:{wip_ref}")
    return wip_ref, git(worker, "rev-parse", "HEAD")


def _ls_remote(bare: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "ls-remote", str(bare), ref], capture_output=True, text=True
    ).stdout.strip()


class _CommittingBackend:
    """Fake agentic backend that writes + commits a file in the worktree."""

    name = "claude-code"
    agentic = True

    def __init__(self, path: str = "GENERATED.txt", content: str = "done\n") -> None:
        self._path = path
        self._content = content

    def available(self, config: Any = None) -> bool:
        return True

    def run(self, spec, emit_log, should_abort, on_worker_start=None) -> WorkerResult:
        (spec.cwd / self._path).write_text(self._content)
        subprocess.run(["git", "add", "-A"], cwd=spec.cwd, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"work {spec.task}"],
            cwd=spec.cwd, check=True, capture_output=True,
        )
        return WorkerResult(returncode=0)


class _NoopBackend:
    """Fake backend that produces no commit (nothing to land)."""

    name = "claude-code"
    agentic = True

    def available(self, config: Any = None) -> bool:
        return True

    def run(self, spec, emit_log, should_abort, on_worker_start=None) -> WorkerResult:
        emit_log("noop\n")
        return WorkerResult(returncode=0)


def _order(repo: str, task: str, base_ref: str | None) -> dict[str, Any]:
    return {
        "task": task,
        "repo": repo,
        "queue": "main",
        "config": {"validate": "true"},  # the shell `true`: a passing validate gate
        "body": f"brief for {task}",
        "base_ref": base_ref,
    }


# --------------------------------------------------------------------------- #
# engine transport helpers
# --------------------------------------------------------------------------- #


def test_publish_fetch_prune_roundtrip(tmp_path: Path) -> None:
    workspace, repo, repo_root, bare = _setup_manager_repo(tmp_path)
    # Build a local task branch with a commit, the way a co-located worker would.
    wt = setup_worktree(workspace, repo, "10.add")
    (wt / "new.txt").write_text("hi\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-m", "work")

    wip_ref, head = publish_task_branch(workspace, repo, "10.add", "origin")
    assert wip_ref == "refs/heads/nightshift-wip/main/10.add"
    assert len(head) == 40
    assert head in _ls_remote(bare, wip_ref)

    # Drop the local branch + worktree, then fetch it back from the remote.
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo_root, capture_output=True)
    git(repo_root, "branch", "-D", worktree_branch("10.add"))
    fetched = fetch_rendezvous_branch(workspace, repo, "origin", wip_ref, "10.add")
    assert fetched == head
    assert git(repo_root, "rev-parse", worktree_branch("10.add")) == head

    prune_rendezvous_branch(workspace, repo, "origin", wip_ref)
    assert _ls_remote(bare, wip_ref) == ""


def test_publish_custom_prefix_roundtrip(tmp_path: Path) -> None:
    """A configured ``wip_ref_prefix`` (here multi-segment) is honored end to
    end: publish lands under the custom namespace and the manager fetch/prune
    use the worker-reported ref verbatim, so the two sides stay consistent."""
    workspace, repo, repo_root, bare = _setup_manager_repo(tmp_path)
    wt = setup_worktree(workspace, repo, "10.add")
    (wt / "new.txt").write_text("hi\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-m", "work")

    wip_ref, head = publish_task_branch(
        workspace, repo, "10.add", "origin", prefix="acme/wip"
    )
    assert wip_ref == "refs/heads/acme/wip/main/10.add"
    assert head in _ls_remote(bare, wip_ref)

    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo_root, capture_output=True)
    git(repo_root, "branch", "-D", worktree_branch("10.add"))
    assert fetch_rendezvous_branch(workspace, repo, "origin", wip_ref, "10.add") == head
    prune_rendezvous_branch(workspace, repo, "origin", wip_ref)
    assert _ls_remote(bare, wip_ref) == ""


def test_normalize_wip_prefix() -> None:
    assert normalize_wip_prefix("nightshift-wip") == "nightshift-wip"
    assert normalize_wip_prefix("  acme/wip/  ") == "acme/wip"
    for bad in ["", "   ", "-bad", "a..b", "a//b", "has space", "carrot^"]:
        try:
            normalize_wip_prefix(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")  # pragma: no cover


def test_publish_raises_without_branch(tmp_path: Path) -> None:
    workspace, repo, _repo_root, _bare = _setup_manager_repo(tmp_path)
    try:
        publish_task_branch(workspace, repo, "99.missing", "origin")
    except RuntimeError as exc:
        assert "no task branch" in str(exc)
    else:  # pragma: no cover - explicit failure
        raise AssertionError("expected RuntimeError for a missing branch")


def test_fetch_returns_none_on_error(tmp_path: Path) -> None:
    workspace, repo, _repo_root, _bare = _setup_manager_repo(tmp_path)
    fetched = fetch_rendezvous_branch(
        workspace, repo, "origin", "refs/heads/nightshift-wip/main/nope", "nope"
    )
    assert fetched is None


def test_sync_main_to_origin_resets_orphan_divergence(tmp_path: Path) -> None:
    workspace, repo, repo_root, bare = _setup_manager_repo(tmp_path)
    m0 = canonical_head(repo_root)

    # Manager local main carries an ephemeral pr-mode squash (orphan-to-be).
    (repo_root / "ephemeral.txt").write_text("local squash\n")
    git_commit_all(repo_root, "ephemeral pr squash")
    local = canonical_head(repo_root)

    # GitHub re-squashed the PR into a *different* commit on origin/main.
    other = _clone(bare, tmp_path / "github")
    (other / "merged.txt").write_text("github merge\n")
    git(other, "add", "-A")
    git(other, "commit", "-m", "squash-merge of PR")
    git(other, "push", "origin", "HEAD:main")
    merged = git(other, "rev-parse", "HEAD")
    assert merged not in (m0, local)

    # The orphan is dropped only because the caller names it explicitly; an
    # unnamed divergent commit would be rescued + replayed (see the operator
    # cherry-pick tests below).
    new_head = sync_main_to_origin(
        workspace, repo, "origin", drop_shas=frozenset({local})
    )
    assert new_head == merged
    assert canonical_head(repo_root) == merged
    assert (repo_root / "merged.txt").exists()
    assert not (repo_root / "ephemeral.txt").exists()  # orphan dropped, no divergence


def test_sync_main_to_origin_rescues_operator_commit_over_divergence(
    tmp_path: Path,
) -> None:
    """A forced reset_divergence sync must replay an unpushed operator commit
    (e.g. a hand cherry-pick) onto the fresh origin/main, never drop it."""
    workspace, repo, repo_root, bare = _setup_manager_repo(tmp_path)

    # Operator commits a lint fix directly on local main (unpushed).
    (repo_root / "lint.txt").write_text("lint fix\n")
    git_commit_all(repo_root, "operator cherry-pick lint fix")

    # origin/main advances independently on another clone.
    other = _clone(bare, tmp_path / "other")
    (other / "feature.txt").write_text("origin feature\n")
    git(other, "add", "-A")
    git(other, "commit", "-m", "advance origin main")
    git(other, "push", "origin", "HEAD:main")
    advanced = git(other, "rev-parse", "HEAD")

    # Forced divergence reset (the land-retry posture) with NO drop set: the
    # operator commit is rescued and replayed on top of origin's advance.
    new_head = sync_main_to_origin(workspace, repo, "origin")
    assert new_head is not None
    assert _is_ancestor(repo_root, advanced, new_head)  # origin's work integrated
    assert (repo_root / "feature.txt").exists()
    assert (repo_root / "lint.txt").exists()  # operator commit survived
    # The operator commit now sits on top of origin's advance, not lost.
    log = git(repo_root, "log", "--format=%s", f"{advanced}..{new_head}")
    assert "operator cherry-pick lint fix" in log


def test_sync_drops_orphan_but_keeps_operator_commit(tmp_path: Path) -> None:
    """When main carries both the manager's orphan squash AND an operator
    commit, naming only the orphan drops it while the operator commit replays."""
    workspace, repo, repo_root, bare = _setup_manager_repo(tmp_path)

    # Operator commit first, then the manager's orphan squash on top.
    (repo_root / "lint.txt").write_text("lint fix\n")
    git_commit_all(repo_root, "operator cherry-pick")
    (repo_root / "ephemeral.txt").write_text("orphan squash\n")
    git_commit_all(repo_root, "ephemeral pr squash")
    orphan = canonical_head(repo_root)

    other = _clone(bare, tmp_path / "github")
    (other / "merged.txt").write_text("github merge\n")
    git(other, "add", "-A")
    git(other, "commit", "-m", "squash-merge of PR")
    git(other, "push", "origin", "HEAD:main")
    merged = git(other, "rev-parse", "HEAD")

    new_head = sync_main_to_origin(
        workspace, repo, "origin", drop_shas=frozenset({orphan})
    )
    assert new_head is not None
    assert _is_ancestor(repo_root, merged, new_head)
    assert (repo_root / "merged.txt").exists()
    assert (repo_root / "lint.txt").exists()  # operator commit replayed
    assert not (repo_root / "ephemeral.txt").exists()  # named orphan dropped


def test_sync_refuses_reset_when_stash_create_fails(tmp_path: Path, monkeypatch) -> None:
    """A failed ``git stash create`` must refuse the sync (mirroring
    squash_to_main's wip_sha guard) — never proceed to ``reset --hard`` over the
    operator's uncommitted work."""
    workspace, repo, repo_root, bare = _setup_manager_repo(tmp_path)

    # origin/main advances (a plain fast-forward would normally follow).
    other = _clone(bare, tmp_path / "other")
    (other / "ahead.txt").write_text("ahead\n")
    git(other, "add", "-A")
    git(other, "commit", "-m", "advance origin main")
    git(other, "push", "origin", "HEAD:main")

    # Operator has uncommitted WIP on a tracked file...
    (repo_root / "README.md").write_text("precious uncommitted work\n")
    head_before = canonical_head(repo_root)

    # ...and `git stash create` fails to capture it.
    monkeypatch.setattr(
        "nightshift.engine._stash_operator_work", lambda *a, **k: None
    )
    new_head = sync_main_to_origin(workspace, repo, "origin")

    # The sync was refused: HEAD did not move and the WIP is intact.
    assert new_head == head_before
    assert canonical_head(repo_root) == head_before
    assert (repo_root / "README.md").read_text() == "precious uncommitted work\n"
    assert not (repo_root / "ahead.txt").exists()


def test_sync_rescue_reports_dropped_conflicting_commit(tmp_path: Path) -> None:
    """A divergence rescue that cannot replay an operator commit (it conflicts
    with the fresh origin/main) must surface the dropped SHA to the caller
    instead of silently burying it in the reflog."""
    workspace, repo, repo_root, bare = _setup_manager_repo(tmp_path)
    (repo_root / "file.txt").write_text("base\n")
    git_commit_all(repo_root, "add file")
    git(repo_root, "push", "origin", "main")

    # Operator commits one version locally (unpushed)...
    (repo_root / "file.txt").write_text("operator version\n")
    git_commit_all(repo_root, "operator edit")
    operator_sha = git(repo_root, "rev-parse", "HEAD")

    # ...while origin advances with a conflicting edit to the same file.
    other = _clone(bare, tmp_path / "other")
    (other / "file.txt").write_text("origin version\n")
    git(other, "add", "-A")
    git(other, "commit", "-m", "conflicting origin edit")
    git(other, "push", "origin", "HEAD:main")

    dropped: list[str] = []
    new_head = sync_main_to_origin(
        workspace, repo, "origin", dropped_commits=dropped
    )
    assert new_head is not None
    assert (repo_root / "file.txt").read_text() == "origin version\n"
    assert dropped == [operator_sha]


def test_land_push_retry_reports_rescue_dropped_commit(tmp_path: Path) -> None:
    """End to end: a push-mode land whose retry re-sync drops a conflicting
    operator commit reports the SHA in ``LandingResult.detail``."""
    workspace, repo, repo_root, bare = _setup_manager_repo(tmp_path)
    reset_origin_sync_throttle()
    (repo_root / "file.txt").write_text("base\n")
    git_commit_all(repo_root, "add file")
    git(repo_root, "push", "origin", "main")
    base = canonical_head(repo_root)

    # The task branch (cut before the operator edit) touches an unrelated file.
    wt = setup_worktree(workspace, repo, "10.add")
    (wt / "new.txt").write_text("task work\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-m", "task work")

    # Operator commits on local main; origin advances with a conflicting edit,
    # so the first push is rejected and the retry re-sync must rescue-and-drop.
    (repo_root / "file.txt").write_text("operator version\n")
    git_commit_all(repo_root, "operator edit")
    operator_sha = git(repo_root, "rev-parse", "HEAD")
    other = _clone(bare, tmp_path / "other")
    (other / "file.txt").write_text("origin version\n")
    git(other, "add", "-A")
    git(other, "commit", "-m", "conflicting origin edit")
    git(other, "push", "origin", "HEAD:main")

    result = land(
        workspace, repo, "10.add", "add file", queue=None, base_ref=base,
        landing_mode="push", rendezvous_remote="origin",
    )
    assert result.landed is True
    assert result.pushed is True
    assert (repo_root / "file.txt").read_text() == "origin version\n"
    assert (repo_root / "new.txt").read_text() == "task work\n"
    assert operator_sha[:12] in result.detail  # the casualty is surfaced


def test_maybe_sync_preserves_local_cherry_pick(tmp_path: Path) -> None:
    """Periodic/poll sync must not reset main when it carries unpushed commits."""
    workspace, repo, repo_root, _bare = _setup_manager_repo(tmp_path)
    reset_origin_sync_throttle()
    before = canonical_head(repo_root)
    (repo_root / "lint.txt").write_text("lint fix\n")
    git_commit_all(repo_root, "cherry-pick lint fix")
    local = canonical_head(repo_root)
    assert local != before

    head = maybe_sync_main_to_origin(workspace, repo, "origin", min_interval_seconds=0)
    assert head == local
    assert canonical_head(repo_root) == local
    assert (repo_root / "lint.txt").exists()


def test_sync_main_to_origin_fast_forwards(tmp_path: Path) -> None:
    workspace, repo, repo_root, bare = _setup_manager_repo(tmp_path)
    other = _clone(bare, tmp_path / "other")
    (other / "ahead.txt").write_text("ahead\n")
    git(other, "add", "-A")
    git(other, "commit", "-m", "advance origin main")
    git(other, "push", "origin", "HEAD:main")
    advanced = git(other, "rev-parse", "HEAD")

    new_head = sync_main_to_origin(workspace, repo, "origin")
    assert new_head == advanced
    assert canonical_head(repo_root) == advanced
    assert (repo_root / "ahead.txt").exists()


def test_sync_main_to_origin_none_without_remote_main(tmp_path: Path) -> None:
    workspace = build_workspace(tmp_path, repos=("longitude",), main_repo="longitude")
    repo_root = workspace / "longitude"
    bare = make_bare_remote(tmp_path / "remotes" / "empty.git")
    add_remote(repo_root, "origin", bare, push_main=False)  # remote has no main
    reset_origin_sync_throttle()
    assert sync_main_to_origin(workspace, "longitude", "origin") is None


def test_maybe_sync_main_to_origin_throttles_fetch(tmp_path: Path, monkeypatch) -> None:
    """Repeated checks within git_refresh_seconds skip the network fetch."""
    workspace, repo, repo_root, bare = _setup_manager_repo(tmp_path)
    reset_origin_sync_throttle()
    other = _clone(bare, tmp_path / "other")
    (other / "ahead.txt").write_text("ahead\n")
    git(other, "add", "-A")
    git(other, "commit", "-m", "advance origin main")
    git(other, "push", "origin", "HEAD:main")
    advanced = git(other, "rev-parse", "HEAD")

    # Spy on the GitRunner seam (not subprocess): count fetches while
    # delegating to the real runner so the sync still talks to the bare remote.
    fetch_calls: list[list[str]] = []
    real_run = GitRunner.run

    def _run(self: GitRunner, *args: str):
        if args[:2] == ("fetch", "origin"):
            fetch_calls.append(["git", *args])
        return real_run(self, *args)

    monkeypatch.setattr(GitRunner, "run", _run)

    assert maybe_sync_main_to_origin(
        workspace, repo, "origin", min_interval_seconds=60.0,
    ) == advanced
    assert fetch_calls == [["git", "fetch", "origin", "main"]]

    fetch_calls.clear()
    (other / "ahead2.txt").write_text("more\n")
    git(other, "add", "-A")
    git(other, "commit", "-m", "advance again")
    git(other, "push", "origin", "HEAD:main")
    again = git(other, "rev-parse", "HEAD")

    # Throttled: no second fetch; local main stays on the first advance.
    assert maybe_sync_main_to_origin(
        workspace, repo, "origin", min_interval_seconds=60.0,
    ) == advanced
    assert fetch_calls == []

    reset_origin_sync_throttle()
    fetch_calls.clear()
    assert maybe_sync_main_to_origin(
        workspace, repo, "origin", min_interval_seconds=60.0, force=True,
    ) == again
    assert fetch_calls == [["git", "fetch", "origin", "main"]]


# --------------------------------------------------------------------------- #
# manager land(): cross-machine obtain / verify / prune
# --------------------------------------------------------------------------- #


def test_cross_machine_land_happy_path(tmp_path: Path) -> None:
    workspace, repo, repo_root, bare = _setup_manager_repo(tmp_path)
    base = canonical_head(repo_root)
    wip_ref, head = _publish_from_worker(
        bare, tmp_path / "work", task="10.add", path="new.txt", content="hello\n"
    )
    assert not worktree_dir(workspace, repo, "10.add").exists()  # cross-machine

    result = land(
        workspace, repo, "10.add", "add new file", queue=None, base_ref=base,
        branch_ref=wip_ref, head_sha=head, rendezvous_remote="origin",
    )
    assert result.landed is True
    assert (repo_root / "new.txt").read_text() == "hello\n"
    # WIP ref is pruned once consumed.
    assert _ls_remote(bare, wip_ref) == ""


def test_cross_machine_head_sha_mismatch_fails_closed(tmp_path: Path) -> None:
    workspace, repo, repo_root, bare = _setup_manager_repo(tmp_path)
    base = canonical_head(repo_root)
    wip_ref, _head = _publish_from_worker(
        bare, tmp_path / "work", task="10.add", path="new.txt", content="hello\n"
    )
    result = land(
        workspace, repo, "10.add", "add new file", queue=None, base_ref=base,
        branch_ref=wip_ref, head_sha="0" * 40, rendezvous_remote="origin",
    )
    assert result.landed is False
    assert result.recoverable is False
    assert "mismatch" in result.detail
    assert not (repo_root / "new.txt").exists()
    assert _ls_remote(bare, wip_ref) != ""  # kept for a resolve re-fetch


def test_cross_machine_missing_remote_or_head_fails_closed(tmp_path: Path) -> None:
    workspace, repo, repo_root, bare = _setup_manager_repo(tmp_path)
    base = canonical_head(repo_root)
    wip_ref, head = _publish_from_worker(
        bare, tmp_path / "work", task="10.add", path="new.txt", content="hello\n"
    )
    no_head = land(
        workspace, repo, "10.add", "t", queue=None, base_ref=base,
        branch_ref=wip_ref, head_sha=None, rendezvous_remote="origin",
    )
    assert no_head.landed is False and no_head.recoverable is False

    no_remote = land(
        workspace, repo, "10.add", "t", queue=None, base_ref=base,
        branch_ref=wip_ref, head_sha=head, rendezvous_remote=None,
    )
    assert no_remote.landed is False and no_remote.recoverable is False


def test_cross_machine_fetch_error_is_recoverable(tmp_path: Path) -> None:
    workspace, repo, repo_root, _bare = _setup_manager_repo(tmp_path)
    base = canonical_head(repo_root)
    result = land(
        workspace, repo, "10.add", "t", queue=None, base_ref=base,
        branch_ref="refs/heads/nightshift-wip/main/10.add",  # never published
        head_sha="a" * 40, rendezvous_remote="origin",
    )
    assert result.landed is False
    assert result.recoverable is True


def test_cross_machine_conflict_keeps_wip_ref(tmp_path: Path) -> None:
    workspace, repo, repo_root, bare = _setup_manager_repo(tmp_path)
    # Give main a file to fight over, and re-sync the bare's main to it.
    (repo_root / "file.txt").write_text("base\n")
    git_commit_all(repo_root, "add file")
    git(repo_root, "push", "origin", "main")
    base = canonical_head(repo_root)

    wip_ref, head = _publish_from_worker(
        bare, tmp_path / "work", task="20.edit", path="file.txt", content="worker change\n"
    )
    # Manager main drifts past base with a conflicting edit.
    (repo_root / "file.txt").write_text("manager change\n")
    git(repo_root, "commit", "-am", "manager edits file")

    result = land(
        workspace, repo, "20.edit", "edit file", queue=None, base_ref=base,
        branch_ref=wip_ref, head_sha=head, rendezvous_remote="origin",
    )
    assert result.landed is False
    assert result.conflict is True
    assert _ls_remote(bare, wip_ref) != ""  # kept for resolve


def test_cross_machine_reland_refetches_corrected_head(tmp_path: Path) -> None:
    workspace, repo, repo_root, bare = _setup_manager_repo(tmp_path)
    base = canonical_head(repo_root)

    wip_ref, head_a = _publish_from_worker(
        bare, tmp_path / "work-a", task="30.fix", path="x.txt", content="A\n"
    )
    # First attempt is rejected (stale/wrong head) but still fetches A locally.
    rejected = land(
        workspace, repo, "30.fix", "t", queue=None, base_ref=base,
        branch_ref=wip_ref, head_sha="0" * 40, rendezvous_remote="origin",
    )
    assert rejected.landed is False

    # Worker republishes a corrected commit B over the same WIP ref.
    _wip_ref2, head_b = _publish_from_worker(
        bare, tmp_path / "work-b", task="30.fix", path="x.txt", content="B\n"
    )
    assert head_b != head_a

    # The retry must re-fetch (gated on the worktree dir, not branch presence),
    # picking up B and verifying it rather than landing the stale A.
    landed = land(
        workspace, repo, "30.fix", "t", queue=None, base_ref=base,
        branch_ref=wip_ref, head_sha=head_b, rendezvous_remote="origin",
    )
    assert landed.landed is True
    assert (repo_root / "x.txt").read_text() == "B\n"


def test_colocated_land_ignores_rendezvous(tmp_path: Path) -> None:
    # Worktree present (co-located): land squashes the local branch and never
    # touches a remote, even if a branch_ref/remote were (spuriously) supplied.
    workspace, repo, repo_root, _bare = _setup_manager_repo(tmp_path)
    wt = setup_worktree(workspace, repo, "40.local")
    (wt / "local.txt").write_text("local\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-m", "local work")
    base = canonical_head(repo_root)

    result = land(
        workspace, repo, "40.local", "local", queue=None, base_ref=base,
        branch_ref=None, head_sha=None, rendezvous_remote="origin",
    )
    assert result.landed is True
    assert (repo_root / "local.txt").read_text() == "local\n"


# --------------------------------------------------------------------------- #
# worker execute: publish step
# --------------------------------------------------------------------------- #


def test_worker_execute_publishes_when_rendezvous_set(tmp_path: Path, monkeypatch) -> None:
    workspace, repo, repo_root, bare = _setup_manager_repo(tmp_path)
    monkeypatch.setattr(backends_mod, "require_backend", lambda name: _CommittingBackend())
    cfg = WorkerConfig(
        workspace=workspace, worker_id="w",
        manager_url="http://x", rendezvous_remote="origin",
    )
    base = canonical_head(repo_root)
    outcome = execute_work_order(
        cfg, _order(repo, "10.do", base), on_phase=lambda p: None, on_log=lambda s: None
    )
    assert outcome.landable is True
    assert outcome.branch_ref == "refs/heads/nightshift-wip/main/10.do"
    assert outcome.head_sha and outcome.head_sha in _ls_remote(bare, outcome.branch_ref)


def test_worker_execute_publish_failure_is_publish_failed(tmp_path: Path, monkeypatch) -> None:
    workspace, repo, repo_root, _bare = _setup_manager_repo(tmp_path)
    monkeypatch.setattr(backends_mod, "require_backend", lambda name: _CommittingBackend())
    cfg = WorkerConfig(
        workspace=workspace, worker_id="w",
        manager_url="http://x", rendezvous_remote="does-not-exist",
    )
    base = canonical_head(repo_root)
    outcome = execute_work_order(
        cfg, _order(repo, "10.do", base), on_phase=lambda p: None, on_log=lambda s: None
    )
    assert outcome.landable is False
    assert outcome.failure_kind == "publish_failed"
    assert outcome.branch_ref is None


def test_worker_execute_no_commit_publishes_nothing(tmp_path: Path, monkeypatch) -> None:
    workspace, repo, repo_root, bare = _setup_manager_repo(tmp_path)
    monkeypatch.setattr(backends_mod, "require_backend", lambda name: _NoopBackend())
    cfg = WorkerConfig(
        workspace=workspace, worker_id="w",
        manager_url="http://x", rendezvous_remote="origin",
    )
    base = canonical_head(repo_root)
    outcome = execute_work_order(
        cfg, _order(repo, "10.do", base), on_phase=lambda p: None, on_log=lambda s: None
    )
    assert outcome.landable is False
    assert outcome.branch_ref is None
    assert _ls_remote(bare, "refs/heads/nightshift-wip/main/10.do") == ""


def test_worker_execute_colocated_does_not_publish(tmp_path: Path, monkeypatch) -> None:
    workspace, repo, repo_root, bare = _setup_manager_repo(tmp_path)
    monkeypatch.setattr(backends_mod, "require_backend", lambda name: _CommittingBackend())
    cfg = WorkerConfig(  # no rendezvous_remote => co-located
        workspace=workspace, worker_id="w", manager_url="http://x",
    )
    base = canonical_head(repo_root)
    outcome = execute_work_order(
        cfg, _order(repo, "10.do", base), on_phase=lambda p: None, on_log=lambda s: None
    )
    assert outcome.landable is True
    assert outcome.branch_ref is None and outcome.head_sha is None
    assert _ls_remote(bare, "refs/heads/nightshift-wip/main/10.do") == ""


# --------------------------------------------------------------------------- #
# config parsing
# --------------------------------------------------------------------------- #


def test_worker_config_rendezvous_remote(tmp_path: Path, monkeypatch) -> None:
    build_workspace(tmp_path, repos=("longitude",), main_repo="longitude")
    monkeypatch.setenv("NIGHTSHIFT_MANAGER_URL", "http://m")
    monkeypatch.delenv("NIGHTSHIFT_RENDEZVOUS_REMOTE", raising=False)
    ns_dir = tmp_path / ".nightshift"
    ns_dir.mkdir(parents=True, exist_ok=True)
    (ns_dir / "worker.json").write_text(json.dumps({"rendezvous_remote": "rdv"}))
    assert load_worker_config(tmp_path).rendezvous_remote == "rdv"

    monkeypatch.setenv("NIGHTSHIFT_RENDEZVOUS_REMOTE", "envwins")
    assert load_worker_config(tmp_path).rendezvous_remote == "envwins"


def test_worker_config_rendezvous_remote_default_none(tmp_path: Path, monkeypatch) -> None:
    build_workspace(tmp_path, repos=("longitude",), main_repo="longitude")
    monkeypatch.setenv("NIGHTSHIFT_MANAGER_URL", "http://m")
    monkeypatch.delenv("NIGHTSHIFT_RENDEZVOUS_REMOTE", raising=False)
    assert load_worker_config(tmp_path).rendezvous_remote is None  # co-located default


def test_manager_config_rendezvous_remote(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("NIGHTSHIFT_RENDEZVOUS_REMOTE", raising=False)
    build_workspace(tmp_path, repos=("longitude",), main_repo="longitude")
    assert load_manager_config(tmp_path).rendezvous_remote == "origin"  # default

    monkeypatch.setenv("NIGHTSHIFT_RENDEZVOUS_REMOTE", "rdv")
    assert load_manager_config(tmp_path).rendezvous_remote == "rdv"


def test_manager_config_wip_ref_prefix(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("NIGHTSHIFT_WIP_REF_PREFIX", raising=False)
    build_workspace(
        tmp_path, repos=("longitude",), main_repo="longitude",
        config={"wip_ref_prefix": "acme/wip"},
    )
    # Top-level config key is honored.
    assert load_manager_config(tmp_path).wip_ref_prefix == "acme/wip"
    # Env wins over the config key.
    monkeypatch.setenv("NIGHTSHIFT_WIP_REF_PREFIX", "team/wip")
    assert load_manager_config(tmp_path).wip_ref_prefix == "team/wip"
    # An unsafe value falls back to the default rather than crashing the manager.
    monkeypatch.setenv("NIGHTSHIFT_WIP_REF_PREFIX", "bad prefix")
    assert load_manager_config(tmp_path).wip_ref_prefix == "nightshift-wip"


def test_manager_config_rendezvous_remote_explicit_null_disables(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("NIGHTSHIFT_RENDEZVOUS_REMOTE", raising=False)
    build_workspace(
        tmp_path, repos=("longitude",), main_repo="longitude",
        config={"rendezvous_remote": None},
    )
    assert load_manager_config(tmp_path).rendezvous_remote is None


# --------------------------------------------------------------------------- #
# make_pr override (effective landing mode)
# --------------------------------------------------------------------------- #


class _LoopClient:
    """Adapts the manager TestClient to the ManagerClient surface the loop uses."""

    def __init__(self, client: TestClient) -> None:
        self._c = client

    def checkin(self, worker_id, *, backend, queues, priorities, models=None, mcps=None, meta=None):
        return self._c.post(
            "/api/worker/checkin",
            json={"worker_id": worker_id, "backend": backend, "queues": queues,
                  "priorities": priorities, "models": models, "mcps": mcps, "meta": meta},
        ).json()

    def poll(self, worker_id, *, backend, queues, priorities, models=None, mcps=None, exclude_queues=None):
        return self._c.post(
            "/api/worker/poll",
            json={"worker_id": worker_id, "backend": backend, "queues": queues,
                  "priorities": priorities, "models": models, "mcps": mcps,
                  "exclude_queues": exclude_queues},
        ).json()

    def heartbeat(self, worker_id, *, lease_id=None, phase=None) -> None:
        self._c.post(
            "/api/worker/heartbeat",
            json={"worker_id": worker_id, "lease_id": lease_id, "phase": phase},
        )

    def post_events(self, run_id, events) -> None:
        self._c.post(f"/api/worker/runs/{run_id}/events", json={"events": events})

    def submit(self, run_id, payload) -> dict[str, Any]:
        return self._c.post(f"/api/worker/runs/{run_id}/submit", json=payload).json()


def _run_once_capturing_land_mode(tmp_path: Path, monkeypatch, brief: str) -> str | None:
    """Drive one poll->execute->submit and return the landing_mode land() saw."""
    workspace = build_workspace(tmp_path, tasks={"10.do": brief})
    monkeypatch.setattr(backends_mod, "require_backend", lambda name: _CommittingBackend())

    captured: dict[str, Any] = {}

    def spy_land(*args, **kwargs):
        captured["landing_mode"] = kwargs.get("landing_mode")
        return LandingResult(landed=True, sha="deadbeef")

    monkeypatch.setattr("nightshift.manager.app.land", spy_land)

    with TestClient(create_app(workspace, store=MemoryStore())) as tc:
        cfg = WorkerConfig(
            workspace=workspace, worker_id="w1", manager_url="http://test",
        )
        loop = WorkerLoop(cfg, _LoopClient(tc), LocalStore(workspace))
        loop.checkin()
        assert loop.run_once() is True
    return captured.get("landing_mode")


def test_make_pr_forces_pr_mode(tmp_path: Path, monkeypatch) -> None:
    # Manager default landing_mode is "none"; make_pr: true must win.
    mode = _run_once_capturing_land_mode(
        tmp_path, monkeypatch, "---\nmodel: auto\nmake_pr: true\n---\nDo it."
    )
    assert mode == "pr"


def test_absent_make_pr_defers_to_manager(tmp_path: Path, monkeypatch) -> None:
    mode = _run_once_capturing_land_mode(
        tmp_path, monkeypatch, "---\nmodel: auto\n---\nDo it."
    )
    assert mode == "none"  # the manager default; make_pr never forces a squash
