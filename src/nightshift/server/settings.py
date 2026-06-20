"""Player settings: a small JSON-backed config with a field schema.

The schema drives a VSCode/Longitude-style settings editor in the UI (labeled
rows with descriptions and typed inputs). Settings persist to a JSON file
distinct from the runner's ``config.json``; code-level defaults apply when the
file is absent.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from nightshift.backends import backend_names


SETTINGS_REL_PATH = ".nightshift/settings.json"

# Derived from the backend registry so a newly registered backend (e.g. gemini)
# is automatically a valid worker_backend choice — no drift between the shim and
# the settings allow-list.
WORKER_BACKENDS = tuple(backend_names())

DEFAULTS: dict[str, Any] = {
    "transport_mode": "auto",
    "repeat_interval": "30m",
    "theme": "dark",
    "port": 8799,
    "worker_backend": "claude-code",
}

SCHEMA: list[dict[str, Any]] = [
    {
        "key": "transport_mode",
        "label": "Transport mode",
        "description": "1-shot runs one task; auto-play runs the queue once; repeat loops the queue on an interval.",
        "type": "enum",
        "options": ["oneshot", "auto", "repeat"],
        "default": DEFAULTS["transport_mode"],
    },
    {
        "key": "repeat_interval",
        "label": "Repeat interval",
        "description": "Wait between repeat play-throughs. Duration like 45s, 30m, 2h, or 1h30m.",
        "type": "duration",
        "default": DEFAULTS["repeat_interval"],
    },
    {
        "key": "theme",
        "label": "Theme",
        "description": "Light or dark UI skin. Applies immediately on save and is remembered in this browser.",
        "type": "enum",
        "options": ["light", "dark"],
        "default": DEFAULTS["theme"],
    },
    {
        "key": "port",
        "label": "Server port",
        "description": "Port the Nightshift UI server listens on (applies on next launch).",
        "type": "int",
        "default": DEFAULTS["port"],
    },
    {
        "key": "worker_backend",
        "label": "Worker backend",
        "description": (
            "Who runs each task. claude-code/cursor are agentic (edit files); "
            "anthropic/ollama are single-shot completions (latency baseline, no edits)."
        ),
        "type": "enum",
        "options": list(WORKER_BACKENDS),
        "default": DEFAULTS["worker_backend"],
    },
]

_DURATION_RE = re.compile(r"(\d+)\s*([smh])", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}


def parse_duration(value: str) -> int:
    """Parse a duration like ``30m`` or ``1h30m`` into seconds. Raises on junk."""
    text = str(value).strip().lower()
    if not text:
        raise ValueError("interval is empty")
    matches = list(_DURATION_RE.finditer(text))
    if not matches or "".join(m.group(0).replace(" ", "") for m in matches) != text.replace(" ", ""):
        raise ValueError(f"invalid duration: {value!r} (use e.g. 30m, 2h, 1h30m)")
    total = sum(int(m.group(1)) * _UNIT_SECONDS[m.group(2)] for m in matches)
    if total <= 0:
        raise ValueError("interval must be greater than zero")
    return total


def settings_path(root: Path) -> Path:
    return root / SETTINGS_REL_PATH


def load_settings(root: Path) -> dict[str, Any]:
    """Return defaults merged with any persisted values."""
    values = dict(DEFAULTS)
    path = settings_path(root)
    if path.exists():
        try:
            stored = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            stored = {}
        for key in DEFAULTS:
            if key in stored:
                values[key] = stored[key]
    return values


def validate_settings(values: dict[str, Any]) -> list[str]:
    """Return a list of human-readable validation errors (empty when valid)."""
    errors: list[str] = []

    mode = values.get("transport_mode")
    if mode not in {"oneshot", "auto", "repeat"}:
        errors.append("transport_mode must be one of oneshot, auto, repeat")

    if mode == "repeat" or values.get("repeat_interval"):
        try:
            parse_duration(values.get("repeat_interval", ""))
        except ValueError as exc:
            errors.append(f"repeat_interval: {exc}")

    if values.get("theme") not in {"light", "dark"}:
        errors.append("theme must be light or dark")

    port = values.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        errors.append("port must be an integer between 1 and 65535")

    if values.get("worker_backend") not in WORKER_BACKENDS:
        errors.append("worker_backend must be one of " + ", ".join(WORKER_BACKENDS))

    return errors


def save_settings(root: Path, incoming: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist settings, returning the merged result.

    Raises ``ValueError`` (joined messages) when validation fails.
    """
    merged = load_settings(root)
    for key in DEFAULTS:
        if key in incoming:
            merged[key] = incoming[key]
    if isinstance(merged.get("port"), str) and merged["port"].isdigit():
        merged["port"] = int(merged["port"])

    errors = validate_settings(merged)
    if errors:
        raise ValueError("; ".join(errors))

    path = settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2) + "\n")
    return merged
