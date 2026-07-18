"""Tests for the settings registry & admin API (spec Part 2, §6).

1. Registry is a faithful projection of the Part 1 dataclasses.
2. GET shape — tiers/categories/fields with stored/effective/env_shadowed.
3. PUT validation — bad values → 400, nothing written; valid delta writes.
4. Secret routing — PUT of a secret writes .env only; GET returns is_set.
5. Apply reporting — live vs restart_required.
6. JSON-schema — emit_json_schema validates a known-good payload.
7. Per-surface scoping — worker endpoint sees only worker fields.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ns = tmp_path / ".nightshift"
    ns.mkdir()
    return tmp_path


# ─── §6.1 Registry is a faithful projection ──────────────────────────────────


class TestRegistryProjection:
    def test_every_editable_field_appears_once(self):
        from nightshift.config.manager import Cadences, ManagerSettings, OperatorConfig
        from nightshift.config.player import PlayerConfig
        from nightshift.config.registry import build_registry
        from nightshift.config.worker import WorkerConfig

        specs = build_registry()
        keys = [(s.surface, s.key) for s in specs]
        assert len(keys) == len(set(keys)), "duplicate (surface, key) in registry"

        expected_fields: set[str] = set()
        for surface, dc_list in [
            ("manager", [ManagerSettings, Cadences, OperatorConfig]),
            ("worker", [WorkerConfig]),
            ("player", [PlayerConfig]),
        ]:
            for dc in dc_list:
                for f in dataclasses.fields(dc):
                    m = f.metadata.get("nightshift")
                    if m is None or m.get("editable") is False:
                        continue
                    if dataclasses.is_dataclass(f.type if isinstance(f.type, type) else None):
                        continue
                    expected_fields.add(f"{surface}.{f.name}")

        registry_fields = {f"{s.surface}.{s.key.split('.')[-1]}" for s in specs}
        missing = expected_fields - registry_fields
        assert not missing, f"fields missing from registry: {missing}"

    def test_categories_sorted_alphabetically(self, workspace: Path, monkeypatch):
        _clear_env(monkeypatch)
        from nightshift.config.validate import build_get_response

        resp = build_get_response(workspace, ["manager"])
        tier = resp["tiers"][0]
        cat_names = [c["name"] for c in tier["categories"]]
        assert cat_names == sorted(cat_names)

    def test_store_correct_for_non_secret(self):
        from nightshift.config.registry import build_registry

        specs = build_registry()
        for s in specs:
            if not s.secret:
                if s.surface == "manager":
                    assert s.store == "manager.json"
                elif s.surface == "worker":
                    assert s.store == "worker.json"
                elif s.surface == "player":
                    assert s.store == "player.json"

    def test_store_overridden_for_secrets(self):
        from nightshift.config.registry import build_registry

        specs = build_registry()
        secret_specs = [s for s in specs if s.secret]
        assert len(secret_specs) >= 2
        for s in secret_specs:
            assert s.store == ".env", f"{s.surface}.{s.key} should route to .env"

    def test_type_inferred_correctly(self):
        from nightshift.config.registry import build_registry

        specs = build_registry()
        by_key = {(s.surface, s.key): s for s in specs}

        assert by_key[("manager", "host")].type == "string"
        assert by_key[("manager", "port")].type == "int"
        assert by_key[("manager", "cadences.poll_seconds")].type == "float"
        assert by_key[("manager", "automerge")].type == "bool"
        assert by_key[("manager", "landing_mode")].type == "enum"
        assert by_key[("manager", "forbidden_paths")].type == "regex_list"
        assert by_key[("manager", "scheduled_models_allow")].type == "string_list"
        assert by_key[("player", "theme")].type == "enum"
        assert by_key[("player", "repeat_interval")].type == "duration"
        assert by_key[("worker", "nightshift.max_tokens")].type == "si_int"
        assert by_key[("worker", "model_aliases")].type == "str_map"

    def test_dotted_keys_for_cadences(self):
        from nightshift.config.registry import build_registry

        specs = build_registry()
        cadence_specs = [s for s in specs if s.category == "Cadences" and s.surface == "manager"]
        assert any(s.key == "cadences.poll_seconds" for s in cadence_specs)
        assert any(s.key == "cadences.heartbeat_seconds" for s in cadence_specs)


# ─── §6.2 GET shape ──────────────────────────────────────────────────────────


class TestGetShape:
    def test_tiers_structure(self, workspace: Path, monkeypatch):
        _clear_env(monkeypatch)
        from nightshift.config.validate import build_get_response

        resp = build_get_response(workspace, ["manager", "player"])
        assert "tiers" in resp
        assert "schema" in resp
        surfaces = [t["surface"] for t in resp["tiers"]]
        assert "manager" in surfaces
        assert "player" in surfaces
        assert "worker" not in surfaces

    def test_fields_have_required_keys(self, workspace: Path, monkeypatch):
        _clear_env(monkeypatch)
        from nightshift.config.validate import build_get_response

        resp = build_get_response(workspace, ["player"])
        tier = resp["tiers"][0]
        for cat in tier["categories"]:
            for field in cat["fields"]:
                for key in ("key", "label", "desc", "type", "apply", "store",
                            "default", "secret"):
                    assert key in field, f"field {field.get('key')} missing {key}"

    def test_stored_effective_env_shadowed(self, workspace: Path, monkeypatch):
        _clear_env(monkeypatch)
        from nightshift.config.io import save_json
        from nightshift.config.validate import build_get_response

        save_json(workspace / ".nightshift" / "manager.json", {"port": 9000})
        monkeypatch.setenv("NIGHTSHIFT_MANAGER_PORT", "7777")

        resp = build_get_response(workspace, ["manager"])
        port_field = _find_field(resp, "manager", "port")
        assert port_field is not None
        assert port_field["stored"] == 9000
        assert port_field["effective"] == 7777
        assert port_field["env_shadowed"] is True

    def test_no_env_shadow_when_no_env_var(self, workspace: Path, monkeypatch):
        _clear_env(monkeypatch)
        from nightshift.config.io import save_json
        from nightshift.config.validate import build_get_response

        save_json(workspace / ".nightshift" / "manager.json", {"port": 9000})
        resp = build_get_response(workspace, ["manager"])
        port_field = _find_field(resp, "manager", "port")
        assert port_field["stored"] == 9000
        assert port_field["effective"] == 9000
        assert port_field["env_shadowed"] is False

    def test_secrets_never_return_value(self, workspace: Path, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("NIGHTSHIFT_SHARED_SECRET", "s3cret")
        from nightshift.config.validate import build_get_response

        resp = build_get_response(workspace, ["manager"])
        secret_field = _find_field(resp, "manager", "shared_secret")
        assert secret_field is not None
        assert secret_field["stored"] is None
        assert secret_field["effective"] is None
        assert secret_field["is_set"] is True
        assert secret_field["secret"] is True

    def test_secret_not_set(self, workspace: Path, monkeypatch):
        _clear_env(monkeypatch)
        from nightshift.config.validate import build_get_response

        resp = build_get_response(workspace, ["manager"])
        secret_field = _find_field(resp, "manager", "shared_secret")
        assert secret_field["is_set"] is False

    def test_default_used_when_no_file_value(self, workspace: Path, monkeypatch):
        _clear_env(monkeypatch)
        from nightshift.config.validate import build_get_response

        resp = build_get_response(workspace, ["player"])
        theme_field = _find_field(resp, "player", "theme")
        assert theme_field["stored"] == "dark"
        assert theme_field["effective"] == "dark"


# ─── §6.3 PUT validation ─────────────────────────────────────────────────────


class TestPutValidation:
    def test_bad_enum_rejected(self):
        from nightshift.config.validate import validate_delta

        delta = {"player": {"theme": "rainbow"}}
        _, errors = validate_delta(delta, {"player"})
        assert "player.theme" in errors

    def test_bad_int_rejected(self):
        from nightshift.config.validate import validate_delta

        delta = {"manager": {"port": "not-a-number"}}
        _, errors = validate_delta(delta, {"manager"})
        assert "manager.port" in errors

    def test_bad_duration_rejected(self):
        from nightshift.config.validate import validate_delta

        delta = {"player": {"repeat_interval": "xyz"}}
        _, errors = validate_delta(delta, {"player"})
        assert "player.repeat_interval" in errors

    def test_si_int_accepts_plain_int(self):
        from nightshift.config.validate import validate_delta

        delta = {"worker": {"nightshift.max_tokens": 8192}}
        resolved, errors = validate_delta(delta, {"worker"})
        assert not errors
        assert resolved["worker"]["nightshift.max_tokens"] == 8192

    def test_si_int_accepts_k_suffix(self):
        from nightshift.config.validate import validate_delta

        delta = {"worker": {"nightshift.max_tokens": "16k"}}
        resolved, errors = validate_delta(delta, {"worker"})
        assert not errors
        assert resolved["worker"]["nightshift.max_tokens"] == 16 * 1024

    def test_si_int_accepts_K_suffix(self):
        from nightshift.config.validate import validate_delta

        delta = {"worker": {"nightshift.max_tokens": "16K"}}
        resolved, errors = validate_delta(delta, {"worker"})
        assert not errors
        assert resolved["worker"]["nightshift.max_tokens"] == 16 * 1024

    def test_si_int_accepts_Mi_suffix(self):
        from nightshift.config.validate import validate_delta

        delta = {"worker": {"nightshift.max_tokens": "1Mi"}}
        resolved, errors = validate_delta(delta, {"worker"})
        assert not errors
        assert resolved["worker"]["nightshift.max_tokens"] == 1024 ** 2

    def test_si_int_accepts_Gi_suffix(self):
        from nightshift.config.validate import validate_delta

        delta = {"worker": {"nightshift.max_tokens": "2Gi"}}
        resolved, errors = validate_delta(delta, {"worker"})
        assert not errors
        assert resolved["worker"]["nightshift.max_tokens"] == 2 * 1024 ** 3

    def test_si_int_accepts_Ti_suffix(self):
        from nightshift.config.validate import validate_delta

        delta = {"worker": {"nightshift.max_tokens": "1Ti"}}
        resolved, errors = validate_delta(delta, {"worker"})
        assert not errors
        assert resolved["worker"]["nightshift.max_tokens"] == 1024 ** 4

    def test_bad_si_int_rejected(self):
        from nightshift.config.validate import validate_delta

        delta = {"worker": {"nightshift.max_tokens": "garbage"}}
        _, errors = validate_delta(delta, {"worker"})
        assert "worker.nightshift.max_tokens" in errors

    def test_si_int_rejects_bool(self):
        from nightshift.config.validate import validate_delta

        delta = {"worker": {"nightshift.max_tokens": True}}
        _, errors = validate_delta(delta, {"worker"})
        assert "worker.nightshift.max_tokens" in errors

    def test_bad_regex_rejected(self):
        from nightshift.config.validate import validate_delta

        delta = {"manager": {"forbidden_paths": ["[invalid"]}}
        _, errors = validate_delta(delta, {"manager"})
        assert "manager.forbidden_paths" in errors

    def test_bad_str_map_rejected(self):
        from nightshift.config.validate import validate_delta

        delta = {"worker": {"model_aliases": {"key": 123}}}
        _, errors = validate_delta(delta, {"worker"})
        assert "worker.model_aliases" in errors

    def test_unknown_key_rejected(self):
        from nightshift.config.validate import validate_delta

        delta = {"manager": {"nonexistent_field": 42}}
        _, errors = validate_delta(delta, {"manager"})
        assert "manager.nonexistent_field" in errors

    def test_unknown_surface_rejected(self):
        from nightshift.config.validate import validate_delta

        delta = {"bogus": {"key": "val"}}
        _, errors = validate_delta(delta, {"manager"})
        assert "bogus" in errors

    def test_valid_delta_passes(self):
        from nightshift.config.validate import validate_delta

        delta = {"player": {"theme": "light", "transport_mode": "repeat"}}
        resolved, errors = validate_delta(delta, {"player"})
        assert not errors
        assert resolved["player"]["theme"] == "light"

    def test_nothing_written_on_error(self, workspace: Path):
        from nightshift.config.io import save_json
        from nightshift.config.validate import validate_delta

        save_json(workspace / ".nightshift" / "player.json", {"theme": "dark"})
        delta = {"player": {"theme": "rainbow", "transport_mode": "repeat"}}
        _, errors = validate_delta(delta, {"player"})
        assert errors
        raw = json.loads((workspace / ".nightshift" / "player.json").read_text())
        assert raw["theme"] == "dark"

    def test_valid_delta_writes_only_touched_files(self, workspace: Path, monkeypatch):
        _clear_env(monkeypatch)
        from nightshift.config.io import save_json
        from nightshift.config.validate import validate_delta, write_delta

        save_json(workspace / ".nightshift" / "player.json", {"theme": "dark"})
        save_json(workspace / ".nightshift" / "manager.json", {"port": 8800})

        delta = {"player": {"theme": "light"}}
        resolved, errors = validate_delta(delta, {"player", "manager"})
        assert not errors
        write_delta(workspace, resolved)

        player = json.loads((workspace / ".nightshift" / "player.json").read_text())
        assert player["theme"] == "light"
        manager = json.loads((workspace / ".nightshift" / "manager.json").read_text())
        assert manager["port"] == 8800

    def test_sibling_keys_preserved(self, workspace: Path, monkeypatch):
        _clear_env(monkeypatch)
        from nightshift.config.io import save_json
        from nightshift.config.validate import validate_delta, write_delta

        save_json(workspace / ".nightshift" / "manager.json", {
            "port": 8800,
            "host": "0.0.0.0",
            "max_per_day": 100,
        })
        delta = {"manager": {"max_per_day": 300}}
        resolved, _ = validate_delta(delta, {"manager"})
        write_delta(workspace, resolved)

        raw = json.loads((workspace / ".nightshift" / "manager.json").read_text())
        assert raw["max_per_day"] == 300
        assert raw["port"] == 8800
        assert raw["host"] == "0.0.0.0"

    def test_nested_cadences_write(self, workspace: Path, monkeypatch):
        _clear_env(monkeypatch)
        from nightshift.config.io import save_json
        from nightshift.config.validate import validate_delta, write_delta

        save_json(workspace / ".nightshift" / "manager.json", {
            "cadences": {"poll_seconds": 5.0, "refresh_ms": 20000},
        })
        delta = {"manager": {"cadences.poll_seconds": 3.0}}
        resolved, _ = validate_delta(delta, {"manager"})
        write_delta(workspace, resolved)

        raw = json.loads((workspace / ".nightshift" / "manager.json").read_text())
        assert raw["cadences"]["poll_seconds"] == 3.0
        assert raw["cadences"]["refresh_ms"] == 20000

    def test_bool_true_not_accepted_as_int(self):
        from nightshift.config.validate import validate_delta

        delta = {"manager": {"port": True}}
        _, errors = validate_delta(delta, {"manager"})
        assert "manager.port" in errors


# ─── §6.4 Secret routing ─────────────────────────────────────────────────────


class TestSecretRouting:
    def test_secret_writes_to_dotenv(self, workspace: Path, monkeypatch):
        _clear_env(monkeypatch)
        from nightshift.config.validate import validate_delta, write_delta

        (workspace / ".env").write_text("# existing\n")
        delta = {"manager": {"shared_secret": "new-secret"}}
        resolved, errors = validate_delta(delta, {"manager"})
        assert not errors
        write_delta(workspace, resolved)

        env_content = (workspace / ".env").read_text()
        assert "NIGHTSHIFT_SHARED_SECRET=new-secret" in env_content

        manager_path = workspace / ".nightshift" / "manager.json"
        if manager_path.exists():
            raw = json.loads(manager_path.read_text())
            assert "shared_secret" not in raw
            assert "new-secret" not in json.dumps(raw)

    def test_get_reflects_is_set_after_write(self, workspace: Path, monkeypatch):
        _clear_env(monkeypatch)
        from nightshift.config.validate import (
            build_get_response,
        )

        monkeypatch.setenv("NIGHTSHIFT_SHARED_SECRET", "test-secret")
        resp = build_get_response(workspace, ["manager"])
        secret_field = _find_field(resp, "manager", "shared_secret")
        assert secret_field["is_set"] is True


# ─── §6.5 Apply reporting ────────────────────────────────────────────────────


class TestApplyReporting:
    def test_live_field_in_applied_live(self, workspace: Path, monkeypatch):
        _clear_env(monkeypatch)
        from nightshift.config.io import save_json
        from nightshift.config.validate import validate_delta, write_delta

        save_json(workspace / ".nightshift" / "player.json", {})
        delta = {"player": {"theme": "light"}}
        resolved, _ = validate_delta(delta, {"player"})
        applied_live, restart_required = write_delta(workspace, resolved)
        assert "player.theme" in applied_live
        assert "player.theme" not in restart_required

    def test_restart_field_in_restart_required(self, workspace: Path, monkeypatch):
        _clear_env(monkeypatch)
        from nightshift.config.io import save_json
        from nightshift.config.validate import validate_delta, write_delta

        save_json(workspace / ".nightshift" / "manager.json", {})
        delta = {"manager": {"cadences.poll_seconds": 3.0}}
        resolved, _ = validate_delta(delta, {"manager"})
        applied_live, restart_required = write_delta(workspace, resolved)
        assert "manager.cadences.poll_seconds" in restart_required
        assert "manager.cadences.poll_seconds" not in applied_live

    def test_worker_fields_always_restart(self, workspace: Path, monkeypatch):
        _clear_env(monkeypatch)
        from nightshift.config.io import save_json
        from nightshift.config.validate import validate_delta, write_delta

        save_json(workspace / ".nightshift" / "worker.json", {})
        delta = {"worker": {"ui_port": 9999}}
        resolved, _ = validate_delta(delta, {"worker"})
        applied_live, restart_required = write_delta(workspace, resolved)
        assert "worker.ui_port" in restart_required
        assert not applied_live


# ─── §6.6 JSON-schema ────────────────────────────────────────────────────────


class TestJsonSchema:
    def test_schema_structure(self):
        from nightshift.config.registry import emit_json_schema

        schema = emit_json_schema()
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert "properties" in schema
        props = schema["properties"]
        assert "player.theme" in props
        assert props["player.theme"]["type"] == "string"
        assert "enum" in props["player.theme"]
        assert "manager.port" in props
        assert props["manager.port"]["type"] == "integer"

    def test_schema_covers_all_registry_fields(self):
        from nightshift.config.registry import build_registry, emit_json_schema

        specs = build_registry()
        schema = emit_json_schema(specs)
        for spec in specs:
            full_key = f"{spec.surface}.{spec.key}"
            assert full_key in schema["properties"], f"schema missing {full_key}"


# ─── §6.7 Per-surface scoping ────────────────────────────────────────────────


class TestPerSurfaceScoping:
    def test_worker_endpoint_only_worker(self, workspace: Path, monkeypatch):
        _clear_env(monkeypatch)
        from nightshift.config.validate import build_get_response

        resp = build_get_response(workspace, ["worker"])
        assert len(resp["tiers"]) == 1
        assert resp["tiers"][0]["surface"] == "worker"
        for cat in resp["tiers"][0]["categories"]:
            for f in cat["fields"]:
                assert f["store"] in ("worker.json", ".env"), (
                    f"worker surface field {f['key']} has wrong store {f['store']}"
                )

    def test_manager_endpoint_no_worker_fields(self, workspace: Path, monkeypatch):
        _clear_env(monkeypatch)
        from nightshift.config.validate import build_get_response

        resp = build_get_response(workspace, ["manager", "player"])
        surfaces = {t["surface"] for t in resp["tiers"]}
        assert "worker" not in surfaces

    def test_server_endpoint_all_surfaces(self, workspace: Path, monkeypatch):
        _clear_env(monkeypatch)
        from nightshift.config.validate import build_get_response

        resp = build_get_response(workspace, ["manager", "worker", "player"])
        surfaces = {t["surface"] for t in resp["tiers"]}
        assert surfaces == {"manager", "worker", "player"}

    def test_put_rejected_for_disallowed_surface(self):
        from nightshift.config.validate import validate_delta

        delta = {"worker": {"ui_port": 9999}}
        _, errors = validate_delta(delta, {"manager", "player"})
        assert "worker" in errors

    def test_put_accepted_for_allowed_surface(self):
        from nightshift.config.validate import validate_delta

        delta = {"worker": {"ui_port": 9999}}
        resolved, errors = validate_delta(delta, {"worker"})
        assert not errors
        assert resolved["worker"]["ui_port"] == 9999


# ─── helpers ──────────────────────────────────────────────────────────────────


_ENV_VARS = [
    "NIGHTSHIFT_PG_DSN", "NIGHTSHIFT_SHARED_SECRET",
    "NIGHTSHIFT_MANAGER_HOST", "NIGHTSHIFT_MANAGER_PORT",
    "NIGHTSHIFT_LANDING_MODE", "NIGHTSHIFT_DEFAULT_MODEL",
    "NIGHTSHIFT_TASKS_REPO", "NIGHTSHIFT_WIP_REF_PREFIX",
    "NIGHTSHIFT_RENDEZVOUS_REMOTE", "NIGHTSHIFT_MAX_PER_DAY",
    "NIGHTSHIFT_WORKER_BACKEND", "NIGHTSHIFT_WORKER_ID",
    "NIGHTSHIFT_MANAGER_URL", "NIGHTSHIFT_WORKER_QUEUES",
    "NIGHTSHIFT_WORKER_PRIORITIES", "NIGHTSHIFT_WORKER_MODELS",
    "NIGHTSHIFT_WORKER_MCPS", "NIGHTSHIFT_WORKER_UI_HOST",
    "NIGHTSHIFT_WORKER_UI_PORT",
]


def _clear_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _find_field(
    resp: dict[str, Any], surface: str, key: str,
) -> dict[str, Any] | None:
    for tier in resp["tiers"]:
        if tier["surface"] != surface:
            continue
        for cat in tier["categories"]:
            for field in cat["fields"]:
                if field["key"] == key:
                    return field
    return None
