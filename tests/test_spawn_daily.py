from __future__ import annotations

from pathlib import Path

from _workspace import build_workspace, git, git_commit_all
from nightshift._paths import TEMPLATES_DIR
from nightshift.spawn_daily import (
    extract_items,
    find_autosplit_sources,
    inspect_source,
    is_disabled,
    matrix_from_task_names,
    recover_matrix,
    spawn_all,
    spawn_source,
    split_frontmatter,
)


# Task templates ship inside the package now; the engine and spawn logic read
# them from here, and the tests compare against the same shipped copies.
TEMPLATES = TEMPLATES_DIR


def _tasks_root(tmp_path: Path, *, commit: bool = False) -> Path:
    """Build a two-root workspace and return its content-store root.

    The spawn logic operates on the ``nightshift-tasks`` content store and its
    default ``main`` queue (``<tasks_root>/main/``). The operator config it
    layers in lives at ``<workspace>/config.json`` (``tasks_root.parent``);
    seed it with the model defaults the original suite relied on so resolved
    models stay stable. No target repos are needed — spawning never dispatches.
    """
    workspace = build_workspace(
        tmp_path,
        main_repo=None,
        repos=(),
        config={
            "model": "claude-sonnet-4-6",
            "max_turns": 60,
            "automerge": True,
            "draft": False,
        },
        commit_tasks=commit,
    )
    return workspace / "nightshift-tasks"


def test_split_frontmatter_parses_model() -> None:
    text = "---\nmodel: claude-opus-4-6\nautomerge: false\n---\n\n## TO DO:\n"
    meta, body = split_frontmatter(text)
    assert meta["model"] == "claude-opus-4-6"
    assert meta["automerge"] is False
    assert "## TO DO:" in body


def test_extract_items_various_formats() -> None:
    body = (
        "Preamble text here.\n\n"
        "* bullet star\n"
        "- bullet dash\n"
        "+ bullet plus\n"
        "1. numbered\n"
        "2) numbered paren\n"
        "[ ] unchecked\n"
        "[x] checked\n"
    )
    preamble, items = extract_items(body)
    assert preamble == "Preamble text here."
    assert len(items) == 7
    assert items[0] == "bullet star"
    assert items[3] == "numbered"
    assert items[5] == "unchecked"
    assert items[6] == "checked"


def test_extract_items_with_headings() -> None:
    body = (
        "Fix the following:\n\n"
        "### Category A\n"
        "* item one\n"
        "* item two\n"
        "### Category B\n"
        "- item three\n"
    )
    preamble, items = extract_items(body)
    assert preamble == "Fix the following:"
    assert len(items) == 3
    assert items[0] == "item one"
    assert items[2] == "item three"


def test_extract_items_empty_body() -> None:
    preamble, items = extract_items("Just preamble, no items.\n")
    assert preamble == "Just preamble, no items."
    assert items == []


def test_find_autosplit_sources(tmp_path: Path) -> None:
    tasks_root = _tasks_root(tmp_path)
    main = tasks_root / "main"
    (main / "00._questions.md").write_text("---\nautosplit: true\n---\n")
    (main / "00._todo.md").write_text("---\nautosplit: true\n---\n")
    (main / "02.service-triage.md").write_text("---\nevergreen: true\n---\n")
    (main / "10.normal-task.md").write_text("Do something.\n")

    sources = find_autosplit_sources(tasks_root)
    assert sources == ["00._questions", "00._todo"]


def test_inspect_daily_todos_items(tmp_path: Path) -> None:
    tasks_root = _tasks_root(tmp_path)
    (tasks_root / "main/00._todo.md").write_text(
        (TEMPLATES / "00._todo.md").read_text()
        + "1. Fix the Ops screen\n* Add disable toggle\n"
    )

    result = inspect_source(tasks_root, "00._todo")
    assert result.item_count == 2
    assert result.items[0] == "Fix the Ops screen"


def test_spawn_writes_tasks_and_resets_template(tmp_path: Path) -> None:
    tasks_root = _tasks_root(tmp_path)
    main = tasks_root / "main"
    (main / "00._todo.md").write_text(
        "---\nautosplit: true\nmodel: claude-opus-4-6\n---\n\n"
        "Fix the following:\n\n"
        "1. First item\n2. Second item\n"
    )

    results = spawn_all(tasks_root, write=True)
    assert len(results) == 1
    assert len(results[0].spawned) == 2
    assert results[0].spawned[0].model == "claude-opus-4-6"
    assert "Fix the following:" in results[0].spawned[0].body
    assert "First item" in results[0].spawned[0].body
    assert list(main.glob("00.*.md"))
    assert (main / "00._todo.md").read_text() == (
        TEMPLATES / "00._todo.md"
    ).read_text()


def test_spawn_questions_include_preamble(tmp_path: Path) -> None:
    tasks_root = _tasks_root(tmp_path)
    (tasks_root / "main/00._questions.md").write_text(
        (TEMPLATES / "00._questions.md").read_text() + "* What is the macro regime?\n"
    )

    result = spawn_source(tasks_root, "00._questions", write=True)
    assert result is not None
    assert "docs/daily/" in result.spawned[0].body
    assert "What is the macro regime?" in result.spawned[0].body


def test_is_disabled_defaults_false() -> None:
    assert is_disabled({}) is False
    assert is_disabled({"model": "claude-opus-4-6"}) is False


def test_is_disabled_true() -> None:
    assert is_disabled({"disabled": True}) is True


def test_is_disabled_false_explicit() -> None:
    assert is_disabled({"disabled": False}) is False


def test_find_autosplit_sources_skips_disabled(tmp_path: Path) -> None:
    tasks_root = _tasks_root(tmp_path)
    main = tasks_root / "main"
    (main / "00._todo.md").write_text("---\nautosplit: true\n---\n")
    (main / "00._questions.md").write_text(
        "---\nautosplit: true\ndisabled: true\n---\n"
    )
    sources = find_autosplit_sources(tasks_root)
    assert sources == ["00._todo"]


def test_matrix_from_task_names_skips_disabled(tmp_path: Path) -> None:
    tasks_root = _tasks_root(tmp_path)
    main = tasks_root / "main"
    (main / "10.active.md").write_text("---\nmodel: claude-sonnet-4-6\n---\nDo it.")
    (main / "20.paused.md").write_text(
        "---\nmodel: claude-sonnet-4-6\ndisabled: true\n---\nDo later."
    )
    entries = matrix_from_task_names(tasks_root, ["10.active", "20.paused"])
    assert len(entries) == 1
    assert entries[0]["task"] == "10.active"


def test_recover_matrix_from_git_diff(tmp_path: Path) -> None:
    tasks_root = _tasks_root(tmp_path, commit=True)
    main = tasks_root / "main"
    (main / "00._todo.md").write_text(
        "---\nautosplit: true\n---\n\nFix the following:\n\n1. Ship dispatch PR flow\n"
    )
    # Commit the autosplit source so the spawned subtasks are the only additions
    # the recovery diff picks up (it runs against the content-store repo itself).
    git_commit_all(tasks_root, "base autosplit source")
    spawn_source(tasks_root, "00._todo", write=True)
    git(tasks_root, "add", "main")
    git_commit_all(tasks_root, "spawn")

    matrix = recover_matrix(tasks_root, base_ref="HEAD^")
    assert len(matrix) == 1
    assert matrix[0]["task"].startswith("00.")
    assert matrix[0]["model"] == "auto"
