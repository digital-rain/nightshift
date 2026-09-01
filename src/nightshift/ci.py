"""The CI seam: nightshift's only window onto a repo's GitHub Actions state.

``GhRunner`` is to ``gh`` what ``git/runner.GitRunner`` is to ``git`` — the ONLY
place ``gh`` is invoked as a subprocess. Everything above this module works with
:class:`CiStatus` values and never shells out.

The gate this feeds is deliberately fail-open: only :attr:`CiState.RED` holds
dispatch. A repo with no remote, a host without ``gh`` on PATH or authenticated,
an unparseable payload, or a cancelled run all resolve to
:attr:`CiState.UNKNOWN`, which dispatches normally.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


_log = logging.getLogger(__name__)

# gh conclusions that mean "main is broken".
_RED_CONCLUSIONS = frozenset({"failure", "timed_out", "action_required"})
# gh conclusions that are completed-and-fine.
_GREEN_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})

_DETAIL_LIMIT = 400
_GH_TIMEOUT_SECONDS = 30.0


class CiState(StrEnum):
    """The four states the gate distinguishes."""

    GREEN = "green"
    RED = "red"
    PENDING = "pending"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CiStatus:
    """The latest CI verdict for one repo's default branch."""

    state: CiState
    head_sha: str | None = None
    url: str | None = None
    detail: str | None = None
    # True when ``gh`` itself could not answer (non-zero exit): the absence of
    # information, not information about an absence. Callers keep their last
    # known state across a transient blip instead of churning holds.
    transient: bool = False


class GhRunner:
    """Runs ``gh`` in a repo. The ONLY place ``gh`` is spawned."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def run(self, *args: str) -> tuple[int, str, str]:
        """Run ``gh``, returning ``(returncode, stdout, stderr)``.

        Never raises: a missing ``gh`` binary, a timeout, or any OS error comes
        back as a non-zero return code so the caller degrades to UNKNOWN.
        """
        argv = ("gh", *args)
        try:
            proc = subprocess.run(  # noqa: S603 — fixed "gh" executable
                argv,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=_GH_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return 127, "", str(exc)
        if os.environ.get("NIGHTSHIFT_GH_TRACE"):
            _log.info(
                "gh %s cwd=%s rc=%d", " ".join(args), self.repo_root, proc.returncode
            )
        return proc.returncode, proc.stdout, proc.stderr


def status_from_runs(payload: str, *, tip_sha: str | None = None) -> CiStatus:
    """Map one ``gh run list --json ...`` payload to a :class:`CiStatus`.

    ``tip_sha`` is the branch's actual head. Runs are evidence about the commit
    they ran on, so only runs at the tip decide the verdict. Inferring the tip
    from run order instead is wrong: ``gh run list`` sorts by run *creation*,
    so workflows for different commits interleave (a slow ``CI Lint`` for an
    older commit can be created after a fast ``CI Validate`` for a newer one),
    and the state then flaps between commits. When the tip has no runs yet the
    answer is PENDING -- CI has not reported on this commit -- never the
    previous commit's GREEN.

    Pure: this is the whole decision table, and it is what the tests pin.
    """
    try:
        runs = json.loads(payload)
    except (ValueError, TypeError):
        return CiStatus(CiState.UNKNOWN, detail="unparseable gh payload")
    if not isinstance(runs, list) or not runs:
        return CiStatus(CiState.UNKNOWN, detail="no workflow runs on branch")

    first = runs[0]
    if not isinstance(first, dict):
        return CiStatus(CiState.UNKNOWN, detail="unexpected gh payload shape")

    # Every run at the tip is evidence about the same commit: a green secondary
    # workflow (nightly, docs, CodeQL) must never mask a failing CI run beside
    # it, and a run at an older sha is already history.
    head_sha = tip_sha or (first.get("headSha") or None)
    at_head = [
        r for r in runs
        if isinstance(r, dict) and (r.get("headSha") or None) == head_sha
    ]
    if not at_head:
        # The tip is real but nothing has run on it yet -- the window between a
        # push and its first workflow. Fail-open PENDING, never the previous
        # commit's verdict.
        return CiStatus(
            CiState.PENDING,
            head_sha=head_sha,
            detail=f"no run yet for {str(head_sha)[:8]}",
        )

    def _mk(state: CiState, run: dict, detail: str) -> CiStatus:
        return CiStatus(
            state,
            head_sha=head_sha,
            url=run.get("url") or None,
            detail=detail[:_DETAIL_LIMIT],
        )

    def _name(run: dict) -> str:
        return run.get("workflowName") or "workflow"

    pending: dict | None = None
    green: dict | None = None
    unknown: dict | None = None
    for run in at_head:
        if run.get("status") != "completed":
            pending = pending or run
            continue
        conclusion = (run.get("conclusion") or "").strip().lower()
        if conclusion in _RED_CONCLUSIONS:
            # Any red at the tip is red, whichever workflow reported it.
            return _mk(CiState.RED, run, f"{_name(run)}: {conclusion}")
        if conclusion in _GREEN_CONCLUSIONS:
            green = green or run
        else:
            # cancelled, or anything gh adds later: not evidence of breakage.
            unknown = unknown or run

    if pending is not None:
        return _mk(
            CiState.PENDING,
            pending,
            f"{_name(pending)} {pending.get('status') or 'running'}",
        )
    if green is not None:
        conclusion = (green.get("conclusion") or "").strip().lower()
        return _mk(CiState.GREEN, green, f"{_name(green)}: {conclusion}")
    run = unknown or first
    conclusion = (run.get("conclusion") or "").strip().lower()
    return _mk(CiState.UNKNOWN, run, f"{_name(run)}: {conclusion or 'no conclusion'}")


def tip_sha(gh: GhRunner, branch: str) -> str | None:
    """The branch's actual head sha, or ``None`` when ``gh`` cannot say.

    ``{owner}``/``{repo}`` are substituted by ``gh`` from the repo's own remote,
    the same way ``gh run list`` resolves it.
    """
    rc, out, _ = gh.run(
        "api", f"repos/{{owner}}/{{repo}}/commits/{branch}", "--jq", ".sha"
    )
    if rc != 0:
        return None
    return out.strip() or None


def check_repo_ci(
    repo_root: Path, *, branch: str = "main", runner: GhRunner | None = None
) -> CiStatus:
    """Latest CI verdict for ``branch`` in the repo at ``repo_root``."""
    gh = runner or GhRunner(repo_root)
    # Anchor on the real tip. If gh cannot resolve it we fall back to inferring
    # it from run order, which is better than nothing but can flap between
    # commits -- see status_from_runs.
    tip = tip_sha(gh, branch)
    rc, out, err = gh.run(
        "run",
        "list",
        "--branch",
        branch,
        "--limit",
        # High enough to see every workflow that ran at the tip, not just the
        # newest one: a green sibling must not be able to mask a red CI run.
        "20",
        "--json",
        "status,conclusion,headSha,url,workflowName",
    )
    if rc != 0:
        return CiStatus(
            CiState.UNKNOWN,
            detail=(err or out).strip()[:_DETAIL_LIMIT],
            transient=True,
        )
    return status_from_runs(out, tip_sha=tip)
