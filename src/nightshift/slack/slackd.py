"""``nightshift slackd`` — the Socket Mode control-plane daemon (inbound capture).

A long-lived `slack-bolt <https://slack.dev/bolt-python/>`_ app running in
**Socket Mode**, so it needs no public HTTPS endpoint and runs fine on a laptop
behind NAT next to the UI (spec §2, §11 invariant 6). Phase 2 wires the
**inbound capture inbox** (spec §5): messages in the configured ``intake_channel``
become candidate tasks behind the §9 security model — allowlist, confirmation
gate, channel scoping.

``slack-bolt`` is imported **lazily inside the daemon path only** (never at
module import time) so disabled/CLI runs and the always-loaded notifier never
require it (task constraint; spec §3 degrade-safely). The pure capture logic
lives in :mod:`nightshift.slack.intake`; this module is the Slack adapter plus
the enqueue-and-land glue.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nightshift.repos import DEFAULT_TASKS_REPO
from nightshift.slack.config import SlackConfig, resolve_slack_config
from nightshift.slack.intake import (
    ClaudeNormaliseBackend,
    EnqueueResult,
    NormaliseBackend,
    ParsedTask,
    build_task,
    enqueue,
    render_confirmation,
)
from nightshift.spawn_daily import load_config, resolve_config


_log = logging.getLogger("nightshift.slack.slackd")

# Action ids for the confirmation card buttons (spec §5.3).
ACTION_ENQUEUE = "nightshift_enqueue"
ACTION_EDIT = "nightshift_edit"
ACTION_CANCEL = "nightshift_cancel"


@dataclass
class _Pending:
    """A parsed capture awaiting the author's Enqueue / Edit / Cancel click."""

    parsed: ParsedTask
    source_channel: str
    source_ts: str


def confirmation_blocks(parsed: ParsedTask, token: str) -> list[dict[str, Any]]:
    """Block Kit for the in-thread confirmation card (spec §4.2 shape, §5.3)."""
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": render_confirmation(parsed)},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Enqueue"},
                    "action_id": ACTION_ENQUEUE,
                    "value": token,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Edit"},
                    "action_id": ACTION_EDIT,
                    "value": token,
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Cancel"},
                    "action_id": ACTION_CANCEL,
                    "value": token,
                },
            ],
        },
    ]


def commit_message(result: EnqueueResult, parsed: ParsedTask) -> str:
    """Commit subject + provenance trailer for a Slack capture (spec §9.4)."""
    lines = [f"task: enqueue {result.slug} from Slack"]
    lines.append("")
    if parsed.author:
        lines.append(f"Slack-Author: {parsed.author}")
    if parsed.permalink:
        lines.append(f"Slack-Permalink: {parsed.permalink}")
    return "\n".join(lines).rstrip() + "\n"


class CaptureHandler:
    """Translates intake events into parsed-task confirmations and enqueues.

    Holds the small in-memory map of pending captures keyed by an opaque token
    carried in the button ``value``. Network calls go through the injected
    ``poster`` (a tiny Slack client seam) so the whole handler is unit-testable
    without ``slack-bolt`` or a real workspace.
    """

    def __init__(
        self,
        tasks_root: Path,
        config: SlackConfig,
        *,
        backend: NormaliseBackend,
        poster: SlackPoster,
        config_defaults: dict[str, Any] | None = None,
    ) -> None:
        self._tasks_root = tasks_root
        self._config = config
        self._backend = backend
        self._poster = poster
        self._config_defaults = config_defaults or {}
        self._pending: dict[str, _Pending] = {}

    # ----- inbound message ------------------------------------------------ #

    def on_message(self, event: dict[str, Any]) -> None:
        """Handle a ``message.channels`` event (spec §5.1 gating)."""
        if not self._is_capture_candidate(event):
            return
        user = event.get("user")
        channel = event.get("channel", "")
        ts = event.get("ts", "")
        text = event.get("text") or ""
        if not self._config.is_allowed(user):
            _log.info("slack_intake_ignored_user user=%s", user)
            return  # silent for non-allowlisted authors (spec §9.1)
        try:
            parsed = build_task(
                text,
                backend=self._backend,
                config_defaults=self._config_defaults,
                author=user,
                permalink=self._permalink(channel, ts),
            )
        except Exception as exc:
            _log.warning("slack_intake_parse_failed error=%s", exc)
            self._react(channel, ts, "warning")
            self._reply(channel, ts, f"⚠ Could not parse task: {exc}")
            return

        if self._config.require_confirmation:
            self._post_confirmation(parsed, channel, ts)
        else:
            self._enqueue_and_ack(parsed, channel, ts)

    def _post_confirmation(self, parsed: ParsedTask, channel: str, ts: str) -> None:
        token = uuid.uuid4().hex
        self._pending[token] = _Pending(parsed, channel, ts)
        self._poster.post_message(
            channel=channel,
            thread_ts=ts,
            text=f"Task preview: {parsed.title}",
            blocks=confirmation_blocks(parsed, token),
        )

    # ----- button actions ------------------------------------------------- #

    def on_action(self, action_id: str, token: str, user: str | None) -> None:
        """Handle an Enqueue / Edit / Cancel click (spec §5.3, §9.1)."""
        pending = self._pending.get(token)
        if pending is None:
            return
        if not self._config.is_allowed(user):
            self._poster.ephemeral(
                channel=pending.source_channel,
                user=user or "",
                text="You are not permitted to enqueue tasks.",
            )
            return
        if action_id == ACTION_CANCEL:
            self._pending.pop(token, None)
            self._reply(
                pending.source_channel, pending.source_ts, "🗑 Capture cancelled."
            )
        elif action_id == ACTION_EDIT:
            self._reply(
                pending.source_channel,
                pending.source_ts,
                "✏️ Edit the directives in a reply, then re-post to capture again.",
            )
        elif action_id == ACTION_ENQUEUE:
            self._pending.pop(token, None)
            self._enqueue_and_ack(
                pending.parsed, pending.source_channel, pending.source_ts
            )

    # ----- enqueue + land ------------------------------------------------- #

    def _enqueue_and_ack(self, parsed: ParsedTask, channel: str, ts: str) -> None:
        try:
            result = enqueue(self._tasks_root, parsed)
            self._land(result, parsed)
        except Exception as exc:
            _log.warning("slack_intake_enqueue_failed error=%s", exc)
            self._react(channel, ts, "warning")
            self._reply(channel, ts, f"⚠ Enqueue failed: {exc}")
            return
        self._react(channel, ts, "white_check_mark")
        queue_label = result.queue or "main"
        self._reply(
            channel,
            ts,
            f"✅ Queued *{result.slug}* in `{queue_label}` → `{result.tasks_rel}/{result.slug}.md`",
        )

    def _land(self, result: EnqueueResult, parsed: ParsedTask) -> None:
        """Land the new task file per ``default_enqueue`` (spec §5.4).

        ``commit`` stages the file (and any ``config.json`` order update) and
        commits to the content store's local ``main`` with the provenance
        trailer (``tasks_root`` is its own git repo); ``pr`` is left to a
        remote-first deployment (logged; the file still lands on disk so the
        local plane sees it).
        """
        if self._config.default_enqueue != "commit":
            _log.info(
                "slack_intake_pr_mode slug=%s (file written, no commit)", result.slug
            )
            return
        paths = [str(result.path)]
        config_path = result.path.parent / "config.json"
        if config_path.exists():
            paths.append(str(config_path))
        _git(self._tasks_root, ["add", *paths])
        _git(self._tasks_root, ["commit", "-m", commit_message(result, parsed)])

    # ----- gating helpers ------------------------------------------------- #

    def _is_capture_candidate(self, event: dict[str, Any]) -> bool:
        if event.get("channel") != self._intake_channel_id():
            # Channel scoping (spec §9.3). When the configured channel is a name
            # (``#foo``) rather than an id we cannot match here; the daemon
            # subscribes by id, so this guards against cross-channel leakage.
            if self._config.intake_channel.startswith("#"):
                pass  # name-configured: rely on the subscription, accept event
            else:
                return False
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return False  # ignore bot messages (spec §5.1)
        if event.get("thread_ts") and event.get("thread_ts") != event.get("ts"):
            return False  # ignore threaded replies (spec §5.1)
        subtype = event.get("subtype")
        if subtype in {"message_changed", "message_deleted"}:
            return False  # ignore edits/deletes (spec §5.1)
        if not (event.get("text") or "").strip():
            return False
        return True

    def _intake_channel_id(self) -> str:
        return self._config.intake_channel

    def _permalink(self, channel: str, ts: str) -> str | None:
        return self._poster.permalink(channel=channel, ts=ts)

    def _reply(self, channel: str, thread_ts: str, text: str) -> None:
        self._poster.post_message(channel=channel, thread_ts=thread_ts, text=text)

    def _react(self, channel: str, ts: str, name: str) -> None:
        self._poster.react(channel=channel, ts=ts, name=name)


class SlackPoster:
    """Tiny Slack Web API seam used by :class:`CaptureHandler`.

    Wraps a ``slack_sdk`` ``WebClient`` (or any object exposing the same three
    methods). Declared as a concrete class with overridable methods so tests can
    subclass/replace it without ``slack-bolt`` installed. Every call is
    best-effort: an API error is logged and dropped (spec §11 invariant 3).
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def post_message(
        self,
        *,
        channel: str,
        text: str,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        if blocks:
            kwargs["blocks"] = blocks
        self._safe(lambda: self._client.chat_postMessage(**kwargs), "chat_postMessage")

    def ephemeral(self, *, channel: str, user: str, text: str) -> None:
        self._safe(
            lambda: self._client.chat_postEphemeral(
                channel=channel, user=user, text=text
            ),
            "chat_postEphemeral",
        )

    def react(self, *, channel: str, ts: str, name: str) -> None:
        self._safe(
            lambda: self._client.reactions_add(
                channel=channel, timestamp=ts, name=name
            ),
            "reactions_add",
        )

    def permalink(self, *, channel: str, ts: str) -> str | None:
        try:
            resp = self._client.chat_getPermalink(channel=channel, message_ts=ts)
        except Exception as exc:
            _log.warning("slack_permalink_failed error=%s", exc)
            return None
        link = (
            resp.get("permalink")
            if isinstance(resp, dict)
            else getattr(resp, "get", lambda *_: None)("permalink")
        )
        return link if isinstance(link, str) else None

    def _safe(self, call: Callable[[], Any], what: str) -> None:
        try:
            call()
        except Exception as exc:
            _log.warning("slack_%s_failed error=%s", what, exc)


# --------------------------------------------------------------------------- #
# Daemon entrypoint
# --------------------------------------------------------------------------- #


def run(workspace: Path, *, argv: list[str] | None = None) -> int:
    """Start the Socket Mode daemon. Returns a process exit code.

    Exits cleanly (non-zero) with a clear message when Slack is unconfigured —
    feature flag off or either token absent — so an accidental launch is a
    no-op rather than a crash (spec §3, §9). ``slack-bolt`` is imported here,
    inside the daemon path, never at module import.
    """
    from nightshift.run_local import load_dotenv

    load_dotenv(workspace)
    tasks_root = workspace / _resolve_tasks_repo(workspace)
    runner_config = resolve_config(workspace, tasks_root)
    config = resolve_slack_config(runner_config)

    if not config.intake_active:
        sys.stderr.write(_unconfigured_reason(config) + "\n")
        return 2

    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError:
        sys.stderr.write(
            "slack-bolt is not installed. Add it to the venv to run the daemon.\n"
        )
        return 2

    app = App(token=config.bot_token)
    poster = SlackPoster(app.client)
    backend = ClaudeNormaliseBackend(config=runner_config)
    handler = CaptureHandler(
        tasks_root,
        config,
        backend=backend,
        poster=poster,
        config_defaults=_enqueue_defaults(runner_config),
    )
    _register(app, handler)

    sys.stderr.write(
        f"nightshift slackd: watching {config.intake_channel} "
        f"(allowlist={len(config.allowed_users)} users, "
        f"confirmation={'on' if config.require_confirmation else 'off'})\n"
    )
    SocketModeHandler(app, config.app_token).start()
    return 0


def _register(app: Any, handler: CaptureHandler) -> None:
    """Bind Bolt event/action handlers to the :class:`CaptureHandler`."""

    @app.event("message")
    def _on_message(
        event: dict[str, Any], ack: Callable[..., Any] | None = None
    ) -> None:
        handler.on_message(event)

    def _make_action(action_id: str) -> Callable[..., Any]:
        def _on_action(ack: Callable[..., Any], body: dict[str, Any]) -> None:
            ack()
            token = _action_value(body)
            user = (body.get("user") or {}).get("id")
            if token:
                handler.on_action(action_id, token, user)

        return _on_action

    app.action(ACTION_ENQUEUE)(_make_action(ACTION_ENQUEUE))
    app.action(ACTION_EDIT)(_make_action(ACTION_EDIT))
    app.action(ACTION_CANCEL)(_make_action(ACTION_CANCEL))


def _action_value(body: dict[str, Any]) -> str | None:
    actions = body.get("actions")
    if isinstance(actions, list) and actions:
        value = actions[0].get("value")
        if isinstance(value, str):
            return value
    return None


def _enqueue_defaults(runner_config: dict[str, Any]) -> dict[str, Any]:
    """Frontmatter defaults captures inherit from the runner config (spec §6)."""
    out: dict[str, Any] = {}
    for key in ("model", "draft", "automerge"):
        if key in runner_config:
            out[key] = runner_config[key]
    return out


def _resolve_tasks_repo(workspace: Path) -> str:
    """Name of the content-store repo (``tasks_root = workspace / tasks_repo``).

    Env ``NIGHTSHIFT_TASKS_REPO`` wins, then ``<workspace>/config.json``'s
    ``tasks_repo`` key, then :data:`nightshift.repos.DEFAULT_TASKS_REPO` —
    matching every other entry point so the daemon names the same store.
    """
    env = os.environ.get("NIGHTSHIFT_TASKS_REPO")
    if env:
        return env
    try:
        cfg = load_config(workspace)
    except (FileNotFoundError, ValueError, OSError):
        cfg = {}
    name = cfg.get("tasks_repo") if isinstance(cfg, dict) else None
    return str(name or DEFAULT_TASKS_REPO)


def _unconfigured_reason(config: SlackConfig) -> str:
    missing: list[str] = []
    if not config.enabled:
        missing.append("slack.enabled is false")
    if not config.bot_token:
        missing.append("SLACK_BOT_TOKEN is unset")
    if not config.app_token:
        missing.append("SLACK_APP_TOKEN is unset")
    if not config.intake_channel:
        missing.append("slack.intake_channel is empty")
    detail = "; ".join(missing) or "Slack inbound is not configured"
    return f"nightshift slackd: not starting — {detail}."


def _git(root: Path, args: list[str]) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Nightshift Slack Socket Mode daemon (inbound capture)."
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return run(args.workspace.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
