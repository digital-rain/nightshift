"""Tests for the outbound Slack notifier (tools/nightshift/slack).

The event→card mapping, thread reuse, idempotent parent updates, and
best-effort error swallowing are exercised with a faked HTTP client — no real
network. Covers spec §4 (outbound activity cards) and §11 invariants 1 (no-op
when unconfigured) and 3 (best-effort, never fatal).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nightshift.events import (
    RUN_FINISHED,
    RUN_STARTED,
    TASK_LOG,
    TASK_RESULT,
    TASK_STARTED,
    TASK_STATUS,
    Event,
)
from nightshift.slack.config import SlackConfig, resolve_slack_config
from nightshift.slack.notify import make_slack_listener
from nightshift.slack.threads import ThreadRef, ThreadStore  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload


class _FakeHttp:
    """Records every POST and replies ``ok`` with an incrementing ``ts``."""

    def __init__(self, *, fail: bool = False, reject: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail = fail
        self._reject = reject
        self._n = 0

    def post(
        self, url: str, *, headers: dict[str, str], json: dict[str, Any]
    ) -> _FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json})
        if self._fail:
            raise RuntimeError("network down")
        if self._reject:
            return _FakeResponse({"ok": False, "error": "channel_not_found"})
        self._n += 1
        return _FakeResponse({"ok": True, "ts": f"171000000.{self._n:06d}"})

    def method_calls(self, method: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["url"].endswith(f"/api/{method}")]


def _active_config() -> SlackConfig:
    return SlackConfig(
        enabled=True,
        activity_channel="#nightshift-activity",
        announce_task_log=False,
        bot_token="xoxb-test",
    )


def _store(tmp_path: Path) -> ThreadStore:
    return ThreadStore(tmp_path / "slack-threads.json")


def _listener(tmp_path: Path, http: _FakeHttp, *, config: SlackConfig | None = None):
    return make_slack_listener(
        config or _active_config(), _store(tmp_path), client=http
    )


# --------------------------------------------------------------------------- #
# Config — invariant 1 (unconfigured = no-op)
# --------------------------------------------------------------------------- #


def test_config_disabled_when_no_slack_block() -> None:
    cfg = resolve_slack_config({"model": "x"}, env={"SLACK_BOT_TOKEN": "xoxb"})
    assert cfg.enabled is False
    assert cfg.active is False


def test_config_inactive_without_token_even_when_enabled() -> None:
    cfg = resolve_slack_config({"slack": {"enabled": True}}, env={})
    assert cfg.enabled is True
    assert cfg.active is False  # no token ⇒ no-op


def test_config_active_with_enabled_and_token() -> None:
    cfg = resolve_slack_config(
        {"slack": {"enabled": True, "activity_channel": "#c"}},
        env={"SLACK_BOT_TOKEN": "xoxb-abc"},
    )
    assert cfg.active is True
    assert cfg.activity_channel == "#c"


def test_config_tolerates_none_and_malformed() -> None:
    assert resolve_slack_config(None, env={}).active is False
    assert resolve_slack_config({"slack": "nope"}, env={}).active is False


def test_listener_no_op_when_inactive(tmp_path: Path) -> None:
    http = _FakeHttp()
    disabled = SlackConfig(
        enabled=False,
        activity_channel="#c",
        announce_task_log=False,
        bot_token="xoxb",
    )
    listener = _listener(tmp_path, http, config=disabled)
    for ev in (
        Event(RUN_STARTED, {"run_id": "r", "tasks": ["10.a"]}),
        Event(TASK_STARTED, {"task": "10.a", "title": "A"}),
        Event(TASK_RESULT, {"task": "10.a", "status": "completed"}),
        Event(RUN_FINISHED, {"run_id": "r"}),
    ):
        listener(ev)
    assert http.calls == []  # never touched the network


# --------------------------------------------------------------------------- #
# Event → card mapping
# --------------------------------------------------------------------------- #


def test_run_started_posts_summary_card(tmp_path: Path) -> None:
    http = _FakeHttp()
    listener = _listener(tmp_path, http)
    listener(Event(RUN_STARTED, {"run_id": "r", "tasks": ["10.a", "20.b"]}))
    posts = http.method_calls("chat.postMessage")
    assert len(posts) == 1
    text = json.dumps(posts[0]["json"])
    assert "Run started" in text
    assert "2 tasks" in text


def test_task_started_posts_parent_and_records_thread(tmp_path: Path) -> None:
    http = _FakeHttp()
    store = _store(tmp_path)
    listener = make_slack_listener(_active_config(), store, client=http)
    listener(
        Event(
            TASK_STARTED,
            {"task": "10.a", "title": "Do A", "frontmatter": {"model": "opus"}},
        )
    )
    posts = http.method_calls("chat.postMessage")
    assert len(posts) == 1
    body = posts[0]["json"]
    assert body["channel"] == "#nightshift-activity"
    assert "Do A" in json.dumps(body)
    ref = store.get("10.a")
    assert ref is not None
    assert ref.channel == "#nightshift-activity"
    assert ref.thread_ts  # recorded the parent message ts


def test_task_status_edits_parent_in_place(tmp_path: Path) -> None:
    http = _FakeHttp()
    listener = _listener(tmp_path, http)
    listener(Event(TASK_STARTED, {"task": "10.a", "title": "Do A"}))
    listener(Event(TASK_STATUS, {"task": "10.a", "phase": "validate"}))
    listener(Event(TASK_STATUS, {"task": "10.a", "phase": "commit"}))

    posts = http.method_calls("chat.postMessage")
    updates = http.method_calls("chat.update")
    assert len(posts) == 1  # one parent created
    assert len(updates) == 2  # edited in place, not re-posted
    # The updates target the same message ts.
    ts_values = {u["json"]["ts"] for u in updates}
    assert len(ts_values) == 1
    assert "validate" in json.dumps(updates[0]["json"])
    assert "commit" in json.dumps(updates[1]["json"])


def test_task_result_finalises_and_posts_reply(tmp_path: Path) -> None:
    http = _FakeHttp()
    listener = _listener(tmp_path, http)
    listener(Event(TASK_STARTED, {"task": "10.a", "title": "Do A"}))
    listener(
        Event(
            TASK_RESULT,
            {
                "task": "10.a",
                "status": "completed",
                "result_line": "landed it",
                "commit_sha": "abcdef1234567",
            },
        )
    )
    updates = http.method_calls("chat.update")
    posts = http.method_calls("chat.postMessage")
    # parent finalised (edit) + threaded reply (post in-thread).
    assert len(updates) == 1
    assert "landed" in json.dumps(updates[0]["json"])
    assert "abcdef1" in json.dumps(updates[0]["json"])  # short sha
    reply = posts[-1]["json"]
    assert reply.get("thread_ts")  # posted as a threaded reply
    assert "landed it" in json.dumps(reply)


def test_task_result_failure_surfaces_failure_kind(tmp_path: Path) -> None:
    http = _FakeHttp()
    listener = _listener(tmp_path, http)
    listener(Event(TASK_STARTED, {"task": "10.a", "title": "Do A"}))
    listener(
        Event(
            TASK_RESULT,
            {
                "task": "10.a",
                "status": "error",
                "error": "validate failed",
                "failure_kind": "validation_error",
            },
        )
    )
    reply = http.method_calls("chat.postMessage")[-1]["json"]
    text = json.dumps(reply)
    assert "validation_error" in text
    assert "validate failed" in text
    parent = http.method_calls("chat.update")[-1]["json"]
    assert "failed" in json.dumps(parent)


def test_run_finished_edits_summary_with_counts(tmp_path: Path) -> None:
    http = _FakeHttp()
    listener = _listener(tmp_path, http)
    listener(Event(RUN_STARTED, {"run_id": "r", "tasks": ["10.a", "20.b", "30.c"]}))
    for slug, status in (("10.a", "completed"), ("20.b", "error"), ("30.c", "skipped")):
        listener(Event(TASK_STARTED, {"task": slug, "title": slug}))
        listener(Event(TASK_RESULT, {"task": slug, "status": status}))
    listener(Event(RUN_FINISHED, {"run_id": "r"}))

    updates = http.method_calls("chat.update")
    summary_edit = updates[-1]["json"]
    text = json.dumps(summary_edit)
    assert "Run finished" in text
    assert "1 landed" in text
    assert "1 failed" in text
    assert "1 skipped" in text


def test_task_log_ignored_by_default(tmp_path: Path) -> None:
    http = _FakeHttp()
    listener = _listener(tmp_path, http)
    listener(Event(TASK_STARTED, {"task": "10.a", "title": "Do A"}))
    before = len(http.calls)
    listener(Event(TASK_LOG, {"task": "10.a", "line": "noisy output\n"}))
    assert len(http.calls) == before  # TASK_LOG dropped


def test_task_log_announced_when_enabled(tmp_path: Path) -> None:
    http = _FakeHttp()
    cfg = SlackConfig(
        enabled=True,
        activity_channel="#c",
        announce_task_log=True,
        bot_token="xoxb",
    )
    listener = _listener(tmp_path, http, config=cfg)
    listener(Event(TASK_STARTED, {"task": "10.a", "title": "Do A"}))
    listener(Event(TASK_LOG, {"task": "10.a", "line": "interesting line\n"}))
    reply = http.method_calls("chat.postMessage")[-1]["json"]
    assert reply.get("thread_ts")
    assert "interesting line" in json.dumps(reply)


# --------------------------------------------------------------------------- #
# Thread reuse + idempotency (spec §4.3)
# --------------------------------------------------------------------------- #


def test_task_started_reuses_existing_thread(tmp_path: Path) -> None:
    http = _FakeHttp()
    store = _store(tmp_path)
    store.set("10.a", ThreadRef(channel="#other", thread_ts="999.123"))
    listener = make_slack_listener(_active_config(), store, client=http)
    listener(Event(TASK_STARTED, {"task": "10.a", "title": "Do A"}))

    # No new parent post — it reused the recorded thread by editing it.
    assert http.method_calls("chat.postMessage") == []
    updates = http.method_calls("chat.update")
    assert len(updates) == 1
    assert updates[0]["json"]["ts"] == "999.123"
    assert updates[0]["json"]["channel"] == "#other"


def test_parent_update_is_idempotent_single_message(tmp_path: Path) -> None:
    http = _FakeHttp()
    store = _store(tmp_path)
    listener = make_slack_listener(_active_config(), store, client=http)
    listener(Event(TASK_STARTED, {"task": "10.a", "title": "Do A"}))
    for phase in ("worker", "validate", "commit"):
        listener(Event(TASK_STATUS, {"task": "10.a", "phase": phase}))
    listener(
        Event(
            TASK_RESULT,
            {"task": "10.a", "status": "completed", "result_line": "done"},
        )
    )

    # Exactly one parent message exists for the slug (one post, then edits).
    assert len(http.method_calls("chat.postMessage")) == 2  # parent + result reply
    ref = store.get("10.a")
    assert ref is not None
    edit_targets = {u["json"]["ts"] for u in http.method_calls("chat.update")}
    assert edit_targets == {ref.thread_ts}


def test_status_after_finalise_does_not_reopen(tmp_path: Path) -> None:
    http = _FakeHttp()
    listener = _listener(tmp_path, http)
    listener(Event(TASK_STARTED, {"task": "10.a", "title": "Do A"}))
    listener(Event(TASK_RESULT, {"task": "10.a", "status": "completed"}))
    edits_before = len(http.method_calls("chat.update"))
    # A late phase event must not overwrite the finalised status.
    listener(Event(TASK_STATUS, {"task": "10.a", "phase": "validate"}))
    assert len(http.method_calls("chat.update")) == edits_before


# --------------------------------------------------------------------------- #
# Best-effort — invariant 3 (never fatal)
# --------------------------------------------------------------------------- #


def test_http_exception_is_swallowed(tmp_path: Path) -> None:
    http = _FakeHttp(fail=True)
    listener = _listener(tmp_path, http)
    # None of these may raise even though every POST throws.
    listener(Event(RUN_STARTED, {"run_id": "r", "tasks": ["10.a"]}))
    listener(Event(TASK_STARTED, {"task": "10.a", "title": "A"}))
    listener(Event(TASK_STATUS, {"task": "10.a", "phase": "validate"}))
    listener(Event(TASK_RESULT, {"task": "10.a", "status": "error", "error": "x"}))
    listener(Event(RUN_FINISHED, {"run_id": "r"}))
    assert http.calls  # it tried, but failures were swallowed


def test_slack_rejection_is_swallowed_and_no_thread_recorded(tmp_path: Path) -> None:
    http = _FakeHttp(reject=True)
    store = _store(tmp_path)
    listener = make_slack_listener(_active_config(), store, client=http)
    listener(Event(TASK_STARTED, {"task": "10.a", "title": "A"}))
    # A rejected post yields no ts ⇒ nothing recorded, but no exception.
    assert store.get("10.a") is None


# --------------------------------------------------------------------------- #
# ThreadStore
# --------------------------------------------------------------------------- #


def test_thread_store_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "slack-threads.json"
    store = ThreadStore(path)
    store.set("10.a", ThreadRef(channel="#c", thread_ts="123.45"))
    # A fresh instance reads it back from disk (survives restart).
    again = ThreadStore(path)
    ref = again.get("10.a")
    assert ref == ThreadRef(channel="#c", thread_ts="123.45")


def test_thread_store_tolerates_missing_and_malformed(tmp_path: Path) -> None:
    assert ThreadStore(tmp_path / "missing.json").get("x") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert ThreadStore(bad).get("x") is None


def test_thread_store_for_queue_path(tmp_path: Path) -> None:
    main = ThreadStore.for_queue(tmp_path, ".tasks")
    assert main.path == tmp_path / ".tasks" / "slack-threads.json"
    pl = ThreadStore.for_queue(tmp_path, ".tasks/experiments")
    assert pl.path == tmp_path / ".tasks/experiments" / "slack-threads.json"
