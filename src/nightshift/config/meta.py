"""Field metadata for the settings editor.

Editor metadata is attached per field via ``dataclasses.field(metadata=…)``.
The ``nightshift`` key in metadata carries a :class:`FieldMeta` dict so a
field's label/category/apply/secret can never separate from its type/default.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Literal, TypedDict


class FieldMeta(TypedDict, total=False):
    category: str
    label: str
    desc: str
    apply: Literal["live", "next-task", "restart"]
    type: str
    options: list[str]
    secret: bool
    env: str
    editable: bool
    flatten: bool
    json_key: str
    validate: str


# ``apply`` is intentionally not required: it defaults to ``"next-task"`` (the
# common case), so only fields that need a restart or apply live carry the tag.
_REQUIRED = ("category", "label", "desc")


def meta(**kw: Any) -> dict[str, Any]:
    """Build a validated metadata mapping for ``dataclasses.field(metadata=…)``.

    ``apply`` defaults to ``"next-task"`` when omitted — label a field's apply
    mode only when it differs (``"restart"`` / ``"live"``).
    """
    kw.setdefault("apply", "next-task")
    return {"nightshift": FieldMeta(**kw)}


def assert_complete(*dataclasses_: type) -> None:
    """Fail-fast at import: every editable field carries the required keys.

    A field is editable iff it carries ``nightshift`` metadata and is not
    ``editable=False``.
    """
    for dc in dataclasses_:
        for f in dataclasses.fields(dc):
            m = f.metadata.get("nightshift")
            if m is None or m.get("editable") is False:
                continue
            missing = [k for k in _REQUIRED if k not in m]
            if missing:
                raise AssertionError(
                    f"{dc.__name__}.{f.name} missing meta keys: {missing}"
                )
