"""Phase 3 — the workflow cursor machine (``transitions.on_workflow_*``).

Pure: policies and outcomes are built by hand — no store, no files. One test
per behavior-table row (plan §Phase 3) plus the two invariants (no engine_meta
on failure; on_workflow_step ignores outcome.signal directly).
"""

from __future__ import annotations

from typing import Any

from nightshift.lifecycle import (
    AttemptRef,
    AttemptState,
    FailureKind,
    GitPhase,
    LandKind,
    LandOutcome,
    Outcome,
    Progress,
    RunStatus,
    SubmitPolicy,
    WorkflowStepPolicy,
)
from nightshift.transitions import (
    on_submit,
    on_workflow_land,
    on_workflow_split,
    on_workflow_step,
)
from nightshift.workflows import END, StepKind


REF = AttemptRef(id="r1", queue=None, task="10.wf")


def _outcome(status: RunStatus, **kw: Any) -> Outcome:
    return Outcome(status=status, model="m1", turns=3, **kw)


def _step_policy(**kw: Any) -> WorkflowStepPolicy:
    defaults: dict[str, Any] = dict(
        workflow="plan-review-implement",
        step_id="plan",
        kind=StepKind.DOC,
        output="plan",
        route_to="review",
        dest_kind=StepKind.DOC,
        dest_visits_exhausted=False,
        evergreen=False,
        visits={"plan": 1},
        exhausted_reason="",
    )
    defaults.update(kw)
    return WorkflowStepPolicy(**defaults)


def _policy(step: WorkflowStepPolicy, **kw: Any) -> SubmitPolicy:
    return SubmitPolicy(workflow_step=step, **kw)


# --------------------------------------------------------------------------- #
# doc step — completed
# --------------------------------------------------------------------------- #


def test_doc_completed_advances_cursor():
    step = _step_policy(route_to="review", visits={"plan": 1})
    out = _outcome(RunStatus.COMPLETED, document="# Plan", landable=False)
    t = on_workflow_step(REF, out, _policy(step))
    assert t.state is AttemptState.NO_CHANGE
    assert t.effects.write_artifact == ("plan", "# Plan")
    assert t.effects.engine_meta == {
        "workflow_step": "review",
        "workflow_visits": "plan:1,review:1",
    }
    assert t.effects.progress is Progress.RESET


def test_doc_completed_destination_exhausted_quarantines():
    step = _step_policy(
        route_to="review", dest_visits_exhausted=True, visits={"plan": 1, "review": 2},
        exhausted_reason="workflow budget exhausted at 'review' after 2 visits",
    )
    out = _outcome(RunStatus.COMPLETED, document="# Plan")
    t = on_workflow_step(REF, out, _policy(step))
    # artifact still committed (the work was good)
    assert t.effects.write_artifact == ("plan", "# Plan")
    flags = {f.key: f for f in t.effects.frontmatter}
    assert flags["quarantined"].value is True
    assert "budget exhausted" in flags["quarantined"].reason
    assert t.effects.engine_meta is None


def test_doc_completed_end_completes():
    step = _step_policy(route_to=END, dest_kind=None)
    out = _outcome(RunStatus.COMPLETED, document="# Gaps")
    t = on_workflow_step(REF, out, _policy(step))
    assert t.state is AttemptState.NO_CHANGE
    assert t.effects.write_artifact == ("plan", "# Gaps")
    flags = {f.key: f for f in t.effects.frontmatter}
    assert flags["completed"].value is True
    assert t.effects.workflow_reset is False


def test_doc_completed_end_evergreen_resets():
    step = _step_policy(route_to=END, dest_kind=None, evergreen=True)
    out = _outcome(RunStatus.COMPLETED, document="# Gaps")
    t = on_workflow_step(REF, out, _policy(step))
    assert t.effects.workflow_reset is True
    assert not any(f.key == "completed" for f in t.effects.frontmatter)


def test_doc_completed_no_document_errors():
    step = _step_policy(route_to="review")
    out = _outcome(RunStatus.COMPLETED, document=None)
    t = on_workflow_step(REF, out, _policy(step))
    assert t.fields["failure_kind"] is FailureKind.WORKER_ERROR
    assert "no document" in t.fields["failure_reason"]
    # cursor untouched
    assert t.effects.engine_meta is None


# --------------------------------------------------------------------------- #
# doc step — failure delegates (cursor untouched, no engine_meta)
# --------------------------------------------------------------------------- #


def test_doc_blocked_delegates():
    step = _step_policy()
    out = _outcome(RunStatus.BLOCKED, result_line="stuck")
    t = on_workflow_step(REF, out, _policy(step))
    assert t.state is AttemptState.BLOCKED
    assert t.effects.engine_meta is None


def test_doc_error_delegates_no_engine_meta():
    step = _step_policy()
    out = _outcome(RunStatus.ERROR, failure_kind=FailureKind.WORKER_ERROR)
    t = on_workflow_step(REF, out, _policy(step))
    assert t.effects.engine_meta is None


# --------------------------------------------------------------------------- #
# code step
# --------------------------------------------------------------------------- #


def test_code_completed_landable_returns_land():
    step = _step_policy(step_id="implement", kind=StepKind.CODE, output=None, route_to=END)
    out = _outcome(RunStatus.COMPLETED, landable=True)
    assert on_workflow_step(REF, out, _policy(step)) is GitPhase.LAND


def _land(kind: LandKind = LandKind.LANDED, **kw: Any) -> LandOutcome:
    return LandOutcome(kind=kind, **kw)


def test_workflow_land_end_nonevergreen_drops_brief():
    step = _step_policy(step_id="implement", kind=StepKind.CODE, output=None, route_to=END)
    out = _outcome(RunStatus.COMPLETED, landable=True)
    t = on_workflow_land(REF, out, _land(sha="abc"), _policy(step))
    assert t.state is AttemptState.LANDED
    assert t.effects.drop_brief is True
    assert t.effects.workflow_reset is False


def test_workflow_land_end_evergreen_resets():
    step = _step_policy(
        step_id="implement", kind=StepKind.CODE, output=None, route_to=END, evergreen=True,
    )
    out = _outcome(RunStatus.COMPLETED, landable=True)
    t = on_workflow_land(REF, out, _land(sha="abc"), _policy(step))
    assert t.effects.drop_brief is False
    assert t.effects.workflow_reset is True


def test_workflow_land_mid_workflow_advances():
    step = _step_policy(
        step_id="implement", kind=StepKind.CODE, output=None, route_to="verify",
        dest_kind=StepKind.DOC, visits={"implement": 1},
    )
    out = _outcome(RunStatus.COMPLETED, landable=True)
    t = on_workflow_land(REF, out, _land(sha="abc"), _policy(step))
    assert t.effects.drop_brief is False
    assert t.effects.engine_meta == {
        "workflow_step": "verify",
        "workflow_visits": "implement:1,verify:1",
    }


def test_workflow_land_mid_workflow_exhausted_quarantines():
    step = _step_policy(
        step_id="implement", kind=StepKind.CODE, output=None, route_to="verify",
        dest_visits_exhausted=True, visits={"implement": 1, "verify": 3},
        exhausted_reason="workflow budget exhausted at 'verify' after 3 visits",
    )
    out = _outcome(RunStatus.COMPLETED, landable=True)
    t = on_workflow_land(REF, out, _land(sha="abc"), _policy(step))
    flags = {f.key: f for f in t.effects.frontmatter}
    assert flags["quarantined"].value is True


def test_workflow_land_failure_delegates():
    step = _step_policy(step_id="implement", kind=StepKind.CODE, output=None, route_to=END)
    out = _outcome(RunStatus.COMPLETED, landable=True)
    t = on_workflow_land(REF, out, _land(LandKind.CONFLICT, detail="conflict"), _policy(step))
    # existing land-failed shape; cursor untouched
    assert t.effects.engine_meta is None
    assert t.effects.drop_brief is False


# --------------------------------------------------------------------------- #
# split step
# --------------------------------------------------------------------------- #


def test_split_completed_returns_harvest():
    step = _step_policy(step_id="split", kind=StepKind.SPLIT, output=None, route_to=END)
    out = _outcome(RunStatus.COMPLETED, landable=False)
    assert on_workflow_step(REF, out, _policy(step)) is GitPhase.HARVEST_SPLIT


def test_workflow_split_children_nonevergreen():
    step = _step_policy(step_id="split", kind=StepKind.SPLIT, output=None, route_to=END)
    out = _outcome(RunStatus.COMPLETED)
    t = on_workflow_split(REF, out, ["10.1.a", "10.2.b"], _policy(step))
    assert t.state is AttemptState.NO_CHANGE
    assert t.effects.workflow_reset is False
    assert t.response["subtasks"] == ["10.1.a", "10.2.b"]


def test_workflow_split_children_evergreen_retains_parent():
    step = _step_policy(
        step_id="split", kind=StepKind.SPLIT, output=None, route_to=END, evergreen=True,
    )
    out = _outcome(RunStatus.COMPLETED)
    t = on_workflow_split(REF, out, ["10.1.a"], _policy(step))
    assert t.effects.workflow_reset is True


def test_workflow_split_zero_children_increments_no_progress():
    step = _step_policy(step_id="split", kind=StepKind.SPLIT, output=None, route_to=END)
    out = _outcome(RunStatus.COMPLETED)
    t = on_workflow_split(REF, out, [], _policy(step))
    assert t.effects.progress is Progress.INCREMENT
    # cursor stays put — no engine_meta, no visit burned
    assert t.effects.engine_meta is None
    assert t.effects.workflow_reset is False


# --------------------------------------------------------------------------- #
# invariants
# --------------------------------------------------------------------------- #


def test_undeclared_signal_identical_to_no_signal():
    # route_to already reflects the routing decision — on_workflow_step never
    # inspects outcome.signal directly. A bogus signal on the outcome must not
    # change the transition when route_to is the same.
    step = _step_policy(route_to="review", visits={"plan": 1})
    out_none = _outcome(RunStatus.COMPLETED, document="# Plan", signal=None)
    out_bogus = _outcome(RunStatus.COMPLETED, document="# Plan", signal="undeclared-token")
    t1 = on_workflow_step(REF, out_none, _policy(step))
    t2 = on_workflow_step(REF, out_bogus, _policy(step))
    assert t1.effects.engine_meta == t2.effects.engine_meta


def test_on_submit_delegates_to_workflow_step():
    step = _step_policy(route_to="review")
    out = _outcome(RunStatus.COMPLETED, document="# Plan")
    t = on_submit(REF, out, _policy(step))
    assert t.effects.write_artifact == ("plan", "# Plan")
