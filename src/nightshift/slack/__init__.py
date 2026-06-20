"""Slack integration for nightshift.

Phase 1 ships the **outbound** half: a best-effort activity-feed notifier
(:mod:`nightshift.slack.notify`) that maps the engine event stream onto one
threaded card per task, plus a small slug→thread store
(:mod:`nightshift.slack.threads`) so a task keeps a single thread across
restarts and planes.

Phase 2 adds the **inbound** half: the capture inbox
(:mod:`nightshift.slack.intake`) turns a free-form intake-channel message into a
queued brief in the content store (``<tasks_root>/<queue>/<slug>.md``), driven by the Socket Mode daemon
(:mod:`nightshift.slack.slackd`). The daemon imports ``slack-bolt`` lazily, so
disabled/CLI runs and the outbound notifier never require it.

Everything degrades to a no-op when Slack is unconfigured (``slack.enabled``
false or a token absent); attaching the notifier never changes run behaviour
and the daemon exits cleanly in that case.
"""

from __future__ import annotations

from nightshift.slack.config import SlackConfig, resolve_slack_config
from nightshift.slack.intake import (
    EnqueueResult,
    ParsedTask,
    build_task,
    enqueue,
    parse_directives,
    render_confirmation,
    render_task_markdown,
)
from nightshift.slack.notify import listener_for_queue, make_slack_listener
from nightshift.slack.threads import ThreadStore


__all__ = [
    "EnqueueResult",
    "ParsedTask",
    "SlackConfig",
    "ThreadStore",
    "build_task",
    "enqueue",
    "listener_for_queue",
    "make_slack_listener",
    "parse_directives",
    "render_confirmation",
    "render_task_markdown",
    "resolve_slack_config",
]
