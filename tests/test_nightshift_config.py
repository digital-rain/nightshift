"""Policy guards on the shipped manager config template.

The repo's example manager config is the ``nightshift init`` template
(``src/nightshift/assets/config/manager.json``) — the legacy root
``config.json`` was deleted with the ``.nightshift/*.json`` migration.
These tests pin the policy choices the template must keep.
"""

from __future__ import annotations

import json
from pathlib import Path

from nightshift.config.manager import ManagerSettings
from nightshift.queue_config import ci_monitoring_enabled, set_ci_monitoring


ROOT = Path(__file__).resolve().parents[1]
MANAGER_TEMPLATE = ROOT / "src" / "nightshift" / "assets" / "config" / "manager.json"


def test_template_forbidden_paths_exclude_templates_dir() -> None:
    config = json.loads(MANAGER_TEMPLATE.read_text())
    joined = "|".join(config["forbidden_paths"])
    assert "^tools/nightshift/templates/" not in joined
    assert joined != "^tools/nightshift/"
    assert "forbidden_template_paths" in config


def test_template_diff_cap_exempts_prose_paths() -> None:
    exempt = json.loads(MANAGER_TEMPLATE.read_text())["diff_cap_exempt_paths"]
    assert "^tests/fixtures/" in exempt
    assert "\\.md$" in exempt
    assert "^docs/" in exempt


def test_template_automerge_defaults_off() -> None:
    # Without a mechanical guard on GitHub, a human is the merge gate: a task
    # auto-merges only when its frontmatter opts in.
    config = json.loads(MANAGER_TEMPLATE.read_text())
    assert config["automerge"] is False


def test_template_scheduled_models_allow_filter() -> None:
    # The scheduled_models_allow filter lists the model ids the manager may
    # auto-schedule (UI dropdown is populated from live worker registrations).
    config = json.loads(MANAGER_TEMPLATE.read_text())
    assert isinstance(config["scheduled_models_allow"], list)
    assert config["scheduled_models_allow"]
    assert "diff_cap_lines" in config


def test_ci_monitoring_defaults_off() -> None:
    assert ci_monitoring_enabled({}) is False
    assert ci_monitoring_enabled({"validate": "just validate"}) is False


def test_ci_monitoring_reads_the_flag() -> None:
    assert ci_monitoring_enabled({"ci_monitoring": True}) is True
    assert ci_monitoring_enabled({"ci_monitoring": False}) is False


def test_ci_refresh_cadence_default() -> None:
    assert ManagerSettings().cadences.ci_refresh_seconds == 120.0


def test_set_ci_monitoring_creates_a_missing_queue_dir(tmp_path: Path) -> None:
    """The switch endpoint must not 500 on a queue whose dir is not there yet."""
    set_ci_monitoring(tmp_path, "newqueue", True)
    written = json.loads((tmp_path / "newqueue" / "config.json").read_text())
    assert written["ci_monitoring"] is True
    assert ci_monitoring_enabled(written) is True


def test_set_ci_monitoring_survives_a_malformed_config(tmp_path: Path) -> None:
    """A corrupt config.json degrades to {} rather than raising into the handler."""
    queue = tmp_path / "main"
    queue.mkdir()
    (queue / "config.json").write_text("{not json at all")
    set_ci_monitoring(tmp_path, "main", True)
    assert json.loads((queue / "config.json").read_text())["ci_monitoring"] is True


def test_set_ci_monitoring_preserves_sibling_keys(tmp_path: Path) -> None:
    queue = tmp_path / "main"
    queue.mkdir()
    (queue / "config.json").write_text(json.dumps({"repo": "longitude", "validate": "just validate"}))
    set_ci_monitoring(tmp_path, "main", True)
    written = json.loads((queue / "config.json").read_text())
    assert written["repo"] == "longitude"
    assert written["validate"] == "just validate"
    assert written["ci_monitoring"] is True
