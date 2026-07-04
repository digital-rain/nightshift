"""Tests for the queue/task-file helpers (``task_files`` / ``queue_config`` /
``playlists`` / ``config.io``): task-list building, ordering + priorities,
play filters, task CRUD + metadata round-trips, validate-command resolution,
config layering, and split (decomposition) harvest.

Relocated in Phase 9 from the legacy ``test_run_local.py`` and
``test_nightshift_ui.py`` suites to the real module homes; behavior unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from _workspace import build_workspace
from nightshift import playlists
from nightshift._paths import TEMPLATES_DIR
from nightshift.config.io import load_dotenv
from nightshift.queue_config import (
    format_validate_cmd,
    load_order,
    load_play_priorities,
    load_sort_mode,
    normalize_validate_command,
    order_stems,
    reorder_queue,
    resolve_validate_cmd,
    save_order,
    save_play_priorities,
    save_queue_config_value,
    save_sort_mode,
    validate_cmd_from_blob,
)
from nightshift.repos import DEFAULT_TASKS_REPO
from nightshift.spawn_daily import (
    load_config,
    resolve_config,
    save_config_value,
    split_frontmatter,
)
from nightshift.task_files import (
    ORIGINAL_BRIEF_MARKER,
    build_task_list,
    create_task,
    delete_task,
    drop_completed_task,
    harvest_split_output,
    join_original,
    list_queue,
    live_ordered_queue,
    read_task,
    resolve_title,
    set_task_meta,
    split_original,
    split_output_dir,
    task_is_evergreen,
)


TEMPLATES = TEMPLATES_DIR
REPO = "longitude"


def _store_only(
    tmp_path: Path,
    *,
    tasks: dict[str, str] | None = None,
    commit: bool = False,
) -> Path:
    """Build only the content store (no target repo) and return ``tasks_root``."""
    workspace = build_workspace(
        tmp_path, tasks=tasks, repos=(), main_repo=None, commit_tasks=commit,
    )
    return workspace / DEFAULT_TASKS_REPO


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
# Pure helpers: resolve_title + .env loading
# --------------------------------------------------------------------------- #


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
# Live queue scan (the play/execute source)
# --------------------------------------------------------------------------- #


def test_live_ordered_queue_skips_disabled_and_orders(tmp_path: Path) -> None:
    tasks_root = _store_only(tmp_path, tasks={
        "10.a": "Do a.",
        "20.b": "---\ndisabled: true\n---\nDo b.",
        "30.c": "Do c.",
    })
    (tasks_root / "main" / "config.json").write_text('{"order": ["30.c", "10.a"]}\n')
    assert live_ordered_queue(tasks_root) == ["30.c", "10.a"]


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
    """The live scan (the play/execute source) honours priority mode."""
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


# --------------------------------------------------------------------------- #
# split (decomposition) plumbing
# --------------------------------------------------------------------------- #


def test_set_task_meta_split_round_trips(tmp_path: Path) -> None:
    """``split`` is in _EDITABLE_META_KEYS and round-trips through set_task_meta."""
    tasks_root = _store_only(tmp_path, tasks={"a": "---\ntitle: A\n---\nDo a."})
    set_task_meta(tasks_root, "a", {"split": True})
    assert read_task(tasks_root, "a")["frontmatter"]["split"] is True
    set_task_meta(tasks_root, "a", {"split": False})
    assert read_task(tasks_root, "a")["frontmatter"]["split"] is False


def test_harvest_split_output_collects_and_retires(tmp_path: Path) -> None:
    """harvest_split_output scans the split dir, enqueues subtask briefs, and
    retires the parent."""
    workspace = build_workspace(
        tmp_path,
        tasks={"04.parent": "---\ntitle: Parent\nsplit: true\n---\nBig task."},
    )
    tasks_root = workspace / DEFAULT_TASKS_REPO

    sdir = split_output_dir(workspace, REPO, "04.parent", queue=None)
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "04.1.setup.md").write_text(
        "---\ntitle: Setup\nautomerge: true\n---\nDo setup.\n"
    )
    (sdir / "04.2.schema.md").write_text(
        "---\ntitle: Schema\nautomerge: true\nafter: 04.1.setup\n---\nDo schema.\n"
    )

    created = harvest_split_output(
        workspace, tasks_root, REPO, "04.parent", {"split": True},
    )
    assert len(created) == 2
    for name in created:
        assert (tasks_root / "main" / f"{name}.md").is_file()
    assert not (tasks_root / "main" / "04.parent.md").exists()
    assert not sdir.exists()


def test_harvest_split_output_empty_dir(tmp_path: Path) -> None:
    """An empty split dir returns no subtasks and doesn't crash."""
    workspace = build_workspace(
        tmp_path,
        tasks={"04.parent": "---\ntitle: Parent\nsplit: true\n---\nBig task."},
    )
    tasks_root = workspace / DEFAULT_TASKS_REPO

    sdir = split_output_dir(workspace, REPO, "04.parent", queue=None)
    sdir.mkdir(parents=True, exist_ok=True)

    created = harvest_split_output(
        workspace, tasks_root, REPO, "04.parent", {"split": True},
    )
    assert created == []
    assert (tasks_root / "main" / "04.parent.md").exists()


def test_harvest_split_output_no_dir(tmp_path: Path) -> None:
    """When the split dir doesn't exist, returns empty and parent stays."""
    workspace = build_workspace(
        tmp_path,
        tasks={"04.parent": "---\ntitle: Parent\nsplit: true\n---\nBig task."},
    )
    tasks_root = workspace / DEFAULT_TASKS_REPO

    created = harvest_split_output(
        workspace, tasks_root, REPO, "04.parent", {"split": True},
    )
    assert created == []
    assert (tasks_root / "main" / "04.parent.md").exists()


def test_split_output_dir_path_format(tmp_path: Path) -> None:
    """split_output_dir returns a .split/ sibling of the worktree dir."""
    sdir = split_output_dir(tmp_path, "myrepo", "04.task", queue=None)
    assert sdir.name == "task-local-main-04.task.split"
    assert sdir.parent == tmp_path / ".worktrees" / "myrepo"

    sdir_q = split_output_dir(tmp_path, "myrepo", "04.task", queue="nightly")
    assert "nightly" in sdir_q.name


# --------------------------------------------------------------------------- #
# Queue file model: list/read/create/delete + frontmatter edits
# (relocated from test_nightshift_ui.py)
# --------------------------------------------------------------------------- #


def _seed(workspace: Path, tasks: dict[str, str] | None = None, **kw: object) -> Path:
    """Scaffold a two-root workspace and pin the operator config to the legacy
    shape (a ``claude-sonnet-4-6`` model default + the ``00._todo`` evergreen
    list, no ``validate`` key) so resolved-default assertions hold."""
    build_workspace(workspace, tasks=tasks, **kw)
    ns_dir = workspace / ".nightshift"
    ns_dir.mkdir(parents=True, exist_ok=True)
    (ns_dir / "manager.json").write_text(json.dumps({
        "default_model": "claude-code/claude-sonnet-4-6",
        "evergreen_tasks": ["00._todo"],
    }))
    return workspace / DEFAULT_TASKS_REPO


def test_delete_task(tmp_path: Path) -> None:
    tasks_root = _seed(tmp_path, tasks={"alpha": "Do alpha."})
    assert (tasks_root / "main/alpha.md").exists()

    result = delete_task(tasks_root, "alpha")
    assert result["deleted"] is True
    assert not (tasks_root / "main/alpha.md").exists()

    with pytest.raises(FileNotFoundError):
        delete_task(tasks_root, "alpha")
    # Path traversal is rejected.
    with pytest.raises(FileNotFoundError):
        delete_task(tasks_root, "../../etc/passwd")


def test_list_queue_skips_subdirs_and_flags_evergreen(tmp_path: Path) -> None:
    tasks_root = _seed(tmp_path, tasks={
        "alpha": "Do alpha.",
        "beta": "---\ntitle: Beta thing\nmodel: claude-opus-4-8\n---\nDo beta.",
        "00._todo": "---\nautosplit: true\n---\nstuff",
        "green": "---\nevergreen: true\n---\nrecurring",
    })
    notes = tasks_root / "main/notes"
    notes.mkdir()
    (notes / "ignore.md").write_text("not a task")

    queue = list_queue(tasks_root)
    names = [q["task"] for q in queue]
    assert "alpha" in names
    assert "ignore" not in names  # subdir skipped
    by = {q["task"]: q for q in queue}
    assert by["beta"]["title"] == "Beta thing"
    assert by["green"]["evergreen"] is True
    assert by["00._todo"]["evergreen"] is True  # via config evergreen_tasks


def test_read_task_returns_brief_with_resolved_frontmatter(tmp_path: Path) -> None:
    # The detail view needs the task's brief: title, body, and frontmatter merged
    # with resolved defaults (model/draft/automerge) even when the file omits them.
    tasks_root = _seed(tmp_path, tasks={
        "beta": "---\ntitle: Beta thing\nmodel: claude-opus-4-8\n---\nDo beta well.\n",
        "plain": "Just a body, no frontmatter.",
        "green": "---\nevergreen: true\n---\nrecurring",
    })

    beta = read_task(tasks_root, "beta")
    assert beta["task"] == "beta"
    assert beta["title"] == "Beta thing"
    assert beta["body"] == "Do beta well."
    assert beta["frontmatter"]["model"] == "claude-opus-4-8"
    # resolved defaults are filled in for omitted fields.
    assert "draft" in beta["frontmatter"]
    assert "automerge" in beta["frontmatter"]
    assert beta["evergreen"] is False

    plain = read_task(tasks_root, "plain")
    assert plain["title"] == "plain"
    assert plain["body"] == "Just a body, no frontmatter."
    # config model default applies when the file has no model.
    assert plain["frontmatter"]["model"] == "claude-code/claude-sonnet-4-6"

    green = read_task(tasks_root, "green")
    assert green["evergreen"] is True

    # missing / traversal-shaped names raise FileNotFoundError.
    with pytest.raises(FileNotFoundError):
        read_task(tasks_root, "nope")
    with pytest.raises(FileNotFoundError):
        read_task(tasks_root, "../../etc/passwd")


def test_set_task_meta_edits_toggles_and_model(tmp_path: Path) -> None:
    # The detail pane edits the frontmatter toggles and model in place, rewriting
    # existing keys where they sit and clearing the model to inherit the default.
    tasks_root = _seed(tmp_path, tasks={
        "alpha": "---\ntitle: Alpha\nmodel: claude-opus-4-8\ndraft: false\n---\nThe brief.\n",
    })

    brief = set_task_meta(tasks_root, "alpha", {"draft": True, "evergreen": True})
    assert brief["frontmatter"]["draft"] is True
    assert brief["evergreen"] is True
    # Untouched keys survive.
    assert brief["frontmatter"]["model"] == "claude-opus-4-8"

    # model None clears the pin so the task inherits the config default.
    cleared = set_task_meta(tasks_root, "alpha", {"model": None})
    assert "model" not in cleared["frontmatter_raw"]
    assert cleared["frontmatter"]["model"] == "claude-code/claude-sonnet-4-6"
    assert "non-editable" not in read_task(tasks_root, "alpha")["body"]


def test_set_task_meta_edits_title_and_body(tmp_path: Path) -> None:
    # "all details are editable": the pane saves a new title (frontmatter
    # headline) and brief prose alongside the toggles.
    tasks_root = _seed(tmp_path, tasks={
        "alpha": "---\ntitle: Old title\nmodel: claude-opus-4-8\n---\nOld brief.\n",
    })

    brief = set_task_meta(
        tasks_root, "alpha", {"title": "New title", "body": "New brief prose."}
    )
    assert brief["title"] == "New title"
    assert brief["body"] == "New brief prose."
    # The title rewrites the existing key in place, not a duplicate.
    text = (tasks_root / "main/alpha.md").read_text()
    assert text.count("title:") == 1
    # The model pin is preserved through a content-only edit.
    assert brief["frontmatter"]["model"] == "claude-opus-4-8"

    # A combined save updates everything in one call.
    combined = set_task_meta(
        tasks_root, "alpha", {"title": "T2", "body": "B2", "disabled": True, "draft": True}
    )
    assert combined["title"] == "T2"
    assert combined["body"] == "B2"
    assert combined["disabled"] is True
    assert combined["frontmatter"]["draft"] is True


def test_set_task_meta_rejects_empty_title_and_bad_keys(tmp_path: Path) -> None:
    tasks_root = _seed(tmp_path, tasks={"alpha": "---\ntitle: Alpha\n---\nbody"})
    with pytest.raises(ValueError):
        set_task_meta(tasks_root, "alpha", {"title": "   "})
    with pytest.raises(ValueError):
        set_task_meta(tasks_root, "alpha", {"bogus": "x"})
    with pytest.raises(FileNotFoundError):
        set_task_meta(tasks_root, "../../etc/passwd", {"draft": True})


def test_list_queue_respects_config_order(tmp_path: Path) -> None:
    tasks_root = _seed(tmp_path, tasks={
        "alpha": "Do alpha.",
        "beta": "Do beta.",
        "gamma": "Do gamma.",
    })
    reorder_queue(tasks_root, ["gamma", "alpha", "beta"])
    queue = list_queue(tasks_root)
    assert [q["task"] for q in queue] == ["gamma", "alpha", "beta"]


# --------------------------------------------------------------------------- #
# execution order — <queue>/config.json (relocated from test_nightshift_ui.py)
# --------------------------------------------------------------------------- #


def test_order_config_drives_queue_order(tmp_path: Path) -> None:
    # Numbering removed: order is driven by the queue's config.json, not the filename.
    tasks_root = _seed(tmp_path, tasks={
        "alpha": "Do alpha.",
        "beta": "Do beta.",
        "gamma": "Do gamma.",
    })
    save_order(tasks_root, ["gamma", "alpha", "beta"])
    names = [q["task"] for q in list_queue(tasks_root)]
    assert names == ["gamma", "alpha", "beta"]


def test_order_unlisted_tasks_fall_back_to_filename(tmp_path: Path) -> None:
    # Listed tasks lead in configured order; unlisted ones follow lexically.
    tasks_root = _seed(tmp_path, tasks={"alpha": "a", "beta": "b", "gamma": "c", "delta": "d"})
    save_order(tasks_root, ["gamma"])
    names = [q["task"] for q in list_queue(tasks_root)]
    assert names == ["gamma", "alpha", "beta", "delta"]


def test_order_stems_ignores_stale_and_missing_config(tmp_path: Path) -> None:
    # A queue with no config.json order falls back to pure filename order. The
    # builder writes a (repo-bound) config with an empty ``order``, so drop it
    # to exercise the "no order configured" path.
    tasks_root = _seed(tmp_path)
    (tasks_root / "main/config.json").unlink()
    assert order_stems(tasks_root, ["b", "a"]) == ["a", "b"]
    # Stale entries (no such stem in the input) are ignored, not surfaced.
    save_order(tasks_root, ["ghost", "b"])
    assert order_stems(tasks_root, ["a", "b"]) == ["b", "a"]


def test_reorder_queue_drops_unknown_and_appends_missing(tmp_path: Path) -> None:
    tasks_root = _seed(tmp_path, tasks={"one": "1", "two": "2", "three": "3"})
    # Reorder with a spoofed name and an omitted real task.
    result = reorder_queue(tasks_root, ["three", "one", "ghost"])
    # ghost is dropped (no file); two is appended in filename order.
    assert result == ["three", "one", "two"]
    assert load_order(tasks_root) == ["three", "one", "two"]
    assert [q["task"] for q in list_queue(tasks_root)] == ["three", "one", "two"]


def test_save_queue_config_value_preserves_siblings(tmp_path: Path) -> None:
    # Persisting the per-queue validate command must keep the order (and any
    # other sibling keys) intact, and clearing it (None) removes the key.
    tasks_root = _seed(tmp_path, tasks={"one": "1", "two": "2"})
    save_order(tasks_root, ["two", "one"])

    save_queue_config_value(tasks_root, "validate", "just check")
    cfg = json.loads((tasks_root / "main/config.json").read_text())
    assert cfg["validate"] == "just check"
    assert cfg["order"] == ["two", "one"]  # sibling preserved

    save_queue_config_value(tasks_root, "validate", None)
    cfg = json.loads((tasks_root / "main/config.json").read_text())
    assert "validate" not in cfg
    assert cfg["order"] == ["two", "one"]


def test_resolve_validate_cmd_absent_uses_default() -> None:
    # An absent validate key inherits the default.
    assert resolve_validate_cmd({}) == ["just", "validate"]
    assert resolve_validate_cmd({"model": "x"}) == ["just", "validate"]


def test_resolve_validate_cmd_present_splits() -> None:
    assert resolve_validate_cmd({"validate": "just check"}) == ["just", "check"]


@pytest.mark.parametrize("empty", ["", "   ", "\t", "\n  "])
def test_resolve_validate_cmd_empty_disables_validation(empty: str) -> None:
    # A present-but-empty validate key disables validation (None) — it must NOT
    # fall back to the inherited default.
    assert resolve_validate_cmd({"validate": empty}) is None


def test_format_validate_cmd() -> None:
    assert format_validate_cmd(["just", "validate"]) == "just validate"
    assert format_validate_cmd(None) == ""


def test_validate_cmd_from_blob_authoritative() -> None:
    argv, display = validate_cmd_from_blob({"validate_cmd": "just check"})
    assert argv == ["just", "check"]
    assert display == "just check"
    assert validate_cmd_from_blob({"validate_cmd": ""}) == (None, None)


def test_validate_cmd_from_blob_legacy_validate_key() -> None:
    argv, display = validate_cmd_from_blob({"validate": "just lint"})
    assert argv == ["just", "lint"]
    assert display == "just lint"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ""),
        ("   ", ""),
        ("''", ""),
        ('""', ""),
        ("  just validate  ", "just validate"),
        ("just check", "just check"),
    ],
)
def test_normalize_validate_command(raw: str, expected: str) -> None:
    assert normalize_validate_command(raw) == expected


def test_resolve_config_empty_validate_overrides_parent_default(tmp_path: Path) -> None:
    # Spec: an empty-string validate on a queue must not pick up the parent
    # config default. The playlist's "" overrides the main queue's command, and
    # resolve_validate_cmd then reads it as "validation disabled".
    tasks_root = _seed(tmp_path)
    # In the two-root model the layer a queue inherits is the content-store
    # config (``<tasks_root>/config.json``), not a sibling queue — that's the
    # "parent default" the playlist overrides.
    (tasks_root / "config.json").write_text(json.dumps({"validate": "just validate"}))
    (tasks_root / "ns").mkdir(parents=True)
    (tasks_root / "ns/config.json").write_text(
        json.dumps({"validate": "", "order": []})
    )

    pl = resolve_config(tmp_path, tasks_root, "ns")
    assert pl["validate"] == ""
    assert resolve_validate_cmd(pl) is None

    # The main queue still validates with the inherited parent command.
    main = resolve_config(tmp_path, tasks_root, "main")
    assert resolve_validate_cmd(main) == ["just", "validate"]


def test_delete_task_removes_from_order(tmp_path: Path) -> None:
    tasks_root = _seed(tmp_path, tasks={"one": "1", "two": "2"})
    save_order(tasks_root, ["two", "one"])
    delete_task(tasks_root, "two")
    assert load_order(tasks_root) == ["one"]


# --------------------------------------------------------------------------- #
# task creation (relocated from test_nightshift_ui.py)
# --------------------------------------------------------------------------- #


def test_create_task_writes_unnumbered_file(tmp_path: Path) -> None:
    # Spec: "remove the numbering from tasks" — new tasks are named by their
    # slugified title with no NN. prefix, and appended to the execution order.
    tasks_root = _seed(tmp_path)
    created = create_task(tasks_root, "Fix the ops screen", "Make it nicer.")
    assert created["task"] == "fix-the-ops-screen"
    dest = tasks_root / "main/fix-the-ops-screen.md"
    assert dest.exists()
    text = dest.read_text()
    assert "title: Fix the ops screen" in text
    assert "Make it nicer." in text
    # The new task lands at the end of the configured order.
    assert load_order(tasks_root) == ["fix-the-ops-screen"]


def test_create_task_rejects_empty_title_and_collision(tmp_path: Path) -> None:
    # Spec: numbering is gone, so there is no longer a numeric `pri` to reject;
    # the remaining guards are an empty title and a name collision.
    tasks_root = _seed(tmp_path)
    with pytest.raises(ValueError):
        create_task(tasks_root, "   ", "body")
    create_task(tasks_root, "Dup", "body")
    with pytest.raises(FileExistsError):
        create_task(tasks_root, "Dup", "body")


# --------------------------------------------------------------------------- #
# original brief (the preserved pre-enhancement text)
# --------------------------------------------------------------------------- #


def test_split_join_original_round_trips() -> None:
    # A body without the marker has no original.
    assert split_original("Just the brief.") == ("Just the brief.", "")
    # join/split are inverses, and both halves come back stripped.
    joined = join_original("Enhanced spec.\n", "\nraw typed text\n")
    assert ORIGINAL_BRIEF_MARKER in joined
    assert split_original(joined) == ("Enhanced spec.", "raw typed text")
    # An empty original writes no marker (byte-for-byte legacy body).
    assert join_original("Enhanced spec.", "") == "Enhanced spec."


def test_create_task_with_original_preserves_both(tmp_path: Path) -> None:
    tasks_root = _seed(tmp_path)
    created = create_task(
        tasks_root, "Enhance me", "The enhanced spec.", original="the raw ask",
    )
    text = (tasks_root / "main" / f"{created['task']}.md").read_text()
    assert ORIGINAL_BRIEF_MARKER in text

    brief = read_task(tasks_root, created["task"])
    assert brief["body"] == "The enhanced spec."
    assert brief["original_brief"] == "the raw ask"
    # The marker never leaks into the effective body the UI edits.
    assert ORIGINAL_BRIEF_MARKER not in brief["body"]


def test_create_task_without_original_writes_no_marker(tmp_path: Path) -> None:
    tasks_root = _seed(tmp_path)
    created = create_task(tasks_root, "Plain create", "Just do it.")
    text = (tasks_root / "main" / f"{created['task']}.md").read_text()
    assert ORIGINAL_BRIEF_MARKER not in text
    assert read_task(tasks_root, created["task"])["original_brief"] == ""


def test_set_task_meta_edits_each_half_independently(tmp_path: Path) -> None:
    tasks_root = _seed(tmp_path)
    create_task(tasks_root, "Halves", "Enhanced spec.", original="raw ask")

    # Editing the body leaves the preserved original untouched.
    brief = set_task_meta(tasks_root, "halves", {"body": "Better spec."})
    assert brief["body"] == "Better spec."
    assert brief["original_brief"] == "raw ask"

    # Editing the original leaves the body untouched.
    brief = set_task_meta(tasks_root, "halves", {"original_brief": "raw ask v2"})
    assert brief["body"] == "Better spec."
    assert brief["original_brief"] == "raw ask v2"

    # Clearing the original drops the marker section from the file.
    brief = set_task_meta(tasks_root, "halves", {"original_brief": ""})
    assert brief["original_brief"] == ""
    assert ORIGINAL_BRIEF_MARKER not in (tasks_root / "main/halves.md").read_text()
    assert brief["body"] == "Better spec."


def test_set_task_meta_enhanced_flag_round_trips(tmp_path: Path) -> None:
    tasks_root = _seed(tmp_path, tasks={"a": "---\ntitle: A\n---\nDo a."})
    set_task_meta(tasks_root, "a", {"enhanced": True})
    assert read_task(tasks_root, "a")["frontmatter"]["enhanced"] is True


# --------------------------------------------------------------------------- #
# completed-task backstop (relocated from test_nightshift_ui.py)
# --------------------------------------------------------------------------- #


def test_drop_completed_task_removes_lingering_file(tmp_path: Path) -> None:
    """A landed regular task whose worker forgot to git-rm its own file is
    dropped from the queue (file + execution-order entry) so the UI stops
    listing a completed task."""
    tasks_root = _seed(tmp_path, tasks={"alpha": "do alpha", "beta": "do beta"})
    save_order(tasks_root, ["alpha", "beta"])

    assert drop_completed_task(tasks_root, "alpha") is True
    assert not (tasks_root / "main/alpha.md").exists()
    assert [q["task"] for q in list_queue(tasks_root)] == ["beta"]
    assert load_order(tasks_root) == ["beta"]

    # Idempotent: a file that's already gone (the worker did remove it) is a no-op.
    assert drop_completed_task(tasks_root, "alpha") is False


def test_drop_completed_task_keeps_evergreen_file(tmp_path: Path) -> None:
    """An evergreen task keeps its file (it resets and re-runs), so the
    completion backstop must not touch it — only regular tasks call it."""
    tasks_root = _seed(tmp_path, tasks={"green": "---\nevergreen: true\n---\nrecurring"})
    config = resolve_config(tmp_path, tasks_root)
    meta = split_frontmatter((tasks_root / "main/green.md").read_text())[0]
    assert task_is_evergreen(meta, "green", config) is True
    # The run paths only call drop_completed_task for non-evergreen tasks; the
    # file is left in place for evergreen ones.
    assert (tasks_root / "main/green.md").exists()


# --------------------------------------------------------------------------- #
# Playlists (directory-backed alternate queues) + config layering
# (relocated from test_nightshift_ui.py)
# --------------------------------------------------------------------------- #


def test_playlists_crud_round_trip(tmp_path: Path) -> None:
    tasks_root = _seed(tmp_path)
    assert playlists.list_playlists(tasks_root) == []

    created = playlists.create_playlist(tasks_root, "Morning Open")
    assert created["name"] == "morning-open"
    # A fresh playlist's config holds only an empty queue order.
    cfg = json.loads((tasks_root / "morning-open/config.json").read_text())
    assert cfg == {"order": []}

    # duplicate name → FileExistsError; empty name → ValueError.
    with pytest.raises(FileExistsError):
        playlists.create_playlist(tasks_root, "Morning Open")
    with pytest.raises(ValueError):
        playlists.create_playlist(tasks_root, "   ")

    assert playlists.exists(tasks_root, "morning-open")
    assert playlists.list_playlists(tasks_root) == [
        {"name": "morning-open", "task_count": 0, "disabled": False}
    ]

    assert playlists.delete_playlist(tasks_root, "morning-open") is True
    assert playlists.delete_playlist(tasks_root, "morning-open") is False
    # traversal-shaped names are rejected outright.
    assert playlists.delete_playlist(tasks_root, "../../etc") is False


def test_resolve_config_layers_shipped_tasks_and_playlist(tmp_path: Path) -> None:
    """Runner config resolves operator/shipped defaults <- content-store
    config.json <- per-queue config.json, so a queue inherits anything it doesn't
    override. The store config (``<tasks_root>/config.json``) is the shared layer
    that replaces the old nested ``.tasks/config.json`` parent."""
    tasks_root = _seed(tmp_path)  # operator config: model claude-sonnet-4-6
    # The content-store layer every queue inherits.
    (tasks_root / "config.json").write_text(
        json.dumps({"validate": "just validate", "automerge": True})
    )
    (tasks_root / "ns").mkdir(parents=True)
    (tasks_root / "ns/config.json").write_text(
        json.dumps({"validate": "just validate-nightshift", "order": []})
    )

    main = resolve_config(tmp_path, tasks_root, "main")
    assert main["validate"] == "just validate"
    assert main["default_model"] == "claude-code/claude-sonnet-4-6"  # operator defaults
    assert main["automerge"] is True              # from the store layer

    pl = resolve_config(tmp_path, tasks_root, "ns")
    assert pl["validate"] == "just validate-nightshift"  # queue override
    assert pl["default_model"] == "claude-code/claude-sonnet-4-6"  # inherited
    assert pl["automerge"] is True                       # inherited


def test_queue_ops_operate_on_playlist_dir(tmp_path: Path) -> None:
    """create/list/reorder/delete work against a playlist sub-dir when tasks_rel
    points at one, leaving the main queue untouched."""
    tasks_root = _seed(
        tmp_path, tasks={"main-a": "a"}, queues={"ns": {"config": {"order": []}}}
    )

    create_task(tasks_root, "Beta", "b", "ns")
    create_task(tasks_root, "Alpha", "a", "ns")
    assert (tasks_root / "ns/beta.md").exists()
    # main queue only sees its own task (sub-dirs are skipped).
    assert [q["task"] for q in list_queue(tasks_root)] == ["main-a"]

    reorder_queue(tasks_root, ["alpha", "beta"], "ns")
    assert [q["task"] for q in list_queue(tasks_root, "ns")] == ["alpha", "beta"]

    delete_task(tasks_root, "beta", "ns")
    assert [q["task"] for q in list_queue(tasks_root, "ns")] == ["alpha"]


def test_save_config_value_round_trips_and_preserves_siblings(tmp_path: Path) -> None:
    """The root-config writer sets one key without disturbing siblings, and
    updates an existing key in place."""
    _seed(tmp_path)
    cfg = tmp_path / ".nightshift" / "manager.json"
    cfg.write_text(json.dumps(
        {"model": "m", "max_per_day": 5, "auto_resolve": True}, indent=2
    ) + "\n")

    save_config_value(tmp_path, "max_concurrent_queues", 3)
    data = json.loads(cfg.read_text())
    assert data["max_concurrent_queues"] == 3
    assert data["model"] == "m"
    assert data["max_per_day"] == 5
    assert data["auto_resolve"] is True
    assert load_config(tmp_path)["max_concurrent_queues"] == 3

    save_config_value(tmp_path, "max_concurrent_queues", 4)
    assert json.loads(cfg.read_text())["max_concurrent_queues"] == 4
