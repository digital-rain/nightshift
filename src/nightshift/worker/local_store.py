"""Local run history + live status for the worker's own minimal UI.

The manager owns durable cross-worker history (Postgres); this is just the
*local* view a single worker shows on its Now / History screens. Records append
to a small JSONL file under the worker's ``--workspace``; live "now playing"
state is held in memory and read by the worker UI's status API.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


LOCAL_DIR = ".nightshift-worker"
RUNS_FILE = "runs.jsonl"
MAX_LOG_TAIL = 400


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class NowPlaying:
    run_id: str
    task: str
    queue: str
    title: str
    model: str
    backend: str
    repo: str = ""
    branch: str | None = None
    phase: str = "worker"
    started_at: str = field(default_factory=_now_iso)
    log_tail: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LOG_TAIL))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["log_tail"] = list(self.log_tail)
        return d


class LocalStore:
    def __init__(self, workspace: Path) -> None:
        self._dir = workspace / LOCAL_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / RUNS_FILE
        self._lock = threading.Lock()
        self._now: NowPlaying | None = None

    # ---- live "now" ------------------------------------------------------- #

    def begin(
        self,
        *,
        run_id: str,
        task: str,
        queue: str,
        title: str,
        model: str,
        backend: str,
        repo: str = "",
        branch: str | None = None,
    ) -> None:
        with self._lock:
            self._now = NowPlaying(
                run_id=run_id,
                task=task,
                queue=queue,
                title=title,
                model=model,
                backend=backend,
                repo=repo,
                branch=branch,
            )

    def set_phase(self, phase: str) -> None:
        with self._lock:
            if self._now is not None:
                self._now.phase = phase

    def log(self, line: str) -> None:
        with self._lock:
            if self._now is not None:
                self._now.log_tail.append(line)

    def now(self) -> dict[str, Any] | None:
        with self._lock:
            return self._now.to_dict() if self._now else None

    def finish(self, record: dict[str, Any]) -> None:
        with self._lock:
            record = {**record, "finished_at": _now_iso()}
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            self._now = None

    # ---- history ---------------------------------------------------------- #

    def history(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self._path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        rows.reverse()
        return rows[:limit]

    def stats(self) -> dict[str, Any]:
        rows = self.history(limit=100000)
        completed = [r for r in rows if r.get("status") == "completed"]
        return {
            "total_runs": len(rows),
            "completed": len(completed),
            "errored": sum(1 for r in rows if r.get("status") == "error"),
            "total_loc": sum(int(r.get("loc") or 0) for r in completed),
        }
