"""Delta validation, write routing, and GET/PUT helpers for /api/settings.

Validates incoming field values against their FieldSpec, routes writes to
the correct file (JSON or .env), and builds the tiered GET response.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Never

from nightshift.config.io import (
    load_json,
    manager_json_path,
    player_json_path,
    save_dotenv_key,
    save_json,
    worker_json_path,
)
from nightshift.config.player import parse_duration, parse_si_int
from nightshift.config.registry import (
    FieldSpec,
    Store,
    emit_json_schema,
    registry_by_key,
    specs_for_surface,
)
from nightshift.git.transport import normalize_wip_prefix
from nightshift.model_id import AGNOSTIC, is_qualified


def _validate_field(spec: FieldSpec, value: Any) -> str | None:
    """Validate a single value against its spec. Returns an error or None."""
    match spec.type:
        case "bool":
            if not isinstance(value, bool):
                return "expected boolean"
        case "int":
            if isinstance(value, bool) or not isinstance(value, int):
                return "expected integer"
        case "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return "expected number"
        case "string":
            if not isinstance(value, str) and value is not None:
                return "expected string"
            if spec.key == "wip_ref_prefix" and isinstance(value, str):
                try:
                    normalize_wip_prefix(value)
                except ValueError as exc:
                    return str(exc)
        case "enum":
            if spec.options and value not in spec.options:
                return f"must be one of {spec.options}"
        case "duration":
            if not isinstance(value, str):
                return "expected duration string"
            try:
                parse_duration(value)
            except ValueError as exc:
                return str(exc)
        case "si_int":
            if isinstance(value, bool):
                return "expected integer or SI string (e.g. 16k, 1Mi)"
            if not isinstance(value, (int, str)):
                return "expected integer or SI string (e.g. 16k, 1Mi)"
            try:
                parse_si_int(value)
            except ValueError as exc:
                return str(exc)
        case "string_list":
            if not isinstance(value, list):
                return "expected list of strings"
            if not all(isinstance(v, str) for v in value):
                return "all items must be strings"
        case "int_list":
            if not isinstance(value, list):
                return "expected list of integers"
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
                return "all items must be integers"
        case "regex_list":
            if not isinstance(value, list):
                return "expected list of regex patterns"
            for i, pattern in enumerate(value):
                if not isinstance(pattern, str):
                    return f"item {i}: expected string"
                try:
                    re.compile(pattern)
                except re.error as exc:
                    return f"item {i}: invalid regex: {exc}"
        case "str_map":
            if not isinstance(value, dict):
                return "expected object (string \u2192 string map)"
            for k, v in value.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    return "all keys and values must be strings"
        case _:
            _exhaustive: Never = spec.type  # noqa: F841
    return None


def _validate_model_id(value: str, allow_keywords: bool = False) -> str | None:
    """Check a single model id is provider-qualified (provider/model).

    If *allow_keywords* is True, agnostic keywords (auto/max/default/"") pass.
    """
    stripped = value.strip()
    if not stripped:
        return None if allow_keywords else "model id must not be empty"
    if stripped.lower() in AGNOSTIC:
        if allow_keywords:
            return None
        return f"'{stripped}' is a keyword, not a qualified model id — use provider/model"
    if not is_qualified(stripped):
        return f"'{stripped}' requires a provider/ prefix (e.g. claude-code/{stripped})"
    return None


def _validate_semantic(spec: FieldSpec, value: Any) -> str | None:
    """Semantic validation beyond type checks, driven by spec.validate."""
    if not spec.validate:
        return None

    match spec.validate:
        case "model_id":
            if isinstance(value, str):
                return _validate_model_id(value)
            if value is None:
                return None
        case "model_id_or_keyword":
            if isinstance(value, str):
                return _validate_model_id(value, allow_keywords=True)
            if value is None:
                return None
        case "model_id_list":
            if isinstance(value, list):
                for i, item in enumerate(value):
                    if not isinstance(item, str):
                        continue
                    err = _validate_model_id(item)
                    if err:
                        return f"item {i}: {err}"
        case "model_id_map":
            if isinstance(value, dict):
                for k, v in value.items():
                    err = _validate_model_id(k)
                    if err:
                        return f"key '{k}': {err}"
                    err = _validate_model_id(v)
                    if err:
                        return f"value for '{k}': {err}"
    return None


def _normalize_field(spec: FieldSpec, value: Any) -> Any:
    """Apply field-specific normalization after validation passes."""
    if spec.type == "si_int":
        return parse_si_int(value)
    if spec.key == "wip_ref_prefix" and isinstance(value, str):
        try:
            return normalize_wip_prefix(value)
        except ValueError:
            pass
    return value


def validate_delta(
    delta: dict[str, dict[str, Any]],
    allowed_surfaces: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Validate a surface-keyed delta envelope.

    Returns ``(resolved, errors)`` where *resolved* maps
    ``{surface: {dotted_key: value}}`` for valid fields and *errors* maps
    ``{surface.dotted_key: message}`` for invalid ones.
    """
    idx = registry_by_key()
    resolved: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}

    for surface, fields in delta.items():
        if surface not in allowed_surfaces:
            errors[surface] = f"unknown or disallowed surface: {surface}"
            continue
        for key, value in fields.items():
            full = f"{surface}.{key}"
            spec = idx.get((surface, key))
            if spec is None:
                errors[full] = "unknown field"
                continue
            err = _validate_field(spec, value)
            if err:
                errors[full] = err
                continue
            err = _validate_semantic(spec, value)
            if err:
                errors[full] = err
            else:
                resolved.setdefault(surface, {})[key] = _normalize_field(spec, value)

    return resolved, errors


def _store_path(store: Store, workspace: Path) -> Path:
    match store:
        case "manager.json":
            return manager_json_path(workspace)
        case "worker.json":
            return worker_json_path(workspace)
        case "player.json":
            return player_json_path(workspace)
        case ".env":
            return workspace / ".env"
        case _:
            _exhaustive: Never = store  # noqa: F841
            raise ValueError(f"unknown store: {store}")


def _set_dotted(data: dict[str, Any], key: str, value: Any) -> None:
    """Set a dotted key in a nested dict (e.g. ``cadences.poll_seconds``)."""
    parts = key.split(".")
    target = data
    for part in parts[:-1]:
        if part not in target or not isinstance(target[part], dict):
            target[part] = {}
        target = target[part]
    target[parts[-1]] = value


def _get_dotted(data: dict[str, Any], key: str) -> Any:
    """Get a dotted key from a nested dict, returning ``_MISSING`` on absence."""
    parts = key.split(".")
    target = data
    for part in parts[:-1]:
        if not isinstance(target, dict) or part not in target:
            return _MISSING
        target = target[part]
    if not isinstance(target, dict):
        return _MISSING
    return target.get(parts[-1], _MISSING)


_MISSING = object()


def write_delta(
    workspace: Path,
    resolved: dict[str, dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Route validated writes to their files.

    Returns ``(applied_live, restart_required)`` — lists of full keys
    (``surface.dotted_key``) indicating which fields were hot-applied vs
    which require a process restart.
    """
    idx = registry_by_key()
    applied_live: list[str] = []
    restart_required: list[str] = []

    # Collect writes grouped by store file, and track apply status.
    json_writes: dict[Store, list[tuple[str, Any]]] = {}
    env_writes: list[tuple[str, str]] = []

    for surface, fields in resolved.items():
        for key, value in fields.items():
            spec = idx[(surface, key)]
            full = f"{surface}.{key}"

            if spec.apply == "live":
                applied_live.append(full)
            else:
                restart_required.append(full)

            if spec.store == ".env":
                if spec.env:
                    env_writes.append((spec.env, str(value)))
            else:
                file_key = spec.json_key or key
                json_writes.setdefault(spec.store, []).append((file_key, value))

    for env_key, env_val in env_writes:
        save_dotenv_key(workspace, env_key, env_val)

    for store, changes in json_writes.items():
        path = _store_path(store, workspace)
        data = load_json(path)
        for key, value in changes:
            _set_dotted(data, key, value)
        save_json(path, data)

    return applied_live, restart_required


def _field_value(
    spec: FieldSpec,
    file_data: dict[str, Any],
    workspace: Path,
) -> dict[str, Any]:
    """Compute stored/effective/env_shadowed for one field."""
    if spec.secret:
        env_val = os.environ.get(spec.env, "") if spec.env else ""
        return {
            "stored": None,
            "is_set": bool(env_val),
            "effective": None,
            "env": spec.env or "",
            "env_shadowed": False,
        }

    stored = _get_dotted(file_data, spec.json_key or spec.key)
    if stored is _MISSING:
        stored = spec.default

    effective = stored
    env_shadowed = False
    if spec.env:
        env_val = os.environ.get(spec.env)
        if env_val is not None:
            env_shadowed = True
            match spec.type:
                case "int":
                    try:
                        effective = int(env_val)
                    except ValueError:
                        effective = stored
                case "float":
                    try:
                        effective = float(env_val)
                    except ValueError:
                        effective = stored
                case "bool":
                    effective = env_val.lower() in ("true", "1", "yes")
                case _:
                    effective = env_val

    if isinstance(stored, tuple):
        stored = list(stored)
    if isinstance(effective, tuple):
        effective = list(effective)

    return {
        "stored": stored,
        "effective": effective,
        "env": spec.env or "\u2014",
        "env_shadowed": env_shadowed,
    }


_SURFACE_STORE: dict[str, Store] = {
    "manager": "manager.json",
    "worker": "worker.json",
    "player": "player.json",
}


def build_get_response(
    workspace: Path,
    surfaces: list[str],
) -> dict[str, Any]:
    """Build the tiered GET /api/settings response."""
    tiers: list[dict[str, Any]] = []

    for surface in surfaces:
        specs = specs_for_surface(surface)
        if not specs:
            continue

        store = _SURFACE_STORE[surface]
        path = _store_path(store, workspace)
        file_data = load_json(path)

        categories: list[dict[str, Any]] = []
        seen_cats: dict[str, list[dict[str, Any]]] = {}

        for spec in specs:
            cat_name = spec.category
            vals = _field_value(spec, file_data, workspace)

            field_payload: dict[str, Any] = {
                "key": spec.key,
                "label": spec.label,
                "desc": spec.desc,
                "type": spec.type,
                "apply": spec.apply,
                "store": spec.store,
                "default": spec.default,
                "secret": spec.secret,
                **vals,
            }
            if spec.options:
                field_payload["options"] = spec.options
            if spec.validate:
                field_payload["validate"] = spec.validate

            if cat_name not in seen_cats:
                cat_fields: list[dict[str, Any]] = []
                seen_cats[cat_name] = cat_fields
                categories.append({"name": cat_name, "fields": cat_fields})
            seen_cats[cat_name].append(field_payload)

        tiers.append({"surface": surface, "categories": categories})

    return {
        "tiers": tiers,
        "schema": emit_json_schema(),
    }
