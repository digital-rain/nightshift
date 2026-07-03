"""Local git-seam tests: worktree lifecycle, squash-to-main landing, LOC
accounting, landing blockers / preconditions, the workspace lock, and
content-store queue commits.

Relocated in Phase 9 from the legacy ``test_run_local.py`` suite to the real
module homes (``git/worktrees``, ``git/squash``, ``git/store``, ``git/locks``,
``preflight``); behavior unchanged.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from _workspace import build_workspace, git, git_commit_all
from nightshift.git.squash import compute_code_loc, squash_to_main
from nightshift.git.store import commit_queue_state
from nightshift.git.worktrees import (
    setup_worktree,
    teardown_worktree,
    worktree_branch,
    worktree_dir,
)
from nightshift.preflight import (
    acquire_lock,
    check_preconditions,
    enough_free_disk,
    landing_blockers,
    porcelain_path,
    run_interruptible,
)
from nightshift.repos import DEFAULT_TASKS_REPO
from nightshift.task_files import materialize_brief


REPO = "longitude"


def _full(
    tmp_path: Path,
    *,
    tasks: dict[str, str] | None = None,
) -> tuple[Path, Path, Path]:
    """Build a full two-root workspace with the default ``longitude`` target repo."""
    workspace = build_workspace(tmp_path, tasks=tasks)
    return workspace, workspace / DEFAULT_TASKS_REPO, workspace / REPO


def _repo(tmp_path: Path) -> Path:
    """A target repo (``<workspace>/longitude``) for pure git-churn tests."""
    workspace = build_workspace(tmp_path)
    return workspace / REPO


def _commit_files(repo: Path, files: dict[str, str]) -> str:
    """Write ``files`` (path → content), commit them, and return the short sha."""
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "work")
    return git(repo, "rev-parse", "--short", "HEAD")


def _commit_work_on_branch(
    workspace: Path, repo: str, task: str, *, queue: str | None = None
) -> Path:
    """Cut a worktree for ``task`` and add a single committed file on its branch."""
    worktree = setup_worktree(workspace, repo, task, queue=queue)
    (worktree / "new_file.py").write_text("print('hello')\n")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "work")
    return worktree


# --------------------------------------------------------------------------- #
# Worktree lifecycle + squash-to-main landing (target repo)
# --------------------------------------------------------------------------- #


def test_setup_worktree_creates_dir_and_symlinks(tmp_path: Path) -> None:
    workspace, _tasks_root, repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    (repo_root / ".venv").mkdir()
    (repo_root / ".venv/bin").mkdir(parents=True)
    (repo_root / "services/dashboard_ui/node_modules").mkdir(parents=True)

    worktree = setup_worktree(workspace, REPO, "10.hello")
    assert worktree.exists()
    # Worktrees live OUTSIDE the target repo, under the workspace.
    assert str(worktree).startswith(str(workspace / ".worktrees" / REPO))
    assert (worktree / ".venv").is_symlink()
    assert (worktree / "services/dashboard_ui/node_modules").is_symlink()


def test_squash_to_main_produces_single_commit(tmp_path: Path) -> None:
    workspace, _tasks_root, repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})

    worktree = setup_worktree(workspace, REPO, "10.hello")
    (worktree / "new_file.py").write_text("print('hello')\n")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "work1")
    (worktree / "another.py").write_text("x = 1\n")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "work2")

    sha, detail, recoverable = squash_to_main(workspace, REPO, "10.hello", "hello world")
    assert sha is not None
    assert detail == ""
    assert recoverable is False

    log = git(repo_root, "log", "--oneline")
    assert "task: hello world" in log
    lines = [line for line in log.strip().splitlines() if line.strip()]
    assert len(lines) == 2


def test_compute_code_loc_counts_code_only(tmp_path: Path) -> None:
    """compute_code_loc counts added/removed code lines and excludes comments,
    blank lines, docs, and build/lock files (the categories the spec drops)."""
    repo_root = _repo(tmp_path)
    sha = _commit_files(
        repo_root,
        {
            # 3 code lines; a comment and a blank line are dropped.
            "mod.py": "import os\n# a comment\nx = 1\n\nprint(x)\n",
            # JS: 1 code line, 1 // comment dropped.
            "app.js": "// header\nconst y = 2;\n",
            # Excluded entirely: docs and a lockfile.
            "README.md": "# Title\n\nProse here.\n",
            "uv.lock": "lots of generated content\n",
        },
    )

    assert compute_code_loc(repo_root, sha) == 4


def test_compute_code_loc_counts_removed_lines(tmp_path: Path) -> None:
    """Churn counts removals as well as additions."""
    repo_root = _repo(tmp_path)
    _commit_files(repo_root, {"mod.py": "a = 1\nb = 2\nc = 3\n"})
    sha = _commit_files(repo_root, {"mod.py": "a = 1\n"})

    # Two lines removed (b, c); nothing added.
    assert compute_code_loc(repo_root, sha) == 2


def test_compute_code_loc_zero_for_docs_only_commit(tmp_path: Path) -> None:
    """A commit touching only docs/build files yields zero code churn."""
    repo_root = _repo(tmp_path)
    sha = _commit_files(
        repo_root,
        {"docs/guide.md": "# Guide\n", "BUILD.bazel": "py_library(name = 'x')\n"},
    )

    assert compute_code_loc(repo_root, sha) == 0


def test_compute_code_loc_bad_sha_returns_zero(tmp_path: Path) -> None:
    """A git error (unknown sha) degrades to 0 rather than raising."""
    repo_root = _repo(tmp_path)

    assert compute_code_loc(repo_root, "deadbeef") == 0


def test_compute_code_loc_excludes_output_dirs(tmp_path: Path) -> None:
    """Files under `dist/` and `build/` never count as code, even when the suffix
    would otherwise be code (a built `dist/*.js` bundle, a `build/*.ts`)."""
    repo_root = _repo(tmp_path)
    sha = _commit_files(
        repo_root,
        {
            "real.py": "x = 1\ny = 2\n",
            "services/ui/dist/bundle.js": "const z = 3;\n",
            "build/out.ts": "const w = 4;\n",
        },
    )

    assert compute_code_loc(repo_root, sha) == 2


def test_landed_loc_matches_squash_commit_after_intra_task_churn(
    tmp_path: Path,
) -> None:
    """The LOC figure a task lands with is the churn of its *squash commit* on
    ``main`` — the same metric the Stats backfill reconstructs from a record's
    ``commit_sha``. A task that writes 3 lines then drops 2 within its branch
    lands a net diff of 1 code line, so the figure is 1 (not the 5-line
    intra-task churn a branch-history sum would report)."""
    workspace, _tasks_root, repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})

    worktree = setup_worktree(workspace, REPO, "10.hello")
    (worktree / "mod.py").write_text("a = 1\nb = 2\nc = 3\n")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "add three")
    (worktree / "mod.py").write_text("a = 1\n")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "drop two")

    sha, _detail, _recoverable = squash_to_main(workspace, REPO, "10.hello", "hello world")
    assert sha is not None

    # Net squash diff: 1 added code line — captured at land time and
    # recoverable from the sha, so the two agree.
    assert compute_code_loc(repo_root, sha) == 1


def test_squash_to_main_lands_over_dirty_code_wip(tmp_path: Path) -> None:
    """Tracked operator code WIP on the target repo's main never blocks a land:
    the plumbing pipeline moves the ref without touching the working tree, and
    the checkout advance carries non-overlapping WIP forward verbatim — no
    stash involved. Briefs live in the separate content store, so every tracked
    change here is genuine operator code WIP."""
    workspace, _tasks_root, repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    wip = repo_root / "app.py"
    wip.write_text("x = 1\n")
    git_commit_all(repo_root, "add app.py")
    _commit_work_on_branch(workspace, REPO, "10.hello")

    # Dirty main on a tracked code file (mirrors a developer mid-edit).
    dirty = wip.read_text() + "\n# local edit\n"
    wip.write_text(dirty)

    sha, detail, recoverable = squash_to_main(workspace, REPO, "10.hello", "hello world")
    assert sha is not None
    assert detail == ""
    assert recoverable is False

    # The task landed, the checkout advanced with it, and the operator's WIP is
    # carried forward verbatim (and NOT part of the task commit).
    assert (repo_root / "new_file.py").exists()
    assert wip.read_text() == dirty
    committed = git(repo_root, "show", "--name-only", "--format=", "HEAD")
    assert "app.py" not in committed
    # No stash machinery is involved at all.
    assert git(repo_root, "stash", "list").strip() == ""

    teardown_worktree(workspace, REPO, "10.hello")


def test_squash_to_main_overlapping_wip_leaves_checkout_behind(tmp_path: Path) -> None:
    """Uncommitted operator edits to a file the land also changes no longer
    block (and are never stashed): the land succeeds on the ref, the checkout
    advance refuses rather than clobber the WIP, and the detail says so — the
    checkout is left behind main with the operator's work exactly as it was."""
    workspace, _tasks_root, repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    target = repo_root / "app.py"
    target.write_text("base\n")
    git_commit_all(repo_root, "add app.py")
    head_before = git(repo_root, "rev-parse", "HEAD")

    # Branch edits an existing tracked file.
    worktree = setup_worktree(workspace, REPO, "10.hello")
    (worktree / "app.py").write_text('{"branch": true}\n')
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "branch edit")

    # Operator has uncommitted edits to the SAME file → the advance must refuse.
    target.write_text('{"operator": true}\n')

    sha, detail, recoverable = squash_to_main(workspace, REPO, "10.hello", "hello world")
    assert sha is not None  # the land happened — main's ref moved
    assert recoverable is False
    assert "behind main" in detail

    # main is authoritative; the checkout stayed put with the WIP untouched.
    assert git(repo_root, "rev-parse", "refs/heads/main") != head_before
    assert git(repo_root, "rev-parse", "HEAD") == head_before
    assert target.read_text() == '{"operator": true}\n'
    assert git(repo_root, "stash", "list").strip() == ""

    teardown_worktree(workspace, REPO, "10.hello")


def test_squash_to_main_lands_over_dirty_queue_state(tmp_path: Path) -> None:
    """Queue state never blocks (or contaminates) a land: a dirty alternate-queue
    brief and a live run record live in the *separate* content store, so a squash
    on the target repo proceeds and stashes nothing — the two roots are isolated.
    """
    workspace, tasks_root, repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})

    # Concurrent queue activity in the content store: an edited brief + a live
    # (gitignored) run record under an alternate queue.
    playlist = tasks_root / "nightshift"
    playlist.mkdir(parents=True, exist_ok=True)
    (playlist / "config.json").write_text('{"order": ["queue-view"], "repo": "longitude"}\n')
    (playlist / "queue-view.md").write_text("---\ntitle: Queue view\n---\nEdited.\n")
    runs = playlist / "runs" / "2026-01-01T00-00-00Z-abc"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "run.json").write_text('{"id": "live", "in_progress": true}\n')

    _commit_work_on_branch(workspace, REPO, "10.hello")

    sha, detail, recoverable = squash_to_main(workspace, REPO, "10.hello", "hello world")
    assert sha is not None
    assert detail == ""
    assert (repo_root / "new_file.py").exists()
    # Nothing was stashed in the target repo — the content store is a separate
    # repo and is never a landing blocker.
    assert git(repo_root, "stash", "list").strip() == ""
    # The content store's live state is untouched by the target-repo land.
    assert (runs / "run.json").read_text() == '{"id": "live", "in_progress": true}\n'
    assert (playlist / "queue-view.md").read_text() == (
        "---\ntitle: Queue view\n---\nEdited.\n"
    )

    teardown_worktree(workspace, REPO, "10.hello")


def test_squash_to_main_reports_content_conflict_unrecoverable(tmp_path: Path) -> None:
    """When the branch and main make overlapping edits to the same file, the
    squash hits a real 3-way conflict. That is NOT retry-recoverable (re-running
    the same merge fails identically), so squash_to_main must say so and name the
    conflicting file rather than dumping git's rerere bookkeeping."""
    workspace, _tasks_root, repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    target = repo_root / "app.py"
    target.write_text("base\n")
    git_commit_all(repo_root, "add app.py")

    # Branch edits an existing tracked file.
    worktree = setup_worktree(workspace, REPO, "10.hello")
    (worktree / "app.py").write_text('{"branch": true}\n')
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "branch edit")

    # main edits the SAME file divergently and commits.
    target.write_text('{"main": true}\n')
    git(repo_root, "add", ".")
    git(repo_root, "commit", "-m", "main edit")
    head_before = git(repo_root, "rev-parse", "HEAD")

    sha, detail, recoverable = squash_to_main(workspace, REPO, "10.hello", "hello world")
    assert sha is None
    assert recoverable is False
    assert "conflict" in detail.lower()
    assert "app.py" in detail

    # The failed merge was cleaned up: HEAD unchanged, no half-merged tree.
    assert git(repo_root, "rev-parse", "HEAD") == head_before
    assert target.read_text() == '{"main": true}\n'

    teardown_worktree(workspace, REPO, "10.hello")


def test_landing_blockers_reports_code_never_queue_state(tmp_path: Path) -> None:
    """`landing_blockers` reports tracked code changes in the target repo but
    never queue/brief churn — briefs live in the separate content store and can
    never appear in the target repo's tree."""
    workspace, tasks_root, repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    code = repo_root / "app.py"
    code.write_text("x = 1\n")
    git_commit_all(repo_root, "add app.py")

    # Edit a brief in the content store (a different repo) AND dirty code in the
    # target repo.
    (tasks_root / "main" / "10.hello.md").write_text("Edited brief.")
    code.write_text("x = 2\n")

    paths = [porcelain_path(line) for line in landing_blockers(repo_root)]
    assert any("app.py" in p for p in paths)
    # The brief lives in the content store, so it never shows up as a blocker.
    assert all("10.hello.md" not in p for p in paths)
    assert all(not p.startswith("main/") for p in paths)


def _prep_preconditions(repo_root: Path, monkeypatch) -> None:
    """Satisfy every non-dirty-tree check so check_preconditions reaches (and
    passes) the dirty-tree gate: fake the claude binary + API key and a trivial
    `just validate` in the target repo."""
    monkeypatch.setattr("nightshift.preflight.shutil.which", lambda _name: "/bin/claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    (repo_root / "justfile").write_text("validate:\n\t@true\n")


def test_check_preconditions_ignores_dirty_queue_state(tmp_path: Path, monkeypatch) -> None:
    """A dirty brief in the content store never blocks a run's preconditions —
    the pre-flight only inspects the target repo (a separate repo)."""
    workspace, tasks_root, repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    _prep_preconditions(repo_root, monkeypatch)

    (tasks_root / "main" / "10.hello.md").write_text("Edited brief.")
    # Should not raise: content-store churn never blocks.
    check_preconditions(workspace, REPO)


def test_check_preconditions_notice_on_code_wip(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Tracked code WIP never blocks a run (lands don't touch the working tree)
    but earns an up-front notice about a possible checkout left behind main."""
    workspace, _tasks_root, repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    _prep_preconditions(repo_root, monkeypatch)

    # Dirty a tracked code file in the target repo.
    code = repo_root / "app.py"
    code.write_text("x = 1\n")
    git_commit_all(repo_root, "add app.py")
    code.write_text("x = 2\n")

    check_preconditions(workspace, REPO)  # notice, never an exit
    assert "behind main" in capsys.readouterr().out


def test_teardown_worktree_removes_dir_and_branch(tmp_path: Path) -> None:
    workspace, _tasks_root, repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})

    worktree = setup_worktree(workspace, REPO, "10.hello")
    assert worktree.exists()

    teardown_worktree(workspace, REPO, "10.hello")
    assert not worktree.exists()
    assert "task-local/main/10.hello" not in git(repo_root, "branch")


def test_enough_free_disk_true_for_low_threshold(tmp_path: Path) -> None:
    # A live filesystem always has well over 0% free.
    assert enough_free_disk(tmp_path, min_free_pct=0.0) is True


def test_enough_free_disk_false_for_impossible_threshold(tmp_path: Path) -> None:
    # No filesystem can have > 100% free, so this must fail the guard.
    assert enough_free_disk(tmp_path, min_free_pct=100.001) is False


def test_acquire_lock_blocks_second_instance(tmp_path: Path) -> None:
    workspace = tmp_path

    fd1 = acquire_lock(workspace)
    assert fd1 >= 0

    with pytest.raises(SystemExit):
        acquire_lock(workspace)

    os.close(fd1)


def test_run_interruptible_kills_on_abort(tmp_path: Path) -> None:
    """run_interruptible terminates a long-running command promptly once
    should_abort fires, returning a non-zero (aborted) result."""
    aborted = {"v": False}

    def should_abort():
        return "stopped" if aborted["v"] else None

    # Flip the abort flag from a side thread shortly after the sleep starts.
    def flip():
        time.sleep(0.2)
        aborted["v"] = True

    t = threading.Thread(target=flip)
    t.start()
    start = time.monotonic()
    result = run_interruptible(
        ["sleep", "30"], cwd=tmp_path, env=None, should_abort=should_abort,
    )
    elapsed = time.monotonic() - start
    t.join()
    assert elapsed < 10  # killed, not waited out
    assert result.returncode != 0


# --------------------------------------------------------------------------- #
# commit_queue_state — commit the queue definition in the CONTENT STORE
# --------------------------------------------------------------------------- #


def test_commit_queue_state_commits_untracked_task(tmp_path: Path) -> None:
    """A task added through the UI (written to the content store, never committed)
    is snapshotted *in the content store* so it lands in that repo's HEAD. The
    target repo receives nothing: briefs/queue config live solely in the content
    store, and the repo only ever takes the implementation squash on main."""
    workspace, tasks_root, repo_root = _full(tmp_path)
    repo_head_before = git(repo_root, "rev-parse", "HEAD")

    # UI "+ Add Task": a brand-new (untracked) brief + an order bump, in the store.
    (tasks_root / "main" / "new-task.md").write_text("---\ntitle: New\n---\nDo it.\n")
    (tasks_root / "main" / "config.json").write_text(
        '{"order": ["new-task"], "repo": "longitude"}\n'
    )

    sha = commit_queue_state(tasks_root)
    assert sha  # a commit was made in the content store

    tracked = git(tasks_root, "ls-files", "main")
    assert "main/new-task.md" in tracked
    assert "main/config.json" in tracked
    # The queue-definition files are now clean in the content store.
    assert git(tasks_root, "status", "--porcelain", "--", "main").strip() == ""
    # The target repo received NONE of it — no brief, no queue config, no commit.
    repo_tracked = git(repo_root, "ls-files")
    assert "new-task.md" not in repo_tracked
    assert "config.json" not in repo_tracked
    assert "main/" not in repo_tracked
    assert git(repo_root, "status", "--porcelain").strip() == ""
    assert git(repo_root, "rev-parse", "HEAD") == repo_head_before


def test_commit_queue_state_noop_when_clean(tmp_path: Path) -> None:
    """With nothing to commit, the call is a no-op (no empty commit)."""
    workspace, tasks_root, _repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    head_before = git(tasks_root, "rev-parse", "HEAD")

    assert commit_queue_state(tasks_root) is None

    assert git(tasks_root, "rev-parse", "HEAD") == head_before


def test_commit_queue_state_delivers_playlist_task_via_scratch(tmp_path: Path) -> None:
    """An alternate-queue brief lives only in the content store and is delivered
    to the worker via a run-scratch file OUTSIDE the worktree — it is never
    snapshotted into the target repo. A worktree cut from the repo's HEAD
    therefore never contains the brief; only the implementation squash ever
    reaches the target repo."""
    workspace, tasks_root, _repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})

    playlist = tasks_root / "nightshift"
    playlist.mkdir(parents=True, exist_ok=True)
    (playlist / "config.json").write_text('{"order": ["queue-view"], "repo": "longitude"}\n')
    (playlist / "queue-view.md").write_text("---\ntitle: Queue view\n---\nShow it.\n")

    # The queue state is committed in the content store (not the target repo).
    assert commit_queue_state(tasks_root, "nightshift")

    # A worktree branched from the target repo's HEAD (as a run would cut it) does
    # NOT contain the brief — it never enters the repo.
    worktree = setup_worktree(workspace, REPO, "queue-view", queue="nightshift")
    try:
        assert not (worktree / "nightshift").exists()
        assert not (worktree / "nightshift" / "queue-view.md").exists()
        # Instead the brief is delivered via a run-scratch file outside the worktree.
        scratch = materialize_brief(
            workspace, REPO, "queue-view", "Show it.", queue="nightshift"
        )
        assert scratch.exists()
        assert str(scratch).startswith(str(workspace / ".worktrees"))
        assert worktree not in scratch.parents
        assert scratch.read_text() == "Show it.\n"
    finally:
        teardown_worktree(workspace, REPO, "queue-view", queue="nightshift")


def test_commit_queue_state_excludes_run_records(tmp_path: Path) -> None:
    """Committing the content store commits queue *definition* only — never the
    queue's run records, which are gitignored runtime state in the store."""
    workspace, tasks_root, _repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})

    playlist = tasks_root / "nightshift"
    playlist.mkdir(parents=True, exist_ok=True)
    (playlist / "config.json").write_text('{"order": []}\n')
    # A live run record under the queue's runs/ (runtime state).
    runs = playlist / "runs" / "2026-01-01T00-00-00Z-abc"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "run.json").write_text('{"id": "live", "in_progress": true}\n')
    # The UI adds a new brief — concurrently.
    (playlist / "new-task.md").write_text("---\ntitle: New\n---\nGo.\n")

    sha = commit_queue_state(tasks_root, "nightshift")
    assert sha

    committed = git(tasks_root, "show", "--name-only", "--format=", "HEAD")
    assert "nightshift/new-task.md" in committed
    assert "run.json" not in committed
    # The run record is gitignored runtime state — not committed, left on disk.
    assert "nightshift/runs/" not in git(tasks_root, "ls-files")
    assert (runs / "run.json").exists()


def test_commit_queue_state_main_scope_excludes_playlist_md(tmp_path: Path) -> None:
    """A main-queue commit commits only the ``main`` queue dir — it must not
    sweep an unrelated alternate queue's edited brief (queue-scoping)."""
    workspace, tasks_root, _repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    playlist = tasks_root / "foo"
    playlist.mkdir(parents=True, exist_ok=True)
    (playlist / "config.json").write_text('{"order": ["task-x"]}\n')
    (playlist / "task-x.md").write_text("---\ntitle: X\n---\nv1\n")
    git_commit_all(tasks_root, "add foo queue")

    # Edit the foo brief AND add a top-level (main) task — concurrently dirty.
    (playlist / "task-x.md").write_text("---\ntitle: X\n---\nv2\n")
    (tasks_root / "main" / "new-top.md").write_text("---\ntitle: T\n---\nGo.\n")

    sha = commit_queue_state(tasks_root)  # main scope
    assert sha
    committed = git(tasks_root, "show", "--name-only", "--format=", "HEAD")
    assert "main/new-top.md" in committed
    assert "task-x.md" not in committed  # foo brief left for its own commit
    assert "task-x.md" in git(tasks_root, "status", "--porcelain")


def test_commit_queue_state_playlist_scope_excludes_top_level_md(tmp_path: Path) -> None:
    """A playlist commit commits only its own queue dir — it must not touch
    top-level ``main`` briefs owned by the main queue."""
    workspace, tasks_root, _repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    playlist = tasks_root / "foo"
    playlist.mkdir(parents=True, exist_ok=True)
    (playlist / "config.json").write_text('{"order": []}\n')
    git_commit_all(tasks_root, "add foo queue")

    # Edit a top-level (main) brief AND add a foo task — concurrently dirty.
    (tasks_root / "main" / "10.hello.md").write_text("Edited main brief.\n")
    (playlist / "new-x.md").write_text("---\ntitle: NX\n---\nGo.\n")

    sha = commit_queue_state(tasks_root, "foo")  # playlist scope
    assert sha
    committed = git(tasks_root, "show", "--name-only", "--format=", "HEAD")
    assert "foo/new-x.md" in committed
    assert "10.hello.md" not in committed  # main brief untouched by playlist commit
    assert "10.hello.md" in git(tasks_root, "status", "--porcelain")


# --------------------------------------------------------------------------- #
# Worktree namespacing, repo-lock serialization, stash isolation
# --------------------------------------------------------------------------- #


def test_worktree_namespacing_isolates_same_named_tasks(tmp_path: Path) -> None:
    """Two queues holding a same-named task cut distinct branches/worktrees;
    tearing one down leaves the other intact."""
    workspace, _tasks_root, repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})

    wt_main = setup_worktree(workspace, REPO, "shared", queue=None)
    wt_foo = setup_worktree(workspace, REPO, "shared", queue="foo")
    try:
        assert wt_main != wt_foo
        assert wt_main.exists() and wt_foo.exists()
        assert wt_main == worktree_dir(workspace, REPO, "shared", None)
        assert wt_foo == worktree_dir(workspace, REPO, "shared", "foo")
        branches = git(repo_root, "branch")
        assert worktree_branch("shared", None) in branches  # task-local/main/shared
        assert worktree_branch("shared", "foo") in branches  # task-local/foo/shared

        # Tearing down one queue's worktree leaves the other's intact.
        teardown_worktree(workspace, REPO, "shared", queue=None)
        assert not wt_main.exists()
        assert wt_foo.exists()
        branches = git(repo_root, "branch")
        assert worktree_branch("shared", None) not in branches
        assert worktree_branch("shared", "foo") in branches
    finally:
        teardown_worktree(workspace, REPO, "shared", queue="foo")


def test_repo_lock_serializes_concurrent_squashes(tmp_path: Path) -> None:
    """Two runner threads squashing distinct task branches at the same instant
    both land cleanly — the RepoLock serializes their ref CAS + checkout advance
    so neither loses its update or sees a half-advanced checkout."""
    workspace, _tasks_root, repo_root = _full(tmp_path, tasks={"10.a": "A", "20.b": "B"})
    for task, fname in (("10.a", "file_a.py"), ("20.b", "file_b.py")):
        wt = setup_worktree(workspace, REPO, task)
        (wt / fname).write_text("x = 1\n")
        git(wt, "add", ".")
        git(wt, "commit", "-m", f"work {task}")

    barrier = threading.Barrier(2)
    results: dict[str, tuple] = {}

    def _land(task: str, title: str) -> None:
        barrier.wait()  # force maximal overlap
        results[task] = squash_to_main(workspace, REPO, task, title)

    threads = [
        threading.Thread(target=_land, args=("10.a", "task a")),
        threading.Thread(target=_land, args=("20.b", "task b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results["10.a"][0] is not None, results["10.a"]
    assert results["20.b"][0] is not None, results["20.b"]
    assert (repo_root / "file_a.py").exists()
    assert (repo_root / "file_b.py").exists()
    log = git(repo_root, "log", "--oneline")
    assert "task: task a" in log
    assert "task: task b" in log
    assert not (repo_root / ".git/index.lock").exists()


def test_land_never_touches_the_stash(tmp_path: Path) -> None:
    """A land over operator WIP is a pure ref operation: the WIP is carried
    forward in place and a pre-existing human stash entry is never consumed,
    reordered, or added to (the autostash machinery is gone)."""
    workspace, _tasks_root, repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    wip = repo_root / "app.py"
    wip.write_text("base\n")
    git_commit_all(repo_root, "add app.py")
    _commit_work_on_branch(workspace, REPO, "10.hello")  # branch adds new_file.py

    # A human stashes unrelated tracked WIP onto the LIFO stack.
    sentinel = repo_root / "sentinel.py"
    sentinel.write_text("# v1\n")
    git(repo_root, "add", ".")
    git(repo_root, "commit", "-m", "add sentinel")
    sentinel.write_text("# human wip\n")
    git(repo_root, "stash", "push", "-m", "human-wip", "--", str(sentinel))

    # Operator has unrelated code WIP; the land must carry it forward in place.
    dirty = wip.read_text() + "\n# operator edit\n"
    wip.write_text(dirty)

    sha, detail, recoverable = squash_to_main(workspace, REPO, "10.hello", "hello world")
    assert sha is not None
    assert detail == ""
    assert wip.read_text() == dirty  # operator WIP carried forward verbatim

    stash_list = git(repo_root, "stash", "list")
    assert "human-wip" in stash_list  # the human's stash was never consumed
    assert stash_list.count("\n") == 0  # exactly one entry remains (no trailing nl)

    teardown_worktree(workspace, REPO, "10.hello")
