"""Tests for ``nightshift.run_local`` — the local nightshift runner.

Migrated to the Nightshift two-root multi-repo model:

* Briefs + queue config live in the content store
  ``<workspace>/nightshift-tasks`` (``tasks_root``); the default queue is the
  ``main`` directory.
* Git operations, worktrees, and landing run against a *target repo*
  ``<workspace>/<repo>`` (``longitude`` by default). Worktrees live OUTSIDE the
  target repo under ``<workspace>/.worktrees/<repo>/``.
* The pre-run target-repo snapshot is gone: briefs are read live from the
  content store and delivered to the worker via a run-scratch file outside the
  worktree, so the target repo only ever receives the implementation squash on
  ``main``.

Fixtures use the shared ``build_workspace`` builder (``tests/_workspace.py``).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

import nightshift.backends as backends_mod
from _workspace import build_workspace, git, git_commit_all
from nightshift._paths import PROMPTS_DIR, TEMPLATES_DIR
from nightshift.backends import AgentStreamParser, WorkerResult
from nightshift.engine import (
    AUTOSTASH_MESSAGE,
    DEFAULT_VALIDATE_CMD,
    TaskResult,
    _landing_blockers,
    _porcelain_path,
    _squash_failure_kind,
    check_preconditions,
    commit_queue_state,
    compute_code_loc,
    list_queue,
    live_ordered_queue,
    load_play_priorities,
    load_sort_mode,
    materialize_brief,
    order_stems,
    read_task,
    run_queue,
    save_play_priorities,
    save_sort_mode,
    set_task_meta,
    worktree_branch,
    worktree_dir,
)
from nightshift.events import (
    RUN_FINISHED,
    TASK_RESULT,
    TASK_STARTED,
    TASK_STATUS,
    Event,
    RunStore,
)
from nightshift.repos import DEFAULT_TASKS_REPO
from nightshift.run_local import (
    _Tee,
    _write_failure_log,
    acquire_lock,
    build_claude_argv,
    build_prompt,
    build_task_list,
    enough_free_disk,
    load_dotenv,
    open_run_log,
    recover_task,
    resolve_task,
    resolve_title,
    run_task,
    setup_worktree,
    squash_to_main,
    teardown_worktree,
)
from nightshift.spawn_daily import resolve_config


TEMPLATES = TEMPLATES_DIR

# The default target repo created by ``build_workspace`` and bound to the main
# queue's config (``repo: longitude``).
REPO = "longitude"


# --------------------------------------------------------------------------- #
# Two-root fixtures
# --------------------------------------------------------------------------- #


def _full(
    tmp_path: Path,
    *,
    tasks: dict[str, str] | None = None,
    queues: dict[str, dict[str, object]] | None = None,
    config: dict[str, object] | None = None,
) -> tuple[Path, Path, Path]:
    """Build a full two-root workspace with the default ``longitude`` target repo.

    Returns ``(workspace, tasks_root, repo_root)``.
    """
    workspace = build_workspace(tmp_path, tasks=tasks, queues=queues, config=config)
    return workspace, workspace / DEFAULT_TASKS_REPO, workspace / REPO


def _store_only(
    tmp_path: Path,
    *,
    tasks: dict[str, str] | None = None,
    queues: dict[str, dict[str, object]] | None = None,
    config: dict[str, object] | None = None,
    commit: bool = False,
) -> Path:
    """Build only the content store (no target repo) and return ``tasks_root``.

    For pure queue/config tests that never resolve a repo. ``commit`` git-inits
    the store so the autosplit-dispatch commit path runs.
    """
    workspace = build_workspace(
        tmp_path,
        tasks=tasks,
        queues=queues,
        config=config,
        repos=(),
        main_repo=None,
        commit_tasks=commit,
    )
    return workspace / DEFAULT_TASKS_REPO


def _repo(tmp_path: Path) -> Path:
    """A target repo (``<workspace>/longitude``) for pure git-churn tests."""
    workspace = build_workspace(tmp_path)
    return workspace / REPO


def _commit_files(repo: Path, files: dict[str, str]) -> str:
    """Write ``files`` (path → content), commit them, and return the short sha.

    Uses the repo's configured identity (set by ``build_workspace``), so no
    explicit git env is needed.
    """
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
# build_task_list (autosplit / ordering) — content store
# --------------------------------------------------------------------------- #


def test_build_task_list_single(tmp_path: Path) -> None:
    tasks_root = _store_only(tmp_path, tasks={"10.hello": "Do something."})
    result = build_task_list(tasks_root, "10.hello")
    assert result == ["10.hello"]


def test_build_task_list_all_skips_disabled(tmp_path: Path) -> None:
    tasks_root = _store_only(
        tmp_path,
        tasks={
            "10.active-task": "---\nmodel: claude-sonnet-4-6\n---\nDo it.",
            "20.paused-task": "---\ndisabled: true\n---\nNot yet.",
            "30.no-frontmatter": "Just a plain task.",
        },
        commit=True,
    )
    result = build_task_list(tasks_root, "all")
    assert "10.active-task" in result
    assert "30.no-frontmatter" in result
    assert "20.paused-task" not in result


def test_build_task_list_all_skips_autosplit(tmp_path: Path) -> None:
    tasks_root = _store_only(
        tmp_path,
        tasks={
            "00._questions": "---\nautosplit: true\n---\n## Questions:\n",
            "00._todo": "---\nautosplit: true\n---\n## TO DO:\n",
            "10.real-task": "Do the thing.",
            "02.service-triage": "---\nevergreen: true\n---\nCheck logs.",
        },
        commit=True,
    )
    result = build_task_list(tasks_root, "all")
    assert "10.real-task" in result
    assert "02.service-triage" in result
    assert "00._questions" not in result
    assert "00._todo" not in result


def test_build_task_list_daily_expansion(tmp_path: Path) -> None:
    tasks_root = _store_only(
        tmp_path,
        tasks={
            "00._todo": "---\nautosplit: true\n---\nFix the following:\n\n1. Fix ops\n2. Add toggle\n",
        },
        commit=True,
    )
    result = build_task_list(tasks_root, "00._todo")
    assert len(result) == 2
    assert all(r.startswith("00.") for r in result)
    assert (tasks_root / "main" / "00._todo.md").read_text() == (
        TEMPLATES / "00._todo.md"
    ).read_text()


def test_build_task_list_all_sorts_spawned_in_place(tmp_path: Path) -> None:
    """Spawned daily items run where they sort, not ahead of the whole queue."""
    tasks_root = _store_only(
        tmp_path,
        tasks={
            "04.2.playbook-thing": "Do the playbook thing.",
            "99._todo": "---\nautosplit: true\n---\nFix the following:\n\n1. Export the universe\n2. Follow tradeview format\n",
        },
        commit=True,
    )
    result = build_task_list(tasks_root, "all")

    spawned = [r for r in result if r.startswith("99.")]
    assert spawned, "expected spawned 99.* subtasks"
    assert "04.2.playbook-thing" in result
    assert result.index("04.2.playbook-thing") < min(
        result.index(s) for s in spawned
    ), f"04.2.* should sort before spawned 99.* items, got {result}"
    assert result == sorted(result)


# --------------------------------------------------------------------------- #
# Tee / log plumbing / pure helpers
# --------------------------------------------------------------------------- #


def test_tee_writes_to_all_streams() -> None:
    a, b = io.StringIO(), io.StringIO()
    tee = _Tee(a, b)
    n = tee.write("hello")
    tee.write(" world")
    assert n == 5
    assert a.getvalue() == "hello world"
    assert b.getvalue() == "hello world"
    assert tee.isatty() is False


def test_open_run_log_creates_timestamped_log(tmp_path: Path) -> None:
    tasks_root = _store_only(tmp_path)
    log = open_run_log(tasks_root)
    try:
        path = Path(log.name)
        # Run logs are gitignored runtime state under the main queue in the store.
        assert path.parent == tasks_root / "main" / "logs"
        assert path.suffix == ".log"
        assert path.name.startswith("nightshift-local-")
        log.write("progress line\n")
        log.flush()
    finally:
        log.close()
    assert path.read_text() == "progress line\n"


# --------------------------------------------------------------------------- #
# build_prompt — now a pure formatter over the materialised brief path
# --------------------------------------------------------------------------- #


def test_build_prompt_matches_ci_format(tmp_path: Path) -> None:
    workspace, _tasks_root, _repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    scratch = materialize_brief(workspace, REPO, "10.hello", "Do something.", queue=None)
    prompt = build_prompt("10.hello", task_file=str(scratch), validate_cmd=DEFAULT_VALIDATE_CMD)
    assert prompt.startswith(f"Your task file is: {scratch}\n")
    assert "The TASK variable is: 10.hello" in prompt
    # The shipped local worker charter is appended verbatim after the injected vars.
    assert "You are the nightshift worker running **locally**." in prompt


def test_build_prompt_injects_default_validate(tmp_path: Path) -> None:
    """With no queue/operator override, the worker is told the engine default
    (``just validate``) — the same command run_task resolves and injects."""
    workspace, tasks_root, _repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    # Clear the seeded operator ``validate`` so the queue inherits the default.
    (workspace / "config.json").write_text(json.dumps({"model": "auto"}) + "\n")
    config = resolve_config(workspace, tasks_root, "main")
    validate_cmd = str(config.get("validate") or DEFAULT_VALIDATE_CMD)
    prompt = build_prompt("10.hello", task_file="/scratch.md", validate_cmd=validate_cmd)
    assert "The VALIDATE command is: just validate" in prompt


def test_build_prompt_injects_playlist_validate_override(tmp_path: Path) -> None:
    """A queue's ``validate`` override is resolved by the runner and injected as
    ``$VALIDATE`` so the worker self-validates with the queue's command, not just
    the engine gate."""
    workspace, tasks_root, _repo_root = _full(
        tmp_path,
        queues={
            "nightshift": {
                "config": {"validate": "just validate-nightshift", "order": [], "repo": REPO}
            }
        },
    )
    config = resolve_config(workspace, tasks_root, "nightshift")
    validate_cmd = str(config.get("validate") or DEFAULT_VALIDATE_CMD)
    prompt = build_prompt("10.hello", task_file="/scratch.md", validate_cmd=validate_cmd)
    assert "The VALIDATE command is: just validate-nightshift" in prompt
    assert "The VALIDATE command is: just validate\n" not in prompt


def test_build_prompt_injects_task_file_path_for_main_queue(tmp_path: Path) -> None:
    """``$TASK_FILE`` carries the brief's *delivered* path — a run-scratch file
    OUTSIDE the target repo (so the brief never enters the repo the agent lands
    in), never ``.tasks/<task>.md`` inside it."""
    workspace, _tasks_root, _repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    scratch = materialize_brief(workspace, REPO, "10.hello", "Do something.", queue=None)
    prompt = build_prompt("10.hello", task_file=str(scratch), validate_cmd=DEFAULT_VALIDATE_CMD)
    assert f"The TASK_FILE variable is: {scratch}" in prompt
    assert f"Your task file is: {scratch}" in prompt
    # The brief is delivered via the worktree-sibling scratch, not the repo tree.
    assert str(scratch).startswith(str(workspace / ".worktrees"))
    assert ".tasks/10.hello.md" not in prompt


def test_build_prompt_injects_playlist_task_file_path(tmp_path: Path) -> None:
    """An alternate-queue task's scratch is namespaced by queue and still lives
    OUTSIDE the target repo — not at ``.tasks/<playlist>/<task>.md``."""
    workspace, _tasks_root, _repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    scratch = materialize_brief(workspace, REPO, "10.hello", "Do something.", queue="nightshift")
    prompt = build_prompt("10.hello", task_file=str(scratch), validate_cmd="just validate-nightshift")
    assert f"The TASK_FILE variable is: {scratch}" in prompt
    assert "task-local-nightshift-10.hello" in str(scratch)
    assert ".tasks/nightshift/10.hello.md" not in prompt


def test_local_prompt_delivers_task_file_as_readonly_scratch() -> None:
    """The shipped local worker prompt delivers ``$TASK_FILE`` as a read-only
    scratch copy that the worker must not modify/move/commit — the brief lives in
    the separate content store and the runner retires it. It must never hardcode
    ``.tasks/$TASK.md`` (which would point at a path that no longer exists)."""
    prompt_body = (PROMPTS_DIR / "nightshift-local.md").read_text()
    assert "$TASK_FILE" in prompt_body
    assert "read-only scratch copy" in prompt_body
    assert "does not live in this repo" in prompt_body
    assert 'git rm ".tasks/$TASK.md"' not in prompt_body


def test_build_claude_argv_with_model() -> None:
    argv = build_claude_argv("do stuff", "claude-opus-4-6", None)
    assert argv[0] == "claude"
    assert "-p" in argv
    assert argv[argv.index("-p") + 1] == "do stuff"
    assert argv[argv.index("--model") + 1] == "claude-opus-4-6"
    assert "--max-turns" not in argv
    assert "--dangerously-skip-permissions" in argv


def test_build_claude_argv_with_max_turns() -> None:
    argv = build_claude_argv("do stuff", "claude-sonnet-4-6", 30)
    assert "--max-turns" in argv
    assert argv[argv.index("--max-turns") + 1] == "30"


def test_build_claude_argv_allowed_tools() -> None:
    argv = build_claude_argv("x", "claude-sonnet-4-6", None)
    idx = argv.index("--allowedTools")
    assert argv[idx + 1] == "Bash,Edit,MultiEdit,Write,Read,Glob,Grep,LS"


def test_build_claude_argv_requests_stream_json_for_telemetry() -> None:
    argv = build_claude_argv("x", "claude-opus-4-6", None)
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv  # claude requires --verbose with -p + stream-json


def test_agent_stream_parser_captures_turns_tokens_cost() -> None:
    parser = AgentStreamParser()
    lines = [
        '{"type":"system","subtype":"init"}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"Working"}]}}',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{}}]}}',
        '{"type":"result","subtype":"success","num_turns":9,'
        '"usage":{"input_tokens":1000,"cache_read_input_tokens":200,"output_tokens":300},'
        '"total_cost_usd":0.1234}',
    ]
    emitted = "".join(parser.feed(line) for line in lines)
    assert "Working" in emitted
    assert "[tool: Bash]" in emitted
    assert parser.turns == 9
    assert parser.input_tokens == 1200  # input + cache_read folded in
    assert parser.output_tokens == 300
    assert round(parser.cost_usd, 4) == 0.1234


def test_agent_stream_parser_passes_through_non_json() -> None:
    parser = AgentStreamParser()
    # An older CLI that ignores --output-format still streams readable text.
    assert parser.feed("plain log line\n") == "plain log line\n"
    assert parser.turns is None and parser.input_tokens is None


def test_resolve_title_from_frontmatter() -> None:
    assert resolve_title("hello", {"title": "Fix the world"}) == "Fix the world"


def test_resolve_title_returns_task_name() -> None:
    assert resolve_title("migrate-ui-stylesheet", {}) == "migrate-ui-stylesheet"
    assert resolve_title("hello-world", {}) == "hello-world"


def test_load_dotenv_loads_key(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TEST_KEY_ABC=test_value_123\n")
    env_backup = os.environ.get("TEST_KEY_ABC")
    try:
        os.environ.pop("TEST_KEY_ABC", None)
        load_dotenv(tmp_path)
        assert os.environ.get("TEST_KEY_ABC") == "test_value_123"
    finally:
        if env_backup is not None:
            os.environ["TEST_KEY_ABC"] = env_backup
        else:
            os.environ.pop("TEST_KEY_ABC", None)


def test_load_dotenv_does_not_overwrite(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TEST_KEY_XYZ=from_file\n")
    os.environ["TEST_KEY_XYZ"] = "from_shell"
    try:
        load_dotenv(tmp_path)
        assert os.environ["TEST_KEY_XYZ"] == "from_shell"
    finally:
        os.environ.pop("TEST_KEY_XYZ", None)


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
    would otherwise be code (a built `dist/*.js` bundle, a `build/*.ts`).

    (The legacy ``.tasks/*.py`` brief case is gone: in the two-root model briefs
    live in the separate content store and can never appear in a target-repo
    commit, so there is no in-repo queue dir left to exclude.)
    """
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
    intra-task churn a branch-history sum would report). Pinning the squash
    metric keeps live capture and backfill consistent across all of history."""
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

    # Net squash diff: 1 added code line. This is what the engine records at land
    # time and what the backfill recovers from the sha — the two now agree.
    assert compute_code_loc(repo_root, sha) == 1


def _write_completed_run(
    store: RunStore, task: str, *, commit_sha: str | None, loc: int | None
) -> str:
    """Record a single completed task whose result carries ``commit_sha``/``loc``."""
    writer = store.start("ui")
    writer.emit(Event(TASK_STARTED, {"task": task, "title": task, "frontmatter": {}}))
    result: dict[str, object] = {"task": task, "status": "completed", "result_line": "ok"}
    if commit_sha is not None:
        result["commit_sha"] = commit_sha
    if loc is not None:
        result["loc"] = loc
    writer.emit(Event(TASK_RESULT, result))
    writer.emit(Event(RUN_FINISHED, {"run_id": writer.run_id}))
    writer.close()
    return writer.run_id


def test_run_store_backfills_missing_loc_from_landed_commit(tmp_path: Path) -> None:
    """A completed record that recorded a commit but no loc (e.g. it predates the
    engine capturing loc) has its figure recovered from the landed commit's own
    churn, so the Stats page reflects shipped work rather than reading zero.

    The Stats backfill recomputes churn against ``RunStore.root`` (the content
    store), so the landed commit is reconstructed from there.
    """
    tasks_root = _store_only(tmp_path, commit=True)
    # Land a real commit: 4 code lines (a comment and a blank dropped).
    sha = _commit_files(tasks_root, {"mod.py": "import os\n# c\nx = 1\n\ny = 2\nz = 3\n"})

    store = RunStore(tasks_root)
    _write_completed_run(store, "10.x", commit_sha=sha, loc=None)

    rec = store.list_runs()[0]["tasks"][0]
    assert rec["loc"] == compute_code_loc(tasks_root, sha)
    assert rec["loc"] == 4


def test_run_store_backfill_sums_multiple_landed_commits(tmp_path: Path) -> None:
    """A task that landed more than once (comma-separated shas) backfills the sum
    of every landed commit's churn."""
    tasks_root = _store_only(tmp_path, commit=True)
    sha1 = _commit_files(tasks_root, {"a.py": "a = 1\nb = 2\n"})
    sha2 = _commit_files(tasks_root, {"c.py": "c = 3\n"})

    store = RunStore(tasks_root)
    _write_completed_run(store, "10.x", commit_sha=f"{sha1}, {sha2}", loc=None)

    rec = store.list_runs()[0]["tasks"][0]
    assert rec["loc"] == compute_code_loc(tasks_root, sha1) + compute_code_loc(tasks_root, sha2)
    assert rec["loc"] == 3


def test_run_store_backfill_keeps_recorded_loc(tmp_path: Path) -> None:
    """A captured non-zero loc is authoritative — backfill never overwrites it.
    (Live capture and backfill use the same squash-commit metric, so they would
    agree anyway; the guard simply avoids recomputing a figure already present.)"""
    tasks_root = _store_only(tmp_path, commit=True)
    sha = _commit_files(tasks_root, {"mod.py": "a = 1\n"})

    store = RunStore(tasks_root)
    _write_completed_run(store, "10.x", commit_sha=sha, loc=99)

    rec = store.list_runs()[0]["tasks"][0]
    assert rec["loc"] == 99


def test_run_store_backfill_skips_records_without_a_commit(tmp_path: Path) -> None:
    """A completed record that landed nothing (no sha — a no-change run) keeps
    loc as None rather than fabricating a count."""
    tasks_root = _store_only(tmp_path, commit=True)

    store = RunStore(tasks_root)
    _write_completed_run(store, "10.x", commit_sha=None, loc=None)

    rec = store.list_runs()[0]["tasks"][0]
    assert rec["loc"] is None


def test_squash_to_main_autostash_lands_over_dirty_code(tmp_path: Path) -> None:
    """With autostash on (the default), tracked operator code WIP on the target
    repo's main is set aside for the merge+commit and restored afterward, so the
    land succeeds and the operator's edit is byte-identical with no leftover
    stash. Briefs live in the separate content store, so every tracked change
    here is genuine operator code WIP."""
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

    # The task landed, and the operator's WIP is restored verbatim (and NOT part
    # of the task commit — it was stashed during the merge).
    assert (repo_root / "new_file.py").exists()
    assert wip.read_text() == dirty
    committed = git(repo_root, "show", "--name-only", "--format=", "HEAD")
    assert "app.py" not in committed
    # No autostash entry is left behind.
    assert AUTOSTASH_MESSAGE not in git(repo_root, "stash", "list")

    teardown_worktree(workspace, REPO, "10.hello")


def test_squash_to_main_refuses_dirty_main_when_autostash_off(tmp_path: Path) -> None:
    """With autostash off, a tracked code change on the target repo's main blocks
    the squash with a precise recoverable reason and must not leave a half-merged
    tree."""
    workspace, _tasks_root, repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    wip = repo_root / "app.py"
    wip.write_text("x = 1\n")
    git_commit_all(repo_root, "add app.py")
    _commit_work_on_branch(workspace, REPO, "10.hello")

    head_before = git(repo_root, "rev-parse", "HEAD")

    wip.write_text(wip.read_text() + "\n# local edit\n")

    sha, detail, recoverable = squash_to_main(
        workspace, REPO, "10.hello", "hello world", autostash=False,
    )
    assert sha is None
    assert "uncommitted changes" in detail
    assert recoverable is True  # clearing the dirty tree lets a retry succeed

    assert git(repo_root, "rev-parse", "HEAD") == head_before
    assert "# local edit" in wip.read_text()

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


def test_squash_to_main_autostash_pop_conflict_preserves_work(tmp_path: Path) -> None:
    """When restoring set-aside WIP conflicts with the file the task just landed,
    the land still records, the failure detail explains it, and the stash entry is
    preserved so nothing is lost."""
    workspace, _tasks_root, repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    target = repo_root / "app.py"
    target.write_text("base\n")
    git_commit_all(repo_root, "add app.py")

    # Branch edits an existing tracked file.
    worktree = setup_worktree(workspace, REPO, "10.hello")
    (worktree / "app.py").write_text('{"branch": true}\n')
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "branch edit")

    # Operator has uncommitted edits to the SAME file → pop will conflict.
    target.write_text('{"operator": true}\n')

    sha, detail, recoverable = squash_to_main(workspace, REPO, "10.hello", "hello world")
    assert sha is not None  # the land happened
    assert "set-aside" in detail and "stash" in detail
    # The stash entry is preserved for manual restore.
    assert AUTOSTASH_MESSAGE in git(repo_root, "stash", "list")

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
    """`_landing_blockers` reports tracked code changes in the target repo but
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

    paths = [_porcelain_path(line) for line in _landing_blockers(repo_root)]
    assert any("app.py" in p for p in paths)
    # The brief lives in the content store, so it never shows up as a blocker.
    assert all("10.hello.md" not in p for p in paths)
    assert all(not p.startswith("main/") for p in paths)


def _prep_preconditions(repo_root: Path, monkeypatch) -> None:
    """Satisfy every non-dirty-tree check so check_preconditions reaches (and
    passes) the dirty-tree gate: fake the claude binary + API key and a trivial
    `just validate` in the target repo."""
    monkeypatch.setattr("nightshift.engine.shutil.which", lambda _name: "/bin/claude")
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


def test_check_preconditions_notice_on_code_wip_autostash_on(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    workspace, _tasks_root, repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    _prep_preconditions(repo_root, monkeypatch)

    # Dirty a tracked code file in the target repo.
    code = repo_root / "app.py"
    code.write_text("x = 1\n")
    git_commit_all(repo_root, "add app.py")
    code.write_text("x = 2\n")

    check_preconditions(workspace, REPO)  # autostash defaults on → notice, no exit
    assert "set aside" in capsys.readouterr().out


def test_check_preconditions_exits_on_code_wip_autostash_off(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace, _tasks_root, repo_root = _full(
        tmp_path,
        tasks={"10.hello": "Do something."},
        config={"autostash_operator_work": False},
    )
    _prep_preconditions(repo_root, monkeypatch)

    code = repo_root / "app.py"
    code.write_text("x = 1\n")
    git_commit_all(repo_root, "add app.py")
    # A tracked code blocker that, with autostash off, must hard-exit.
    code.write_text("x = 2\n")

    with pytest.raises(SystemExit):
        check_preconditions(workspace, REPO)


def test_recover_task_relands_after_blocker_cleared(tmp_path: Path) -> None:
    """A squash that fails on a dirty tree leaves the branch intact; once the
    blocker is cleared, recover_task lands the work and tears the branch down."""
    workspace, _tasks_root, repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    wip = repo_root / "app.py"
    wip.write_text("x = 1\n")
    git_commit_all(repo_root, "add app.py")
    _commit_work_on_branch(workspace, REPO, "10.hello")

    original = wip.read_text()
    wip.write_text(original + "\n# local edit\n")

    sha, _detail, _ = squash_to_main(
        workspace, REPO, "10.hello", "hello world", autostash=False,
    )
    assert sha is None  # blocked by the dirty tree

    # Branch is preserved so the validated work can be recovered.
    assert "task-local/main/10.hello" in git(repo_root, "branch")

    # Clear the blocker, then recover.
    wip.write_text(original)
    result = recover_task(workspace, REPO, "10.hello", "hello world")
    assert result.success
    assert result.commit_sha
    assert (repo_root / "new_file.py").exists()

    # Branch is gone and the squash landed as a single commit.
    assert "task-local/main/10.hello" not in git(repo_root, "branch")
    assert "task: hello world" in git(repo_root, "log", "--oneline")


def test_recover_task_without_branch_reports_clearly(tmp_path: Path) -> None:
    workspace, _tasks_root, _repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    result = recover_task(workspace, REPO, "10.hello", "hello world")
    assert not result.success
    assert "nothing to recover" in (result.error or "")


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


def test_write_failure_log_captures_error_and_diff(tmp_path: Path) -> None:
    workspace, _tasks_root, _repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})

    worktree = setup_worktree(workspace, REPO, "10.hello")

    log_path = _write_failure_log(
        workspace, REPO, worktree, "10.hello", "just validate failed: syntax error",
        validate_stderr="broken.py:1: SyntaxError: invalid syntax\n",
    )

    # Failure logs live under the workspace worktree area for the repo, never in
    # the target repo.
    assert str(log_path).startswith(str(workspace / ".worktrees" / REPO))
    assert log_path.exists()
    content = log_path.read_text()
    assert "10.hello" in content
    assert "just validate failed" in content
    assert "SyntaxError" in content
    lines = content.strip().splitlines()
    assert len(lines) <= 5


# --------------------------------------------------------------------------- #
# Classified failure reasons + resolve_task
# --------------------------------------------------------------------------- #


def test_squash_failure_kind_classifies_conflict_vs_rejected() -> None:
    # A real 3-way conflict is a content conflict; a dirty main or a failed
    # commit is a (transient) rejection.
    assert _squash_failure_kind(False, "merge conflict in foo.py") == "merge_conflict"
    assert _squash_failure_kind(True, "main has uncommitted changes") == "merge_rejected"
    assert _squash_failure_kind(False, "commit failed: nothing to commit") == "merge_rejected"


class _StubBackend:
    """A worker backend that runs an injected callback in the worktree instead
    of launching a real agent (used to drive the resolve agent path)."""

    def __init__(self, resolve_fn) -> None:
        self.resolve_fn = resolve_fn
        self.calls = 0

    def run(self, spec, emit_log, should_abort, on_worker_start=None) -> WorkerResult:
        self.calls += 1
        self.resolve_fn(spec.cwd)
        return WorkerResult(returncode=0)


def _seed_conflict_repo(
    tmp_path: Path,
    *,
    branch_content: str,
    main_content: str,
    max_attempts: int = 2,
) -> tuple[Path, Path, Path]:
    """Seed a workspace where the task branch and the target repo's main make
    overlapping edits to a shared file, so the squash hits a real content
    conflict. The seeded operator config disables validation friction via the
    shared builder (``validate`` resolves to a trivially-passing command), so the
    resolve path can validate cleanly. Returns ``(workspace, tasks_root,
    repo_root)``."""
    workspace, tasks_root, repo_root = _full(
        tmp_path,
        tasks={"10.hello": "Do something."},
        config={"max_resolve_attempts": max_attempts},
    )
    (repo_root / "shared.txt").write_text("base\n")
    git_commit_all(repo_root, "add shared.txt")

    worktree = setup_worktree(workspace, REPO, "10.hello")
    (worktree / "shared.txt").write_text(branch_content)
    git(worktree, "add", "shared.txt")
    git(worktree, "commit", "-m", "branch edit")

    (repo_root / "shared.txt").write_text(main_content)
    git(repo_root, "add", "shared.txt")
    git(repo_root, "commit", "-m", "main edit")
    return workspace, tasks_root, repo_root


def test_resolve_task_without_branch_reports_clearly(tmp_path: Path) -> None:
    workspace, tasks_root, _repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    result = resolve_task(
        workspace, REPO, tasks_root, "10.hello", "hello world", emit=lambda _e: None
    )
    assert not result.success
    assert "nothing to resolve" in (result.error or "")
    assert result.failure_kind == "merge_rejected"


def test_resolve_task_lands_transient_via_cheap_path(tmp_path: Path) -> None:
    """A transient blocker (dirty main) that has cleared lands on the cheap
    re-squash path — no agent involved."""
    workspace, tasks_root, repo_root = _full(tmp_path, tasks={"10.hello": "Do something."})
    wip = repo_root / "app.py"
    wip.write_text("x = 1\n")
    git_commit_all(repo_root, "add app.py")
    _commit_work_on_branch(workspace, REPO, "10.hello")

    original = wip.read_text()
    wip.write_text(original + "\n# local edit\n")
    sha, _detail, _rec = squash_to_main(
        workspace, REPO, "10.hello", "hello world", autostash=False,
    )
    assert sha is None  # blocked by the dirty tree
    wip.write_text(original)  # clear the blocker

    result = resolve_task(
        workspace, REPO, tasks_root, "10.hello", "hello world", emit=lambda _e: None
    )
    assert result.success
    assert result.commit_sha
    assert (repo_root / "new_file.py").exists()
    assert "task-local/main/10.hello" not in git(repo_root, "branch")


def test_resolve_task_agent_resolves_content_conflict(tmp_path: Path) -> None:
    """A content conflict routes to the agent path: rebase onto main, the worker
    resolves + continues the rebase, validate passes, and the work squashes in."""
    workspace, tasks_root, repo_root = _seed_conflict_repo(
        tmp_path, branch_content="branch\n", main_content="main\n",
    )

    def _resolver(cwd: Path) -> None:
        (Path(cwd) / "shared.txt").write_text("resolved\n")
        subprocess.run(
            ["git", "add", "shared.txt"], cwd=cwd, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "rebase", "--continue"],
            cwd=cwd, check=True, capture_output=True,
            env={**os.environ, "GIT_EDITOR": "true"},
        )

    stub = _StubBackend(_resolver)
    original = backends_mod.get_backend
    backends_mod.get_backend = lambda *_a, **_k: stub
    try:
        events: list = []
        result = resolve_task(
            workspace, REPO, tasks_root, "10.hello", "hello world", emit=events.append
        )
    finally:
        backends_mod.get_backend = original

    assert result.success, result.error
    assert stub.calls == 1
    assert (repo_root / "shared.txt").read_text() == "resolved\n"
    assert "task-local/main/10.hello" not in git(repo_root, "branch")
    phases = [e.payload.get("phase") for e in events if e.type == TASK_STATUS]
    assert "resolve" in phases


def test_resolve_task_bounded_attempts_preserves_branch(tmp_path: Path) -> None:
    """When the agent can't resolve, the run is bounded by max_resolve_attempts
    and the branch is preserved for manual resolution."""
    workspace, tasks_root, repo_root = _seed_conflict_repo(
        tmp_path, branch_content="branch\n", main_content="main\n", max_attempts=1,
    )

    def _gives_up(cwd: Path) -> None:
        # Leave the rebase conflicted — the engine should abort and stop.
        pass

    stub = _StubBackend(_gives_up)
    original = backends_mod.get_backend
    backends_mod.get_backend = lambda *_a, **_k: stub
    try:
        result = resolve_task(
            workspace, REPO, tasks_root, "10.hello", "hello world", emit=lambda _e: None
        )
    finally:
        backends_mod.get_backend = original

    assert not result.success
    assert result.failure_kind == "merge_conflict"
    assert stub.calls == 1  # bounded by max_resolve_attempts
    assert "task-local/main/10.hello" in git(repo_root, "branch")


# --------------------------------------------------------------------------- #
# commit_queue_state — commit the queue definition in the CONTENT STORE
# (the pre-run target-repo snapshot is gone)
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
    # The queue-definition files are now clean in the content store — the new
    # invariant that replaces the removed ``QUEUE_SNAPSHOT_PATHSPECS`` porcelain
    # check (which asserted the snapshot left the in-repo queue dir clean).
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
    """Inverted for the two-root model: an alternate-queue brief lives only in the
    content store and is delivered to the worker via a run-scratch file OUTSIDE
    the worktree — it is never snapshotted into the target repo. A worktree cut
    from the repo's HEAD therefore never contains the brief; only the
    implementation squash ever reaches the target repo."""
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


def test_landing_lock_serializes_concurrent_squashes(tmp_path: Path) -> None:
    """Two runner threads squashing distinct task branches at the same instant
    both land cleanly — the landing lock serializes their root index/HEAD writes
    so neither sees a half-merged tree or a corrupt index.lock."""
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


def test_autostash_does_not_disturb_existing_human_stash(tmp_path: Path) -> None:
    """The set-aside is stack-independent (``git stash create``): a land over
    operator WIP restores it without consuming a pre-existing human stash entry,
    and leaves no ``nightshift-autostash`` on the stack on success."""
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

    # Operator now has unrelated code WIP; the land must set it aside + restore it.
    dirty = wip.read_text() + "\n# operator edit\n"
    wip.write_text(dirty)

    sha, detail, recoverable = squash_to_main(workspace, REPO, "10.hello", "hello world")
    assert sha is not None
    assert detail == ""
    assert wip.read_text() == dirty  # operator WIP restored verbatim

    stash_list = git(repo_root, "stash", "list")
    assert AUTOSTASH_MESSAGE not in stash_list  # no leftover autostash on success
    assert "human-wip" in stash_list  # the human's stash was never consumed
    assert stash_list.count("\n") == 0  # exactly one entry remains (no trailing nl)

    teardown_worktree(workspace, REPO, "10.hello")


def test_failure_kind_round_trips_through_run_store(tmp_path: Path) -> None:
    """A classified failure_kind written on a task_result survives reconstruction
    so History/UI can render it."""
    tasks_root = _store_only(tmp_path)
    store = RunStore(tasks_root)
    writer = store.start(launched_by="test")
    writer.close()
    assert store.append_task_result(
        writer.run_id, "10.hello",
        status="error", error="boom", failure_kind="validation_error",
    )
    rec = store.find_task(writer.run_id, "10.hello")
    assert rec is not None
    assert rec["failure_kind"] == "validation_error"


# --------------------------------------------------------------------------- #
# Layer 4 — run_queue drains the live queue (mid-run additions)
# --------------------------------------------------------------------------- #


def test_live_ordered_queue_skips_disabled_and_orders(tmp_path: Path) -> None:
    tasks_root = _store_only(tmp_path, tasks={
        "10.a": "Do a.",
        "20.b": "---\ndisabled: true\n---\nDo b.",
        "30.c": "Do c.",
    })
    (tasks_root / "main" / "config.json").write_text('{"order": ["30.c", "10.a"]}\n')
    assert live_ordered_queue(tasks_root) == ["30.c", "10.a"]


def test_run_queue_follow_picks_up_midrun_addition(tmp_path: Path, monkeypatch) -> None:
    """With follow_queue on, a task that appears mid-run is executed in the same
    run, and the brief is delivered live from the content store (no pre-run
    target-repo snapshot) — present in the store working tree when run_task runs.
    """
    workspace = build_workspace(tmp_path, tasks={"a": "Do a."})
    tasks_root = workspace / DEFAULT_TASKS_REPO
    (tasks_root / "main" / "config.json").write_text('{"order": ["a"]}\n')

    calls: list[str] = []
    present_when_run: dict[str, bool] = {}

    def fake_run_task(ws: Path, tr: Path, task: str, **_kwargs) -> TaskResult:
        calls.append(task)
        # The brief is read live from the content store working tree; record
        # whether it is present (delivered live) when the task runs.
        present_when_run[task] = (tr / "main" / f"{task}.md").exists()
        if task == "a":  # UI adds a task mid-run
            (tr / "main" / "b.md").write_text("---\ntitle: B\n---\nDo b.\n")
        return TaskResult(task=task, title=task, success=True)

    monkeypatch.setattr("nightshift.engine.run_task", fake_run_task)
    run_queue(workspace, tasks_root, ["a"], follow_queue=True)

    assert calls == ["a", "b"]
    # The mid-run addition was delivered live from the content store (no snapshot
    # into a target repo was needed) before it was run.
    assert present_when_run["b"] is True


def test_run_queue_oneshot_does_not_drain(tmp_path: Path, monkeypatch) -> None:
    """With follow_queue off, only the passed tasks run even if siblings exist."""
    workspace = build_workspace(tmp_path, tasks={"a": "Do a.", "b": "Do b."})
    tasks_root = workspace / DEFAULT_TASKS_REPO

    calls: list[str] = []

    def fake_run_task(ws: Path, tr: Path, task: str, **_kwargs) -> TaskResult:
        calls.append(task)
        return TaskResult(task=task, title=task, success=True)

    monkeypatch.setattr("nightshift.engine.run_task", fake_run_task)
    run_queue(workspace, tasks_root, ["a"], follow_queue=False)

    assert calls == ["a"]


def test_run_queue_follow_attempts_each_task_once(tmp_path: Path, monkeypatch) -> None:
    """Tasks that remain on disk (a failed task, an evergreen task) are attempted
    exactly once per run — the drain loop terminates instead of re-running them."""
    workspace = build_workspace(tmp_path, tasks={
        "a": "---\nevergreen: true\n---\nDo a.",
        "b": "Do b.",
    })
    tasks_root = workspace / DEFAULT_TASKS_REPO

    calls: list[str] = []

    def fake_run_task(ws: Path, tr: Path, task: str, **_kwargs) -> TaskResult:
        calls.append(task)
        # a is evergreen (stays on disk); b "fails" (also stays on disk). Neither
        # should be retried within this run.
        return TaskResult(task=task, title=task, success=(task != "b"))

    monkeypatch.setattr("nightshift.engine.run_task", fake_run_task)
    run_queue(workspace, tasks_root, ["a", "b"], follow_queue=True)

    assert sorted(calls) == ["a", "b"]
    assert len(calls) == 2  # no re-runs, loop terminated


# --------------------------------------------------------------------------- #
# Layer 5 — run_task re-checks the disabled flag before launching a worker
# --------------------------------------------------------------------------- #


class _RecordingBackend:
    """A worker backend that records whether it was ever invoked.

    Used to prove run_task short-circuits a disabled task before it ever
    reaches the worker, rather than launching the agent and discarding the
    result downstream."""

    name = "recording"

    def __init__(self) -> None:
        self.calls = 0

    def run(self, spec, emit_log, should_abort, on_worker_start=None) -> WorkerResult:
        self.calls += 1
        return WorkerResult(returncode=0)


def test_run_task_skips_disabled_without_launching_worker(
    tmp_path: Path, monkeypatch,
) -> None:
    """A task disabled at launch time is skipped — the worker is never invoked
    and no worktree is cut. Guards the single named-task path (which bypasses
    the queue scan) and the race where a task is disabled after the list was
    built."""
    workspace, tasks_root, _repo_root = _full(tmp_path, tasks={
        "10.paused": "---\ndisabled: true\n---\nNot yet.",
    })

    backend = _RecordingBackend()
    original = backends_mod.get_backend
    backends_mod.get_backend = lambda *_a, **_k: backend
    try:
        events: list = []
        result = run_task(workspace, tasks_root, "10.paused", emit=events.append)
    finally:
        backends_mod.get_backend = original

    assert backend.calls == 0  # worker never launched
    assert result.status == "skipped"
    assert not result.success
    # A skip is not a failure (deliberate operator choice).
    assert result.resolved_status() == "skipped"
    # No worktree was created for the skipped task (under the workspace, outside
    # the target repo).
    assert not worktree_dir(workspace, REPO, "10.paused", queue=None).exists()
    # The result event reports the skip with a disabled reason.
    statuses = [
        e.payload.get("status")
        for e in events
        if e.type == TASK_RESULT
    ]
    assert statuses == ["skipped"]


def test_run_task_runs_enabled_task(tmp_path: Path, monkeypatch) -> None:
    """The disabled re-check does not block a normal (enabled) task: the worker
    is invoked as usual."""
    workspace, tasks_root, _repo_root = _full(tmp_path, tasks={"10.go": "Do it."})

    backend = _RecordingBackend()
    original = backends_mod.get_backend
    backends_mod.get_backend = lambda *_a, **_k: backend
    try:
        run_task(workspace, tasks_root, "10.go", emit=lambda _e: None)
    finally:
        backends_mod.get_backend = original

    assert backend.calls == 1  # enabled task reaches the worker


class _CommittingBackend:
    """A worker backend that produces a real commit on the task branch, so
    run_task proceeds past the no-commits short-circuit into validate/squash."""

    name = "committing"

    def __init__(self) -> None:
        self.calls = 0

    def run(self, spec, emit_log, should_abort, on_worker_start=None) -> WorkerResult:
        self.calls += 1
        worktree = Path(spec.cwd)
        (worktree / "produced.py").write_text("print('work')\n")
        subprocess.run(["git", "add", "."], cwd=worktree, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "work"],
            cwd=worktree, check=True, capture_output=True,
        )
        return WorkerResult(returncode=0)


def test_run_task_empty_validate_lands_without_running_validate(
    tmp_path: Path,
) -> None:
    """A queue with an empty validate command opts out of validation: the
    worker's commit lands even though the inherited default validate would fail.

    Licensed by the task spec — an empty-string validate disables validation for
    that queue and must not fall back to the inherited default.
    """
    # The inherited operator default validate is a command that always fails — if
    # it ran, the task could not land.
    workspace, tasks_root, repo_root = _full(
        tmp_path, tasks={"10.skip": "Do it."}, config={"validate": "false"},
    )
    # The queue explicitly opts out of validation (empty string).
    (tasks_root / "main" / "config.json").write_text(
        json.dumps({"validate": "", "order": [], "repo": "longitude"})
    )

    backend = _CommittingBackend()
    original = backends_mod.get_backend
    backends_mod.get_backend = lambda *_a, **_k: backend
    try:
        events: list = []
        result = run_task(workspace, tasks_root, "10.skip", emit=events.append)
    finally:
        backends_mod.get_backend = original

    assert backend.calls == 1
    assert result.success, result.error
    # No validate phase was emitted (it was skipped, not merely passed).
    phases = [
        e.payload.get("phase")
        for e in events
        if e.type == TASK_STATUS and e.payload.get("phase")
    ]
    assert "validate" not in phases
    # The worker's file actually landed on the target repo's main.
    assert (repo_root / "produced.py").exists()


def test_run_queue_skips_task_disabled_after_list_built(
    tmp_path: Path, monkeypatch,
) -> None:
    """A task disabled after run_queue's order list was built is skipped at
    launch by run_task's re-check, not run."""
    workspace, tasks_root, _repo_root = _full(tmp_path, tasks={
        "a": "Do a.",
        "b": "---\ndisabled: true\n---\nNot yet.",
    })

    calls: list[str] = []

    def fake_run_task(ws: Path, tr: Path, task: str, **kwargs) -> TaskResult:
        # Delegate to the real run_task so the disabled re-check runs, but only
        # record the tasks that get past it (skipped tasks return early).
        result = run_task(ws, tr, task, **kwargs)
        if result.status != "skipped":
            calls.append(task)
        return result

    backend = _RecordingBackend()
    original = backends_mod.get_backend
    backends_mod.get_backend = lambda *_a, **_k: backend
    monkeypatch.setattr("nightshift.engine.run_task", fake_run_task)
    try:
        summary = run_queue(workspace, tasks_root, ["a", "b"], follow_queue=False)
    finally:
        backends_mod.get_backend = original

    assert "b" not in calls  # disabled task never ran
    assert "a" in calls
    skipped = [r for r in summary.results if r.status == "skipped"]
    assert [r.task for r in skipped] == ["b"]


# --------------------------------------------------------------------------- #
# Task priorities + queue sort mode (content store)
# --------------------------------------------------------------------------- #


def test_order_stems_manual_mode_unchanged(tmp_path: Path) -> None:
    """Manual mode (the default) keeps listed-by-config then lexicographic order
    regardless of priorities — the legacy behavior."""
    tasks_root = _store_only(tmp_path, tasks={
        "a": "---\npriority: 0\n---\nDo a.",
        "b": "---\npriority: 5\n---\nDo b.",
        "c": "Do c.",
    })
    (tasks_root / "main" / "config.json").write_text('{"order": ["b", "a"]}\n')
    assert load_sort_mode(tasks_root) == "manual"
    # b, a are listed (config order); c is unlisted (lexicographic, last).
    assert order_stems(tasks_root, ["a", "b", "c"]) == ["b", "a", "c"]


def test_order_stems_priority_mode_sorts_by_priority(tmp_path: Path) -> None:
    """Priority mode sorts ascending by priority (0 = highest)."""
    tasks_root = _store_only(tmp_path, tasks={
        "a": "---\npriority: 3\n---\nDo a.",
        "b": "---\npriority: 0\n---\nDo b.",
        "c": "---\npriority: 1\n---\nDo c.",
    })
    save_sort_mode(tasks_root, "priority")
    assert load_sort_mode(tasks_root) == "priority"
    assert order_stems(tasks_root, ["a", "b", "c"]) == ["b", "c", "a"]


def test_order_stems_priority_ties_break_by_manual_order(tmp_path: Path) -> None:
    """Equal priorities keep the manual (config order) relative arrangement."""
    tasks_root = _store_only(tmp_path, tasks={
        "a": "---\npriority: 2\n---\nDo a.",
        "b": "---\npriority: 2\n---\nDo b.",
        "c": "---\npriority: 0\n---\nDo c.",
    })
    # Manual order puts b before a; both are priority 2, so they keep that order
    # under the higher-priority c.
    (tasks_root / "main" / "config.json").write_text(
        '{"order": ["b", "a"], "sort": "priority"}\n'
    )
    assert order_stems(tasks_root, ["a", "b", "c"]) == ["c", "b", "a"]


def test_order_stems_missing_priority_defaults_lowest(tmp_path: Path) -> None:
    """A task without a priority field sorts as the lowest (5)."""
    tasks_root = _store_only(tmp_path, tasks={
        "a": "Do a.",                       # no priority -> 5
        "b": "---\npriority: 0\n---\nDo b.",
    })
    save_sort_mode(tasks_root, "priority")
    assert order_stems(tasks_root, ["a", "b"]) == ["b", "a"]


def test_save_sort_mode_coerces_unknown_to_manual(tmp_path: Path) -> None:
    """An unknown mode degrades to manual, and the order key is preserved."""
    tasks_root = _store_only(tmp_path, tasks={"a": "Do a."})
    (tasks_root / "main" / "config.json").write_text('{"order": ["a"]}\n')
    assert save_sort_mode(tasks_root, "bogus") == "manual"
    data = json.loads((tasks_root / "main" / "config.json").read_text())
    assert data["sort"] == "manual"
    assert data["order"] == ["a"]  # sibling key preserved


def test_live_ordered_queue_respects_priority_mode(tmp_path: Path) -> None:
    """The engine's live scan (the play/execute source) honours priority mode."""
    tasks_root = _store_only(tmp_path, tasks={
        "a": "---\npriority: 4\n---\nDo a.",
        "b": "---\npriority: 1\n---\nDo b.",
        "c": "---\npriority: 1\n---\nDo c.",
    })
    (tasks_root / "main" / "config.json").write_text(
        '{"order": ["a", "c", "b"], "sort": "priority"}\n'
    )
    # b and c tie at priority 1; manual order lists c before b, so c precedes b.
    assert live_ordered_queue(tasks_root) == ["c", "b", "a"]


def test_list_queue_includes_priority_and_orders(tmp_path: Path) -> None:
    """list_queue (the UI source) returns each task's priority and orders by the
    active sort mode."""
    tasks_root = _store_only(tmp_path, tasks={
        "a": "---\npriority: 3\n---\nDo a.",
        "b": "---\npriority: 0\n---\nDo b.",
    })
    save_sort_mode(tasks_root, "priority")
    queue = list_queue(tasks_root)
    assert [item["task"] for item in queue] == ["b", "a"]
    by_task = {item["task"]: item for item in queue}
    assert by_task["a"]["priority"] == 3
    assert by_task["b"]["priority"] == 0


def test_play_priorities_round_trip_and_clean(tmp_path: Path) -> None:
    """The play-priority filter persists sorted/de-duped/clamped, preserving
    sibling keys; an empty list clears it."""
    tasks_root = _store_only(tmp_path, tasks={"a": "Do a."})
    (tasks_root / "main" / "config.json").write_text('{"order": ["a"]}\n')
    # Out-of-range (7) and duplicate (1) are dropped; result is sorted.
    assert save_play_priorities(tasks_root, [3, 1, 1, 7]) == [1, 3]
    assert load_play_priorities(tasks_root) == [1, 3]
    data = json.loads((tasks_root / "main" / "config.json").read_text())
    assert data["play_priorities"] == [1, 3]
    assert data["order"] == ["a"]  # sibling key preserved
    # An empty list clears the filter (all priorities play).
    assert save_play_priorities(tasks_root, []) == []
    assert load_play_priorities(tasks_root) == []


def test_live_ordered_queue_applies_play_filter(tmp_path: Path) -> None:
    """live_ordered_queue (the play/execute source) drops tasks outside the
    active play-priority filter, including non-contiguous selections."""
    tasks_root = _store_only(tmp_path, tasks={
        "a": "---\npriority: 0\n---\nDo a.",
        "b": "---\npriority: 1\n---\nDo b.",
        "c": "---\npriority: 3\n---\nDo c.",
        "d": "Do d.",  # no priority -> 5
    })
    # No filter: everything is runnable (filename order, manual sort).
    assert live_ordered_queue(tasks_root) == ["a", "b", "c", "d"]
    # Non-contiguous selection P0 + P3 keeps only a and c.
    save_play_priorities(tasks_root, [0, 3])
    assert live_ordered_queue(tasks_root) == ["a", "c"]
    # The default (5) is selectable too — it matches the file with no priority.
    save_play_priorities(tasks_root, [5])
    assert live_ordered_queue(tasks_root) == ["d"]


def test_list_queue_not_filtered_by_play_priorities(tmp_path: Path) -> None:
    """list_queue (the UI management view) shows every task regardless of the
    play filter, so out-of-scope tasks remain visible and editable."""
    tasks_root = _store_only(tmp_path, tasks={
        "a": "---\npriority: 0\n---\nDo a.",
        "b": "---\npriority: 3\n---\nDo b.",
    })
    save_play_priorities(tasks_root, [0])
    assert [item["task"] for item in list_queue(tasks_root)] == ["a", "b"]


def test_set_task_meta_priority_round_trips(tmp_path: Path) -> None:
    """Editing priority through set_task_meta persists and is read back."""
    tasks_root = _store_only(tmp_path, tasks={"a": "---\ntitle: A\n---\nDo a."})
    set_task_meta(tasks_root, "a", {"priority": 2})
    assert read_task(tasks_root, "a")["frontmatter"]["priority"] == 2
    # A task with no priority field resolves to the default (lowest).
    (tasks_root / "main" / "plain.md").write_text("---\ntitle: Plain\n---\nDo it.\n")
    assert read_task(tasks_root, "plain")["frontmatter"]["priority"] == 5


def test_run_queue_resorts_pending_by_priority_between_tasks(
    tmp_path: Path, monkeypatch,
) -> None:
    """A priority change made mid-run reshuffles the not-yet-run tail at the next
    task boundary, without re-running an already-started task."""
    workspace = build_workspace(tmp_path, tasks={
        "a": "---\npriority: 0\n---\nDo a.",
        "b": "---\npriority: 5\n---\nDo b.",
        "c": "---\npriority: 5\n---\nDo c.",
    })
    tasks_root = workspace / DEFAULT_TASKS_REPO
    (tasks_root / "main" / "config.json").write_text(
        '{"order": ["a", "b", "c"], "sort": "priority"}\n'
    )

    calls: list[str] = []

    def fake_run_task(ws: Path, tr: Path, task: str, **_kwargs) -> TaskResult:
        calls.append(task)
        if task == "a":
            # While a runs, the operator bumps c above b. The next pick must be c.
            (tr / "main" / "c.md").write_text(
                "---\npriority: 1\n---\nDo c.\n"
            )
        return TaskResult(task=task, title=task, success=True)

    monkeypatch.setattr("nightshift.engine.run_task", fake_run_task)
    run_queue(workspace, tasks_root, ["a", "b", "c"], follow_queue=True)

    # a first (it was highest at play). After a, c (now P1) jumps ahead of b (P5).
    assert calls == ["a", "c", "b"]
    assert len(calls) == 3  # the started task is never re-run


def test_run_queue_oneshot_ignores_sort_mode(tmp_path: Path, monkeypatch) -> None:
    """Oneshot runs drain exactly the passed list and are not re-sorted live."""
    workspace = build_workspace(tmp_path, tasks={
        "a": "---\npriority: 5\n---\nDo a.",
        "b": "---\npriority: 0\n---\nDo b.",
    })
    tasks_root = workspace / DEFAULT_TASKS_REPO
    save_sort_mode(tasks_root, "priority")

    calls: list[str] = []

    def fake_run_task(ws: Path, tr: Path, task: str, **_kwargs) -> TaskResult:
        calls.append(task)
        return TaskResult(task=task, title=task, success=True)

    monkeypatch.setattr("nightshift.engine.run_task", fake_run_task)
    run_queue(workspace, tasks_root, ["a"], follow_queue=False)

    assert calls == ["a"]
