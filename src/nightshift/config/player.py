"""Player configuration model — .nightshift/player.json.

Operator UI/player preferences as a typed dataclass carrying editor metadata
(the settings registry derives the /api/settings surface from it). The
``port`` knob died with the legacy single-box UI server in Phase 9.
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

_SI_RE = re.compile(r"(\d{1,15})\s{0,10}([kKMGT]i?)\b")
_SI_MULTIPLIERS = {
    "k": 1024, "K": 1024,
    "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4,
}


def parse_si_int(value: str | int) -> int:
    """Parse a value like ``16k``, ``1Mi``, or a plain integer into an int.

    Raises :class:`ValueError` on unrecognised input.
    """
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        raise ValueError("value is empty")
    try:
        return int(text)
    except ValueError:
        pass
    m = _SI_RE.fullmatch(text)
    if not m:
        raise ValueError(f"invalid SI value: {value!r} (use e.g. 16k, 1Mi, 2Gi)")
    digits, suffix = m.group(1), m.group(2)
    multiplier = _SI_MULTIPLIERS.get(suffix)
    if multiplier is None:
        raise ValueError(f"unknown SI suffix: {suffix!r} (use k, K, Mi, Gi, Ti)")
    result = int(digits) * multiplier
    if result <= 0:
        raise ValueError("value must be greater than zero")
    return result


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


def load_player_config(workspace: Path) -> PlayerConfig:
    """Load player config from ``.nightshift/player.json``."""
    data = load_json(player_json_path(workspace))
    return PlayerConfig(
        theme=data.get("theme", "dark"),
        transport_mode=data.get("transport_mode", "auto"),
        repeat_interval=data.get("repeat_interval", "30m"),
    )


def save_player_config(workspace: Path, config: PlayerConfig) -> None:
    """Persist a PlayerConfig to ``.nightshift/player.json``."""
    data: dict[str, Any] = {
        "theme": config.theme,
        "transport_mode": config.transport_mode,
        "repeat_interval": config.repeat_interval,
    }
    save_json(player_json_path(workspace), data)
