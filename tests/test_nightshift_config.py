from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NIGHTSHIFT_CONFIG = ROOT / "config.json"


def test_nightshift_forbidden_paths_exclude_templates_dir() -> None:
    config = json.loads(NIGHTSHIFT_CONFIG.read_text())
    joined = "|".join(config["forbidden_paths"])
    assert "^tools/nightshift/templates/" not in joined
    assert joined != "^tools/nightshift/"
    assert "forbidden_template_paths" in config


def test_nightshift_diff_cap_exempts_prose_and_task_queue_paths() -> None:
    exempt = json.loads(NIGHTSHIFT_CONFIG.read_text())["diff_cap_exempt_paths"]
    assert "^tests/fixtures/" in exempt
    assert "^\\.tasks/" in exempt
    assert "\\.md$" in exempt
    assert "^docs/" in exempt


def test_nightshift_automerge_defaults_off() -> None:
    # Without a mechanical guard on GitHub, a human is the merge gate: a task
    # auto-merges only when its frontmatter opts in.
    config = json.loads(NIGHTSHIFT_CONFIG.read_text())
    assert config["automerge"] is False


def test_nightshift_scheduled_models_pin_only_allow_set() -> None:
    # The scheduled_models allow-set survived extraction and still lists the
    # pinnable model ids the manager may schedule.
    config = json.loads(NIGHTSHIFT_CONFIG.read_text())
    assert isinstance(config["scheduled_models"], list)
    assert config["scheduled_models"]
    assert "diff_cap_lines" in config
