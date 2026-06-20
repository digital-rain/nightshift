"""Persistent slug→thread map for Slack activity threads.

A task maps to a single Slack parent message (``thread_ts``) reused across
intake → local → remote and across restarts (spec §11 invariant 2). The map is
a small JSON file under the queue dir (``main/slack-threads.json`` for the
default queue, ``<playlist>/slack-threads.json`` for a playlist) so the
same slug shares one thread even when a later run is a different process.

Writes are serialised under a lock and persisted atomically; reads tolerate a
missing or malformed file (returning "no thread yet") so a corrupt store never
breaks a run.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


THREADS_FILENAME = "slack-threads.json"


@dataclass(frozen=True)
class ThreadRef:
    """Where a task's parent card lives: its channel and message ``ts``."""

    channel: str
    thread_ts: str


class ThreadStore:
    """Process-local cache over a JSON ``slug → {channel, thread_ts}`` map.

    One instance per queue. ``get`` is best-effort and never raises; ``set`` is
    serialised and writes atomically so concurrent notifiers (e.g. the CLI and
    the UI player) can't corrupt the file.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._cache: dict[str, ThreadRef] = {}
        self._loaded = False

    @classmethod
    def for_queue(cls, tasks_root: Path, tasks_rel: str = "main") -> ThreadStore:
        """Store at ``<tasks_root>/<tasks_rel>/slack-threads.json``."""
        return cls(tasks_root / tasks_rel / THREADS_FILENAME)

    @property
    def path(self) -> Path:
        return self._path

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._cache = self._read_file()
        self._loaded = True

    def _read_file(self) -> dict[str, ThreadRef]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, ThreadRef] = {}
        for slug, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            channel = entry.get("channel")
            thread_ts = entry.get("thread_ts")
            if isinstance(channel, str) and isinstance(thread_ts, str):
                out[slug] = ThreadRef(channel=channel, thread_ts=thread_ts)
        return out

    def get(self, slug: str) -> ThreadRef | None:
        """The recorded thread for ``slug``, or ``None`` if none yet."""
        with self._lock:
            self._ensure_loaded()
            return self._cache.get(slug)

    def set(self, slug: str, ref: ThreadRef) -> None:
        """Record (or replace) the thread for ``slug`` and persist."""
        with self._lock:
            self._ensure_loaded()
            self._cache[slug] = ref
            self._flush()

    def _flush(self) -> None:
        data: dict[str, Any] = {
            slug: {"channel": ref.channel, "thread_ts": ref.thread_ts}
            for slug, ref in self._cache.items()
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            # Best-effort: a store we can't persist degrades to in-memory only.
            pass
