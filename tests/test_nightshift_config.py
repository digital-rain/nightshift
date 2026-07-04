"""Policy guards on the shipped manager config template.

The repo's example manager config is the ``nightshift init`` template
(``src/nightshift/assets/config/manager.json``) — the legacy root
``config.json`` was deleted with the ``.nightshift/*.json`` migration.
These tests pin the policy choices the template must keep.
"""

from __future__ import annotations

import json
from pathlib import Path


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
