"""Inbound capture inbox — turn a Slack message into a queued task.

This module is the pure, network-free core of the inbound half (spec §5, §6).
The Socket Mode daemon (:mod:`nightshift.slack.slackd`) owns the Slack wiring
and calls in here to:

1. parse the optional power-user directives out of free-form text
   (:func:`parse_directives`, spec §6);
2. normalise the message into a structured task — a concise ``title`` plus a
   cleaned body — via a pluggable :class:`NormaliseBackend` (Claude in
   production, a fake in tests). A pasted ``---`` frontmatter block bypasses the
   LLM entirely and is honoured verbatim (:func:`build_task`);
3. render a confirmation preview (:func:`render_confirmation`);
4. materialise the file through the existing ``render_task.py`` /
   ``templates/task.md`` path under the right queue, updating the queue's
   ``config.json`` ``order`` (:func:`enqueue`).

Nothing here imports ``slack-bolt`` or touches the network, so the directive
parser, the normalise→frontmatter mapping, and the enqueue writer are all unit
testable with a faked backend against a temp repo.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from nightshift import playlists
from nightshift.spawn_daily import split_frontmatter


# Model aliases for the ``#opus`` / ``#sonnet`` directives (spec §6). These are
# the canonical Claude ids used across the runner config.
MODEL_ALIASES = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
}

# The frontmatter keys the engine + templates/task.md understand, in render
# order (spec §6). ``after`` is a legacy header but rendered inside the block.
_FRONTMATTER_ORDER = (
    "title",
    "model",
    "draft",
    "automerge",
    "split",
    "loc",
    "turns",
    "after",
)

BODY_MARKER = "Task description goes here."


class NormaliseBackend(Protocol):
    """Turns free-form prose into a ``(title, body)`` pair.

    Production uses a Claude-backed implementation; tests inject a fake. The
    backend never sees directives — they are stripped first by
    :func:`parse_directives` — so it only has to produce a concise title and a
    cleaned description.
    """

    def normalise(self, text: str) -> tuple[str, str]: ...


@dataclass
class ParsedTask:
    """A capture message resolved into a materialisable task."""

    title: str
    body: str
    frontmatter: dict[str, object]
    queue: str | None = None
    now: bool = False
    # Provenance (spec §9.4): recorded in the commit message / PR body.
    author: str | None = None
    permalink: str | None = None

    @property
    def slug(self) -> str:
        return slugify_title(self.title)


@dataclass
class _Directives:
    overrides: dict[str, object] = field(default_factory=dict)
    queue: str | None = None
    now: bool = False


# --------------------------------------------------------------------------- #
# Directive parsing (spec §6)
# --------------------------------------------------------------------------- #

# ``key: value`` directives. Matched case-insensitively; value runs to EOL or
# the next ``#hashtag`` / ``key:`` token on the same line is left in place via a
# token-wise scan rather than a greedy regex.
_KV_KEYS = {"model", "queue", "loc", "turns", "after"}
_HASH_RE = re.compile(r"(?<!\w)#([a-z0-9][a-z0-9-]*)", re.IGNORECASE)
_KV_TOKEN_RE = re.compile(r"(?<!\w)([a-z]+):\s*(\S+)", re.IGNORECASE)


def parse_directives(text: str) -> tuple[str, _Directives]:
    """Strip and interpret the optional directives from ``text`` (spec §6).

    Returns the cleaned text (directives removed) and a :class:`_Directives`
    capturing the frontmatter overrides plus queue routing and the ``#now``
    flag. Directives are order-independent and may appear anywhere in the body.
    """
    directives = _Directives()
    cleaned = text

    def _take_kv(match: re.Match[str]) -> str:
        key = match.group(1).lower()
        value = match.group(2)
        if key not in _KV_KEYS:
            return match.group(0)
        if key == "queue":
            directives.queue = _slug_or_none(value)
        elif key == "model":
            directives.overrides["model"] = value
        elif key in {"loc", "turns"}:
            if value.isdigit():
                directives.overrides[key] = int(value)
        elif key == "after":
            directives.overrides["after"] = value
        return " "

    cleaned = _KV_TOKEN_RE.sub(_take_kv, cleaned)

    def _take_hash(match: re.Match[str]) -> str:
        tag = match.group(1).lower()
        if tag == "draft":
            directives.overrides["draft"] = True
        elif tag == "automerge":
            directives.overrides["automerge"] = True
        elif tag == "split":
            directives.overrides["split"] = True
        elif tag == "now":
            directives.now = True
        elif tag in MODEL_ALIASES:
            directives.overrides["model"] = MODEL_ALIASES[tag]
        elif tag.startswith("q-") and len(tag) > 2:
            directives.queue = _slug_or_none(tag[2:])
        else:
            return match.group(0)  # leave unknown hashtags in the body
        return " "

    cleaned = _HASH_RE.sub(_take_hash, cleaned)
    cleaned = _tidy(cleaned)
    return cleaned, directives


def _slug_or_none(name: str) -> str | None:
    slug = playlists.slugify_name(name)
    return slug or None


def _tidy(text: str) -> str:
    """Collapse the whitespace left behind by removed directives."""
    lines = [re.sub(r"[ \t]{2,}", " ", ln).rstrip() for ln in text.splitlines()]
    joined = "\n".join(lines)
    joined = re.sub(r"\n{3,}", "\n\n", joined)  # collapse blank-line runs
    return joined.strip()


# --------------------------------------------------------------------------- #
# Normalise → task (spec §5.2, §6)
# --------------------------------------------------------------------------- #


def build_task(
    text: str,
    *,
    backend: NormaliseBackend,
    config_defaults: dict[str, object] | None = None,
    author: str | None = None,
    permalink: str | None = None,
) -> ParsedTask:
    """Resolve a raw intake message into a :class:`ParsedTask`.

    A pasted ``---`` frontmatter block bypasses the LLM and is honoured verbatim
    (spec §5.2). Otherwise directives are stripped, the remainder is normalised
    by ``backend`` into a title + clean body, and the directives are folded into
    frontmatter on top of the config-inherited defaults.
    """
    config_defaults = config_defaults or {}
    if text.lstrip().startswith("---"):
        return _build_from_frontmatter(
            text, config_defaults, author=author, permalink=permalink
        )

    cleaned, directives = parse_directives(text)
    title, body = backend.normalise(cleaned)
    title = (title or "").strip() or _fallback_title(cleaned)
    body = (body or "").strip() or cleaned

    frontmatter = _merge_frontmatter(config_defaults, directives.overrides, title)
    return ParsedTask(
        title=title,
        body=body,
        frontmatter=frontmatter,
        queue=directives.queue,
        now=directives.now,
        author=author,
        permalink=permalink,
    )


def _build_from_frontmatter(
    text: str,
    config_defaults: dict[str, object],
    *,
    author: str | None,
    permalink: str | None,
) -> ParsedTask:
    meta, body = split_frontmatter(text.lstrip())
    title = str(meta.get("title") or "").strip() or _fallback_title(body)
    # A verbatim block still inherits unset defaults so the file is complete,
    # but every key the author wrote wins.
    frontmatter = _merge_frontmatter(config_defaults, dict(meta), title)
    return ParsedTask(
        title=title,
        body=body.strip(),
        frontmatter=frontmatter,
        author=author,
        permalink=permalink,
    )


def _merge_frontmatter(
    config_defaults: dict[str, object],
    overrides: dict[str, object],
    title: str,
) -> dict[str, object]:
    """Layer overrides onto the config-inherited defaults (spec §6 mapping)."""
    out: dict[str, object] = {}
    for key in ("model", "draft", "automerge"):
        if key in config_defaults:
            out[key] = config_defaults[key]
    out.update(overrides)
    out["title"] = title
    # Keep render order stable + drop a None-valued title key from overrides.
    ordered = {k: out[k] for k in _FRONTMATTER_ORDER if k in out and out[k] is not None}
    return ordered


def _fallback_title(text: str) -> str:
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    first = first.lstrip("#").strip()
    return (first[:80] or "Untitled task").strip()


def slugify_title(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return slug or "untitled-task"


# --------------------------------------------------------------------------- #
# Confirmation preview (spec §5.3)
# --------------------------------------------------------------------------- #


def render_task_markdown(parsed: ParsedTask) -> str:
    """The full ``<queue>/<slug>.md`` text — frontmatter block + body."""
    lines = ["---"]
    for key in _FRONTMATTER_ORDER:
        if key in parsed.frontmatter:
            lines.append(f"{key}: {_yaml_scalar(parsed.frontmatter[key])}")
    lines.append("---")
    lines.append("")
    lines.append(parsed.body.strip())
    return "\n".join(lines) + "\n"


def render_confirmation(parsed: ParsedTask, *, body_preview: int = 600) -> str:
    """A compact preview of the rendered task for the in-thread reply."""
    queue_label = parsed.queue or "main"
    body = parsed.body.strip()
    if len(body) > body_preview:
        body = body[: body_preview - 1].rstrip() + "…"
    fm_lines = [
        f"{key}: {_yaml_scalar(parsed.frontmatter[key])}"
        for key in _FRONTMATTER_ORDER
        if key in parsed.frontmatter
    ]
    placement = f"queue `{queue_label}`"
    if parsed.now:
        placement += " · *#now* (front of queue)"
    return (
        f"*{parsed.title}*\n"
        f"{placement}\n"
        "```\n---\n" + "\n".join(fm_lines) + "\n---\n```\n"
        f"{body}"
    )


def _yaml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# --------------------------------------------------------------------------- #
# Enqueue writer (spec §5.4)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EnqueueResult:
    slug: str
    path: Path
    tasks_rel: str
    queue: str | None


def enqueue(tasks_root: Path, parsed: ParsedTask) -> EnqueueResult:
    """Materialise ``parsed`` into the right queue and update its ``order``.

    Writes ``<queue>/<slug>.md`` under the content store — ``main/<slug>.md`` for
    the default queue, ``<playlist>/<slug>.md`` for a ``queue:`` directive —
    creating the playlist if needed (spec §6). The slug is the task's
    ``config.json`` ``order`` key: ``#now`` prepends it, otherwise it is
    appended. Reuses the engine's frontmatter format so both planes read the
    same file (spec §11 invariant 5). Returns where it landed.
    """
    queue = parsed.queue
    if queue and not playlists.exists(tasks_root, queue):
        playlists.create_playlist(tasks_root, queue)
    tasks_rel = playlists.tasks_rel(queue)
    queue_dir = tasks_root / tasks_rel
    queue_dir.mkdir(parents=True, exist_ok=True)

    slug = _unique_slug(queue_dir, parsed.slug)
    dest = queue_dir / f"{slug}.md"
    dest.write_text(render_task_markdown(parsed), encoding="utf-8")

    _update_order(queue_dir, slug, prepend=parsed.now)
    return EnqueueResult(slug=slug, path=dest, tasks_rel=tasks_rel, queue=queue)


def _unique_slug(queue_dir: Path, slug: str) -> str:
    if not (queue_dir / f"{slug}.md").exists():
        return slug
    n = 2
    while (queue_dir / f"{slug}-{n}.md").exists():
        n += 1
    return f"{slug}-{n}"


def _update_order(queue_dir: Path, slug: str, *, prepend: bool) -> None:
    """Insert ``slug`` into the queue's ``config.json`` ``order`` list.

    The default queue may have no ``config.json`` (it lives at ``main/config.json``
    with other settings); a playlist always has one. Either way we read what is
    there, splice the slug in, and write it back, leaving other keys untouched.
    """
    config_path = queue_dir / "config.json"
    data: dict[str, object] = {}
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    order = data.get("order")
    if not isinstance(order, list):
        order = []
    order = [o for o in order if o != slug]
    if prepend:
        order.insert(0, slug)
    else:
        order.append(slug)
    data["order"] = order
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Claude-backed normaliser (production)
# --------------------------------------------------------------------------- #


_NORMALISE_PROMPT = """\
You convert a free-form work request into a task. Reply with STRICT JSON only,
no prose, no code fences: {{"title": "...", "body": "..."}}.

- "title": a concise imperative summary, max ~10 words, no trailing period.
- "body": the cleaned-up description as Markdown. Preserve the author's intent
  and detail; fix obvious typos; do not invent requirements or add directives.

Request:
{text}
"""


class ClaudeNormaliseBackend:
    """Normalise via the Claude Code CLI as a single-shot completion.

    Runs ``claude -p`` with a JSON-only prompt and parses ``{title, body}`` out
    of the reply. Imported lazily by the daemon; a launch/parse failure falls
    back to a heuristic split so a capture never hard-fails on the model.
    """

    def __init__(
        self, *, config: dict[str, object] | None = None, model: str | None = None
    ) -> None:
        self._config = config or {}
        self._model = model or str(self._config.get("model") or "claude-sonnet-4-6")

    def normalise(self, text: str) -> tuple[str, str]:
        import subprocess

        from nightshift.engine import resolve_claude_bin

        prompt = _NORMALISE_PROMPT.format(text=text)
        argv = [
            resolve_claude_bin(self._config),
            "-p",
            prompt,
            "--model",
            self._model,
        ]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            return _heuristic_split(text)
        if proc.returncode != 0:
            return _heuristic_split(text)
        return _parse_normalise_reply(proc.stdout) or _heuristic_split(text)


def _parse_normalise_reply(reply: str) -> tuple[str, str] | None:
    reply = reply.strip()
    start = reply.find("{")
    end = reply.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(reply[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    title = str(data.get("title") or "").strip()
    body = str(data.get("body") or "").strip()
    if not title and not body:
        return None
    return title, body


def _heuristic_split(text: str) -> tuple[str, str]:
    """First non-blank line → title, the rest → body (spec §6 baseline)."""
    lines = text.strip().splitlines()
    if not lines:
        return "Untitled task", ""
    title = lines[0].lstrip("#").strip()
    body = "\n".join(lines[1:]).strip()
    return title or "Untitled task", body
