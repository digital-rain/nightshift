"""Tests for the gh seam: gh JSON -> CiStatus."""

from __future__ import annotations

import json
from pathlib import Path

from nightshift.ci import CiState, check_repo_ci, status_from_runs


def _runs(**over) -> str:
    run = {
        "status": "completed",
        "conclusion": "success",
        "headSha": "abc123",
        "url": "https://github.com/o/r/actions/runs/1",
        "workflowName": "pytest",
    }
    run.update(over)
    return json.dumps([run])


def test_completed_success_is_green():
    st = status_from_runs(_runs())
    assert st.state is CiState.GREEN
    assert st.head_sha == "abc123"


def test_completed_failure_is_red():
    st = status_from_runs(_runs(conclusion="failure"))
    assert st.state is CiState.RED
    assert st.url == "https://github.com/o/r/actions/runs/1"
    assert "pytest" in (st.detail or "")


def test_timed_out_and_action_required_are_red():
    for c in ("timed_out", "action_required"):
        assert status_from_runs(_runs(conclusion=c)).state is CiState.RED


def test_skipped_and_neutral_are_green():
    for c in ("skipped", "neutral"):
        assert status_from_runs(_runs(conclusion=c)).state is CiState.GREEN


def test_cancelled_is_unknown_not_red():
    # A cancelled run is not evidence main is broken; never gate on it.
    assert status_from_runs(_runs(conclusion="cancelled")).state is CiState.UNKNOWN


def test_in_progress_is_pending():
    st = status_from_runs(_runs(status="in_progress", conclusion=None))
    assert st.state is CiState.PENDING


def test_no_runs_is_unknown():
    assert status_from_runs("[]").state is CiState.UNKNOWN


def test_garbage_payload_is_unknown():
    assert status_from_runs("not json").state is CiState.UNKNOWN


class _StubGh:
    """Answers the two calls check_repo_ci makes: the tip lookup, then the runs.

    ``tip`` defaults to the sha the canned runs are on, so a plain stub behaves
    like a repo whose tip is exactly what CI last reported on.
    """

    def __init__(
        self, rc: int, out: str, err: str = "", *, tip: str | None = "abc123",
        tip_rc: int = 0,
    ) -> None:
        self._r = (rc, out, err)
        self._tip = (tip_rc, (tip or "") + "\n", "")
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str) -> tuple[int, str, str]:
        self.calls.append(args)
        return self._tip if args and args[0] == "api" else self._r

    def run_list_call(self) -> tuple[str, ...]:
        return next(c for c in self.calls if c and c[0] == "run")


def test_check_repo_ci_passes_branch_and_parses():
    gh = _StubGh(0, _runs(conclusion="failure"))
    st = check_repo_ci(Path("/nonexistent"), branch="main", runner=gh)
    assert st.state is CiState.RED
    call = gh.run_list_call()
    assert "--branch" in call and "main" in call


def test_gh_failure_degrades_to_unknown():
    gh = _StubGh(127, "", "gh: command not found")
    st = check_repo_ci(Path("/nonexistent"), runner=gh)
    assert st.state is CiState.UNKNOWN
    assert "not found" in (st.detail or "")


# --------------------------------------------------------------------------- #
# Regression: every run at the newest head sha is sampled, not just runs[0]
# --------------------------------------------------------------------------- #


def _at(sha: str, *, name: str, url: str, **over) -> dict:
    run = {
        "status": "completed",
        "conclusion": "success",
        "headSha": sha,
        "url": url,
        "workflowName": name,
    }
    run.update(over)
    return run


def test_red_sibling_at_the_tip_beats_a_green_first_run():
    """A green nightly/docs run listed first must not mask a failing CI run at
    the same tip -- that lifts the gate onto a broken main."""
    payload = json.dumps([
        _at("tip", name="docs", url="https://gh/run/docs"),
        _at("tip", name="pytest", url="https://gh/run/ci", conclusion="failure"),
    ])
    st = status_from_runs(payload)
    assert st.state is CiState.RED
    assert st.head_sha == "tip"
    # detail/url must come from the run that actually failed.
    assert st.url == "https://gh/run/ci"
    assert "pytest" in (st.detail or "")


def test_pending_sibling_at_the_tip_is_pending_not_green():
    payload = json.dumps([
        _at("tip", name="docs", url="https://gh/run/docs"),
        _at("tip", name="pytest", url="https://gh/run/ci",
            status="in_progress", conclusion=None),
    ])
    assert status_from_runs(payload).state is CiState.PENDING


def test_only_runs_at_the_newest_sha_are_sampled():
    """gh returns newest-first: a failure at an older sha is already history."""
    payload = json.dumps([
        _at("tip", name="pytest", url="https://gh/run/ci"),
        _at("older", name="pytest", url="https://gh/run/old", conclusion="failure"),
    ])
    assert status_from_runs(payload).state is CiState.GREEN


def test_run_list_limit_sees_sibling_workflows():
    gh = _StubGh(0, _runs())
    check_repo_ci(Path("/nonexistent"), runner=gh)
    args = gh.run_list_call()
    limit = int(args[args.index("--limit") + 1])
    assert limit >= 10, "a --limit of 1 can never see a sibling run at the tip"


# --------------------------------------------------------------------------
# The flap: runs are evidence about the commit they ran on, so the verdict is
# anchored to the branch tip -- never inferred from run ordering.
# --------------------------------------------------------------------------

def test_a_newer_run_on_an_older_commit_does_not_decide_the_tip():
    """The observed 2026-09-01 flap: a green `CI Lint` for an older commit was
    created after the tip's failing `CI Validate`, so run order named the wrong
    commit and the repo went red -> green -> red without anything changing."""
    payload = json.dumps([
        _at("older", name="CI Lint", url="https://gh/run/lint"),
        _at("tip", name="CI Validate", url="https://gh/run/ci", conclusion="failure"),
    ])
    # Run order alone would pick "older" and report GREEN.
    assert status_from_runs(payload).state is CiState.GREEN
    # Anchored to the real tip it is RED, which is the truth.
    st = status_from_runs(payload, tip_sha="tip")
    assert st.state is CiState.RED
    assert st.head_sha == "tip"


def test_a_tip_with_no_runs_yet_is_pending_not_the_previous_green():
    """The window between a push and its first workflow must not report the
    previous commit's verdict."""
    payload = json.dumps([_at("older", name="CI Validate", url="https://gh/run/old")])
    st = status_from_runs(payload, tip_sha="brand-new-sha")
    assert st.state is CiState.PENDING
    assert st.head_sha == "brand-new-sha"
    assert "no run yet" in (st.detail or "")


def test_check_repo_ci_anchors_on_the_resolved_tip():
    gh = _StubGh(0, json.dumps([
        _at("older", name="CI Lint", url="https://gh/run/lint"),
        _at("tip", name="CI Validate", url="https://gh/run/ci", conclusion="failure"),
    ]), tip="tip")
    st = check_repo_ci(Path("/nonexistent"), runner=gh)
    assert st.state is CiState.RED
    assert gh.calls[0][0] == "api", "the tip must be resolved before the runs"


def test_unresolvable_tip_falls_back_to_run_order():
    """gh cannot answer for the tip (no remote, offline): still report something
    rather than nothing -- fail-open beats a hard stop."""
    gh = _StubGh(0, _runs(conclusion="failure"), tip=None, tip_rc=1)
    st = check_repo_ci(Path("/nonexistent"), runner=gh)
    assert st.state is CiState.RED
    assert st.transient is False


def test_a_gh_command_failure_is_marked_transient():
    """rc != 0 is the absence of information: callers keep their last state."""
    gh = _StubGh(127, "", "gh: command not found")
    st = check_repo_ci(Path("/nonexistent"), runner=gh)
    assert st.state is CiState.UNKNOWN
    assert st.transient is True


def test_a_real_no_runs_answer_is_not_transient():
    """gh answered fine, the branch simply has no workflow runs."""
    gh = _StubGh(0, "[]")
    st = check_repo_ci(Path("/nonexistent"), runner=gh)
    assert st.state is CiState.UNKNOWN
    assert st.transient is False
