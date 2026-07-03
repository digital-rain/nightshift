"""Resolved Slack configuration for the outbound notifier and inbound inbox.

The ``slack`` block lives in the layered runner config (operator
``<workspace>/config.json`` defaults, overridable by the content store's
``<tasks_root>/config.json`` and a queue's ``config.json`` via
:func:`spawn_daily.resolve_config`). When no ``slack`` block is present — the
default ship state — the notifier is disabled and every hook is a cheap no-op
(spec §11 invariant 1).

The tokens are *secrets*, never config: the bot token is read from
``SLACK_BOT_TOKEN`` and the Socket Mode app token from ``SLACK_APP_TOKEN``
(both loaded from ``.env`` by ``config.io.load_dotenv``). Absent token ⇒ that
direction is disabled, regardless of the ``enabled`` flag.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


# Shipped defaults for the ``slack`` config block (spec §3). Kept here rather
# than in ``tools/nightshift/config.json`` so the notifier has a sane disabled
# default even where the config file carries no ``slack`` key at all.
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "activity_channel": "#nightshift-activity",
    "intake_channel": "#nightshift-intake",
    "allowed_users": [],
    "require_confirmation": True,
    "announce_task_log": False,
    "default_enqueue": "commit",
}

BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"
APP_TOKEN_ENV = "SLACK_APP_TOKEN"


@dataclass(frozen=True)
class SlackConfig:
    """The subset of Slack settings the notifier and inbox need."""

    enabled: bool
    activity_channel: str
    announce_task_log: bool
    bot_token: str | None
    intake_channel: str = ""
    allowed_users: tuple[str, ...] = field(default_factory=tuple)
    require_confirmation: bool = True
    default_enqueue: str = "commit"
    app_token: str | None = None

    @property
    def active(self) -> bool:
        """True only when the notifier should actually talk to Slack.

        Requires the feature flag *and* a bot token *and* a target channel. Any
        missing piece collapses to a no-op so an unconfigured (or half-configured)
        deployment never changes run behaviour or errors mid-run.
        """
        return bool(self.enabled and self.bot_token and self.activity_channel)

    @property
    def intake_active(self) -> bool:
        """True only when the inbound capture daemon can run.

        Socket Mode requires the feature flag, *both* tokens (bot for Web API
        calls, app for the Socket Mode connection), and an intake channel to
        watch. Any missing piece means the daemon should exit cleanly rather
        than half-start (spec §3, §9 channel scoping).
        """
        return bool(
            self.enabled and self.bot_token and self.app_token and self.intake_channel
        )

    def is_allowed(self, user_id: str | None) -> bool:
        """Whether ``user_id`` may enqueue (spec §9 allowlist).

        An empty allowlist permits nobody — the safe default for an autonomous
        code-writing agent.
        """
        return bool(user_id) and user_id in self.allowed_users


def resolve_slack_config(
    runner_config: dict[str, Any] | None,
    *,
    env: dict[str, str] | None = None,
) -> SlackConfig:
    """Build a :class:`SlackConfig` from the resolved runner config + env.

    ``runner_config`` is the layered config dict (e.g. from
    ``spawn_daily.resolve_config``); its ``slack`` sub-dict overrides
    :data:`DEFAULTS`. ``env`` defaults to ``os.environ`` and supplies the secret
    tokens. Tolerant of ``None``/malformed input — any problem yields a
    disabled config rather than raising.
    """
    block = {}
    if isinstance(runner_config, dict):
        raw = runner_config.get("slack")
        if isinstance(raw, dict):
            block = raw
    merged = {**DEFAULTS, **block}
    environ = os.environ if env is None else env
    bot_token = environ.get(BOT_TOKEN_ENV) or None
    app_token = environ.get(APP_TOKEN_ENV) or None
    raw_users = merged.get("allowed_users") or []
    allowed_users = (
        tuple(str(u) for u in raw_users if isinstance(u, str) and u)
        if isinstance(raw_users, (list, tuple))
        else ()
    )
    enqueue = str(merged.get("default_enqueue") or "commit").lower()
    if enqueue not in {"commit", "pr"}:
        enqueue = "commit"
    return SlackConfig(
        enabled=bool(merged.get("enabled", False)),
        activity_channel=str(merged.get("activity_channel") or ""),
        announce_task_log=bool(merged.get("announce_task_log", False)),
        bot_token=bot_token,
        intake_channel=str(merged.get("intake_channel") or ""),
        allowed_users=allowed_users,
        require_confirmation=bool(merged.get("require_confirmation", True)),
        default_enqueue=enqueue,
        app_token=app_token,
    )
