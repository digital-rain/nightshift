"""Cross-package private-name import guard.

A ``from nightshift.<other-package> import _name`` is a fictional boundary:
the importer depends on an underscore-prefixed internal it does not own
(docs/reviews/git-management-review.md §1.1; recurred post-rebuild as
``reconciler`` importing ``_wip_ref``). This guard structurally encodes the
correction: a name shared across package boundaries must be public.

The boundary unit is the first component under ``nightshift`` — a top-level
module (``backends``) or a subpackage (``git``, ``manager``, ``config``).
Imports of private names *within* one unit are that unit's own business, as
are private *modules* with public names (``nightshift._paths``). Tests are
exempt — reaching into internals is what some tests are for.
"""

from __future__ import annotations

import ast
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src" / "nightshift"


def _unit_of(parts: tuple[str, ...]) -> str:
    """The boundary unit of a dotted path under ``nightshift``."""
    return parts[0] if parts else ""


def test_no_cross_package_private_name_imports() -> None:
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC)
        importer_unit = _unit_of(rel.parts) if len(rel.parts) > 1 else rel.stem
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level:
                continue  # relative imports stay inside their own package
            module = node.module or ""
            if not module.startswith("nightshift"):
                continue
            imported_unit = _unit_of(tuple(module.split(".")[1:]))
            if imported_unit == importer_unit:
                continue
            for alias in node.names:
                if alias.name.startswith("_"):
                    violations.append(
                        f"{rel}: from {module} import {alias.name}"
                    )
    assert not violations, (
        "private names imported across package boundaries — promote them to "
        "public names instead:\n  " + "\n  ".join(violations)
    )
