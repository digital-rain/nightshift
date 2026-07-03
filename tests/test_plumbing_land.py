"""Phase 6 verification (git greenfield §10) — the plumbing land's invariants.

* The property test: after ANY ``land()`` outcome, ``git status --porcelain``
  in the target repo is unchanged — landing is a ref operation and can never
  regress to working-tree merges.
* The CAS race: concurrent lands (and an operator commit racing the CAS)
  never lose an update — the loser re-produces from the fresh tip.
* RepoLock: re-entry raises, repos don't serialize with each other, and the
  primitives assert the orchestration lock is held.
* The grep gate: the live landing/sync modules contain no ``reset --hard``,
  ``merge --squash``, or ``stash`` invocations.
"""

from __future__ import annotations

import inspect
import re
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from _workspace import build_workspace, git_commit_all
from nightshift.git import landing as git_landing_mod
from nightshift.git import locks as locks_mod
from nightshift.git import refs as refs_mod
from nightshift.git import squash as squash_mod
from nightshift.git import sync as sync_mod
from nightshift.git import transport as transport_mod
from nightshift.git.landing import (
    ProduceResult,
    RepoContext,
    attempt_trailer_line,
    integrate_and_push,
    push_main,
    squash_produce,
)
from nightshift.git.locks import repo_lock
from nightshift.git.refs import replay_commit
from nightshift.git.runner import GitResult, GitRunner
from nightshift.git.worktrees import setup_worktree, worktree_branch
from nightshift.lifecycle import LandingMode, LandKind, LandOutcome
from nightshift.manager import landing as manager_landing_mod
from nightshift.manager.landing import adopt_or_nothing, canonical_head, land


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def _init_repo(tmp_path: Path) -> tuple[Path, str, Path]:
    workspace = build_workspace(tmp_path, repos=("longitude",), main_repo="longitude")
    repo = "longitude"
    repo_root = workspace / repo
    (repo_root / "file.txt").write_text("base\n")
    git_commit_all(repo_root, "add file")
    return workspace, repo, repo_root


def _make_branch_commit(
    workspace: Path, repo: str, task: str, *, path: str, content: str
) -> None:
    wt = setup_worktree(workspace, repo, task)
    (wt / path).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"work {task}"], cwd=wt, check=True, capture_output=True
    )


# --------------------------------------------------------------------------- #
# §10 property test — `git status --porcelain` is invariant across land()
# --------------------------------------------------------------------------- #


def _dirty_unstaged(repo_root: Path) -> None:
    (repo_root / "file.txt").write_text("operator mid-edit\n")


def _dirty_staged(repo_root: Path) -> None:
    (repo_root / "file.txt").write_text("operator staged edit\n")
    _git(repo_root, "add", "file.txt")


def _dirty_untracked(repo_root: Path) -> None:
    (repo_root / "stray.txt").write_text("operator scratch file\n")


_DIRTY: dict[str, Callable[[Path], None]] = {
    "unstaged": _dirty_unstaged,
    "staged": _dirty_staged,
    "untracked": _dirty_untracked,
}

# A scenario seeds one reachable outcome kind and returns the land invocation.
# The dirty state is applied AFTER seeding (it is the operator's live WIP at
# land time); `checkout_behind` picks its overlap target from the dirty kind.
_Invoke = Callable[[], LandOutcome]


def _scenario_landed(
    workspace: Path, repo: str, repo_root: Path, dirty: str, monkeypatch
) -> tuple[_Invoke, LandKind]:
    _make_branch_commit(workspace, repo, "10.add", path="new.txt", content="hello\n")
    base = canonical_head(repo_root)
    return (
        lambda: land(workspace, repo, "10.add", "add new file", queue=None, base_ref=base),
        LandKind.LANDED,
    )


def _scenario_no_changes(
    workspace: Path, repo: str, repo_root: Path, dirty: str, monkeypatch
) -> tuple[_Invoke, LandKind]:
    base = canonical_head(repo_root)
    return (
        lambda: adopt_or_nothing(
            workspace, repo, "10.idle", "nothing to land", queue=None, base_ref=base
        ),
        LandKind.NO_CHANGES,
    )


def _scenario_adopted(
    workspace: Path, repo: str, repo_root: Path, dirty: str, monkeypatch
) -> tuple[_Invoke, LandKind]:
    base = canonical_head(repo_root)
    (repo_root / "adopted.txt").write_text("agent landed\n")
    _git(repo_root, "add", "adopted.txt")
    _git(repo_root, "commit", "-m", "feat: agent landed on main")
    return (
        lambda: land(workspace, repo, "10.adopt", "adopt land", queue=None, base_ref=base),
        LandKind.ADOPTED,
    )


def _scenario_conflict(
    workspace: Path, repo: str, repo_root: Path, dirty: str, monkeypatch
) -> tuple[_Invoke, LandKind]:
    base = canonical_head(repo_root)
    _make_branch_commit(
        workspace, repo, "20.edit", path="file.txt", content="branch change\n"
    )
    (repo_root / "file.txt").write_text("main change\n")
    _git(repo_root, "commit", "-am", "main edits file")
    return (
        lambda: land(workspace, repo, "20.edit", "edit file", queue=None, base_ref=base),
        LandKind.CONFLICT,
    )


def _scenario_checkout_behind(
    workspace: Path, repo: str, repo_root: Path, dirty: str, monkeypatch
) -> tuple[_Invoke, LandKind]:
    # The land must change exactly what the dirty state dirties: an untracked
    # file in the way of a new path, or WIP on a file the land rewrites.
    path = "stray.txt" if dirty == "untracked" else "file.txt"
    _make_branch_commit(workspace, repo, "20.edit", path=path, content="branch change\n")
    base = canonical_head(repo_root)
    return (
        lambda: land(workspace, repo, "20.edit", "edit file", queue=None, base_ref=base),
        LandKind.CHECKOUT_BEHIND,
    )


def _scenario_push_rejected(
    workspace: Path, repo: str, repo_root: Path, dirty: str, monkeypatch
) -> tuple[_Invoke, LandKind]:
    _make_branch_commit(workspace, repo, "10.add", path="new.txt", content="hello\n")
    base = canonical_head(repo_root)

    def rejected(*_args: object) -> GitResult:
        return GitResult(
            argv=("git", "push"), returncode=1,
            stdout="", stderr="non-fast-forward (simulated)",
        )

    monkeypatch.setattr(git_landing_mod, "push_main", rejected)
    return (
        lambda: land(
            workspace, repo, "10.add", "add new file", queue=None, base_ref=base,
            landing_mode="push",
        ),
        LandKind.PUSH_REJECTED,
    )


_SCENARIOS = {
    "landed": _scenario_landed,
    "no_changes": _scenario_no_changes,
    "adopted": _scenario_adopted,
    "conflict": _scenario_conflict,
    "checkout_behind": _scenario_checkout_behind,
    "push_rejected": _scenario_push_rejected,
}


@pytest.mark.parametrize("dirty", sorted(_DIRTY))
@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_porcelain_status_invariant_across_every_land_outcome(
    tmp_path: Path, monkeypatch, scenario: str, dirty: str
) -> None:
    """git greenfield §10: whatever land() returns, the operator's
    ``git status --porcelain`` is byte-identical before and after — the land
    is a ref operation and never a working-tree merge."""
    workspace, repo, repo_root = _init_repo(tmp_path)
    invoke, expected = _SCENARIOS[scenario](workspace, repo, repo_root, dirty, monkeypatch)
    _DIRTY[dirty](repo_root)

    before = _git(repo_root, "status", "--porcelain")
    result = invoke()
    assert result.kind is expected, result
    assert _git(repo_root, "status", "--porcelain") == before


# --------------------------------------------------------------------------- #
# CAS race — no lost updates
# --------------------------------------------------------------------------- #


def test_cas_race_two_concurrent_lands_lose_nothing(tmp_path: Path) -> None:
    """Two threads race integrate_and_push on ONE repo: the RepoLock serializes
    them, the second produces from the first's tip, and final main contains
    both commits."""
    workspace, repo, repo_root = _init_repo(tmp_path)
    for task, fname in (("10.a", "file_a.py"), ("20.b", "file_b.py")):
        wt = setup_worktree(workspace, repo, task)
        (wt / fname).write_text("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"work {task}"],
            cwd=wt, check=True, capture_output=True,
        )

    barrier = threading.Barrier(2)
    outcomes: dict[str, LandOutcome] = {}

    def _race(task: str, title: str) -> None:
        barrier.wait()  # force maximal overlap
        outcomes[task] = integrate_and_push(
            RepoContext(workspace=workspace, repo=repo),
            squash_produce(repo_root, worktree_branch(task), title),
            mode=LandingMode.NONE,
        )

    threads = [
        threading.Thread(target=_race, args=("10.a", "task a")),
        threading.Thread(target=_race, args=("20.b", "task b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes["10.a"].kind is LandKind.LANDED, outcomes["10.a"]
    assert outcomes["20.b"].kind is LandKind.LANDED, outcomes["20.b"]
    assert (repo_root / "file_a.py").exists()
    assert (repo_root / "file_b.py").exists()
    log = _git(repo_root, "log", "--oneline")
    assert "task: task a" in log
    assert "task: task b" in log


def test_cas_rejection_reproduces_from_the_moved_tip(tmp_path: Path) -> None:
    """An operator commit sneaking onto main between produce and CAS makes the
    ``update-ref`` CAS fail; the loop re-produces from the fresh tip and both
    commits survive — no lost update, no bookkeeping."""
    workspace, repo, repo_root = _init_repo(tmp_path)
    _make_branch_commit(workspace, repo, "10.add", path="new.txt", content="hello\n")
    produce = squash_produce(repo_root, worktree_branch("10.add"), "add new file")
    calls = {"n": 0}

    def racing_produce(base: str) -> ProduceResult:
        result = produce(base)
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulated racer: main moves AFTER the commit object is built but
            # BEFORE the CAS (an operator committing directly to main).
            (repo_root / "operator.txt").write_text("operator work\n")
            _git(repo_root, "add", "operator.txt")
            _git(repo_root, "commit", "-m", "operator commit")
        return result

    outcome = integrate_and_push(
        RepoContext(workspace=workspace, repo=repo),
        racing_produce,
        mode=LandingMode.NONE,
    )
    assert outcome.kind is LandKind.LANDED, outcome
    assert calls["n"] == 2  # first CAS lost the race → re-produced
    log = _git(repo_root, "log", "--oneline")
    assert "operator commit" in log
    assert "task: add new file" in log
    assert (repo_root / "new.txt").exists()
    assert (repo_root / "operator.txt").exists()


# --------------------------------------------------------------------------- #
# replay_commit: the land idempotency trailer on the resolve (cherry) path
# --------------------------------------------------------------------------- #


def test_replay_commit_appends_the_trailer_exactly_once(tmp_path: Path) -> None:
    """``replay_commit(..., extra_trailer=...)`` stamps the land idempotency
    trailer on the replayed commit as a git-parseable trailer — and never
    duplicates it when the source message already carries it (a resolve
    re-run replaying its own earlier product)."""
    workspace, repo, repo_root = _init_repo(tmp_path)
    _make_branch_commit(workspace, repo, "10.x", path="r.txt", content="resolved\n")
    git = GitRunner(repo_root)
    base = canonical_head(repo_root)
    source = git.out("rev-parse", worktree_branch("10.x"))
    assert source is not None
    trailer = attempt_trailer_line("attempt-1")

    replayed = replay_commit(git, base, source, extra_trailer=trailer)
    assert replayed.sha not in (None, base)
    message = git.out("log", "-1", "--format=%B", replayed.sha) or ""
    assert "work 10.x" in message          # the original message survives
    assert message.count(trailer) == 1
    # git's own trailer parser sees it (a real trailer block, not body text).
    parsed = git.out(
        "log", "-1", "--format=%(trailers:key=Nightshift-Attempt,valueonly)",
        replayed.sha,
    )
    assert (parsed or "").strip() == "attempt-1"

    # Replaying a commit that ALREADY carries the trailer must not stack a
    # second copy.
    again = replay_commit(git, base, replayed.sha, extra_trailer=trailer)
    assert again.sha not in (None, base)
    message2 = git.out("log", "-1", "--format=%B", again.sha) or ""
    assert message2.count(trailer) == 1


# --------------------------------------------------------------------------- #
# RepoLock
# --------------------------------------------------------------------------- #


def test_repo_lock_reentry_raises(tmp_path: Path) -> None:
    lock = repo_lock(tmp_path, "repo-a")
    with lock:
        with pytest.raises(RuntimeError, match="re-entered"):
            lock.acquire()
    # The failed re-entry did not poison the lock.
    with lock:
        assert lock.is_held_by_current_thread()
    assert not lock.is_held_by_current_thread()


def test_repo_lock_registry_keys_per_repo(tmp_path: Path) -> None:
    """Different repos get different lock objects (lands never serialize across
    repos); the same repo always gets the same object."""
    a = repo_lock(tmp_path, "repo-a")
    b = repo_lock(tmp_path, "repo-b")
    assert a is not b
    assert repo_lock(tmp_path, "repo-a") is a
    # Structurally non-serializing: holding one, the other acquires instantly
    # on the same thread (which would deadlock/raise on a shared lock).
    with a, b:
        assert a.is_held_by_current_thread()
        assert b.is_held_by_current_thread()


def test_repo_lock_not_held_by_other_threads(tmp_path: Path) -> None:
    lock = repo_lock(tmp_path, "repo-a")
    seen: dict[str, bool] = {}

    def probe() -> None:
        seen["held"] = lock.is_held_by_current_thread()

    with lock:
        t = threading.Thread(target=probe)
        t.start()
        t.join()
    assert seen["held"] is False


def test_primitives_assert_the_lock_is_held(tmp_path: Path) -> None:
    """A primitive called without the orchestration lock is a loud failure,
    not a silent race."""
    workspace, repo, _repo_root = _init_repo(tmp_path)
    with pytest.raises(AssertionError, match="RepoLock"):
        push_main(workspace, repo, "origin", "deadbeef")


# --------------------------------------------------------------------------- #
# Grep gate — no working-tree merge machinery in the live landing/sync paths
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "module",
    [
        git_landing_mod,
        locks_mod,
        refs_mod,
        squash_mod,
        sync_mod,
        transport_mod,
        manager_landing_mod,
    ],
    ids=lambda m: m.__name__,
)
def test_no_working_tree_merge_or_stash_invocations(module: object) -> None:
    """The landing/sync code owned by the plumbing land never invokes
    ``reset --hard``, ``merge --squash``, or ``stash`` (prose mentions are
    fine; invocation argument tuples are not). Whitespace is normalized so a
    formatter splitting an argument list across lines can't disable the gate."""
    source = re.sub(r"\s+", " ", inspect.getsource(module))
    assert '"reset", "--hard"' not in source
    assert '"merge", "--squash"' not in source
    assert '"stash"' not in source
