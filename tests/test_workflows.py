"""Phase 1 — the pure definition layer (``nightshift.workflows``).

Covers every validation rule, the role-resolution ladder, ``route`` semantics,
visit round-tripping, and the three shipped definitions (plus the post-v1
looping-verify fixture, which must validate even though it is not shipped).
"""

from __future__ import annotations

import json

import pytest

from nightshift import workflows
from nightshift.workflows import (
    END,
    StepKind,
    WorkflowDef,
    WorkflowError,
    WorkflowStep,
    format_visits,
    load_workflows,
    parse_visits,
    resolve_role_model,
    route,
    step_max_turns,
)


# --------------------------------------------------------------------------- #
# Helpers to build definitions from raw JSON dicts (mirrors the loader path).
# --------------------------------------------------------------------------- #


def _def(steps: list[dict], name: str = "wf") -> WorkflowDef:
    return workflows.parse_workflow({"name": name, "steps": steps})


REF_STEPS = [
    {
        "id": "plan",
        "kind": "doc",
        "role": "planner",
        "prompt": "workflow-plan.md",
        "inputs": ["brief"],
        "output": "plan",
        "max_turns": 30,
        "signals": {"plan-trivial": "implement"},
    },
    {
        "id": "review",
        "kind": "doc",
        "role": "implementor",
        "prompt": "workflow-review.md",
        "inputs": ["brief", "plan"],
        "output": "review",
        "max_turns": 20,
        "signals": {"review-clear": "implement"},
    },
    {
        "id": "revise",
        "kind": "doc",
        "role": "planner",
        "prompt": "workflow-revise.md",
        "inputs": ["brief", "plan", "review"],
        "output": "plan",
        "max_turns": 30,
    },
    {
        "id": "implement",
        "kind": "code",
        "role": "implementor",
        "inputs": ["brief", "plan"],
        "max_turns": None,
    },
]


# --------------------------------------------------------------------------- #
# max_turns tri-state
# --------------------------------------------------------------------------- #


def test_max_turns_absent_inherits():
    d = _def(
        [
            {"id": "p", "kind": "doc", "role": "planner", "prompt": "x.md",
             "inputs": ["brief"], "output": "plan"},
            {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief", "plan"]},
        ]
    )
    assert d.step("p").max_turns is workflows._INHERIT


def test_max_turns_null_is_unbounded():
    d = _def(
        [
            {"id": "p", "kind": "doc", "role": "planner", "prompt": "x.md",
             "inputs": ["brief"], "output": "plan", "max_turns": None},
            {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief", "plan"]},
        ]
    )
    assert d.step("p").max_turns is None


def test_max_turns_int_override():
    d = _def(
        [
            {"id": "p", "kind": "doc", "role": "planner", "prompt": "x.md",
             "inputs": ["brief"], "output": "plan", "max_turns": 42},
            {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief", "plan"]},
        ]
    )
    assert d.step("p").max_turns == 42


# --------------------------------------------------------------------------- #
# step_max_turns() resolution (§3.1)
# --------------------------------------------------------------------------- #


def test_step_max_turns_inherit_passes_through():
    d = _def(
        [
            {"id": "p", "kind": "doc", "role": "planner", "prompt": "x.md",
             "inputs": ["brief"], "output": "plan"},
            {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief", "plan"]},
        ]
    )
    assert step_max_turns(d.step("p"), 42) == 42


def test_step_max_turns_none_returns_none():
    d = _def(
        [
            {"id": "p", "kind": "doc", "role": "planner", "prompt": "x.md",
             "inputs": ["brief"], "output": "plan", "max_turns": None},
            {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief", "plan"]},
        ]
    )
    assert step_max_turns(d.step("p"), 42) is None


def test_step_max_turns_int_overrides():
    d = _def(
        [
            {"id": "p", "kind": "doc", "role": "planner", "prompt": "x.md",
             "inputs": ["brief"], "output": "plan", "max_turns": 10},
            {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief", "plan"]},
        ]
    )
    assert step_max_turns(d.step("p"), 42) == 10


# --------------------------------------------------------------------------- #
# Validation rules
# --------------------------------------------------------------------------- #


def test_doc_step_requires_prompt_and_output():
    with pytest.raises(WorkflowError):
        _def([
            {"id": "p", "kind": "doc", "role": "planner", "inputs": ["brief"], "output": "plan"},
            {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief", "plan"]},
        ])
    with pytest.raises(WorkflowError):
        _def([
            {"id": "p", "kind": "doc", "role": "planner", "prompt": "x.md", "inputs": ["brief"]},
            {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief"]},
        ])


def test_code_step_must_not_set_prompt_or_output():
    with pytest.raises(WorkflowError):
        _def([
            {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief"], "prompt": "x.md"},
        ])
    with pytest.raises(WorkflowError):
        _def([
            {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief"], "output": "plan"},
        ])


def test_max_turns_rejects_bool():
    with pytest.raises(WorkflowError):
        _def([
            {"id": "p", "kind": "doc", "role": "planner", "prompt": "x.md",
             "inputs": ["brief"], "output": "plan", "max_turns": True},
            {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief", "plan"]},
        ])
    with pytest.raises(WorkflowError):
        _def([
            {"id": "p", "kind": "doc", "role": "planner", "prompt": "x.md",
             "inputs": ["brief"], "output": "plan", "max_turns": False},
            {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief", "plan"]},
        ])


def test_max_visits_rejects_bool():
    with pytest.raises(WorkflowError):
        _def([
            {"id": "p", "kind": "doc", "role": "planner", "prompt": "x.md",
             "inputs": ["brief"], "output": "plan", "max_visits": True},
            {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief", "plan"]},
        ])
    with pytest.raises(WorkflowError):
        _def([
            {"id": "p", "kind": "doc", "role": "planner", "prompt": "x.md",
             "inputs": ["brief"], "output": "plan", "max_visits": False},
            {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief", "plan"]},
        ])


def test_split_step_must_not_set_prompt_or_output():
    with pytest.raises(WorkflowError):
        _def([
            {"id": "s", "kind": "split", "role": "planner", "inputs": ["brief"], "output": "plan"},
        ])


def test_duplicate_step_ids_rejected():
    with pytest.raises(WorkflowError):
        _def([
            {"id": "dup", "kind": "code", "role": "implementor", "inputs": ["brief"]},
            {"id": "dup", "kind": "code", "role": "implementor", "inputs": ["brief"]},
        ])


def test_inputs_must_name_brief_or_earlier_output():
    with pytest.raises(WorkflowError):
        _def([
            {"id": "p", "kind": "doc", "role": "planner", "prompt": "x.md",
             "inputs": ["brief", "nonexistent"], "output": "plan"},
            {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief"]},
        ])


def test_inputs_cannot_reference_later_output():
    # review lists "plan" but plan is produced by a *later* step.
    with pytest.raises(WorkflowError):
        _def([
            {"id": "review", "kind": "doc", "role": "implementor", "prompt": "r.md",
             "inputs": ["brief", "plan"], "output": "review"},
            {"id": "plan", "kind": "doc", "role": "planner", "prompt": "p.md",
             "inputs": ["brief"], "output": "plan"},
            {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief"]},
        ])


def test_split_step_must_route_to_end():
    with pytest.raises(WorkflowError):
        _def([
            {"id": "p", "kind": "doc", "role": "planner", "prompt": "x.md",
             "inputs": ["brief"], "output": "plan", "next": "s"},
            {"id": "s", "kind": "split", "role": "planner", "inputs": ["brief", "plan"], "next": "p"},
        ])


def test_split_step_signals_must_route_to_end():
    with pytest.raises(WorkflowError):
        _def([
            {"id": "s", "kind": "split", "role": "planner", "inputs": ["brief"],
             "signals": {"go": "s"}, "max_visits": 2},
        ])


def test_default_path_must_reach_code_or_split():
    with pytest.raises(WorkflowError):
        _def([
            {"id": "p", "kind": "doc", "role": "planner", "prompt": "x.md",
             "inputs": ["brief"], "output": "plan"},
        ])


def test_self_reachable_step_requires_max_visits():
    # implement.next = "verify", verify.next = "implement" — cycle without max_visits.
    with pytest.raises(WorkflowError):
        _def([
            {"id": "verify", "kind": "doc", "role": "implementor", "prompt": "v.md",
             "inputs": ["brief"], "output": "gaps", "next": "implement"},
            {"id": "implement", "kind": "code", "role": "implementor",
             "inputs": ["brief"], "next": "verify"},
        ])


def test_signal_destinations_must_exist():
    with pytest.raises(WorkflowError):
        _def([
            {"id": "p", "kind": "doc", "role": "planner", "prompt": "x.md",
             "inputs": ["brief"], "output": "plan", "signals": {"go": "nowhere"}},
            {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief"]},
        ])


def test_next_must_name_existing_step():
    with pytest.raises(WorkflowError):
        _def([
            {"id": "p", "kind": "doc", "role": "planner", "prompt": "x.md",
             "inputs": ["brief"], "output": "plan", "next": "ghost"},
            {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief"]},
        ])


def test_end_destinations_are_valid():
    d = _def([
        {"id": "p", "kind": "doc", "role": "planner", "prompt": "x.md",
         "inputs": ["brief"], "output": "plan", "signals": {"done": "$end"}},
        {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief"]},
    ])
    assert route(d.step("p"), "done") == END


# --------------------------------------------------------------------------- #
# Role → model resolution ladder (§3.2)
# --------------------------------------------------------------------------- #


def test_implementor_brief_model_wins():
    assert resolve_role_model(
        "implementor",
        brief_meta={"model": "sonnet"},
        queue_config={"workflow_models": {"implementor": "haiku"}},
        planner_model="opus",
        default_model="auto",
    ) == "sonnet"


def test_implementor_queue_then_default():
    assert resolve_role_model(
        "implementor",
        brief_meta={},
        queue_config={"workflow_models": {"implementor": "haiku"}},
        planner_model="opus",
        default_model="auto",
    ) == "haiku"
    assert resolve_role_model(
        "implementor",
        brief_meta={},
        queue_config={},
        planner_model="opus",
        default_model="auto",
    ) == "auto"


def test_planner_ladder():
    # brief planner_model wins
    assert resolve_role_model(
        "planner", brief_meta={"planner_model": "opus"}, queue_config={},
        planner_model="grok", default_model="auto",
    ) == "opus"
    # queue workflow_models next
    assert resolve_role_model(
        "planner", brief_meta={}, queue_config={"workflow_models": {"planner": "gemini"}},
        planner_model="grok", default_model="auto",
    ) == "gemini"
    # manager planner_model next (non-empty)
    assert resolve_role_model(
        "planner", brief_meta={}, queue_config={}, planner_model="grok", default_model="auto",
    ) == "grok"
    # empty planner_model falls through to default
    assert resolve_role_model(
        "planner", brief_meta={}, queue_config={}, planner_model="", default_model="auto",
    ) == "auto"


def test_other_role_no_planner_fallback():
    # Any other role: queue workflow_models, else default; planner_model does NOT apply.
    assert resolve_role_model(
        "reviewer", brief_meta={}, queue_config={"workflow_models": {"reviewer": "haiku"}},
        planner_model="opus", default_model="auto",
    ) == "haiku"
    # unresolved custom role → None (caller marks blocked)
    assert resolve_role_model(
        "reviewer", brief_meta={}, queue_config={},
        planner_model="opus", default_model="",
    ) is None


def test_unresolvable_returns_none():
    # implementor with no brief/queue/default → None
    assert resolve_role_model(
        "implementor", brief_meta={}, queue_config={},
        planner_model="opus", default_model="",
    ) is None


# --------------------------------------------------------------------------- #
# route()
# --------------------------------------------------------------------------- #


def _plan_step() -> WorkflowStep:
    return _def(REF_STEPS).step("plan")


def test_route_declared_signal_wins():
    assert route(_plan_step(), "plan-trivial") == "implement"


def test_route_undeclared_signal_uses_next():
    # plan's next defaults to "review"
    assert route(_plan_step(), "bogus") == "review"


def test_route_no_signal_uses_next():
    assert route(_plan_step(), None) == "review"


def test_route_to_end():
    d = _def([
        {"id": "verify", "kind": "doc", "role": "implementor", "prompt": "v.md",
         "inputs": ["brief"], "output": "gaps", "signals": {"clear": "$end"}, "next": "gap"},
        {"id": "gap", "kind": "doc", "role": "planner", "prompt": "g.md",
         "inputs": ["brief", "gaps"], "output": "plan", "next": "i"},
        {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief", "plan"]},
    ])
    assert route(d.step("verify"), "clear") == END


# --------------------------------------------------------------------------- #
# visits round-trip
# --------------------------------------------------------------------------- #


def test_parse_visits():
    assert parse_visits("plan:1,implement:2") == {"plan": 1, "implement": 2}
    assert parse_visits(None) == {}
    assert parse_visits("") == {}


def test_format_visits_roundtrip():
    visits = {"plan": 1, "review": 1, "implement": 2}
    assert parse_visits(format_visits(visits)) == visits


# --------------------------------------------------------------------------- #
# Shipped definitions load and validate
# --------------------------------------------------------------------------- #


def test_shipped_definitions_load(tmp_path):
    defs = load_workflows(tmp_path)
    assert set(defs) >= {"plan-review-implement", "verify-refine", "plan-split"}
    pri = defs["plan-review-implement"]
    assert pri.first.id == "plan"
    assert pri.step("implement").kind is StepKind.CODE
    # plan-split ends in a split step
    ps = defs["plan-split"]
    assert ps.steps[-1].kind is StepKind.SPLIT
    # verify-refine
    vr = defs["verify-refine"]
    assert vr.first.id == "verify"
    assert route(vr.step("verify"), "verify-clear") == END


def test_operator_shadows_shipped(tmp_path):
    op_dir = tmp_path / ".nightshift" / "workflows"
    op_dir.mkdir(parents=True)
    (op_dir / "plan-review-implement.json").write_text(
        json.dumps({
            "name": "plan-review-implement",
            "steps": [
                {"id": "i", "kind": "code", "role": "implementor", "inputs": ["brief"]},
            ],
        })
    )
    defs = load_workflows(tmp_path)
    assert len(defs["plan-review-implement"].steps) == 1


# --------------------------------------------------------------------------- #
# The looping-verify fixture (spec §10) — must validate. Not a shipped asset.
# --------------------------------------------------------------------------- #


VERIFY_LOOP = {
    "name": "verify-loop",
    "steps": [
        {"id": "plan", "kind": "doc", "role": "planner", "prompt": "workflow-plan.md",
         "inputs": ["brief"], "output": "plan", "max_turns": 30},
        {"id": "implement", "kind": "code", "role": "implementor",
         "inputs": ["brief", "plan"], "next": "verify", "max_visits": 3},
        {"id": "verify", "kind": "doc", "role": "implementor", "prompt": "workflow-verify.md",
         "inputs": ["brief", "plan"], "output": "gaps",
         "signals": {"verify-clear": "$end"}, "next": "gap-plan", "max_visits": 3},
        {"id": "gap-plan", "kind": "doc", "role": "planner", "prompt": "workflow-gap-plan.md",
         "inputs": ["brief", "plan", "gaps"], "output": "plan", "next": "implement",
         "max_visits": 3},
    ],
}


def test_verify_loop_validates():
    d = workflows.parse_workflow(VERIFY_LOOP)
    assert route(d.step("verify"), "verify-clear") == END
    assert route(d.step("verify"), None) == "gap-plan"
    assert d.step("implement").max_visits == 3


def test_verify_loop_without_max_visits_fails():
    bad = json.loads(json.dumps(VERIFY_LOOP))
    for step in bad["steps"]:
        step.pop("max_visits", None)
    with pytest.raises(WorkflowError):
        workflows.parse_workflow(bad)
