"""Player configuration model — .nightshift/player.json.

Replaces the hand-maintained SCHEMA/DEFAULTS/validate_settings in
server/settings.py with a typed dataclass carrying editor metadata.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nightshift.config.io import load_json, player_json_path, save_json
from nightshift.config.meta import meta


# Bounded quantifiers keep the scan linear on junk input (a long digit run
# with no trailing unit would otherwise backtrack polynomially — ReDoS guard).
# Nine digits of seconds is already ~31 years; real values are "30m"-sized.
_DURATION_RE = re.compile(r"(\d{1,9})\s{0,10}([smh])", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}


def parse_duration(value: str) -> int:
    """Parse a duration like ``30m`` or ``1h30m`` into seconds. Raises on junk."""
    text = str(value).strip().lower()
    if not text:
        raise ValueError("interval is empty")
    matches = list(_DURATION_RE.finditer(text))
    if not matches or "".join(
        m.group(0).replace(" ", "") for m in matches
    ) != text.replace(" ", ""):
        raise ValueError(f"invalid duration: {value!r} (use e.g. 30m, 2h, 1h30m)")
    total = sum(int(m.group(1)) * _UNIT_SECONDS[m.group(2)] for m in matches)
    if total <= 0:
        raise ValueError("interval must be greater than zero")
    return total


@dataclass(frozen=True)
class PlayerConfig:
    """Operator UI/player preferences — .nightshift/player.json."""

    theme: str = field(default="dark", metadata=meta(
        category="Appearance", label="Theme",
        desc="Light or dark UI skin. Applies immediately on save.",
        apply="live", options=["light", "dark"]))
    transport_mode: str = field(default="auto", metadata=meta(
        category="Transport", label="Transport mode",
        desc="1-shot runs one task; auto-play runs the queue once; repeat loops.",
        apply="live", options=["oneshot", "auto", "repeat"]))
    repeat_interval: str = field(default="30m", metadata=meta(
        category="Transport", label="Repeat interval",
        desc="Wait between repeat play-throughs (e.g. 45s, 30m, 2h, 1h30m).",
        apply="live", type="duration"))
    port: int = field(default=8799, metadata=meta(
        category="Server", label="Server port",
        desc="Port the Nightshift UI server listens on (applies on next launch).",
        apply="restart"))


def load_player_config(workspace: Path) -> PlayerConfig:
    """Load player config from ``.nightshift/player.json``."""
    data = load_json(player_json_path(workspace))
    return PlayerConfig(
        theme=data.get("theme", "dark"),
        transport_mode=data.get("transport_mode", "auto"),
        repeat_interval=data.get("repeat_interval", "30m"),
        port=int(data.get("port", 8799)),
    )


def save_player_config(workspace: Path, config: PlayerConfig) -> None:
    """Persist a PlayerConfig to ``.nightshift/player.json``."""
    data: dict[str, Any] = {
        "theme": config.theme,
        "transport_mode": config.transport_mode,
        "repeat_interval": config.repeat_interval,
        "port": config.port,
    }
    save_json(player_json_path(workspace), data)


def validate_player_config(values: dict[str, Any]) -> list[str]:
    """Return human-readable validation errors (empty when valid)."""
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

    return errors
