"""Outbound Slack notifier — threaded activity cards.

Maps the engine :class:`~nightshift.events.Event` stream onto Slack: one
**parent card per task**, edited in place as the task moves worker → validate →
commit → resolve, finalised on the terminal result with a terse threaded reply;
plus a **run summary** card edited from start to finish (spec §4).

Design constraints (spec §4.3, §11):

- **Best-effort, never fatal.** Every Slack/HTTP error is logged and dropped;
  the listener never raises into the run.
- **Unconfigured = invisible.** When :meth:`SlackConfig.active` is false the
  listener short-circuits before any work — byte-for-byte identical runs.
- **One thread per task.** A slug reuses an existing ``thread_ts`` from the
  store (intake/remote may have created it) instead of posting a new parent.

Posting uses ``chat.postMessage`` / ``chat.update`` over ``httpx`` (already a
dep); no ``slack-bolt`` is pulled in for outbound, and no new required
dependency is introduced — outbound works with only ``httpx`` + a bot token.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from nightshift.events import (
    RUN_FINISHED,
    RUN_STARTED,
    TASK_LOG,
    TASK_RESULT,
    TASK_STARTED,
    TASK_STATUS,
    Event,
    Listener,
)
from nightshift.slack.config import SlackConfig, resolve_slack_config
from nightshift.slack.threads import ThreadRef, ThreadStore


_log = logging.getLogger("nightshift.slack.notify")

DEFAULT_BASE_URL = "https://slack.com"

# Phase status line (engine ``phase`` values, spec §4.1).
_PHASE_LINE = {
    "worker": "🤖 worker",
    "validate": "🧪 validate",
    "commit": "💾 commit",
    "resolve": "🩹 resolve",
}

# Terminal status → finalised parent status line (spec §4.1). Keys are the
# terminal statuses defined in events.py (``completed``/``error``/``skipped``/
# ``stopped``/``aborted``).
_TERMINAL_LINE = {
    "completed": "✅ landed",
    "error": "❌ failed",
    "skipped": "⏭ skipped",
    "stopped": "🛑 stopped",
    "aborted": "⚠ aborted",
}


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


class HttpClient(Protocol):
    """Minimal synchronous POST client (``httpx.Client``-shaped).

    Declared as a Protocol so tests can inject a fake without a real network.
    """

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
    ) -> HttpResponse: ...


def _make_httpx_client() -> HttpClient:
    import httpx

    return httpx.Client(timeout=10.0)


@dataclass
class _TaskCard:
    """In-memory mirror of one task's parent card."""

    title: str
    thread_ts: str | None = None
    status_line: str = "▶ running"
    model: str | None = None
    finalised: bool = False


@dataclass
class _RunSummary:
    ts: str | None = None
    queue: str | None = None
    total: int = 0


class SlackNotifier:
    """Stateful, best-effort event→card mapper for one notifier instance.

    Holds the run-summary message ``ts`` and a per-slug card mirror so updates
    edit in place. Safe to attach to several runs; thread persistence is
    serialised by :class:`ThreadStore`.
    """

    def __init__(
        self,
        config: SlackConfig,
        store: ThreadStore,
        *,
        client: HttpClient | None = None,
        base_url: str = DEFAULT_BASE_URL,
        queue: str | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._base_url = base_url.rstrip("/")
        self._queue = queue
        self._cards: dict[str, _TaskCard] = {}
        self._run = _RunSummary(queue=queue)
        self._client = client
        self._client_failed = False

    # ----- public listener entrypoint ---------------------------------- #

    def __call__(self, event: Event) -> None:
        """Dispatch one event. Never raises: every failure is swallowed."""
        if not self._config.active:
            return
        try:
            self._dispatch(event)
        except Exception as exc:  # noqa: BLE001 — best-effort, never fatal
            _log.warning("slack_notify_failed type=%s error=%s", event.type, exc)

    def _dispatch(self, event: Event) -> None:
        handler = {
            RUN_STARTED: self._on_run_started,
            TASK_STARTED: self._on_task_started,
            TASK_STATUS: self._on_task_status,
            TASK_RESULT: self._on_task_result,
            RUN_FINISHED: self._on_run_finished,
            TASK_LOG: self._on_task_log,
        }.get(event.type)
        if handler is not None:
            handler(event.payload)

    # ----- event handlers ---------------------------------------------- #

    def _on_run_started(self, payload: dict[str, Any]) -> None:
        tasks = payload.get("tasks") or []
        self._run = _RunSummary(queue=self._queue, total=len(tasks))
        ts = self._post(
            self._channel(),
            text=self._run_summary_text(),
            blocks=_run_summary_blocks(self._run, "started"),
        )
        if ts:
            self._run.ts = ts

    def _on_task_started(self, payload: dict[str, Any]) -> None:
        slug = payload.get("task")
        if not slug:
            return
        title = payload.get("title") or slug
        frontmatter = payload.get("frontmatter") or {}
        model = frontmatter.get("model") if isinstance(frontmatter, dict) else None
        card = self._cards.get(slug)
        if card is None:
            card = _TaskCard(title=title, model=model)
            self._cards[slug] = card
        else:
            card.title = title
            card.model = model or card.model
        card.status_line = "▶ running"
        card.finalised = False

        # Reuse a thread the slug already owns (intake/remote may have made it),
        # else post a fresh parent and record it.
        ref = self._store.get(slug)
        if ref is not None:
            card.thread_ts = ref.thread_ts
            self._update(ref.channel, ref.thread_ts, card)
            return
        ts = self._post(
            self._channel(),
            text=f"{card.status_line} · {card.title}",
            blocks=_task_card_blocks(card, self._queue),
        )
        if ts:
            card.thread_ts = ts
            self._store.set(slug, ThreadRef(channel=self._channel(), thread_ts=ts))

    def _on_task_status(self, payload: dict[str, Any]) -> None:
        slug = payload.get("task")
        if not slug:
            return
        card = self._cards.get(slug)
        if card is None or card.finalised:
            return
        phase = payload.get("phase")
        line = _PHASE_LINE.get(phase) if phase else None
        if line is None:
            return
        card.status_line = line
        self._edit_parent(slug, card)

    def _on_task_result(self, payload: dict[str, Any]) -> None:
        slug = payload.get("task")
        if not slug:
            return
        card = self._cards.get(slug)
        if card is None:
            card = _TaskCard(title=slug)
            self._cards[slug] = card
        status = payload.get("status") or "error"
        commit_sha = payload.get("commit_sha")
        line = _TERMINAL_LINE.get(status, f"• {status}")
        if status == "completed" and commit_sha:
            line = f"{line} · `{_short_sha(commit_sha)}`"
        card.status_line = line
        card.finalised = True
        self._edit_parent(slug, card)
        self._post_result_reply(slug, card, payload)

    def _on_run_finished(self, payload: dict[str, Any]) -> None:
        if self._run.ts is None:
            return
        landed = sum(1 for c in self._cards.values() if c.status_line.startswith("✅"))
        failed = sum(1 for c in self._cards.values() if c.status_line.startswith("❌"))
        skipped = sum(
            1
            for c in self._cards.values()
            if c.status_line.startswith(("⏭", "🛑", "⚠"))
        )
        self._update(
            self._channel(),
            self._run.ts,
            text=self._run_summary_text(),
            blocks=_run_summary_blocks(
                self._run, "finished", landed=landed, failed=failed, skipped=skipped
            ),
        )

    def _on_task_log(self, payload: dict[str, Any]) -> None:
        if not self._config.announce_task_log:
            return
        slug = payload.get("task")
        card = self._cards.get(slug) if slug else None
        if card is None or card.thread_ts is None:
            return
        line = (payload.get("line") or "").strip()
        if not line:
            return
        self._post(
            self._channel(),
            text=_truncate(line, 280),
            thread_ts=card.thread_ts,
        )

    # ----- card helpers ------------------------------------------------- #

    def _edit_parent(self, slug: str, card: _TaskCard) -> None:
        ref = self._store.get(slug)
        thread_ts = card.thread_ts or (ref.thread_ts if ref else None)
        channel = ref.channel if ref else self._channel()
        if thread_ts is None:
            # No parent to edit — fall back to a fresh post (spec §4.3 rule 2).
            ts = self._post(
                channel,
                text=f"{card.status_line} · {card.title}",
                blocks=_task_card_blocks(card, self._queue),
            )
            if ts:
                card.thread_ts = ts
                self._store.set(slug, ThreadRef(channel=channel, thread_ts=ts))
            return
        card.thread_ts = thread_ts
        self._update(channel, thread_ts, card)

    def _update(
        self,
        channel: str,
        ts: str,
        card: _TaskCard | None = None,
        *,
        text: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        if card is not None:
            text = f"{card.status_line} · {card.title}"
            blocks = _task_card_blocks(card, self._queue)
        payload = {"channel": channel, "ts": ts, "text": text or ""}
        if blocks is not None:
            payload["blocks"] = blocks
        self._call("chat.update", payload)

    def _post_result_reply(
        self, slug: str, card: _TaskCard, payload: dict[str, Any]
    ) -> None:
        if card.thread_ts is None:
            return
        lines: list[str] = []
        result_line = (payload.get("result_line") or "").strip()
        if result_line:
            lines.append(result_line)
        failure_kind = payload.get("failure_kind")
        if failure_kind:
            lines.append(f"*kind:* `{failure_kind}`")
        error = (payload.get("error") or "").strip()
        if error:
            lines.append(_truncate(error, 500))
        commit_sha = payload.get("commit_sha")
        if commit_sha:
            lines.append(f"*commit:* `{_short_sha(commit_sha)}`")
        if not lines:
            return
        body = f"{card.status_line}\n" + "\n".join(lines)
        self._post(self._channel(), text=body, thread_ts=card.thread_ts)

    # ----- transport ---------------------------------------------------- #

    def _channel(self) -> str:
        return self._config.activity_channel

    def _run_summary_text(self) -> str:
        return f"Nightshift run · {self._run.total} tasks"

    def _post(
        self,
        channel: str,
        *,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
    ) -> str | None:
        payload: dict[str, Any] = {"channel": channel, "text": text}
        if blocks is not None:
            payload["blocks"] = blocks
        if thread_ts is not None:
            payload["thread_ts"] = thread_ts
        return self._call("chat.postMessage", payload)

    def _call(self, method: str, payload: dict[str, Any]) -> str | None:
        """POST to a Slack Web API method. Returns the message ``ts`` on success,
        else ``None``. Any error is logged and swallowed (best-effort)."""
        client = self._http()
        if client is None:
            return None
        url = f"{self._base_url}/api/{method}"
        headers = {
            "Authorization": f"Bearer {self._config.bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        try:
            resp = client.post(url, headers=headers, json=payload)
        except Exception as exc:  # noqa: BLE001 — HTTP/transport errors are non-fatal
            _log.warning("slack_%s_failed error=%s", method, exc)
            return None
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}
        if resp.status_code != 200 or not (isinstance(body, dict) and body.get("ok")):
            err = body.get("error") if isinstance(body, dict) else None
            _log.warning(
                "slack_%s_rejected status=%s error=%s", method, resp.status_code, err
            )
            return None
        ts = body.get("ts")
        return ts if isinstance(ts, str) else None

    def _http(self) -> HttpClient | None:
        if self._client is not None:
            return self._client
        if self._client_failed:
            return None
        try:
            self._client = _make_httpx_client()
        except Exception as exc:  # noqa: BLE001 — missing dep ⇒ stay a no-op
            _log.warning("slack_http_client_unavailable error=%s", exc)
            self._client_failed = True
            return None
        return self._client


# --------------------------------------------------------------------------- #
# Block Kit builders (built like services/dispatcher/receivers.py:build_block_kit)
# --------------------------------------------------------------------------- #


def _task_card_blocks(card: _TaskCard, queue: str | None) -> list[dict[str, Any]]:
    queue_label = queue or "main"
    context_bits = [f"queue `{queue_label}`"]
    if card.model:
        context_bits.append(f"model `{card.model}`")
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{card.title}*\n{card.status_line}",
            },
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": " · ".join(context_bits)}],
        },
    ]


def _run_summary_blocks(
    run: _RunSummary,
    state: str,
    *,
    landed: int = 0,
    failed: int = 0,
    skipped: int = 0,
) -> list[dict[str, Any]]:
    queue_label = run.queue or "main"
    if state == "finished":
        headline = (
            f"*Run finished* · {landed} landed · {failed} failed · {skipped} skipped"
        )
    else:
        headline = f"*Run started* · {run.total} tasks"
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": headline},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"queue `{queue_label}`"}],
        },
    ]


def _short_sha(sha: str) -> str:
    return sha[:7] if sha else sha


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def make_slack_listener(
    config: SlackConfig,
    store: ThreadStore,
    *,
    client: HttpClient | None = None,
    base_url: str = DEFAULT_BASE_URL,
    queue: str | None = None,
) -> Listener:
    """Build the outbound Slack listener.

    When ``config.active`` is false the returned callable still works but
    short-circuits on the first line of every call — a cheap no-op. ``store``
    persists slug→thread reuse; ``client`` is injectable for tests.
    """
    return SlackNotifier(
        config, store, client=client, base_url=base_url, queue=queue
    )


def listener_for_queue(
    root: Path,
    *,
    tasks_rel: str = ".tasks",
    queue: str | None = None,
) -> Listener:
    """Convenience wiring used by ``run_local.py`` / ``server/player.py``.

    Resolves the layered ``slack`` config for the queue and builds a notifier
    backed by the queue's thread store. The result is always a valid listener;
    when Slack is unconfigured it short-circuits to a no-op (so callers can
    attach it unconditionally without changing run behaviour). Never raises:
    any setup error degrades to a disabled no-op.
    """
    try:
        from nightshift.spawn_daily import resolve_config

        runner_config = resolve_config(root, tasks_rel)
    except Exception as exc:  # noqa: BLE001 — config problems must not break wiring
        _log.warning("slack_config_resolve_failed error=%s", exc)
        runner_config = {}
    config = resolve_slack_config(runner_config)
    store = ThreadStore.for_queue(root, tasks_rel)
    return make_slack_listener(config, store, queue=queue)
