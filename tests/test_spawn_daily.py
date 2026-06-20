from __future__ import annotations

import json
from pathlib import Path

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


def _seed_nightshift_tree(tmp_path: Path, *, names: tuple[str, ...] = ()) -> Path:
    # The operator config the spawn logic reads lives at the repo root. Templates
    # are resolved from the installed package, so the temp tree only needs the
    # config and an empty ``.tasks`` queue.
    (tmp_path / "config.json").write_text(
        json.dumps({"model": "claude-sonnet-4-6", "max_turns": 60, "automerge": True, "draft": False})
    )
    (tmp_path / ".tasks").mkdir(parents=True, exist_ok=True)
    return tmp_path


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
    root = _seed_nightshift_tree(tmp_path, names=("task.md",))
    (root / ".tasks/00._questions.md").write_text("---\nautosplit: true\n---\n")
    (root / ".tasks/00._todo.md").write_text("---\nautosplit: true\n---\n")
    (root / ".tasks/02.service-triage.md").write_text("---\nevergreen: true\n---\n")
    (root / ".tasks/10.normal-task.md").write_text("Do something.\n")

    sources = find_autosplit_sources(root)
    assert sources == ["00._questions", "00._todo"]


def test_inspect_daily_todos_items(tmp_path: Path) -> None:
    root = _seed_nightshift_tree(tmp_path, names=("task.md", "00._todo.md"))
    (root / ".tasks/00._todo.md").write_text(
        (TEMPLATES / "00._todo.md").read_text()
        + "1. Fix the Ops screen\n* Add disable toggle\n"
    )

    result = inspect_source(root, "00._todo")
    assert result.item_count == 2
    assert result.items[0] == "Fix the Ops screen"


def test_spawn_writes_tasks_and_resets_template(tmp_path: Path) -> None:
    root = _seed_nightshift_tree(tmp_path, names=("task.md", "00._todo.md"))
    (root / ".tasks/00._todo.md").write_text(
        "---\nautosplit: true\nmodel: claude-opus-4-6\n---\n\n"
        "Fix the following:\n\n"
        "1. First item\n2. Second item\n"
    )

    results = spawn_all(root, write=True)
    assert len(results) == 1
    assert len(results[0].spawned) == 2
    assert results[0].spawned[0].model == "claude-opus-4-6"
    assert "Fix the following:" in results[0].spawned[0].body
    assert "First item" in results[0].spawned[0].body
    assert list((root / ".tasks").glob("00.*.md"))
    assert (root / ".tasks/00._todo.md").read_text() == (
        TEMPLATES / "00._todo.md"
    ).read_text()


def test_spawn_questions_include_preamble(tmp_path: Path) -> None:
    root = _seed_nightshift_tree(tmp_path, names=("task.md", "00._questions.md"))
    (root / ".tasks/00._questions.md").write_text(
        (TEMPLATES / "00._questions.md").read_text() + "* What is the macro regime?\n"
    )

    result = spawn_source(root, "00._questions", write=True)
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
    root = _seed_nightshift_tree(tmp_path, names=("task.md",))
    (root / ".tasks/00._todo.md").write_text("---\nautosplit: true\n---\n")
    (root / ".tasks/00._questions.md").write_text(
        "---\nautosplit: true\ndisabled: true\n---\n"
    )
    sources = find_autosplit_sources(root)
    assert sources == ["00._todo"]


def test_matrix_from_task_names_skips_disabled(tmp_path: Path) -> None:
    root = _seed_nightshift_tree(tmp_path, names=("task.md",))
    (root / ".tasks/10.active.md").write_text("---\nmodel: claude-sonnet-4-6\n---\nDo it.")
    (root / ".tasks/20.paused.md").write_text(
        "---\nmodel: claude-sonnet-4-6\ndisabled: true\n---\nDo later."
    )
    entries = matrix_from_task_names(root, ["10.active", "20.paused"])
    assert len(entries) == 1
    assert entries[0]["task"] == "10.active"


def test_recover_matrix_from_git_diff(tmp_path: Path) -> None:
    import subprocess

    root = _seed_nightshift_tree(tmp_path, names=("task.md", "00._todo.md"))
    (root / ".tasks/00._todo.md").write_text(
        "---\nautosplit: true\n---\n\nFix the following:\n\n1. Ship dispatch PR flow\n"
    )
    git_env = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "base"],
        cwd=root, check=True, capture_output=True, env=git_env,
    )
    spawn_source(root, "00._todo", write=True)
    subprocess.run(["git", "add", ".tasks/"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "spawn"],
        cwd=root, check=True, capture_output=True, env=git_env,
    )
    matrix = recover_matrix(root, base_ref="HEAD^")
    assert len(matrix) == 1
    assert matrix[0]["task"].startswith("00.")
    assert matrix[0]["model"] == "claude-sonnet-4-6"
