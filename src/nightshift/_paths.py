"""Filesystem locations for Nightshift.

Two distinct kinds of path:

* **Shipped assets** — the operator UI, worker UI, task templates, agent prompt
  charters, and DB migrations. These ride inside the installed package and are
  resolved relative to *this* module, never relative to the working tree.
* **Operator state** — ``config.json``, ``config.json.local``, ``.tasks/``,
  ``.worktrees/`` and the like. These live under the *root* handed to each entry
  point (defaults to the current working directory) and are read/written at run
  time.

Keeping the split explicit is what lets Nightshift run from an installed package
while still managing an arbitrary working tree passed as ``--root``.
"""

from __future__ import annotations

import os
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PACKAGE_DIR / "assets"


def _ui_dir(env_var: str, default: Path) -> Path:
    """Resolve a UI-serving directory, allowing an env override.

    The operator + worker UIs default to the hand-written assets shipped in the
    package. Setting ``NIGHTSHIFT_UI_DIR`` / ``NIGHTSHIFT_WORKER_UI_DIR`` points
    the static mount at a different build instead — e.g. the React/Vite bundle
    under ``assets/ui-react/dist-manager`` — without touching any serving code.
    This is what lets the React frontend be swapped in as "just another shell on
    the API" (see ``just manager-react-prod``). An unset or blank value keeps the
    legacy default.
    """
    override = os.environ.get(env_var, "").strip()
    return Path(override).expanduser().resolve() if override else default


UI_DIR = _ui_dir("NIGHTSHIFT_UI_DIR", ASSETS_DIR / "ui")
WORKER_UI_DIR = _ui_dir("NIGHTSHIFT_WORKER_UI_DIR", ASSETS_DIR / "ui-worker")
TEMPLATES_DIR = ASSETS_DIR / "templates"
PROMPTS_DIR = ASSETS_DIR / "prompts"
MIGRATIONS_DIR = ASSETS_DIR / "migrations"
CONFIG_TEMPLATES_DIR = ASSETS_DIR / "config"


def asset(*parts: str) -> Path:
    """Return the path to a shipped asset under the package ``assets/`` dir."""
    return ASSETS_DIR.joinpath(*parts)
