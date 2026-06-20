"""Tests for ``nightshift.run_local`` — the local nightshift runner."""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

import nightshift.backends as backends_mod
from nightshift._paths import PROMPTS_DIR, TEMPLATES_DIR
from nightshift.backends import WorkerResult
from nightshift.engine import (
    AUTOSTASH_MESSAGE,
    QUEUE_SNAPSHOT_PATHSPECS,
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


TEMPLATES = TEMPLATES_DIR


def _git_init(root: Path) -> None:
    """Init a repo with a repo-local identity.

    The functions under test (``_commit_dispatch``, ``squash_to_main``) run
    ``git commit`` themselves, so the identity must live in the repo config —
    not just in the env passed to the test's own subprocess calls. Otherwise
    those internal commits fail on hosts without a global git identity (CI).

    The branch is pinned to ``main`` for the same host-independence reason: the
    engine rebases onto ``main`` by name, so a host whose ``init.defaultBranch``
    is ``master`` would otherwise fail with "invalid upstream 'main'". The
    portable ``symbolic-ref`` rename works on the unborn branch before any
    commit and on all git versions (unlike ``git init -b``).
    """
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
        cwd=root, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test"],
        cwd=root, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=root, check=True, capture_output=True,
    )


def _seed_tree(tmp_path: Path, *, tasks: dict[str, str] | None = None) -> Path:
    """Create a minimal nightshift tree for testing.

    The operator ``config.json`` lives at the repo root (where the engine reads
    it). Task templates and the worker prompt charter ship inside the installed
    package, so the temp tree only needs the config and a ``.tasks`` queue.
    """
    (tmp_path / "config.json").write_text(
        json.dumps({
            "model": "claude-sonnet-4-6",
            "max_turns": 60,
            "automerge": True,
            "draft": False,
            "evergreen_tasks": ["00._questions", "00._todo"],
            "diff_cap_lines": 1500,
        })
    )

    (tmp_path / ".tasks").mkdir(parents=True, exist_ok=True)
    if tasks:
        for name, content in tasks.items():
            (tmp_path / ".tasks" / f"{name}.md").write_text(content)
    return tmp_path


def test_build_task_list_single(tmp_path: Path) -> None:
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    result = build_task_list(root, "10.hello")
    assert result == ["10.hello"]


def test_build_task_list_all_skips_disabled(tmp_path: Path) -> None:
    import subprocess

    root = _seed_tree(tmp_path, tasks={
        "10.active-task": "---\nmodel: claude-sonnet-4-6\n---\nDo it.",
        "20.paused-task": "---\ndisabled: true\n---\nNot yet.",
        "30.no-frontmatter": "Just a plain task.",
    })
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=root, check=True, capture_output=True, env=git_env,
    )

    result = build_task_list(root, "all")
    assert "10.active-task" in result
    assert "30.no-frontmatter" in result
    assert "20.paused-task" not in result


def test_build_task_list_all_skips_autosplit(tmp_path: Path) -> None:
    root = _seed_tree(tmp_path, tasks={
        "00._questions": "---\nautosplit: true\n---\n## Questions:\n",
        "00._todo": "---\nautosplit: true\n---\n## TO DO:\n",
        "10.real-task": "Do the thing.",
        "02.service-triage": "---\nevergreen: true\n---\nCheck logs.",
    })
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }
    _git_init(root)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=root, check=True, capture_output=True, env=git_env,
    )

    result = build_task_list(root, "all")
    assert "10.real-task" in result
    assert "02.service-triage" in result
    assert "00._questions" not in result
    assert "00._todo" not in result


def test_build_task_list_daily_expansion(tmp_path: Path) -> None:
    root = _seed_tree(tmp_path, tasks={
        "00._todo": "---\nautosplit: true\n---\nFix the following:\n\n1. Fix ops\n2. Add toggle\n",
    })
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }
    _git_init(root)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=root, check=True, capture_output=True, env=git_env,
    )

    result = build_task_list(root, "00._todo")
    assert len(result) == 2
    assert all(r.startswith("00.") for r in result)
    assert (root / ".tasks/00._todo.md").read_text() == (
        TEMPLATES / "00._todo.md"
    ).read_text()


def test_build_task_list_all_sorts_spawned_in_place(tmp_path: Path) -> None:
    """Spawned daily items run where they sort, not ahead of the whole queue."""
    root = _seed_tree(tmp_path, tasks={
        "04.2.playbook-thing": "Do the playbook thing.",
        "99._todo": "---\nautosplit: true\n---\nFix the following:\n\n1. Export the universe\n2. Follow tradeview format\n",
    })
    _git_init(root)
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=root, check=True, capture_output=True, env=git_env,
    )

    result = build_task_list(root, "all")

    spawned = [r for r in result if r.startswith("99.")]
    assert spawned, "expected spawned 99.* subtasks"
    assert "04.2.playbook-thing" in result
    assert result.index("04.2.playbook-thing") < min(
        result.index(s) for s in spawned
    ), f"04.2.* should sort before spawned 99.* items, got {result}"
    assert result == sorted(result)


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
    log = open_run_log(tmp_path)
    try:
        path = Path(log.name)
        assert path.parent == tmp_path / ".tasks" / "logs"
        assert path.suffix == ".log"
        assert path.name.startswith("nightshift-local-")
        log.write("progress line\n")
        log.flush()
    finally:
        log.close()
    assert path.read_text() == "progress line\n"


def test_build_prompt_matches_ci_format(tmp_path: Path) -> None:
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    prompt = build_prompt(root, "10.hello")
    assert prompt.startswith("Your task file is: .tasks/10.hello.md\n")
    assert "The TASK variable is: 10.hello" in prompt
    # The shipped local worker charter is appended verbatim after the injected vars.
    assert "You are the nightshift worker running **locally**." in prompt


def test_build_prompt_injects_default_validate(tmp_path: Path) -> None:
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    prompt = build_prompt(root, "10.hello")
    assert "The VALIDATE command is: just validate" in prompt


def test_build_prompt_injects_playlist_validate_override(tmp_path: Path) -> None:
    """A playlist's ``validate`` override must reach the worker prompt, not just
    the engine gate, so the worker self-validates with the queue's command."""
    root = _seed_tree(tmp_path)
    playlist_dir = root / ".tasks" / "nightshift"
    playlist_dir.mkdir(parents=True, exist_ok=True)
    (playlist_dir / "config.json").write_text(
        json.dumps({"validate": "just validate-nightshift", "order": []})
    )
    (playlist_dir / "10.hello.md").write_text("Do something.")
    prompt = build_prompt(root, "10.hello", ".tasks/nightshift")
    assert "The VALIDATE command is: just validate-nightshift" in prompt
    assert "The VALIDATE command is: just validate\n" not in prompt


def test_build_prompt_injects_task_file_path_for_main_queue(tmp_path: Path) -> None:
    """``$TASK_FILE`` carries the task's real path so the worker removes the right
    file. For the main queue that's ``.tasks/<task>.md``."""
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    prompt = build_prompt(root, "10.hello")
    assert "The TASK_FILE variable is: .tasks/10.hello.md" in prompt


def test_build_prompt_injects_playlist_task_file_path(tmp_path: Path) -> None:
    """A playlist task lives at ``.tasks/<playlist>/<task>.md``, so ``$TASK_FILE``
    must point there — not at a non-existent ``.tasks/<task>.md``. This is what a
    completed playlist task needs removed so it leaves the queue after it lands."""
    root = _seed_tree(tmp_path)
    playlist_dir = root / ".tasks" / "nightshift"
    playlist_dir.mkdir(parents=True, exist_ok=True)
    (playlist_dir / "config.json").write_text(json.dumps({"order": []}))
    (playlist_dir / "10.hello.md").write_text("Do something.")
    prompt = build_prompt(root, "10.hello", ".tasks/nightshift")
    assert "The TASK_FILE variable is: .tasks/nightshift/10.hello.md" in prompt
    assert "The TASK_FILE variable is: .tasks/10.hello.md\n" not in prompt


def test_local_prompt_removes_task_file_by_path_not_hardcoded() -> None:
    """The shipped local worker prompt must remove ``$TASK_FILE`` (the task's real
    path), not a hardcoded ``.tasks/$TASK.md`` — otherwise a completed playlist
    task's file is never deleted and it lingers in the queue after completion."""
    prompt_body = (PROMPTS_DIR / "nightshift-local.md").read_text()
    assert 'git rm "$TASK_FILE"' in prompt_body
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
    from nightshift.backends import AgentStreamParser

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
    from nightshift.backends import AgentStreamParser

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


def test_setup_worktree_creates_dir_and_symlinks(tmp_path: Path) -> None:
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    (root / ".venv").mkdir()
    (root / ".venv/bin").mkdir(parents=True)
    (root / "services/dashboard_ui/node_modules").mkdir(parents=True)

    _git_init(root)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=root,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@test",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@test",
        },
    )

    worktree = setup_worktree(root, "10.hello")
    assert worktree.exists()
    assert (worktree / ".venv").is_symlink()
    assert (worktree / "services/dashboard_ui/node_modules").is_symlink()


def test_squash_to_main_produces_single_commit(tmp_path: Path) -> None:
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }

    _git_init(root)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=root, check=True, capture_output=True, env=git_env,
    )

    worktree = setup_worktree(root, "10.hello")
    (worktree / "new_file.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "."], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "work1"],
        cwd=worktree, check=True, capture_output=True, env=git_env,
    )
    (worktree / "another.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "work2"],
        cwd=worktree, check=True, capture_output=True, env=git_env,
    )

    sha, detail, recoverable = squash_to_main(root, "10.hello", "hello world")
    assert sha is not None
    assert detail == ""
    assert recoverable is False

    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=root, capture_output=True, text=True,
    ).stdout
    assert "task: hello world" in log
    lines = [l for l in log.strip().splitlines() if l.strip()]
    assert len(lines) == 2


def _commit_files(root: Path, files: dict[str, str], git_env: dict[str, str]) -> str:
    """Write ``files`` (path → content), commit them, and return the short sha."""
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "work"],
        cwd=root, check=True, capture_output=True, env=git_env,
    )
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=root, capture_output=True, text=True,
    ).stdout.strip()


def test_compute_code_loc_counts_code_only(tmp_path: Path) -> None:
    """compute_code_loc counts added/removed code lines and excludes comments,
    blank lines, docs, and build/lock files (the categories the spec drops)."""
    root = _seed_tree(tmp_path)
    git_env = _git_init_commit(root)
    sha = _commit_files(
        root,
        {
            # 3 code lines; a comment and a blank line are dropped.
            "mod.py": "import os\n# a comment\nx = 1\n\nprint(x)\n",
            # JS: 1 code line, 1 // comment dropped.
            "app.js": "// header\nconst y = 2;\n",
            # Excluded entirely: docs and a lockfile.
            "README.md": "# Title\n\nProse here.\n",
            "uv.lock": "lots of generated content\n",
        },
        git_env,
    )

    assert compute_code_loc(root, sha) == 4


def test_compute_code_loc_counts_removed_lines(tmp_path: Path) -> None:
    """Churn counts removals as well as additions."""
    root = _seed_tree(tmp_path)
    git_env = _git_init_commit(root)
    _commit_files(root, {"mod.py": "a = 1\nb = 2\nc = 3\n"}, git_env)
    sha = _commit_files(root, {"mod.py": "a = 1\n"}, git_env)

    # Two lines removed (b, c); nothing added.
    assert compute_code_loc(root, sha) == 2


def test_compute_code_loc_zero_for_docs_only_commit(tmp_path: Path) -> None:
    """A commit touching only docs/build files yields zero code churn."""
    root = _seed_tree(tmp_path)
    git_env = _git_init_commit(root)
    sha = _commit_files(
        root,
        {"docs/guide.md": "# Guide\n", "BUILD.bazel": "py_library(name = 'x')\n"},
        git_env,
    )

    assert compute_code_loc(root, sha) == 0


def test_compute_code_loc_bad_sha_returns_zero(tmp_path: Path) -> None:
    """A git error (unknown sha) degrades to 0 rather than raising."""
    root = _seed_tree(tmp_path)
    _git_init_commit(root)

    assert compute_code_loc(root, "deadbeef") == 0


def test_compute_code_loc_excludes_queue_and_output_dirs(tmp_path: Path) -> None:
    """Files under `.tasks/`, `dist/`, and `build/` never count as code, even
    when the suffix would otherwise be code (a `.tasks/*.py` brief, a built
    `dist/*.js` bundle)."""
    root = _seed_tree(tmp_path)
    git_env = _git_init_commit(root)
    sha = _commit_files(
        root,
        {
            "real.py": "x = 1\ny = 2\n",
            ".tasks/99.brief.py": "ignored = True\n",
            "services/ui/dist/bundle.js": "const z = 3;\n",
            "build/out.ts": "const w = 4;\n",
        },
        git_env,
    )

    assert compute_code_loc(root, sha) == 2


def test_landed_loc_matches_squash_commit_after_intra_task_churn(
    tmp_path: Path,
) -> None:
    """The LOC figure a task lands with is the churn of its *squash commit* on
    ``main`` — the same metric the Stats backfill reconstructs from a record's
    ``commit_sha``. A task that writes 3 lines then drops 2 within its branch
    lands a net diff of 1 code line, so the figure is 1 (not the 5-line
    intra-task churn a branch-history sum would report). Pinning the squash
    metric keeps live capture and backfill consistent across all of history."""
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    git_env = _git_init_commit(root)

    worktree = setup_worktree(root, "10.hello")
    (worktree / "mod.py").write_text("a = 1\nb = 2\nc = 3\n")
    subprocess.run(["git", "add", "."], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add three"],
        cwd=worktree, check=True, capture_output=True, env=git_env,
    )
    (worktree / "mod.py").write_text("a = 1\n")
    subprocess.run(["git", "add", "."], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "drop two"],
        cwd=worktree, check=True, capture_output=True, env=git_env,
    )

    sha, _detail, _recoverable = squash_to_main(root, "10.hello", "hello world")
    assert sha is not None

    # Net squash diff: 1 added code line. This is what the engine records at land
    # time and what the backfill recovers from the sha — the two now agree.
    assert compute_code_loc(root, sha) == 1


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
    churn, so the Stats page reflects shipped work rather than reading zero."""
    root = _seed_tree(tmp_path)
    git_env = _git_init_commit(root)
    # Land a real commit on main: 4 code lines (a comment and a blank dropped).
    sha = _commit_files(
        root, {"mod.py": "import os\n# c\nx = 1\n\ny = 2\nz = 3\n"}, git_env
    )

    store = RunStore(root)
    _write_completed_run(store, "10.x", commit_sha=sha, loc=None)

    rec = store.list_runs()[0]["tasks"][0]
    assert rec["loc"] == compute_code_loc(root, sha)
    assert rec["loc"] == 4


def test_run_store_backfill_sums_multiple_landed_commits(tmp_path: Path) -> None:
    """A task that landed more than once (comma-separated shas) backfills the sum
    of every landed commit's churn."""
    root = _seed_tree(tmp_path)
    git_env = _git_init_commit(root)
    sha1 = _commit_files(root, {"a.py": "a = 1\nb = 2\n"}, git_env)
    sha2 = _commit_files(root, {"c.py": "c = 3\n"}, git_env)

    store = RunStore(root)
    _write_completed_run(store, "10.x", commit_sha=f"{sha1}, {sha2}", loc=None)

    rec = store.list_runs()[0]["tasks"][0]
    assert rec["loc"] == compute_code_loc(root, sha1) + compute_code_loc(root, sha2)
    assert rec["loc"] == 3


def test_run_store_backfill_keeps_recorded_loc(tmp_path: Path) -> None:
    """A captured non-zero loc is authoritative — backfill never overwrites it.
    (Live capture and backfill use the same squash-commit metric, so they would
    agree anyway; the guard simply avoids recomputing a figure already present.)"""
    root = _seed_tree(tmp_path)
    git_env = _git_init_commit(root)
    sha = _commit_files(root, {"mod.py": "a = 1\n"}, git_env)

    store = RunStore(root)
    _write_completed_run(store, "10.x", commit_sha=sha, loc=99)

    rec = store.list_runs()[0]["tasks"][0]
    assert rec["loc"] == 99


def test_run_store_backfill_skips_records_without_a_commit(tmp_path: Path) -> None:
    """A completed record that landed nothing (no sha — a no-change run) keeps
    loc as None rather than fabricating a count."""
    root = _seed_tree(tmp_path)
    _git_init_commit(root)

    store = RunStore(root)
    _write_completed_run(store, "10.x", commit_sha=None, loc=None)

    rec = store.list_runs()[0]["tasks"][0]
    assert rec["loc"] is None


def _git_init_commit(root: Path) -> dict[str, str]:
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }
    _git_init(root)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=root, check=True, capture_output=True, env=git_env,
    )
    return git_env


def _commit_work_on_branch(root: Path, task: str, git_env: dict[str, str]) -> Path:
    worktree = setup_worktree(root, task)
    (worktree / "new_file.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "."], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "work"],
        cwd=worktree, check=True, capture_output=True, env=git_env,
    )
    return worktree


def test_squash_to_main_autostash_lands_over_dirty_code(tmp_path: Path) -> None:
    """With autostash on (the default), tracked operator code WIP on main is set
    aside for the merge+commit and restored afterward, so the land succeeds and
    the operator's edit is byte-identical with no leftover stash."""
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    git_env = _git_init_commit(root)
    _commit_work_on_branch(root, "10.hello", git_env)

    # Dirty main on a tracked code file (mirrors a developer mid-edit).
    cfg = root / "config.json"
    dirty = cfg.read_text() + "\n# local edit\n"
    cfg.write_text(dirty)

    sha, detail, recoverable = squash_to_main(root, "10.hello", "hello world")
    assert sha is not None
    assert detail == ""
    assert recoverable is False

    # The task landed, and the operator's WIP is restored verbatim (and NOT part
    # of the task commit — it was stashed during the merge).
    assert (root / "new_file.py").exists()
    assert cfg.read_text() == dirty
    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=root, capture_output=True, text=True,
    ).stdout
    assert "config.json" not in committed
    # No autostash entry is left behind.
    stash_list = subprocess.run(
        ["git", "stash", "list"], cwd=root, capture_output=True, text=True,
    ).stdout
    assert AUTOSTASH_MESSAGE not in stash_list

    teardown_worktree(root, "10.hello")


def test_squash_to_main_refuses_dirty_main_when_autostash_off(tmp_path: Path) -> None:
    """With autostash off, a tracked code change on main blocks the squash with a
    precise recoverable reason and must not leave a half-merged tree."""
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    git_env = _git_init_commit(root)
    _commit_work_on_branch(root, "10.hello", git_env)

    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
    ).stdout.strip()

    cfg = root / "config.json"
    cfg.write_text(cfg.read_text() + "\n# local edit\n")

    sha, detail, recoverable = squash_to_main(
        root, "10.hello", "hello world", autostash=False,
    )
    assert sha is None
    assert "uncommitted changes" in detail
    assert recoverable is True  # clearing the dirty tree lets a retry succeed

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
    ).stdout.strip()
    assert head_after == head_before
    assert "# local edit" in cfg.read_text()

    teardown_worktree(root, "10.hello")


def test_squash_to_main_lands_over_dirty_tasks(tmp_path: Path) -> None:
    """Queue state never blocks a land: a dirty task brief and a tracked live
    playlist run record do not trip the precheck and are not stashed."""
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    playlist = root / ".tasks/nightshift"
    playlist.mkdir(parents=True, exist_ok=True)
    (playlist / "config.json").write_text('{"order": ["queue-view"]}\n')
    (playlist / "queue-view.md").write_text("---\ntitle: Queue view\n---\nShow it.\n")
    runs = playlist / "runs/2026-01-01T00-00-00Z-abc"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "run.json").write_text('{"id": "old"}\n')

    git_env = _git_init_commit(root)
    _commit_work_on_branch(root, "10.hello", git_env)

    # Mid-run dirt: an edited brief AND a rewritten live run record (both tracked).
    (playlist / "queue-view.md").write_text("---\ntitle: Queue view\n---\nEdited.\n")
    (runs / "run.json").write_text('{"id": "live", "in_progress": true}\n')

    sha, detail, recoverable = squash_to_main(root, "10.hello", "hello world")
    assert sha is not None
    assert detail == ""
    assert (root / "new_file.py").exists()
    # Nothing was stashed — .tasks/ is excluded from the landing blockers.
    stash_list = subprocess.run(
        ["git", "stash", "list"], cwd=root, capture_output=True, text=True,
    ).stdout
    assert stash_list.strip() == ""
    # The live run record is still uncommitted runtime state, not swept in.
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True,
    ).stdout
    assert "run.json" in dirty

    teardown_worktree(root, "10.hello")


def test_squash_to_main_autostash_pop_conflict_preserves_work(tmp_path: Path) -> None:
    """When restoring set-aside WIP conflicts with the file the task just landed,
    the land still records, the failure detail explains it, and the stash entry is
    preserved so nothing is lost."""
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    git_env = _git_init_commit(root)

    # Branch edits an existing tracked file.
    worktree = setup_worktree(root, "10.hello")
    (worktree / "config.json").write_text('{"branch": true}\n')
    subprocess.run(["git", "add", "."], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "branch edit"],
        cwd=worktree, check=True, capture_output=True, env=git_env,
    )

    # Operator has uncommitted edits to the SAME file → pop will conflict.
    cfg = root / "config.json"
    cfg.write_text('{"operator": true}\n')

    sha, detail, recoverable = squash_to_main(root, "10.hello", "hello world")
    assert sha is not None  # the land happened
    assert "set-aside" in detail and "stash" in detail
    # The stash entry is preserved for manual restore.
    stash_list = subprocess.run(
        ["git", "stash", "list"], cwd=root, capture_output=True, text=True,
    ).stdout
    assert AUTOSTASH_MESSAGE in stash_list

    teardown_worktree(root, "10.hello")


def test_squash_to_main_reports_content_conflict_unrecoverable(tmp_path: Path) -> None:
    """When the branch and main make overlapping edits to the same file, the
    squash hits a real 3-way conflict. That is NOT retry-recoverable (re-running
    the same merge fails identically), so squash_to_main must say so and name the
    conflicting file rather than dumping git's rerere bookkeeping."""
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    git_env = _git_init_commit(root)

    target = root / "config.json"

    # Branch edits an existing tracked file.
    worktree = setup_worktree(root, "10.hello")
    (worktree / "config.json").write_text('{"branch": true}\n')
    subprocess.run(["git", "add", "."], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "branch edit"],
        cwd=worktree, check=True, capture_output=True, env=git_env,
    )

    # main edits the SAME file divergently and commits.
    target.write_text('{"main": true}\n')
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "main edit"],
        cwd=root, check=True, capture_output=True, env=git_env,
    )
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
    ).stdout.strip()

    sha, detail, recoverable = squash_to_main(root, "10.hello", "hello world")
    assert sha is None
    assert recoverable is False
    assert "conflict" in detail.lower()
    assert "config.json" in detail

    # The failed merge was cleaned up: HEAD unchanged, no half-merged tree.
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
    ).stdout.strip()
    assert head_after == head_before
    assert target.read_text() == '{"main": true}\n'

    teardown_worktree(root, "10.hello")


def test_landing_blockers_excludes_tasks(tmp_path: Path) -> None:
    """`_landing_blockers` reports tracked code changes but never `.tasks/` ones."""
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    _git_init_commit(root)

    (root / ".tasks/10.hello.md").write_text("Edited brief.")
    cfg = root / "config.json"
    cfg.write_text(cfg.read_text() + "\n# local edit\n")

    paths = [_porcelain_path(line) for line in _landing_blockers(root)]
    assert any("config.json" in p for p in paths)
    assert all(not p.startswith(".tasks/") for p in paths)


def _prep_preconditions(root: Path, monkeypatch) -> None:
    """Satisfy every non-dirty-tree check so check_preconditions reaches (and
    passes) the dirty-tree gate: fake the claude binary + API key and a trivial
    `just validate`."""
    monkeypatch.setattr("nightshift.engine.shutil.which", lambda _name: "/bin/claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    (root / "justfile").write_text("validate:\n\t@true\n")


def test_check_preconditions_ignores_dirty_tasks(tmp_path: Path, monkeypatch) -> None:
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    _git_init_commit(root)
    _prep_preconditions(root, monkeypatch)

    (root / ".tasks/10.hello.md").write_text("Edited brief.")
    # Should not raise: .tasks/ dirt never blocks.
    check_preconditions(root)


def test_check_preconditions_notice_on_code_wip_autostash_on(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    _git_init_commit(root)
    _prep_preconditions(root, monkeypatch)

    # Dirty a tracked code file. Keep config.json valid JSON so autostash stays on
    # (an invalid edit would make resolve_config fall back to defaults anyway).
    cfg = root / "config.json"
    config = json.loads(cfg.read_text())
    config["_operator_local_edit"] = True
    cfg.write_text(json.dumps(config) + "\n")

    check_preconditions(root)  # autostash defaults on → notice, no exit
    assert "set aside" in capsys.readouterr().out


def test_check_preconditions_exits_on_code_wip_autostash_off(
    tmp_path: Path, monkeypatch,
) -> None:
    import pytest

    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    _git_init_commit(root)
    _prep_preconditions(root, monkeypatch)

    cfg = root / "config.json"
    config = json.loads(cfg.read_text())
    config["autostash_operator_work"] = False
    # Valid JSON, but different from the committed version → a tracked code
    # blocker that, with autostash off, must hard-exit.
    cfg.write_text(json.dumps(config) + "\n")

    with pytest.raises(SystemExit):
        check_preconditions(root)


def test_recover_task_relands_after_blocker_cleared(tmp_path: Path) -> None:
    """A squash that fails on a dirty tree leaves the branch intact; once the
    blocker is cleared, recover_task lands the work and tears the branch down."""
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    git_env = _git_init_commit(root)
    _commit_work_on_branch(root, "10.hello", git_env)

    cfg = root / "config.json"
    original = cfg.read_text()
    cfg.write_text(original + "\n# local edit\n")

    sha, detail, _ = squash_to_main(root, "10.hello", "hello world", autostash=False)
    assert sha is None  # blocked by the dirty tree

    # Branch is preserved so the validated work can be recovered.
    branches = subprocess.run(
        ["git", "branch"], cwd=root, capture_output=True, text=True,
    ).stdout
    assert "task-local/main/10.hello" in branches

    # Clear the blocker, then recover.
    cfg.write_text(original)
    result = recover_task(root, "10.hello", "hello world")
    assert result.success
    assert result.commit_sha
    assert (root / "new_file.py").exists()

    # Branch is gone and the squash landed as a single commit.
    branches = subprocess.run(
        ["git", "branch"], cwd=root, capture_output=True, text=True,
    ).stdout
    assert "task-local/main/10.hello" not in branches
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=root, capture_output=True, text=True,
    ).stdout
    assert "task: hello world" in log


def test_recover_task_without_branch_reports_clearly(tmp_path: Path) -> None:
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    _git_init_commit(root)
    result = recover_task(root, "10.hello", "hello world")
    assert not result.success
    assert "nothing to recover" in (result.error or "")


def test_teardown_worktree_removes_dir_and_branch(tmp_path: Path) -> None:
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }
    _git_init(root)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=root, check=True, capture_output=True, env=git_env,
    )

    worktree = setup_worktree(root, "10.hello")
    assert worktree.exists()

    teardown_worktree(root, "10.hello")
    assert not worktree.exists()
    branches = subprocess.run(
        ["git", "branch"], cwd=root, capture_output=True, text=True,
    ).stdout
    assert "task-local/main/10.hello" not in branches


def test_enough_free_disk_true_for_low_threshold(tmp_path: Path) -> None:
    # A live filesystem always has well over 0% free.
    assert enough_free_disk(tmp_path, min_free_pct=0.0) is True


def test_enough_free_disk_false_for_impossible_threshold(tmp_path: Path) -> None:
    # No filesystem can have > 100% free, so this must fail the guard.
    assert enough_free_disk(tmp_path, min_free_pct=100.001) is False


def test_acquire_lock_blocks_second_instance(tmp_path: Path) -> None:
    root = tmp_path
    (root / ".worktrees").mkdir(parents=True)

    fd1 = acquire_lock(root)
    assert fd1 >= 0

    import pytest
    with pytest.raises(SystemExit):
        acquire_lock(root)

    os.close(fd1)


def test_write_failure_log_captures_error_and_diff(tmp_path: Path) -> None:
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }
    _git_init(root)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=root, check=True, capture_output=True, env=git_env,
    )

    worktree = setup_worktree(root, "10.hello")

    log_path = _write_failure_log(
        root, worktree, "10.hello", "just validate failed: syntax error",
        validate_stderr="broken.py:1: SyntaxError: invalid syntax\n",
    )

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
) -> dict[str, str]:
    """Seed a repo where the task branch and main make overlapping edits to a
    shared (non-config) file, so the squash hits a real content conflict. A
    no-op ``just validate`` recipe lets the resolve path validate cleanly."""
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    cfg = root / "config.json"
    config = json.loads(cfg.read_text())
    config["max_resolve_attempts"] = max_attempts
    cfg.write_text(json.dumps(config))
    # The resolve charter ships inside the package; the engine reads it from there.
    (root / "justfile").write_text("validate:\n\t@true\n")
    (root / "shared.txt").write_text("base\n")
    git_env = _git_init_commit(root)

    worktree = setup_worktree(root, "10.hello")
    (worktree / "shared.txt").write_text(branch_content)
    subprocess.run(["git", "add", "."], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "branch edit"],
        cwd=worktree, check=True, capture_output=True, env=git_env,
    )

    (root / "shared.txt").write_text(main_content)
    # Add only the conflict file: `git add .` would embed the linked worktree as
    # a gitlink (the real repo gitignores .worktrees/), which a later rebase would
    # then make "dirty" and block the squash.
    subprocess.run(["git", "add", "shared.txt"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "main edit"],
        cwd=root, check=True, capture_output=True, env=git_env,
    )
    return git_env


def test_resolve_task_without_branch_reports_clearly(tmp_path: Path) -> None:
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    _git_init_commit(root)
    result = resolve_task(root, "10.hello", "hello world", emit=lambda _e: None)
    assert not result.success
    assert "nothing to resolve" in (result.error or "")
    assert result.failure_kind == "merge_rejected"


def test_resolve_task_lands_transient_via_cheap_path(tmp_path: Path) -> None:
    """A transient blocker (dirty main) that has cleared lands on the cheap
    re-squash path — no agent involved."""
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    git_env = _git_init_commit(root)
    _commit_work_on_branch(root, "10.hello", git_env)

    cfg = root / "config.json"
    original = cfg.read_text()
    cfg.write_text(original + "\n# local edit\n")
    sha, _detail, _rec = squash_to_main(root, "10.hello", "hello world", autostash=False)
    assert sha is None  # blocked by the dirty tree
    cfg.write_text(original)  # clear the blocker

    result = resolve_task(root, "10.hello", "hello world", emit=lambda _e: None)
    assert result.success
    assert result.commit_sha
    assert (root / "new_file.py").exists()
    branches = subprocess.run(
        ["git", "branch"], cwd=root, capture_output=True, text=True,
    ).stdout
    assert "task-local/main/10.hello" not in branches


def test_resolve_task_agent_resolves_content_conflict(tmp_path: Path) -> None:
    """A content conflict routes to the agent path: rebase onto main, the worker
    resolves + continues the rebase, validate passes, and the work squashes in."""
    git_env = _seed_conflict_repo(
        tmp_path, branch_content="branch\n", main_content="main\n",
    )
    root = tmp_path

    def _resolver(cwd: Path) -> None:
        (Path(cwd) / "shared.txt").write_text("resolved\n")
        subprocess.run(
            ["git", "add", "shared.txt"], cwd=cwd, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "rebase", "--continue"],
            cwd=cwd, check=True, capture_output=True,
            env={**git_env, "GIT_EDITOR": "true"},
        )

    stub = _StubBackend(_resolver)
    original = backends_mod.get_backend
    backends_mod.get_backend = lambda *_a, **_k: stub
    try:
        events: list = []
        result = resolve_task(root, "10.hello", "hello world", emit=events.append)
    finally:
        backends_mod.get_backend = original

    assert result.success, result.error
    assert stub.calls == 1
    assert (root / "shared.txt").read_text() == "resolved\n"
    branches = subprocess.run(
        ["git", "branch"], cwd=root, capture_output=True, text=True,
    ).stdout
    assert "task-local/main/10.hello" not in branches
    phases = [e.payload.get("phase") for e in events if e.type == TASK_STATUS]
    assert "resolve" in phases


def test_resolve_task_bounded_attempts_preserves_branch(tmp_path: Path) -> None:
    """When the agent can't resolve, the run is bounded by max_resolve_attempts
    and the branch is preserved for manual resolution."""
    git_env = _seed_conflict_repo(
        tmp_path, branch_content="branch\n", main_content="main\n", max_attempts=1,
    )
    root = tmp_path

    def _gives_up(cwd: Path) -> None:
        # Leave the rebase conflicted — the engine should abort and stop.
        pass

    stub = _StubBackend(_gives_up)
    original = backends_mod.get_backend
    backends_mod.get_backend = lambda *_a, **_k: stub
    try:
        result = resolve_task(root, "10.hello", "hello world", emit=lambda _e: None)
    finally:
        backends_mod.get_backend = original

    assert not result.success
    assert result.failure_kind == "merge_conflict"
    assert stub.calls == 1  # bounded by max_resolve_attempts
    branches = subprocess.run(
        ["git", "branch"], cwd=root, capture_output=True, text=True,
    ).stdout
    assert "task-local/main/10.hello" in branches


# --------------------------------------------------------------------------- #
# commit_queue_state — snapshot the queue before a run cuts a worktree
# --------------------------------------------------------------------------- #


def test_commit_queue_state_commits_untracked_task(tmp_path: Path) -> None:
    """A task added through the UI (written to the working tree, never committed)
    is snapshotted so it lands in HEAD — the state a run's worktree branches
    from."""
    root = _seed_tree(tmp_path)
    _git_init_commit(root)

    # UI "+ Add Task": a brand-new (untracked) task file + an order bump.
    (root / ".tasks/new-task.md").write_text("---\ntitle: New\n---\nDo it.\n")
    (root / ".tasks/config.json").write_text('{"order": ["new-task"]}\n')

    sha = commit_queue_state(root)
    assert sha  # a commit was made

    tracked = subprocess.run(
        ["git", "ls-files", ".tasks"], cwd=root, capture_output=True, text=True,
    ).stdout
    assert ".tasks/new-task.md" in tracked
    assert ".tasks/config.json" in tracked
    # The definition files are now clean (nothing left uncommitted for them).
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", *QUEUE_SNAPSHOT_PATHSPECS],
        cwd=root, capture_output=True, text=True,
    ).stdout
    assert dirty.strip() == ""


def test_commit_queue_state_noop_when_clean(tmp_path: Path) -> None:
    """With nothing to snapshot, the call is a no-op (no empty commit)."""
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    _git_init_commit(root)
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
    ).stdout.strip()

    assert commit_queue_state(root) is None

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
    ).stdout.strip()
    assert head_after == head_before


def test_commit_queue_state_snapshots_playlist_task(tmp_path: Path) -> None:
    """The regression case: a playlist task added in the working tree is present
    in a worktree cut from HEAD only because the queue was snapshotted first.
    Without the snapshot the worktree (branched from HEAD) would lack the file —
    exactly the reported failure."""
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    _git_init_commit(root)

    playlist = root / ".tasks/nightshift"
    playlist.mkdir(parents=True, exist_ok=True)
    (playlist / "config.json").write_text('{"order": ["queue-view"]}\n')
    (playlist / "queue-view.md").write_text("---\ntitle: Queue view\n---\nShow it.\n")

    # The playlist run snapshots its own subtree (the main-queue snapshot no
    # longer sweeps playlist sub-dirs — that's queue-scoping).
    assert commit_queue_state(root, ".tasks/nightshift")

    # A worktree branched from HEAD (as a run would cut it) now has the file.
    worktree = setup_worktree(root, "10.hello")
    try:
        assert (worktree / ".tasks/nightshift/queue-view.md").exists()
        assert (worktree / ".tasks/nightshift/config.json").exists()
    finally:
        teardown_worktree(root, "10.hello")


def test_commit_queue_state_excludes_run_records(tmp_path: Path) -> None:
    """Snapshotting commits queue *definition* only — never the (tracked) playlist
    run records, which are live runtime state."""
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})

    # A tracked playlist run record exists from a prior run.
    runs = root / ".tasks/nightshift/runs/2026-01-01T00-00-00Z-abc"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "run.json").write_text('{"id": "old"}\n')
    (root / ".tasks/nightshift/config.json").write_text('{"order": []}\n')
    _git_init_commit(root)

    # A new run rewrites that record, and the UI adds a task — concurrently.
    (runs / "run.json").write_text('{"id": "live", "in_progress": true}\n')
    (root / ".tasks/nightshift/new-task.md").write_text("---\ntitle: New\n---\nGo.\n")

    sha = commit_queue_state(root, ".tasks/nightshift")
    assert sha

    # The new task landed; the live run record did NOT get swept into the commit.
    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=root, capture_output=True, text=True,
    ).stdout
    assert ".tasks/nightshift/new-task.md" in committed
    assert "run.json" not in committed
    # The run record is still uncommitted (left as live runtime state).
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True,
    ).stdout
    assert "run.json" in dirty


def test_commit_queue_state_main_scope_excludes_playlist_md(tmp_path: Path) -> None:
    """A main-queue snapshot commits only top-level ``.tasks/*.md`` — it must not
    sweep an unrelated playlist's edited brief (queue-scoping)."""
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    playlist = root / ".tasks/foo"
    playlist.mkdir(parents=True, exist_ok=True)
    (playlist / "config.json").write_text('{"order": ["task-x"]}\n')
    (playlist / "task-x.md").write_text("---\ntitle: X\n---\nv1\n")
    _git_init_commit(root)

    # Edit the playlist brief AND add a top-level task — concurrently dirty.
    (playlist / "task-x.md").write_text("---\ntitle: X\n---\nv2\n")
    (root / ".tasks/new-top.md").write_text("---\ntitle: T\n---\nGo.\n")

    sha = commit_queue_state(root)  # main scope
    assert sha
    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=root, capture_output=True, text=True,
    ).stdout
    assert ".tasks/new-top.md" in committed
    assert "task-x.md" not in committed  # playlist brief left for its own snapshot
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True,
    ).stdout
    assert "task-x.md" in dirty


def test_commit_queue_state_playlist_scope_excludes_top_level_md(tmp_path: Path) -> None:
    """A playlist snapshot commits only its own subtree — it must not touch
    top-level ``.tasks/*.md`` owned by the main queue."""
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    playlist = root / ".tasks/foo"
    playlist.mkdir(parents=True, exist_ok=True)
    (playlist / "config.json").write_text('{"order": []}\n')
    _git_init_commit(root)

    # Edit a top-level brief AND add a playlist task — concurrently dirty.
    (root / ".tasks/10.hello.md").write_text("Edited main brief.\n")
    (playlist / "new-x.md").write_text("---\ntitle: NX\n---\nGo.\n")

    sha = commit_queue_state(root, ".tasks/foo")  # playlist scope
    assert sha
    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=root, capture_output=True, text=True,
    ).stdout
    assert ".tasks/foo/new-x.md" in committed
    assert "10.hello.md" not in committed  # top-level brief untouched by playlist
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True,
    ).stdout
    assert "10.hello.md" in dirty


def test_worktree_namespacing_isolates_same_named_tasks(tmp_path: Path) -> None:
    """Two queues holding a same-named task cut distinct branches/worktrees;
    tearing one down leaves the other intact."""
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    _git_init_commit(root)

    wt_main = setup_worktree(root, "shared", queue=None)
    wt_foo = setup_worktree(root, "shared", queue="foo")
    try:
        assert wt_main != wt_foo
        assert wt_main.exists() and wt_foo.exists()
        assert wt_main == worktree_dir(root, "shared", None)
        assert wt_foo == worktree_dir(root, "shared", "foo")
        branches = subprocess.run(
            ["git", "branch"], cwd=root, capture_output=True, text=True,
        ).stdout
        assert worktree_branch("shared", None) in branches  # task-local/main/shared
        assert worktree_branch("shared", "foo") in branches  # task-local/foo/shared

        # Tearing down one queue's worktree leaves the other's intact.
        teardown_worktree(root, "shared", queue=None)
        assert not wt_main.exists()
        assert wt_foo.exists()
        branches = subprocess.run(
            ["git", "branch"], cwd=root, capture_output=True, text=True,
        ).stdout
        assert worktree_branch("shared", None) not in branches
        assert worktree_branch("shared", "foo") in branches
    finally:
        teardown_worktree(root, "shared", queue="foo")


def test_landing_lock_serializes_concurrent_squashes(tmp_path: Path) -> None:
    """Two runner threads squashing distinct task branches at the same instant
    both land cleanly — the landing lock serializes their root index/HEAD writes
    so neither sees a half-merged tree or a corrupt index.lock."""
    import threading

    root = _seed_tree(tmp_path, tasks={"10.a": "A", "20.b": "B"})
    git_env = _git_init_commit(root)
    for task, fname in (("10.a", "file_a.py"), ("20.b", "file_b.py")):
        wt = setup_worktree(root, task)
        (wt / fname).write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=wt, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"work {task}"],
            cwd=wt, check=True, capture_output=True, env=git_env,
        )

    barrier = threading.Barrier(2)
    results: dict[str, tuple] = {}

    def _land(task: str, title: str) -> None:
        barrier.wait()  # force maximal overlap
        results[task] = squash_to_main(root, task, title)

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
    assert (root / "file_a.py").exists()
    assert (root / "file_b.py").exists()
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=root, capture_output=True, text=True,
    ).stdout
    assert "task: task a" in log
    assert "task: task b" in log
    assert not (root / ".git/index.lock").exists()


def test_autostash_does_not_disturb_existing_human_stash(tmp_path: Path) -> None:
    """The set-aside is stack-independent (``git stash create``): a land over
    operator WIP restores it without consuming a pre-existing human stash entry,
    and leaves no ``nightshift-autostash`` on the stack on success."""
    root = _seed_tree(tmp_path, tasks={"10.hello": "Do something."})
    git_env = _git_init_commit(root)
    _commit_work_on_branch(root, "10.hello", git_env)  # branch adds new_file.py

    # A human stashes unrelated tracked WIP onto the LIFO stack.
    sentinel = root / "sentinel.py"
    sentinel.write_text("# v1\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add sentinel"],
        cwd=root, check=True, capture_output=True, env=git_env,
    )
    sentinel.write_text("# human wip\n")
    subprocess.run(
        ["git", "stash", "push", "-m", "human-wip", "--", str(sentinel)],
        cwd=root, check=True, capture_output=True,
    )

    # Operator now has unrelated code WIP; the land must set it aside + restore it.
    cfg = root / "config.json"
    dirty = cfg.read_text() + "\n# operator edit\n"
    cfg.write_text(dirty)

    sha, detail, recoverable = squash_to_main(root, "10.hello", "hello world")
    assert sha is not None
    assert detail == ""
    assert cfg.read_text() == dirty  # operator WIP restored verbatim

    stash_list = subprocess.run(
        ["git", "stash", "list"], cwd=root, capture_output=True, text=True,
    ).stdout
    assert AUTOSTASH_MESSAGE not in stash_list  # no leftover autostash on success
    assert "human-wip" in stash_list  # the human's stash was never consumed
    assert stash_list.count("\n") == 1  # exactly one entry remains

    teardown_worktree(root, "10.hello")


def test_failure_kind_round_trips_through_run_store(tmp_path: Path) -> None:
    """A classified failure_kind written on a task_result survives reconstruction
    so History/UI can render it."""
    store = RunStore(tmp_path)
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
    root = _seed_tree(tmp_path, tasks={
        "10.a": "Do a.",
        "20.b": "---\ndisabled: true\n---\nDo b.",
        "30.c": "Do c.",
    })
    (root / ".tasks/config.json").write_text('{"order": ["30.c", "10.a"]}\n')
    assert live_ordered_queue(root) == ["30.c", "10.a"]


def test_run_queue_follow_picks_up_midrun_addition(tmp_path: Path, monkeypatch) -> None:
    """With follow_queue on, a task that appears mid-run is executed in the same
    run, and the per-task snapshot has committed its file before run_task runs."""
    root = _seed_tree(tmp_path, tasks={"a": "Do a."})
    (root / ".tasks/config.json").write_text('{"order": ["a"]}\n')
    _git_init_commit(root)

    calls: list[str] = []
    committed_when_run: dict[str, bool] = {}

    def fake_run_task(root_arg: Path, task: str, **_kwargs) -> TaskResult:
        calls.append(task)
        tracked = subprocess.run(
            ["git", "ls-files", f".tasks/{task}.md"],
            cwd=root_arg, capture_output=True, text=True,
        ).stdout.strip()
        committed_when_run[task] = bool(tracked)
        if task == "a":  # UI adds a task mid-run
            (root_arg / ".tasks/b.md").write_text("---\ntitle: B\n---\nDo b.\n")
        return TaskResult(task=task, title=task, success=True)

    monkeypatch.setattr("nightshift.engine.run_task", fake_run_task)
    run_queue(root, ["a"], follow_queue=True)

    assert calls == ["a", "b"]
    # The per-task commit_queue_state committed b.md before it was run.
    assert committed_when_run["b"] is True


def test_run_queue_oneshot_does_not_drain(tmp_path: Path, monkeypatch) -> None:
    """With follow_queue off, only the passed tasks run even if siblings exist."""
    root = _seed_tree(tmp_path, tasks={"a": "Do a.", "b": "Do b."})
    _git_init_commit(root)

    calls: list[str] = []

    def fake_run_task(root_arg: Path, task: str, **_kwargs) -> TaskResult:
        calls.append(task)
        return TaskResult(task=task, title=task, success=True)

    monkeypatch.setattr("nightshift.engine.run_task", fake_run_task)
    run_queue(root, ["a"], follow_queue=False)

    assert calls == ["a"]


def test_run_queue_follow_attempts_each_task_once(tmp_path: Path, monkeypatch) -> None:
    """Tasks that remain on disk (a failed task, an evergreen task) are attempted
    exactly once per run — the drain loop terminates instead of re-running them."""
    root = _seed_tree(tmp_path, tasks={
        "a": "---\nevergreen: true\n---\nDo a.",
        "b": "Do b.",
    })
    _git_init_commit(root)

    calls: list[str] = []

    def fake_run_task(root_arg: Path, task: str, **_kwargs) -> TaskResult:
        calls.append(task)
        # a is evergreen (stays on disk); b "fails" (also stays on disk). Neither
        # should be retried within this run.
        return TaskResult(task=task, title=task, success=(task != "b"))

    monkeypatch.setattr("nightshift.engine.run_task", fake_run_task)
    run_queue(root, ["a", "b"], follow_queue=True)

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
    root = _seed_tree(tmp_path, tasks={
        "10.paused": "---\ndisabled: true\n---\nNot yet.",
    })
    _git_init_commit(root)

    backend = _RecordingBackend()
    original = backends_mod.get_backend
    backends_mod.get_backend = lambda *_a, **_k: backend
    try:
        events: list = []
        result = run_task(root, "10.paused", emit=events.append)
    finally:
        backends_mod.get_backend = original

    assert backend.calls == 0  # worker never launched
    assert result.status == "skipped"
    assert not result.success
    # A skip is not a failure (deliberate operator choice).
    assert result.resolved_status() == "skipped"
    # No worktree was created for the skipped task.
    assert not (root / ".worktrees/task-local/10.paused").exists()
    # The result event reports the skip with a disabled reason.
    statuses = [
        e.payload.get("status")
        for e in events
        if e.type == "task_result"
    ]
    assert statuses == ["skipped"]


def test_run_task_runs_enabled_task(tmp_path: Path, monkeypatch) -> None:
    """The disabled re-check does not block a normal (enabled) task: the worker
    is invoked as usual."""
    root = _seed_tree(tmp_path, tasks={"10.go": "Do it."})
    (root / "justfile").write_text("validate:\n\t@true\n")
    _git_init_commit(root)

    backend = _RecordingBackend()
    original = backends_mod.get_backend
    backends_mod.get_backend = lambda *_a, **_k: backend
    try:
        run_task(root, "10.go", emit=lambda _e: None)
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
    worker's commit lands even though the justfile's validate recipe would fail.

    Licensed by the task spec — an empty-string validate disables validation for
    that queue and must not fall back to the inherited default."""
    root = _seed_tree(tmp_path, tasks={"10.skip": "Do it."})
    # A validate recipe that always fails — if it ran, the task could not land.
    (root / "justfile").write_text("validate:\n\t@exit 1\n")
    (root / ".tasks/config.json").write_text(json.dumps({"validate": "", "order": []}))
    _git_init_commit(root)

    backend = _CommittingBackend()
    original = backends_mod.get_backend
    backends_mod.get_backend = lambda *_a, **_k: backend
    try:
        events: list = []
        result = run_task(root, "10.skip", emit=events.append)
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
    # The worker's file actually landed on main.
    assert (root / "produced.py").exists()


def test_run_queue_skips_task_disabled_after_list_built(
    tmp_path: Path, monkeypatch,
) -> None:
    """A task disabled after run_queue's order list was built is skipped at
    launch by run_task's re-check, not run."""
    root = _seed_tree(tmp_path, tasks={
        "a": "Do a.",
        "b": "---\ndisabled: true\n---\nNot yet.",
    })
    _git_init_commit(root)

    calls: list[str] = []

    def fake_run_task(root_arg: Path, task: str, **kwargs) -> TaskResult:
        # Delegate to the real run_task so the disabled re-check runs, but only
        # record the tasks that get past it (skipped tasks return early).
        result = run_task(root_arg, task, **kwargs)
        if result.status != "skipped":
            calls.append(task)
        return result

    backend = _RecordingBackend()
    original = backends_mod.get_backend
    backends_mod.get_backend = lambda *_a, **_k: backend
    monkeypatch.setattr("nightshift.engine.run_task", fake_run_task)
    (root / "justfile").write_text("validate:\n\t@true\n")
    try:
        summary = run_queue(root, ["a", "b"], follow_queue=False)
    finally:
        backends_mod.get_backend = original

    assert "b" not in calls  # disabled task never ran
    assert "a" in calls
    skipped = [r for r in summary.results if r.status == "skipped"]
    assert [r.task for r in skipped] == ["b"]


# --------------------------------------------------------------------------- #
# Task priorities + queue sort mode
# --------------------------------------------------------------------------- #


def test_order_stems_manual_mode_unchanged(tmp_path: Path) -> None:
    """Manual mode (the default) keeps listed-by-config then lexicographic order
    regardless of priorities — the legacy behavior."""
    root = _seed_tree(tmp_path, tasks={
        "a": "---\npriority: 0\n---\nDo a.",
        "b": "---\npriority: 5\n---\nDo b.",
        "c": "Do c.",
    })
    (root / ".tasks/config.json").write_text('{"order": ["b", "a"]}\n')
    assert load_sort_mode(root) == "manual"
    # b, a are listed (config order); c is unlisted (lexicographic, last).
    assert order_stems(root, ["a", "b", "c"]) == ["b", "a", "c"]


def test_order_stems_priority_mode_sorts_by_priority(tmp_path: Path) -> None:
    """Priority mode sorts ascending by priority (0 = highest)."""
    root = _seed_tree(tmp_path, tasks={
        "a": "---\npriority: 3\n---\nDo a.",
        "b": "---\npriority: 0\n---\nDo b.",
        "c": "---\npriority: 1\n---\nDo c.",
    })
    save_sort_mode(root, "priority")
    assert load_sort_mode(root) == "priority"
    assert order_stems(root, ["a", "b", "c"]) == ["b", "c", "a"]


def test_order_stems_priority_ties_break_by_manual_order(tmp_path: Path) -> None:
    """Equal priorities keep the manual (config order) relative arrangement."""
    root = _seed_tree(tmp_path, tasks={
        "a": "---\npriority: 2\n---\nDo a.",
        "b": "---\npriority: 2\n---\nDo b.",
        "c": "---\npriority: 0\n---\nDo c.",
    })
    # Manual order puts b before a; both are priority 2, so they keep that order
    # under the higher-priority c.
    (root / ".tasks/config.json").write_text(
        '{"order": ["b", "a"], "sort": "priority"}\n'
    )
    assert order_stems(root, ["a", "b", "c"]) == ["c", "b", "a"]


def test_order_stems_missing_priority_defaults_lowest(tmp_path: Path) -> None:
    """A task without a priority field sorts as the lowest (5)."""
    root = _seed_tree(tmp_path, tasks={
        "a": "Do a.",                       # no priority -> 5
        "b": "---\npriority: 0\n---\nDo b.",
    })
    save_sort_mode(root, "priority")
    assert order_stems(root, ["a", "b"]) == ["b", "a"]


def test_save_sort_mode_coerces_unknown_to_manual(tmp_path: Path) -> None:
    """An unknown mode degrades to manual, and the order key is preserved."""
    root = _seed_tree(tmp_path, tasks={"a": "Do a."})
    (root / ".tasks/config.json").write_text('{"order": ["a"]}\n')
    assert save_sort_mode(root, "bogus") == "manual"
    data = json.loads((root / ".tasks/config.json").read_text())
    assert data["sort"] == "manual"
    assert data["order"] == ["a"]  # sibling key preserved


def test_live_ordered_queue_respects_priority_mode(tmp_path: Path) -> None:
    """The engine's live scan (the play/execute source) honours priority mode."""
    root = _seed_tree(tmp_path, tasks={
        "a": "---\npriority: 4\n---\nDo a.",
        "b": "---\npriority: 1\n---\nDo b.",
        "c": "---\npriority: 1\n---\nDo c.",
    })
    (root / ".tasks/config.json").write_text(
        '{"order": ["a", "c", "b"], "sort": "priority"}\n'
    )
    # b and c tie at priority 1; manual order lists c before b, so c precedes b.
    assert live_ordered_queue(root) == ["c", "b", "a"]


def test_list_queue_includes_priority_and_orders(tmp_path: Path) -> None:
    """list_queue (the UI source) returns each task's priority and orders by the
    active sort mode."""
    root = _seed_tree(tmp_path, tasks={
        "a": "---\npriority: 3\n---\nDo a.",
        "b": "---\npriority: 0\n---\nDo b.",
    })
    save_sort_mode(root, "priority")
    queue = list_queue(root)
    assert [item["task"] for item in queue] == ["b", "a"]
    by_task = {item["task"]: item for item in queue}
    assert by_task["a"]["priority"] == 3
    assert by_task["b"]["priority"] == 0


def test_play_priorities_round_trip_and_clean(tmp_path: Path) -> None:
    """The play-priority filter persists sorted/de-duped/clamped, preserving
    sibling keys; an empty list clears it."""
    root = _seed_tree(tmp_path, tasks={"a": "Do a."})
    (root / ".tasks/config.json").write_text('{"order": ["a"]}\n')
    # Out-of-range (7) and duplicate (1) are dropped; result is sorted.
    assert save_play_priorities(root, [3, 1, 1, 7]) == [1, 3]
    assert load_play_priorities(root) == [1, 3]
    data = json.loads((root / ".tasks/config.json").read_text())
    assert data["play_priorities"] == [1, 3]
    assert data["order"] == ["a"]  # sibling key preserved
    # An empty list clears the filter (all priorities play).
    assert save_play_priorities(root, []) == []
    assert load_play_priorities(root) == []


def test_live_ordered_queue_applies_play_filter(tmp_path: Path) -> None:
    """live_ordered_queue (the play/execute source) drops tasks outside the
    active play-priority filter, including non-contiguous selections."""
    root = _seed_tree(tmp_path, tasks={
        "a": "---\npriority: 0\n---\nDo a.",
        "b": "---\npriority: 1\n---\nDo b.",
        "c": "---\npriority: 3\n---\nDo c.",
        "d": "Do d.",  # no priority -> 5
    })
    # No filter: everything is runnable (filename order, manual sort).
    assert live_ordered_queue(root) == ["a", "b", "c", "d"]
    # Non-contiguous selection P0 + P3 keeps only a and c.
    save_play_priorities(root, [0, 3])
    assert live_ordered_queue(root) == ["a", "c"]
    # The default (5) is selectable too — it matches the file with no priority.
    save_play_priorities(root, [5])
    assert live_ordered_queue(root) == ["d"]


def test_list_queue_not_filtered_by_play_priorities(tmp_path: Path) -> None:
    """list_queue (the UI management view) shows every task regardless of the
    play filter, so out-of-scope tasks remain visible and editable."""
    root = _seed_tree(tmp_path, tasks={
        "a": "---\npriority: 0\n---\nDo a.",
        "b": "---\npriority: 3\n---\nDo b.",
    })
    save_play_priorities(root, [0])
    assert [item["task"] for item in list_queue(root)] == ["a", "b"]


def test_set_task_meta_priority_round_trips(tmp_path: Path) -> None:
    """Editing priority through set_task_meta persists and is read back."""
    root = _seed_tree(tmp_path, tasks={"a": "---\ntitle: A\n---\nDo a."})
    set_task_meta(root, "a", {"priority": 2})
    assert read_task(root, "a")["frontmatter"]["priority"] == 2
    # A task with no priority field resolves to the default (lowest).
    (root / ".tasks/plain.md").write_text("---\ntitle: Plain\n---\nDo it.\n")
    assert read_task(root, "plain")["frontmatter"]["priority"] == 5


def test_run_queue_resorts_pending_by_priority_between_tasks(
    tmp_path: Path, monkeypatch,
) -> None:
    """A priority change made mid-run reshuffles the not-yet-run tail at the next
    task boundary, without re-running an already-started task."""
    root = _seed_tree(tmp_path, tasks={
        "a": "---\npriority: 0\n---\nDo a.",
        "b": "---\npriority: 5\n---\nDo b.",
        "c": "---\npriority: 5\n---\nDo c.",
    })
    (root / ".tasks/config.json").write_text(
        '{"order": ["a", "b", "c"], "sort": "priority"}\n'
    )
    _git_init_commit(root)

    calls: list[str] = []

    def fake_run_task(root_arg: Path, task: str, **_kwargs) -> TaskResult:
        calls.append(task)
        if task == "a":
            # While a runs, the operator bumps c above b. The next pick must be c.
            (root_arg / ".tasks/c.md").write_text(
                "---\npriority: 1\n---\nDo c.\n"
            )
        return TaskResult(task=task, title=task, success=True)

    monkeypatch.setattr("nightshift.engine.run_task", fake_run_task)
    run_queue(root, ["a", "b", "c"], follow_queue=True)

    # a first (it was highest at play). After a, c (now P1) jumps ahead of b (P5).
    assert calls == ["a", "c", "b"]
    assert len(calls) == 3  # the started task is never re-run


def test_run_queue_oneshot_ignores_sort_mode(tmp_path: Path, monkeypatch) -> None:
    """Oneshot runs drain exactly the passed list and are not re-sorted live."""
    root = _seed_tree(tmp_path, tasks={
        "a": "---\npriority: 5\n---\nDo a.",
        "b": "---\npriority: 0\n---\nDo b.",
    })
    save_sort_mode(root, "priority")
    _git_init_commit(root)

    calls: list[str] = []

    def fake_run_task(root_arg: Path, task: str, **_kwargs) -> TaskResult:
        calls.append(task)
        return TaskResult(task=task, title=task, success=True)

    monkeypatch.setattr("nightshift.engine.run_task", fake_run_task)
    run_queue(root, ["a"], follow_queue=False)

    assert calls == ["a"]
