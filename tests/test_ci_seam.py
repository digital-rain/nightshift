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
    def __init__(self, rc: int, out: str, err: str = "") -> None:
        self._r = (rc, out, err)
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str) -> tuple[int, str, str]:
        self.calls.append(args)
        return self._r


def test_check_repo_ci_passes_branch_and_parses():
    gh = _StubGh(0, _runs(conclusion="failure"))
    st = check_repo_ci(Path("/nonexistent"), branch="main", runner=gh)
    assert st.state is CiState.RED
    assert "--branch" in gh.calls[0] and "main" in gh.calls[0]


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
    args = gh.calls[0]
    limit = int(args[args.index("--limit") + 1])
    assert limit >= 10, "a --limit of 1 can never see a sibling run at the tip"
