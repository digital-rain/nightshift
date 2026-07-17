"""Phase 5 — manager scheduling + work orders for workflow tasks.

Covers ``build_candidates`` step-model resolution (via a fake resolver + the
real ``make_resolver``), ``WorkerFilter`` routing on the step model, unroutable
reporting the step model, and ``build_work_order``'s workflow block + artifact
embedding + max_turns override, plus byte-identity for non-workflow orders.
"""

from __future__ import annotations

from pathlib import Path

from _workspace import build_workspace
from nightshift.config.manager import ManagerConfig
from nightshift.manager.scheduler import (
    WorkerFilter,
    build_candidates,
    unroutable,
)
from nightshift.manager.work_orders import build_work_order
from nightshift.repos import DEFAULT_TASKS_REPO
from nightshift.task_files import write_artifact
from nightshift.workflows import load_workflows, make_resolver


REPO = "longitude"


def _ws(tmp_path: Path, tasks: dict[str, str]) -> tuple[Path, Path]:
    workspace = build_workspace(tmp_path, tasks=tasks)
    return workspace, workspace / DEFAULT_TASKS_REPO


def _fake_resolver(step_model: str):
    def resolve(meta, queue_config):
        name = str(meta.get("workflow") or "")
        step = str(meta.get("workflow_step") or "") or "plan"
        return (name, step, step_model)

    return resolve


# --------------------------------------------------------------------------- #
# build_candidates
# --------------------------------------------------------------------------- #


def test_candidate_carries_step_resolved_model(tmp_path: Path) -> None:
    _ws_root, tasks_root = _ws(
        tmp_path, {"10.wf": "---\nworkflow: plan-review-implement\n---\nDo it."}
    )
    cands = build_candidates(
        tasks_root, None, default_model="auto",
        workflow_resolver=_fake_resolver("opus-planner"),
    )
    c = next(c for c in cands if c.task == "10.wf")
    assert c.model == "opus-planner"
    assert c.workflow == "plan-review-implement"
    assert c.workflow_step == "plan"


def test_worker_filter_routes_on_step_model(tmp_path: Path) -> None:
    _ws_root, tasks_root = _ws(
        tmp_path, {"10.wf": "---\nworkflow: plan-review-implement\n---\nDo it."}
    )
    cands = build_candidates(
        tasks_root, None, workflow_resolver=_fake_resolver("opus-planner"),
    )
    c = next(c for c in cands if c.task == "10.wf")
    assert WorkerFilter(worker_id="w", models=["opus-planner"]).accepts(c)
    assert not WorkerFilter(worker_id="w", models=["something-else"]).accepts(c)


def test_unroutable_reports_step_model(tmp_path: Path) -> None:
    _ws_root, tasks_root = _ws(
        tmp_path, {"10.wf": "---\nworkflow: plan-review-implement\n---\nDo it."}
    )
    cands = build_candidates(
        tasks_root, None, workflow_resolver=_fake_resolver("opus-planner"),
    )
    reasons = unroutable({None: cands}, available_models={"other"}, available_mcps=set())
    matched = [r for c, r in reasons if c.task == "10.wf"]
    assert matched and "opus-planner" in matched[0]


def test_absent_workflow_step_defaults_to_first_via_real_resolver(tmp_path: Path) -> None:
    _ws_root, tasks_root = _ws(
        tmp_path, {"10.wf": "---\nworkflow: plan-review-implement\n---\nDo it."}
    )
    defs = load_workflows(_ws_root)
    resolver = make_resolver(defs, planner_model="", default_model="auto")
    cands = build_candidates(tasks_root, None, workflow_resolver=resolver)
    c = next(c for c in cands if c.task == "10.wf")
    assert c.workflow_step == "plan"       # first step
    assert c.model == "auto"               # planner → default (empty planner_model)
    assert c.workflow_error is None


def test_unknown_definition_sets_workflow_error(tmp_path: Path) -> None:
    _ws_root, tasks_root = _ws(
        tmp_path, {"10.wf": "---\nworkflow: does-not-exist\n---\nDo it."}
    )
    defs = load_workflows(_ws_root)
    resolver = make_resolver(defs, planner_model="", default_model="auto")
    cands = build_candidates(tasks_root, None, workflow_resolver=resolver)
    c = next(c for c in cands if c.task == "10.wf")
    assert c.workflow_error is not None
    assert "unknown workflow" in c.workflow_error


def test_unknown_step_sets_workflow_error(tmp_path: Path) -> None:
    _ws_root, tasks_root = _ws(
        tmp_path,
        {"10.wf": "---\nworkflow: plan-review-implement\nworkflow_step: ghost\n---\nDo it."},
    )
    defs = load_workflows(_ws_root)
    resolver = make_resolver(defs, planner_model="", default_model="auto")
    cands = build_candidates(tasks_root, None, workflow_resolver=resolver)
    c = next(c for c in cands if c.task == "10.wf")
    assert c.workflow_error is not None
    assert "no step 'ghost'" in c.workflow_error


def test_unresolvable_role_sets_workflow_error(tmp_path: Path) -> None:
    _ws_root, tasks_root = _ws(
        tmp_path, {"10.wf": "---\nworkflow: plan-review-implement\n---\nDo it."}
    )
    defs = load_workflows(_ws_root)
    # empty default + empty planner → planner role cannot resolve
    resolver = make_resolver(defs, planner_model="", default_model="")
    cands = build_candidates(tasks_root, None, workflow_resolver=resolver)
    c = next(c for c in cands if c.task == "10.wf")
    assert c.workflow_error is not None
    assert "cannot resolve model" in c.workflow_error


# --------------------------------------------------------------------------- #
# build_work_order
# --------------------------------------------------------------------------- #


def _cfg() -> ManagerConfig:
    return ManagerConfig()


def test_work_order_embeds_workflow_block_and_artifacts(tmp_path: Path) -> None:
    ws, tasks_root = _ws(
        tmp_path,
        {"10.wf": "---\nworkflow: plan-review-implement\nworkflow_step: review\n---\nDo it."},
    )
    write_artifact(tasks_root, "10.wf", "plan", "# The Plan")
    defs = load_workflows(ws)
    order = build_work_order(
        ws, tasks_root, "10.wf", None, REPO, "l1", "r1", "HEAD", _cfg(),
        workflow_defs=defs,
    )
    wf = order["config"]["workflow"]
    assert wf["name"] == "plan-review-implement"
    assert wf["step"] == "review"
    assert wf["kind"] == "doc"
    assert wf["prompt"] == "workflow-review.md"
    assert wf["output"] == "review"
    # review's inputs are [brief, plan]; both materialized into artifacts
    assert wf["artifacts"]["plan"].startswith("# The Plan")
    assert "brief" in wf["artifacts"]
    # review's max_turns is 20 (override)
    assert order["config"]["max_turns"] == 20


def test_work_order_split_step_sets_split_flag(tmp_path: Path) -> None:
    ws, tasks_root = _ws(
        tmp_path,
        {"10.wf": "---\nworkflow: plan-split\nworkflow_step: split\n---\nDo it."},
    )
    write_artifact(tasks_root, "10.wf", "plan", "# The Plan")
    defs = load_workflows(ws)
    order = build_work_order(
        ws, tasks_root, "10.wf", None, REPO, "l1", "r1", "HEAD", _cfg(),
        workflow_defs=defs,
    )
    assert order["config"]["workflow"]["kind"] == "split"
    assert order["config"]["split"] is True


def test_work_order_max_turns_null_is_unbounded(tmp_path: Path) -> None:
    ws, tasks_root = _ws(
        tmp_path,
        {"10.wf": "---\nworkflow: plan-review-implement\nworkflow_step: implement\n---\nDo it."},
    )
    write_artifact(tasks_root, "10.wf", "plan", "# The Plan")
    defs = load_workflows(ws)
    order = build_work_order(
        ws, tasks_root, "10.wf", None, REPO, "l1", "r1", "HEAD", _cfg(),
        workflow_defs=defs,
    )
    # implement's max_turns is null → unbounded (None)
    assert order["config"]["max_turns"] is None


def test_non_workflow_order_unchanged(tmp_path: Path) -> None:
    ws, tasks_root = _ws(tmp_path, {"10.plain": "Just do it."})
    defs = load_workflows(ws)
    with_defs = build_work_order(
        ws, tasks_root, "10.plain", None, REPO, "l1", "r1", "HEAD", _cfg(),
        workflow_defs=defs,
    )
    without = build_work_order(
        ws, tasks_root, "10.plain", None, REPO, "l1", "r1", "HEAD", _cfg(),
    )
    assert "workflow" not in with_defs["config"]
    assert with_defs == without
