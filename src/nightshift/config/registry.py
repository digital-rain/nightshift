"""FieldSpec registry — typed projection of the Part 1 config dataclasses.

Walks ``dataclasses.fields()`` on the config models, reads
``.metadata["nightshift"]``, and builds a flat ``FieldSpec`` list that the
admin API and the UI consume. The registry is **derived**, never a parallel
source: add a field in the config model → it appears in the API automatically.
"""

from __future__ import annotations

import dataclasses
import types
from typing import Any, Literal, Never, get_type_hints


FieldType = Literal[
    "string", "int", "float", "bool", "enum", "duration",
    "string_list", "int_list", "regex_list", "str_map",
]
ApplyMode = Literal["live", "next-task", "restart"]
Store = Literal["manager.json", "worker.json", "player.json", ".env"]


@dataclasses.dataclass(frozen=True)
class FieldSpec:
    key: str
    surface: Literal["manager", "worker", "player"]
    store: Store
    category: str
    label: str
    desc: str
    type: FieldType
    apply: ApplyMode
    default: Any
    options: list[str] | None = None
    secret: bool = False
    env: str | None = None


_SURFACE_STORE: dict[str, Store] = {
    "manager": "manager.json",
    "worker": "worker.json",
    "player": "player.json",
}

_ANNOTATION_TYPE_MAP: dict[type, FieldType] = {
    bool: "bool",
    int: "int",
    float: "float",
    str: "string",
}


def _infer_type(annotation: Any, meta: dict[str, Any]) -> FieldType:
    """Derive FieldType from explicit metadata override or the field annotation."""
    if "type" in meta:
        return meta["type"]
    if "options" in meta:
        return "enum"

    origin = getattr(annotation, "__origin__", None)

    if annotation in _ANNOTATION_TYPE_MAP:
        return _ANNOTATION_TYPE_MAP[annotation]

    # str | None, int | None, etc.  (types.UnionType for X | Y syntax)
    if origin is types.UnionType or isinstance(annotation, types.UnionType):
        args = [a for a in annotation.__args__ if a is not type(None)]
        if len(args) == 1 and args[0] in _ANNOTATION_TYPE_MAP:
            return _ANNOTATION_TYPE_MAP[args[0]]

    # tuple[str, ...] → string_list; list[str] → string_list
    if origin in (tuple, list):
        return "string_list"

    # dict[str, str] → str_map
    if origin is dict:
        return "str_map"

    return "string"


def _default_value(f: dataclasses.Field) -> Any:
    """Extract the default from a dataclass field."""
    if f.default is not dataclasses.MISSING:
        val = f.default
    elif f.default_factory is not dataclasses.MISSING:
        val = f.default_factory()
    else:
        return None
    if isinstance(val, tuple):
        return list(val)
    return val


def _walk_dataclass(
    dc: type,
    surface: Literal["manager", "worker", "player"],
    prefix: str,
) -> list[FieldSpec]:
    """Yield FieldSpecs for every editable field on *dc*."""
    hints = get_type_hints(dc)
    specs: list[FieldSpec] = []
    for f in dataclasses.fields(dc):
        resolved_type = hints.get(f.name, f.type)

        # Nested dataclass (e.g. Cadences, OperatorConfig) — always descend,
        # even when the container field is editable=False.
        if isinstance(resolved_type, type) and dataclasses.is_dataclass(resolved_type):
            m = f.metadata.get("nightshift")
            flatten = m.get("flatten", False) if m else False
            nested_prefix = prefix if flatten else (f"{prefix}{f.name}." if prefix else f"{f.name}.")
            specs.extend(_walk_dataclass(resolved_type, surface, nested_prefix))
            continue

        m = f.metadata.get("nightshift")
        if m is None or m.get("editable") is False:
            continue

        key = f"{prefix}{f.name}"
        secret = bool(m.get("secret", False))
        store: Store = ".env" if secret else _SURFACE_STORE[surface]
        field_type = _infer_type(resolved_type, m)

        specs.append(FieldSpec(
            key=key,
            surface=surface,
            store=store,
            category=m["category"],
            label=m["label"],
            desc=m["desc"],
            type=field_type,
            apply=m["apply"],
            default=_default_value(f),
            options=m.get("options"),
            secret=secret,
            env=m.get("env"),
        ))
    return specs


def build_registry() -> list[FieldSpec]:
    """Walk the Part 1 dataclasses → ordered FieldSpec list.

    Skips fields without ``nightshift`` metadata or with ``editable=False``.
    """
    from nightshift.config.manager import ManagerSettings
    from nightshift.config.player import PlayerConfig
    from nightshift.config.worker import WorkerConfig

    specs: list[FieldSpec] = []
    specs.extend(_walk_dataclass(ManagerSettings, "manager", ""))
    specs.extend(_walk_dataclass(WorkerConfig, "worker", ""))
    specs.extend(_walk_dataclass(PlayerConfig, "player", ""))
    return specs


def _json_schema_for_type(spec: FieldSpec) -> dict[str, Any]:
    """Produce a JSON-schema fragment for one FieldSpec."""
    match spec.type:
        case "string":
            schema: dict[str, Any] = {"type": "string"}
        case "int":
            schema = {"type": "integer"}
        case "float":
            schema = {"type": "number"}
        case "bool":
            schema = {"type": "boolean"}
        case "enum":
            schema = {"type": "string", "enum": spec.options or []}
        case "duration":
            schema = {"type": "string", "pattern": r"^(\d+[smh]\s*)+$"}
        case "string_list":
            schema = {"type": "array", "items": {"type": "string"}}
        case "int_list":
            schema = {"type": "array", "items": {"type": "integer"}}
        case "regex_list":
            schema = {"type": "array", "items": {"type": "string", "format": "regex"}}
        case "str_map":
            schema = {"type": "object", "additionalProperties": {"type": "string"}}
        case _:
            _exhaustive: Never = spec.type  # noqa: F841
            schema = {"type": "string"}

    if spec.default is not None:
        schema["default"] = spec.default
    return schema


def emit_json_schema(specs: list[FieldSpec] | None = None) -> dict[str, Any]:
    """Produce a JSON-schema document for the frontend/tests."""
    if specs is None:
        specs = build_registry()

    properties: dict[str, Any] = {}
    for spec in specs:
        full_key = f"{spec.surface}.{spec.key}"
        properties[full_key] = {
            **_json_schema_for_type(spec),
            "title": spec.label,
            "description": spec.desc,
        }

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
    }


# Pre-built index for O(1) lookups by (surface, key).
_REGISTRY: list[FieldSpec] | None = None


def get_registry() -> list[FieldSpec]:
    """Return the cached registry (built once per process)."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = build_registry()
    return _REGISTRY


def registry_by_key() -> dict[tuple[str, str], FieldSpec]:
    """Return ``{(surface, dotted_key): FieldSpec}`` for O(1) lookup."""
    return {(s.surface, s.key): s for s in get_registry()}


def specs_for_surface(surface: str) -> list[FieldSpec]:
    """Return the ordered FieldSpecs for a single surface."""
    return [s for s in get_registry() if s.surface == surface]
