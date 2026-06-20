"""Tests for the Slack capture daemon glue (tools/nightshift/slack/slackd.py).

Exercises the §5/§9 security model and capture flow through
:class:`CaptureHandler` with a fake poster and a faked normalise backend — no
``slack-bolt``, no network. Also checks the unconfigured-exit message and the
provenance trailer (spec §9.4).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nightshift.slack import slackd
from nightshift.slack.config import SlackConfig
from nightshift.slack.intake import ParsedTask
from nightshift.slack.slackd import (
    ACTION_CANCEL,
    ACTION_ENQUEUE,
    CaptureHandler,
    commit_message,
)


class _FakeBackend:
    def normalise(self, text: str) -> tuple[str, str]:
        return "Captured task", "cleaned body"


class _FakePoster:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.reactions: list[dict[str, Any]] = []
        self.ephemerals: list[dict[str, Any]] = []

    def post_message(
        self,
        *,
        channel: str,
        text: str,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        self.messages.append(
            {"channel": channel, "text": text, "thread_ts": thread_ts, "blocks": blocks}
        )

    def ephemeral(self, *, channel: str, user: str, text: str) -> None:
        self.ephemerals.append({"channel": channel, "user": user, "text": text})

    def react(self, *, channel: str, ts: str, name: str) -> None:
        self.reactions.append({"channel": channel, "ts": ts, "name": name})

    def permalink(self, *, channel: str, ts: str) -> str | None:
        return f"https://slack/{channel}/{ts}"


def _config(**over: Any) -> SlackConfig:
    base: dict[str, Any] = dict(
        enabled=True,
        activity_channel="#a",
        announce_task_log=False,
        bot_token="xoxb",
        intake_channel="C-INTAKE",
        allowed_users=("U-OK",),
        require_confirmation=True,
        default_enqueue="commit",
        app_token="xapp",
    )
    base.update(over)
    return SlackConfig(**base)


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".tasks").mkdir()
    (tmp_path / ".tasks" / "config.json").write_text(json.dumps({"order": []}) + "\n")
    return tmp_path


def _handler(tmp_path: Path, poster: _FakePoster, **cfg: Any) -> CaptureHandler:
    return CaptureHandler(
        _repo(tmp_path), _config(**cfg), backend=_FakeBackend(), poster=poster
    )


def _msg(**over: Any) -> dict[str, Any]:
    base = {"channel": "C-INTAKE", "ts": "1.1", "user": "U-OK", "text": "do a thing"}
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# Gating (spec §5.1, §9)
# --------------------------------------------------------------------------- #


def test_ignores_non_allowlisted_user(tmp_path: Path) -> None:
    poster = _FakePoster()
    handler = _handler(tmp_path, poster)
    handler.on_message(_msg(user="U-NOPE"))
    assert poster.messages == []  # silent (spec §9.1)


def test_ignores_wrong_channel(tmp_path: Path) -> None:
    poster = _FakePoster()
    handler = _handler(tmp_path, poster)
    handler.on_message(_msg(channel="C-OTHER"))
    assert poster.messages == []


def test_ignores_threaded_reply_bot_and_edit(tmp_path: Path) -> None:
    poster = _FakePoster()
    handler = _handler(tmp_path, poster)
    handler.on_message(_msg(thread_ts="0.9"))  # reply to another message
    handler.on_message(_msg(bot_id="B1"))
    handler.on_message(_msg(subtype="message_changed"))
    handler.on_message(_msg(text="   "))
    assert poster.messages == []


# --------------------------------------------------------------------------- #
# Confirmation flow (spec §5.3)
# --------------------------------------------------------------------------- #


def test_confirmation_posts_card_with_buttons(tmp_path: Path) -> None:
    poster = _FakePoster()
    handler = _handler(tmp_path, poster)
    handler.on_message(_msg())
    assert len(poster.messages) == 1
    msg = poster.messages[0]
    assert msg["thread_ts"] == "1.1"
    action_ids = {
        el["action_id"]
        for block in msg["blocks"]
        if block["type"] == "actions"
        for el in block["elements"]
    }
    assert {ACTION_ENQUEUE, ACTION_CANCEL} <= action_ids


def test_enqueue_action_lands_and_acks(tmp_path: Path, monkeypatch: Any) -> None:
    commits: list[list[str]] = []
    monkeypatch.setattr(slackd, "_git", lambda root, args: commits.append(args))
    poster = _FakePoster()
    handler = _handler(tmp_path, poster)
    handler.on_message(_msg())
    token = poster.messages[0]["blocks"][1]["elements"][0]["value"]

    handler.on_action(ACTION_ENQUEUE, token, "U-OK")

    assert any(r["name"] == "white_check_mark" for r in poster.reactions)
    assert any("Queued" in m["text"] for m in poster.messages)
    assert any(args[0] == "commit" for args in commits)


def test_cancel_action_drops_capture(tmp_path: Path) -> None:
    poster = _FakePoster()
    handler = _handler(tmp_path, poster)
    handler.on_message(_msg())
    token = poster.messages[0]["blocks"][1]["elements"][0]["value"]
    handler.on_action(ACTION_CANCEL, token, "U-OK")
    # A second click on the same token is a no-op (already popped).
    handler.on_action(ACTION_ENQUEUE, token, "U-OK")
    assert not any(r["name"] == "white_check_mark" for r in poster.reactions)
    assert any("cancelled" in m["text"].lower() for m in poster.messages)


def test_action_rejects_non_allowlisted_user(tmp_path: Path) -> None:
    poster = _FakePoster()
    handler = _handler(tmp_path, poster)
    handler.on_message(_msg())
    token = poster.messages[0]["blocks"][1]["elements"][0]["value"]
    handler.on_action(ACTION_ENQUEUE, token, "U-NOPE")
    assert poster.ephemerals  # ephemeral "not permitted" (spec §9.1)
    assert not any(r["name"] == "white_check_mark" for r in poster.reactions)


def test_no_confirmation_enqueues_directly(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(slackd, "_git", lambda root, args: None)
    poster = _FakePoster()
    handler = _handler(tmp_path, poster, require_confirmation=False)
    handler.on_message(_msg())
    assert any(r["name"] == "white_check_mark" for r in poster.reactions)


def test_pr_mode_skips_commit(tmp_path: Path, monkeypatch: Any) -> None:
    commits: list[list[str]] = []
    monkeypatch.setattr(slackd, "_git", lambda root, args: commits.append(args))
    poster = _FakePoster()
    handler = _handler(
        tmp_path, poster, require_confirmation=False, default_enqueue="pr"
    )
    handler.on_message(_msg())
    assert commits == []  # file written, no commit (spec §5.4 pr mode)
    assert any(r["name"] == "white_check_mark" for r in poster.reactions)


# --------------------------------------------------------------------------- #
# Provenance + unconfigured exit (spec §9.4, §3)
# --------------------------------------------------------------------------- #


def test_commit_message_carries_provenance() -> None:
    from nightshift.slack.intake import EnqueueResult

    parsed = ParsedTask(
        title="t",
        body="b",
        frontmatter={"title": "t"},
        author="U-OK",
        permalink="https://slack/p",
    )
    result = EnqueueResult(slug="s", path=Path("x"), tasks_rel=".tasks", queue=None)
    msg = commit_message(result, parsed)
    assert "Slack-Author: U-OK" in msg
    assert "Slack-Permalink: https://slack/p" in msg


def test_run_exits_cleanly_when_unconfigured(tmp_path: Path, monkeypatch: Any) -> None:
    root = _repo(tmp_path)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    # No tokens, slack disabled ⇒ intake inactive ⇒ exit 2, not a crash.
    rc = slackd.run(root)
    assert rc == 2
