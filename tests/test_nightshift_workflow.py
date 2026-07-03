"""Shape checks for the standalone repo's operator entry points.

Nightshift was extracted out of the longitude monorepo, where dispatch ran from
a GitHub Actions workflow (``.github/workflows/nightshift.yml``) toggled by a
repo variable. The standalone repo has no such CI lane: the operator drives the
manager and workers locally through the ``justfile``. These tests pin the recipe
surface the README + setup guide document so it cannot silently regress.
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JUSTFILE_PATH = ROOT / "justfile"


def _recipe_names(justfile: str) -> set[str]:
    """Return the set of recipe names declared in the justfile.

    A recipe header is a line starting in column 0 of the form ``name args:``
    (recipe bodies are indented, and ``:=`` assignments are excluded)."""
    names: set[str] = set()
    for line in justfile.splitlines():
        match = re.match(r"^([a-zA-Z][\w-]*)(?:\s+[^:]*)?:(?!=)", line)
        if match:
            names.add(match.group(1))
    return names


def test_justfile_exposes_core_recipes() -> None:
    recipes = _recipe_names(JUSTFILE_PATH.read_text())
    for recipe in ("manager", "worker", "migrate", "rollback", "test"):
        assert recipe in recipes, f"justfile is missing the `{recipe}` recipe"


def test_justfile_run_recipes_invoke_package_modules() -> None:
    justfile = JUSTFILE_PATH.read_text()
    # The run recipes drive the installed package entry modules, not tools/ paths.
    assert "-m nightshift.manager" in justfile
    assert "-m nightshift.worker" in justfile


def test_justfile_migrations_point_at_package_assets() -> None:
    justfile = JUSTFILE_PATH.read_text()
    # Migrations are shipped inside the package now, not under tools/nightshift/.
    assert "src/nightshift/assets/migrations" in justfile
    assert "tools/nightshift/migrations" not in justfile


def test_justfile_test_recipe_runs_pytest() -> None:
    justfile = JUSTFILE_PATH.read_text()
    assert "-m pytest tests" in justfile
