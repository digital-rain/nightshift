"""The migration-pinning table functions for the Phase 8 attempts fold.

:func:`fold_legacy` and :func:`split_state` ARE the semantics of the CASE
expressions in ``20260731000004_nightshift_attempts.sql`` (up and down) — kept
in lockstep with that migration, pinned by the round-trip tests in
``test_lifecycle.py``, and consumed by nothing else. Split out of
:mod:`nightshift.lifecycle` in Phase 9: live code never folds the legacy
(lease × run) pair.
"""

from __future__ import annotations

from typing import assert_never

from nightshift.lifecycle import (
    MERGE_FAILURE_KINDS,
    RESOLVE_WORKER_ID,
    AttemptState,
    RunStatus,
)


# String values, compared as strings: legacy runs rows can carry retired
# failure kinds ('repo_config', 'disk', 'aborted') that FailureKind no longer
# has, so constructing the enum here would raise where the migration's CASE
# (failure_kind IN ('merge_conflict', 'merge_rejected') → conflict, else
# failed) folds them to FAILED.
_MERGE_KIND_VALUES = frozenset(k.value for k in MERGE_FAILURE_KINDS)


def fold_legacy(
    lease_status: str | None,
    run_status: str,
    *,
    phase: str | None = None,
    failure_kind: str | None = None,
    worker_id: str | None = None,
) -> AttemptState:
    """The forward data-migration fold: today's (lease.status × run.status ×
    phase × failure_kind) → :class:`AttemptState`. This function IS the
    semantics of the CASE expression in
    ``20260731000004_nightshift_attempts.sql`` (up) — keep them in lockstep;
    the round-trip tests in ``test_lifecycle.py`` pin both directions.

    Zombie combos canonicalize (the Phase 8 behavior fixes):

    - a cancelled lease left its run ``running`` forever → ABORTED;
    - an expired lease left its run ``running`` forever → EXPIRED;
    - a released lease with a ``running`` run (the degenerate neutral submit)
      and orphaned lease-less running rows → ABORTED;
    - lease-less running rows for ``manager:resolve`` → RESOLVING.
    """
    if lease_status == "cancelled":
        return AttemptState.ABORTED
    if lease_status == "expired":
        return AttemptState.EXPIRED
    if lease_status == "landed":
        return AttemptState.LANDED
    if lease_status == "leased" and run_status == "running":
        return AttemptState.LANDING if phase == "landing" else AttemptState.RUNNING
    # Released / lease-less (and the leased-but-run-terminal zombie): fold by
    # the run status alone.
    match RunStatus(run_status):
        case RunStatus.COMPLETED:
            return AttemptState.NO_CHANGE
        case RunStatus.BLOCKED:
            return AttemptState.BLOCKED
        case RunStatus.ERROR:
            if failure_kind in _MERGE_KIND_VALUES:
                return AttemptState.CONFLICT
            return AttemptState.FAILED
        case RunStatus.ABORTED:
            return AttemptState.ABORTED
        case RunStatus.SKIPPED:
            return AttemptState.SKIPPED
        case RunStatus.RUNNING:
            if lease_status is None and worker_id == RESOLVE_WORKER_ID:
                return AttemptState.RESOLVING
            return AttemptState.ABORTED
        case _:
            assert_never(run_status)


def split_state(state: AttemptState) -> tuple[str | None, str]:
    """The backward data-migration split: state → (lease_status | None,
    run_status). This function IS the semantics of the CASE expressions in
    ``20260731000004_nightshift_attempts.sql`` (down). ``None`` means no
    lease row is recreated (resolve children never held one).

    Round-trip laws (pinned in ``test_lifecycle.py``): ``fold(split(s)) == s``
    for every storable state; ``split(fold(combo)) == combo`` for every
    non-degenerate legacy combo (ABORTED canonicalizes released/cancelled
    aborts to ``(cancelled, aborted)`` — documented in :func:`fold_legacy`).
    """
    match state:
        case AttemptState.RUNNING | AttemptState.LANDING:
            # LANDING is distinguished on refold by the row's phase column
            # ("landing"), which both directions copy verbatim.
            return ("leased", "running")
        case AttemptState.LANDED:
            return ("landed", "completed")
        case AttemptState.NO_CHANGE:
            return ("released", "completed")
        case AttemptState.BLOCKED:
            return ("released", "blocked")
        case AttemptState.FAILED | AttemptState.CONFLICT:
            # CONFLICT is distinguished on refold by failure_kind (always a
            # merge kind — see error_state_for), copied verbatim.
            return ("released", "error")
        case AttemptState.SKIPPED:
            return ("released", "skipped")
        case AttemptState.ABORTED:
            return ("cancelled", "aborted")
        case AttemptState.EXPIRED:
            return ("expired", "running")
        case AttemptState.RESOLVING:
            return (None, "running")
        case AttemptState.CLAIMED | AttemptState.SUBMITTED:
            # Unreachable today (see AttemptState) — never stored, so the
            # down-migration never sees them.
            raise ValueError(f"{state} is never stored; nothing to split")
        case _:
            assert_never(state)
