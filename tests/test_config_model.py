"""Tests for the unified config model (spec §8).

1. Defaults-drift guard — dataclass defaults match the old inline defaults.
2. Metadata completeness — assert_complete passes; every editable field has
   unique (surface, json-path) and a known apply/type.
3. Load/save round-trip per file.
4. Secrets isolation — JSON files never contain secrets; .env writer upserts.
5. Apply classification — representative set against declared apply.
6. No legacy paths remain — nothing reads config.json / config.json.local /
   .nightshift/settings.json.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Workspace with .nightshift/ and .env."""
    ns = tmp_path / ".nightshift"
    ns.mkdir()
    return tmp_path


# ─── §8.1 Defaults-drift guard ───────────────────────────────────────────────


class TestDefaultsDriftGuard:
    """Assert each dataclass default equals the value the old inline defaults used."""

    def test_operator_max_per_day(self):
        from nightshift.config.manager import OperatorConfig
        assert OperatorConfig().max_per_day == 200

    def test_operator_automerge_is_false(self):
        """The automerge default was inconsistent (True vs false); standardized to False."""
        from nightshift.config.manager import OperatorConfig
        assert OperatorConfig().automerge is False

    def test_operator_draft(self):
        from nightshift.config.manager import OperatorConfig
        assert OperatorConfig().draft is False

    def test_operator_max_fix_attempts(self):
        from nightshift.config.manager import OperatorConfig
        assert OperatorConfig().max_fix_attempts == 6

    def test_operator_diff_cap_lines(self):
        from nightshift.config.manager import OperatorConfig
        assert OperatorConfig().diff_cap_lines == 1500

    def test_operator_auto_resolve(self):
        from nightshift.config.manager import OperatorConfig
        assert OperatorConfig().auto_resolve is True

    def test_operator_max_resolve_attempts(self):
        from nightshift.config.manager import OperatorConfig
        assert OperatorConfig().max_resolve_attempts == 2

    def test_operator_landing_mode(self):
        from nightshift.config.manager import OperatorConfig
        assert OperatorConfig().landing_mode == "none"

    def test_operator_tasks_repo(self):
        from nightshift.config.manager import OperatorConfig
        assert OperatorConfig().tasks_repo == "nightshift-tasks"

    def test_operator_wip_ref_prefix(self):
        from nightshift.config.manager import OperatorConfig
        assert OperatorConfig().wip_ref_prefix == "nightshift-wip"

    def test_operator_default_model(self):
        from nightshift.config.manager import OperatorConfig
        assert OperatorConfig().default_model == "auto"

    def test_manager_host(self):
        from nightshift.config.manager import ManagerSettings
        assert ManagerSettings().host == "0.0.0.0"

    def test_manager_port(self):
        from nightshift.config.manager import ManagerSettings
        assert ManagerSettings().port == 8800

    def test_cadences_poll(self):
        from nightshift.config.manager import Cadences
        assert Cadences().poll_seconds == 5.0

    def test_cadences_heartbeat(self):
        from nightshift.config.manager import Cadences
        assert Cadences().heartbeat_seconds == 10.0

    def test_cadences_lease_ttl(self):
        from nightshift.config.manager import Cadences
        assert Cadences().lease_ttl_seconds == 120.0

    def test_cadences_worker_stale(self):
        from nightshift.config.manager import Cadences
        assert Cadences().worker_stale_seconds == 45.0

    def test_cadences_refresh_ms(self):
        from nightshift.config.manager import Cadences
        assert Cadences().refresh_ms == 20000

    def test_worker_auto_model(self):
        from nightshift.config.worker import WorkerConfig
        assert WorkerConfig(workspace=Path(".")).auto_model == "claude-code/claude-sonnet-4-6"

    def test_worker_manager_url(self):
        from nightshift.config.worker import WorkerConfig
        assert WorkerConfig(workspace=Path(".")).manager_url == "http://localhost:8800"

    def test_worker_ui_port(self):
        from nightshift.config.worker import WorkerConfig
        assert WorkerConfig(workspace=Path(".")).ui_port == 8810

    def test_player_theme(self):
        from nightshift.config.player import PlayerConfig
        assert PlayerConfig().theme == "dark"

    def test_player_transport_mode(self):
        from nightshift.config.player import PlayerConfig
        assert PlayerConfig().transport_mode == "auto"

    def test_player_repeat_interval(self):
        from nightshift.config.player import PlayerConfig
        assert PlayerConfig().repeat_interval == "30m"


# ─── §8.2 Metadata completeness ──────────────────────────────────────────────


class TestMetadataCompleteness:
    """assert_complete runs at import and no AssertionError."""

    def test_import_succeeds(self):
        import nightshift.config  # noqa: F401

    def test_all_editable_fields_have_unique_paths(self):
        from nightshift.config.manager import Cadences, ManagerSettings, OperatorConfig
        from nightshift.config.player import PlayerConfig
        from nightshift.config.worker import WorkerConfig

        seen: set[tuple[str, str]] = set()
        for surface_name, dc in [
            ("manager", ManagerSettings),
            ("manager.cadences", Cadences),
            ("manager.operator", OperatorConfig),
            ("worker", WorkerConfig),
            ("player", PlayerConfig),
        ]:
            for f in dataclasses.fields(dc):
                m = f.metadata.get("nightshift")
                if m is None or m.get("editable") is False:
                    continue
                path = (surface_name, f.name)
                assert path not in seen, f"Duplicate (surface, field): {path}"
                seen.add(path)

    def test_all_editable_fields_have_known_apply(self):
        from nightshift.config.manager import Cadences, ManagerSettings, OperatorConfig
        from nightshift.config.player import PlayerConfig
        from nightshift.config.worker import WorkerConfig

        valid_apply = {"live", "next-task", "restart"}
        for dc in [ManagerSettings, Cadences, OperatorConfig, WorkerConfig, PlayerConfig]:
            for f in dataclasses.fields(dc):
                m = f.metadata.get("nightshift")
                if m is None or m.get("editable") is False:
                    continue
                assert m.get("apply") in valid_apply, (
                    f"{dc.__name__}.{f.name} has invalid apply: {m.get('apply')}"
                )


# ─── §8.3 Load/save round-trip ───────────────────────────────────────────────


class TestRoundTrip:
    def test_manager_round_trip(self, workspace: Path, monkeypatch):
        monkeypatch.delenv("NIGHTSHIFT_PG_DSN", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_SHARED_SECRET", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_MANAGER_HOST", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_MANAGER_PORT", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_LANDING_MODE", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_DEFAULT_MODEL", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_TASKS_REPO", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_WIP_REF_PREFIX", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_RENDEZVOUS_REMOTE", raising=False)

        from nightshift.config.manager import (
            ManagerSettings,
            load_manager_settings,
            save_manager_settings,
        )

        original = ManagerSettings()
        save_manager_settings(workspace, original)
        loaded = load_manager_settings(workspace)

        assert loaded.host == original.host
        assert loaded.port == original.port
        assert loaded.cadences.poll_seconds == original.cadences.poll_seconds
        assert loaded.cadences.refresh_ms == original.cadences.refresh_ms
        assert loaded.operator.max_per_day == original.operator.max_per_day
        assert loaded.operator.automerge == original.operator.automerge

    def test_worker_round_trip(self, workspace: Path, monkeypatch):
        monkeypatch.delenv("NIGHTSHIFT_WORKER_BACKEND", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_WORKER_ID", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_MANAGER_URL", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_SHARED_SECRET", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_RENDEZVOUS_REMOTE", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_WORKER_QUEUES", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_WORKER_PRIORITIES", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_WORKER_MODELS", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_WORKER_MCPS", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_WORKER_UI_HOST", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_WORKER_UI_PORT", raising=False)

        from nightshift.config.worker import (
            WorkerConfig,
            load_worker_config,
            save_worker_config,
        )

        original = WorkerConfig(
            workspace=workspace,
            worker_id="test-1",
            models=["gemini/gemini-2.5-pro"],
            auto_model="gemini/gemini-2.5-flash",
            max_model="gemini/gemini-2.5-pro",
        )
        save_worker_config(workspace, original)
        loaded = load_worker_config(workspace)

        assert loaded.worker_id == "test-1"
        assert loaded.models == ["gemini/gemini-2.5-pro"]
        assert loaded.auto_model == "gemini/gemini-2.5-flash"
        assert loaded.ui_port == 8810

    def test_player_round_trip(self, workspace: Path):
        from nightshift.config.player import (
            PlayerConfig,
            load_player_config,
            save_player_config,
        )

        original = PlayerConfig(theme="light", transport_mode="repeat",
                                repeat_interval="1h")
        save_player_config(workspace, original)
        loaded = load_player_config(workspace)

        assert loaded.theme == "light"
        assert loaded.transport_mode == "repeat"
        assert loaded.repeat_interval == "1h"

    def test_cadences_nested_in_manager_json(self, workspace: Path, monkeypatch):
        monkeypatch.delenv("NIGHTSHIFT_PG_DSN", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_SHARED_SECRET", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_MANAGER_HOST", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_MANAGER_PORT", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_LANDING_MODE", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_DEFAULT_MODEL", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_TASKS_REPO", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_WIP_REF_PREFIX", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_RENDEZVOUS_REMOTE", raising=False)

        from nightshift.config.manager import (
            Cadences,
            ManagerSettings,
            save_manager_settings,
        )

        settings = ManagerSettings(cadences=Cadences(poll_seconds=3.0, refresh_ms=5000))
        save_manager_settings(workspace, settings)

        raw = json.loads((workspace / ".nightshift" / "manager.json").read_text())
        assert raw["cadences"]["poll_seconds"] == 3.0
        assert raw["cadences"]["refresh_ms"] == 5000

    def test_env_overrides_file_value(self, workspace: Path, monkeypatch):
        monkeypatch.delenv("NIGHTSHIFT_PG_DSN", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_SHARED_SECRET", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_LANDING_MODE", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_DEFAULT_MODEL", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_TASKS_REPO", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_WIP_REF_PREFIX", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_RENDEZVOUS_REMOTE", raising=False)

        from nightshift.config.io import save_json
        from nightshift.config.manager import load_manager_settings

        save_json(workspace / ".nightshift" / "manager.json", {"port": 9000})
        monkeypatch.setenv("NIGHTSHIFT_MANAGER_PORT", "7777")

        loaded = load_manager_settings(workspace)
        assert loaded.port == 7777


# ─── §8.4 Secrets isolation ──────────────────────────────────────────────────


class TestSecretsIsolation:
    def test_manager_json_never_contains_secrets(self, workspace: Path, monkeypatch):
        monkeypatch.setenv("NIGHTSHIFT_SHARED_SECRET", "s3cret")
        monkeypatch.setenv("NIGHTSHIFT_PG_DSN", "postgresql://x")
        monkeypatch.delenv("NIGHTSHIFT_MANAGER_HOST", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_MANAGER_PORT", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_LANDING_MODE", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_DEFAULT_MODEL", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_TASKS_REPO", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_WIP_REF_PREFIX", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_RENDEZVOUS_REMOTE", raising=False)

        from nightshift.config.manager import (
            ManagerSettings,
            save_manager_settings,
        )

        settings = ManagerSettings(shared_secret="s3cret", dsn="postgresql://x")
        save_manager_settings(workspace, settings)

        raw = json.loads((workspace / ".nightshift" / "manager.json").read_text())
        assert "shared_secret" not in raw
        assert "dsn" not in raw
        assert "s3cret" not in json.dumps(raw)

    def test_worker_json_never_contains_secrets(self, workspace: Path, monkeypatch):
        monkeypatch.delenv("NIGHTSHIFT_SHARED_SECRET", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_WORKER_BACKEND", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_WORKER_ID", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_MANAGER_URL", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_RENDEZVOUS_REMOTE", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_WORKER_QUEUES", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_WORKER_PRIORITIES", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_WORKER_MODELS", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_WORKER_MCPS", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_WORKER_UI_HOST", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_WORKER_UI_PORT", raising=False)

        from nightshift.config.worker import WorkerConfig, save_worker_config

        cfg = WorkerConfig(workspace=workspace, shared_secret="s3cret")
        save_worker_config(workspace, cfg)

        raw = json.loads((workspace / ".nightshift" / "worker.json").read_text())
        assert "shared_secret" not in raw
        assert "s3cret" not in json.dumps(raw)

    def test_dotenv_writer_upserts_key(self, workspace: Path):
        from nightshift.config.io import save_dotenv_key

        (workspace / ".env").write_text("# comment\nEXISTING=val\n")
        save_dotenv_key(workspace, "NEW_KEY", "new_val")

        content = (workspace / ".env").read_text()
        assert "# comment" in content
        assert "EXISTING=val" in content
        assert "NEW_KEY=new_val" in content

    def test_dotenv_writer_updates_existing_key(self, workspace: Path):
        from nightshift.config.io import save_dotenv_key

        (workspace / ".env").write_text("KEY=old\nOTHER=keep\n")
        save_dotenv_key(workspace, "KEY", "new")

        content = (workspace / ".env").read_text()
        assert "KEY=new" in content
        assert "KEY=old" not in content
        assert "OTHER=keep" in content

    def test_secret_resolves_from_env(self, workspace: Path, monkeypatch):
        monkeypatch.setenv("NIGHTSHIFT_SHARED_SECRET", "from-env")
        monkeypatch.delenv("NIGHTSHIFT_PG_DSN", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_MANAGER_HOST", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_MANAGER_PORT", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_LANDING_MODE", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_DEFAULT_MODEL", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_TASKS_REPO", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_WIP_REF_PREFIX", raising=False)
        monkeypatch.delenv("NIGHTSHIFT_RENDEZVOUS_REMOTE", raising=False)

        from nightshift.config.io import save_json
        from nightshift.config.manager import load_manager_settings

        save_json(workspace / ".nightshift" / "manager.json", {})
        loaded = load_manager_settings(workspace)
        assert loaded.shared_secret == "from-env"


# ─── §8.5 Apply classification ───────────────────────────────────────────────


class TestApplyClassification:
    """Verify representative fields against their declared apply value."""

    def test_player_theme_is_live(self):
        from nightshift.config.player import PlayerConfig
        f = next(f for f in dataclasses.fields(PlayerConfig) if f.name == "theme")
        assert f.metadata["nightshift"]["apply"] == "live"

    def test_operator_max_per_day_is_next_task(self):
        from nightshift.config.manager import OperatorConfig
        f = next(f for f in dataclasses.fields(OperatorConfig) if f.name == "max_per_day")
        assert f.metadata["nightshift"]["apply"] == "next-task"

    def test_manager_host_is_restart(self):
        from nightshift.config.manager import ManagerSettings
        f = next(f for f in dataclasses.fields(ManagerSettings) if f.name == "host")
        assert f.metadata["nightshift"]["apply"] == "restart"

    def test_cadences_poll_is_restart(self):
        from nightshift.config.manager import Cadences
        f = next(f for f in dataclasses.fields(Cadences) if f.name == "poll_seconds")
        assert f.metadata["nightshift"]["apply"] == "restart"

    def test_worker_model_timeout_is_restart(self):
        from nightshift.config.worker import WorkerConfig
        f = next(f for f in dataclasses.fields(WorkerConfig) if f.name == "model_timeout_seconds")
        assert f.metadata["nightshift"]["apply"] == "restart"


# ─── §8.6 No legacy paths remain ─────────────────────────────────────────────


class TestNoLegacyPaths:
    """Nothing reads config.json / config.json.local / .nightshift/settings.json."""

    def test_spawn_daily_reads_new_path(self):
        """load_config reads .nightshift/manager.json, not config.json."""
        import inspect

        from nightshift.spawn_daily import load_config

        source = inspect.getsource(load_config)
        assert ".nightshift" in source or "manager.json" in source
        assert 'workspace / "config.json"' not in source.replace(
            ".nightshift", ""
        ).replace("manager.json", "")

    def test_settings_reads_player_json(self):
        """Operator UI preferences live at .nightshift/player.json, not the
        legacy .nightshift/settings.json."""
        from pathlib import Path

        from nightshift.config.io import player_json_path
        ws = Path("/fake")
        assert player_json_path(ws) == ws / ".nightshift" / "player.json"
