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


def status_from_runs(payload: str) -> CiStatus:
    """Map one ``gh run list --json ...`` payload to a :class:`CiStatus`.

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

    # ``gh run list`` returns newest-first, so runs[0] is at the tip. Every run
    # at that same head sha is evidence about the same commit: a green
    # secondary workflow (nightly, docs, CodeQL) must never mask a failing CI
    # run beside it, and a failure at an older sha is already history.
    head_sha = first.get("headSha") or None
    at_head = [
        r for r in runs
        if isinstance(r, dict) and (r.get("headSha") or None) == head_sha
    ]

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


def check_repo_ci(
    repo_root: Path, *, branch: str = "main", runner: GhRunner | None = None
) -> CiStatus:
    """Latest CI verdict for ``branch`` in the repo at ``repo_root``."""
    gh = runner or GhRunner(repo_root)
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
        return CiStatus(CiState.UNKNOWN, detail=(err or out).strip()[:_DETAIL_LIMIT])
    return status_from_runs(out)
