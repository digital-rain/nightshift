"""Config file I/O and .env handling shared by all surfaces."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    """Load a JSON config file, returning ``{}`` when absent or malformed."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_json(path: Path, data: dict[str, Any]) -> None:
    """Write a dict as pretty-printed JSON, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def load_dotenv(workspace: Path) -> None:
    """Load ``<workspace>/.env`` into ``os.environ`` (setdefault semantics).

    Must be called at the start of every entrypoint (manager, worker, server)
    so secrets-in-.env resolve uniformly. A real env var always wins.
    """
    path = workspace / ".env"
    if not path.exists():
        return
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_ENV_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=")


def save_dotenv_key(workspace: Path, key: str, value: str) -> None:
    """Upsert a single key in ``<workspace>/.env``, preserving other lines/comments."""
    path = workspace / ".env"
    lines: list[str] = []
    found = False

    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            m = _ENV_LINE_RE.match(line)
            if m and m.group(1) == key:
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)

    if not found:
        lines.append(f"{key}={value}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def manager_json_path(workspace: Path) -> Path:
    return workspace / ".nightshift" / "manager.json"


def worker_json_path(workspace: Path) -> Path:
    return workspace / ".nightshift" / "worker.json"


def player_json_path(workspace: Path) -> Path:
    return workspace / ".nightshift" / "player.json"
