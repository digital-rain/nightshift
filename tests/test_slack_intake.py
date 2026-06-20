"""Tests for the inbound Slack capture inbox (tools/nightshift/slack/intake.py).

Covers the directive parser (spec §6), the normalise→frontmatter mapping with a
faked backend (spec §5.2), the verbatim-frontmatter bypass, the confirmation
preview, and the enqueue writer against a temp repo — file lands in the right
queue, ``order`` updated for ``#now``, playlist created when needed (spec §5.4).
No Slack, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

from _workspace import build_workspace
from nightshift.slack.intake import (
    ParsedTask,
    build_task,
    enqueue,
    parse_directives,
    render_confirmation,
    render_task_markdown,
)


class _FakeBackend:
    """Records the text it was handed and returns a canned (title, body)."""

    def __init__(self, title: str = "Canned title", body: str = "Canned body.") -> None:
        self.title = title
        self.body = body
        self.seen: list[str] = []

    def normalise(self, text: str) -> tuple[str, str]:
        self.seen.append(text)
        return self.title, self.body


# --------------------------------------------------------------------------- #
# Directive parser (spec §6)
# --------------------------------------------------------------------------- #


def test_parse_flag_directives() -> None:
    cleaned, d = parse_directives("Do a thing #draft #automerge #split #now")
    assert d.overrides["draft"] is True
    assert d.overrides["automerge"] is True
    assert d.overrides["split"] is True
    assert d.now is True
    assert "#draft" not in cleaned and "#now" not in cleaned
    assert cleaned.strip() == "Do a thing"


def test_parse_model_aliases_and_explicit() -> None:
    _, opus = parse_directives("x #opus")
    assert opus.overrides["model"] == "claude-opus-4-8"
    _, sonnet = parse_directives("x #sonnet")
    assert sonnet.overrides["model"] == "claude-sonnet-4-6"
    _, explicit = parse_directives("x model: claude-haiku-9")
    assert explicit.overrides["model"] == "claude-haiku-9"


def test_parse_queue_routing_both_forms() -> None:
    _, kv = parse_directives("x queue: experiments")
    assert kv.queue == "experiments"
    _, hashq = parse_directives("x #q-experiments")
    assert hashq.queue == "experiments"


def test_parse_numeric_and_after() -> None:
    _, d = parse_directives("x loc: 200 turns: 12 after: tz-helpers")
    assert d.overrides["loc"] == 200
    assert d.overrides["turns"] == 12
    assert d.overrides["after"] == "tz-helpers"


def test_parse_loc_ignores_non_numeric() -> None:
    _, d = parse_directives("x loc: huge")
    assert "loc" not in d.overrides


def test_parse_leaves_unknown_hashtags_in_body() -> None:
    cleaned, d = parse_directives("Fix the #frontend bug")
    assert "#frontend" in cleaned
    assert d.overrides == {}


def test_parse_directives_order_independent() -> None:
    cleaned, d = parse_directives(
        "queue: exp first line\nbody text #automerge after: foo"
    )
    assert d.queue == "exp"
    assert d.overrides["automerge"] is True
    assert d.overrides["after"] == "foo"
    assert "queue:" not in cleaned and "after:" not in cleaned


# --------------------------------------------------------------------------- #
# Normalise → frontmatter mapping (spec §5.2)
# --------------------------------------------------------------------------- #


def test_build_task_uses_backend_and_strips_directives() -> None:
    backend = _FakeBackend(title="Make rollup tz-aware", body="cleaned body")
    parsed = build_task(
        "Fix flaky timezone test\n\nsuspect naive datetime #sonnet #automerge",
        backend=backend,
    )
    # Backend never sees the directives.
    assert "#sonnet" not in backend.seen[0]
    assert "#automerge" not in backend.seen[0]
    assert parsed.title == "Make rollup tz-aware"
    assert parsed.body == "cleaned body"
    assert parsed.frontmatter["model"] == "claude-sonnet-4-6"
    assert parsed.frontmatter["automerge"] is True
    assert parsed.frontmatter["title"] == "Make rollup tz-aware"


def test_build_task_inherits_config_defaults() -> None:
    backend = _FakeBackend()
    parsed = build_task(
        "do it",
        backend=backend,
        config_defaults={"model": "claude-opus-4-8", "draft": False},
    )
    assert parsed.frontmatter["model"] == "claude-opus-4-8"
    assert parsed.frontmatter["draft"] is False


def test_build_task_override_beats_default() -> None:
    backend = _FakeBackend()
    parsed = build_task(
        "do it #opus",
        backend=backend,
        config_defaults={"model": "claude-sonnet-4-6"},
    )
    assert parsed.frontmatter["model"] == "claude-opus-4-8"


def test_build_task_records_provenance() -> None:
    parsed = build_task(
        "do it", backend=_FakeBackend(), author="U123", permalink="https://x/p"
    )
    assert parsed.author == "U123"
    assert parsed.permalink == "https://x/p"


def test_build_task_frontmatter_block_bypasses_backend() -> None:
    backend = _FakeBackend()
    text = (
        "---\n"
        "title: Verbatim task\n"
        "model: claude-opus-4-8\n"
        "automerge: true\n"
        "---\n"
        "The literal body.\n"
    )
    parsed = build_task(text, backend=backend)
    assert backend.seen == []  # LLM bypassed (spec §5.2)
    assert parsed.title == "Verbatim task"
    assert parsed.frontmatter["model"] == "claude-opus-4-8"
    assert parsed.frontmatter["automerge"] is True
    assert parsed.body == "The literal body."


def test_slug_derives_from_title() -> None:
    parsed = build_task("x", backend=_FakeBackend(title="Fix Flaky TZ Test!"))
    assert parsed.slug == "fix-flaky-tz-test"


# --------------------------------------------------------------------------- #
# Confirmation preview (spec §5.3)
# --------------------------------------------------------------------------- #


def test_render_confirmation_shows_title_frontmatter_and_now() -> None:
    parsed = ParsedTask(
        title="A task",
        body="Body here",
        frontmatter={"title": "A task", "automerge": True},
        queue="experiments",
        now=True,
    )
    out = render_confirmation(parsed)
    assert "*A task*" in out
    assert "automerge: true" in out
    assert "experiments" in out
    assert "#now" in out


def test_render_task_markdown_round_trips() -> None:
    parsed = ParsedTask(
        title="A task",
        body="Body here",
        frontmatter={"title": "A task", "model": "claude-opus-4-8", "draft": False},
    )
    md = render_task_markdown(parsed)
    assert md.startswith("---\n")
    assert "title: A task" in md
    assert "model: claude-opus-4-8" in md
    assert "draft: false" in md
    assert md.rstrip().endswith("Body here")


# --------------------------------------------------------------------------- #
# Enqueue writer (spec §5.4)
# --------------------------------------------------------------------------- #


def _tasks_root(tmp_path: Path, *, order: list[str] | None = None) -> Path:
    """Build a content store (``<ws>/nightshift-tasks``) with a seeded ``main``
    queue ``order`` and return its root — the value ``enqueue`` now addresses."""
    workspace = build_workspace(
        tmp_path, main_repo=None, repos=(), commit_tasks=False
    )
    tasks_root = workspace / "nightshift-tasks"
    seeded = list(order if order is not None else ["existing"])
    (tasks_root / "main" / "config.json").write_text(
        json.dumps({"order": seeded}) + "\n"
    )
    return tasks_root


def test_enqueue_main_queue_appends_order(tmp_path: Path) -> None:
    tasks_root = _tasks_root(tmp_path)
    parsed = build_task("x", backend=_FakeBackend(title="New task"))
    result = enqueue(tasks_root, parsed)
    assert result.path == tasks_root / "main" / "new-task.md"
    assert result.path.exists()
    order = json.loads((tasks_root / "main" / "config.json").read_text())["order"]
    assert order == ["existing", "new-task"]


def test_enqueue_now_prepends_order(tmp_path: Path) -> None:
    tasks_root = _tasks_root(tmp_path)
    parsed = build_task("x #now", backend=_FakeBackend(title="Urgent"))
    result = enqueue(tasks_root, parsed)
    assert result.slug == "urgent"
    order = json.loads((tasks_root / "main" / "config.json").read_text())["order"]
    assert order == ["urgent", "existing"]


def test_enqueue_creates_playlist_when_needed(tmp_path: Path) -> None:
    tasks_root = _tasks_root(tmp_path)
    parsed = build_task("x queue: experiments", backend=_FakeBackend(title="Exp task"))
    result = enqueue(tasks_root, parsed)
    assert result.queue == "experiments"
    assert result.tasks_rel == "experiments"
    assert (tasks_root / "experiments" / "exp-task.md").exists()
    cfg = json.loads((tasks_root / "experiments" / "config.json").read_text())
    assert cfg["order"] == ["exp-task"]


def test_enqueue_unique_slug_on_collision(tmp_path: Path) -> None:
    tasks_root = _tasks_root(tmp_path)
    first = enqueue(tasks_root, build_task("x", backend=_FakeBackend(title="Dup")))
    second = enqueue(tasks_root, build_task("x", backend=_FakeBackend(title="Dup")))
    assert first.slug == "dup"
    assert second.slug == "dup-2"
    assert second.path.exists()


def test_enqueue_writes_directive_frontmatter(tmp_path: Path) -> None:
    tasks_root = _tasks_root(tmp_path)
    parsed = build_task(
        "x #automerge loc: 50 after: foo",
        backend=_FakeBackend(title="Rich"),
    )
    result = enqueue(tasks_root, parsed)
    text = result.path.read_text()
    assert "automerge: true" in text
    assert "loc: 50" in text
    assert "after: foo" in text
