"""Player settings — delegates to the unified config package.

Backward-compatible module: callers that import ``SCHEMA``, ``DEFAULTS``,
``load_settings``, ``save_settings``, ``parse_duration``, and
``resolve_launch_workspace`` from here continue to work. The authoritative
model is ``nightshift.config.player.PlayerConfig``.

The ``worker_backend`` key is removed from player settings (§7.4 of the spec):
the backend selector lives only as ``WorkerConfig.backend`` in worker.json.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from nightshift.config.io import load_json, player_json_path, save_json
from nightshift.config.player import (
    parse_duration,
    validate_player_config,
)


DEFAULTS: dict[str, Any] = {
    "transport_mode": "auto",
    "repeat_interval": "30m",
    "theme": "dark",
    "port": 8799,
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
]


def settings_path(workspace: Path) -> Path:
    return player_json_path(workspace)


def load_settings(workspace: Path) -> dict[str, Any]:
    """Return defaults merged with any persisted values from player.json."""
    values = dict(DEFAULTS)
    data = load_json(player_json_path(workspace))
    for key in DEFAULTS:
        if key in data:
            values[key] = data[key]
    return values


def validate_settings(values: dict[str, Any]) -> list[str]:
    """Return a list of human-readable validation errors (empty when valid)."""
    return validate_player_config(values)


def save_settings(workspace: Path, incoming: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist settings, returning the merged result.

    Raises ``ValueError`` (joined messages) when validation fails.
    """
    merged = load_settings(workspace)
    for key in DEFAULTS:
        if key in incoming:
            merged[key] = incoming[key]
    if isinstance(merged.get("port"), str) and merged["port"].isdigit():
        merged["port"] = int(merged["port"])

    errors = validate_settings(merged)
    if errors:
        raise ValueError("; ".join(errors))

    path = player_json_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(path, merged)
    return merged


def resolve_launch_workspace(cli_workspace: Path | None) -> Path:
    """The workspace the server binds to at launch.

    Precedence: explicit ``--workspace`` flag, then ``NIGHTSHIFT_WORKSPACE``
    env, then the current directory. The user-level config persistence path
    is removed (workspace is a pure launch input per §1 of the spec).
    """
    if cli_workspace is not None:
        return cli_workspace.expanduser().resolve()
    env = os.environ.get("NIGHTSHIFT_WORKSPACE")
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd().resolve()


def save_user_config_value(key: str, value: Any) -> Any:
    """No-op stub: workspace persistence is removed (workspace is a launch input).

    Kept as a no-op so callers that still reference it don't crash during the
    transition; the call site in server/app.py should be removed.
    """
    return value


__all__ = [
    "DEFAULTS",
    "SCHEMA",
    "load_settings",
    "parse_duration",
    "resolve_launch_workspace",
    "save_settings",
    "save_user_config_value",
    "settings_path",
    "validate_settings",
]
