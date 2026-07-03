"""The plumbing land (git greenfield §0/§3/§5) — landing as a ref operation.

The pipeline never touches the operator's working tree:

1. ``git merge-tree --write-tree`` — conflict check + merged tree OID, in one
   call, touching nothing (the ONE conflict-detection path);
2. ``git commit-tree <tree> -p <base>`` — the squash commit as a pure object;
3. ``git update-ref refs/heads/main <new> <old>`` — atomic CAS: the land
   happens here or not at all;
4. best-effort checkout advance (:func:`nightshift.git.refs.advance_checkout`,
   the ``reset --keep`` semantics): operator WIP is carried forward when
   clean; an overlap leaves the checkout behind ``main`` → CHECKOUT_BEHIND,
   still a success.

:func:`integrate_and_push` is the ONE integrate loop (replacing ``land()``'s
inline loop, ``push_resolved_main``'s copy, and the resolve preamble): sync
origin → produce a commit on the fresh tip → push (PUSH mode) → CAS → advance.
A rejected push simply drops our own commit object (it never touched ``main``)
and re-produces from the newly-synced tip — the ``orphan_squash``/``drop_shas``
bookkeeping this replaced is gone by construction.

The produce step is a callback (``produce(base_sha) -> ProduceResult``): a
normal land passes :func:`squash_produce`; a resolve passes
:func:`cherry_produce`. Remote policy is one exhaustive ``LandingMode``
dispatch; :func:`push_main` is the ONE push-main helper.

``gh`` (PR creation) is the only non-git subprocess here; git itself goes
through :class:`~nightshift.git.runner.GitRunner` exclusively.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import assert_never

from nightshift.git import GitResult, GitRunner
from nightshift.git.locks import repo_lock
from nightshift.git.refs import (
    CheckoutState,
    advance_checkout,
    branch_exists,
    checkout_state,
    main_sha,
    replay_commit,
    update_main_cas,
)
from nightshift.git.sync import SyncThrottle, sync_main_locked
from nightshift.git.worktrees import queue_slug
from nightshift.lifecycle import LandingMode, LandKind, LandOutcome


# Land idempotency trailer (Phase 8): every manager-produced land commit
# carries `Nightshift-Attempt: <attempt id>` so startup recovery can tell a
# completed land from an interrupted one by scanning main. Direct/legacy
# callers pass no attempt_id and get no trailer — only manager lands are
# recovery-eligible.
ATTEMPT_TRAILER = "Nightshift-Attempt"


def attempt_trailer_line(attempt_id: str) -> str:
    return f"{ATTEMPT_TRAILER}: {attempt_id}"


def find_landed_attempt(
    repo_root: Path, attempt_id: str, *, limit: int = 500
) -> str | None:
    """Scan the last ``limit`` commits of canonical ``main`` for the land
    idempotency trailer of ``attempt_id``; return the carrying commit's sha
    (None when absent). Bounded so recovery stays cheap on old repos; a land
    older than the window has long been observed by other paths."""
    tip = main_sha(repo_root)
    if tip is None:
        return None
    res = GitRunner(repo_root).run(
        "log", "-n", str(limit),
        f"--format=%H%x09%(trailers:key={ATTEMPT_TRAILER},valueonly,separator=%x2C)",
        tip,
    )
    if not res.ok:
        return None
    for line in res.stdout.splitlines():
        sha, _, values = line.partition("\t")
        if attempt_id in (v.strip() for v in values.split(",")):
            return sha
    return None


@dataclass(frozen=True)
class PrSpec:
    """What PR mode needs beyond the commit itself."""

    task: str
    title: str
    queue: str | None = None
    automerge: bool = True
    draft: bool = False


@dataclass(frozen=True)
class RepoContext:
    """Everything :func:`integrate_and_push` needs to know about the repo and
    its remote posture. ``remote`` is the push/fetch remote (PUSH mode falls
    back to ``origin`` when unset, matching the historical direct-push
    behavior); ``sync`` gates the origin integration step. The first sync of
    an attempt-0 land is throttled by ``git_refresh_seconds`` and does not
    rescue a divergence (matching the old ``maybe_sync`` posture) unless
    ``rescue_divergence_on_first_sync`` is set (the resolve path, whose local
    ``main`` deliberately carries the resolved commit)."""

    workspace: Path
    repo: str
    remote: str | None = None
    sync: bool = False
    git_refresh_seconds: float = 0.0
    rescue_divergence_on_first_sync: bool = False
    pr: PrSpec | None = None
    # App-owned sync throttle (Phase 7); None = unthrottled first sync.
    throttle: SyncThrottle | None = None

    @property
    def repo_root(self) -> Path:
        return self.workspace / self.repo


@dataclass(frozen=True)
class ProduceResult:
    """What a ``produce`` callback returns: a commit object built with the
    handed-in base as parent (full ``sha``, plus the operator-facing
    ``display_sha`` — short for squash lands, matching what run rows always
    recorded), or a terminal ``failure`` outcome (e.g. CONFLICT). ``sha`` may
    equal the base itself when the production is redundant (the content is
    already on the base — the cherry path after a rescue replay)."""

    sha: str | None = None
    display_sha: str | None = None
    failure: LandOutcome | None = None


def merge_tree_conflicts(
    repo_root: Path, branch: str, *, base: str = "HEAD"
) -> list[str]:
    """Preview a squash of ``branch`` onto ``base`` without touching the tree.

    No production caller since Phase 6 (the pipeline detects conflicts inside
    :func:`squash_produce`); kept as the preview/compat API and re-export.

    Uses ``git merge-tree --write-tree`` (git ≥ 2.38). Returns the conflict
    listing (empty when the merge is clean) in the historical preview shape —
    conflicted file names AND git's informational messages. Any failure to run
    the preview returns ``[]`` so detection never blocks a land the pipeline
    would handle.
    """
    _tree, _paths, listing = _merge_tree(GitRunner(repo_root), base, branch)
    return listing


def _merge_tree(
    git: GitRunner, base: str, branch: str
) -> tuple[str | None, list[str], list[str]]:
    """One ``merge-tree --write-tree`` call: ``(merged tree OID, conflicted
    paths, full conflict listing)``.

    On a clean merge both lists are empty. On a conflict the tree is ``None``;
    ``paths`` is the name-only section (the true conflicted files, what rides
    ``LandOutcome.conflicts``) and ``listing`` is every non-blank output line
    after the OID — names AND git's informational messages, exactly the way
    the pre-Phase-6 preview parsed it, so operator-visible conflict details
    stay byte-identical. A hard failure returns ``(None, [], [])``.
    """
    res = git.run("merge-tree", "--write-tree", "--name-only", base, branch)
    if res.ok:
        first = res.stdout.splitlines()[0].strip() if res.stdout.strip() else ""
        return (first or None), [], []
    # Conflict output: <OID>\n<conflicted names>\n\n<informational messages>.
    sections = res.stdout.split("\n\n", 1)
    head_lines = [ln for ln in sections[0].splitlines() if ln.strip()]
    paths = head_lines[1:]
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    listing = lines[1:] if len(lines) > 1 else lines
    return None, paths, listing


def squash_produce(
    repo_root: Path, branch: str, title: str, attempt_id: str | None = None
) -> Callable[[str], ProduceResult]:
    """The normal-land producer: squash ``branch`` onto the base as one
    commit object (``task: <title>``, the historical squash subject).
    ``attempt_id`` appends the land idempotency trailer as a proper git
    trailer block (blank line before it)."""

    def produce(base: str) -> ProduceResult:
        git = GitRunner(repo_root)
        if not branch_exists(repo_root, branch):
            return ProduceResult(failure=LandOutcome(
                kind=LandKind.CONFLICT,
                detail=f"no task branch '{branch}' to merge (nothing to recover)",
            ))
        tree, paths, listing = _merge_tree(git, base, branch)
        if listing:
            # Detail keeps the pre-Phase-6 preview wording (full listing);
            # `conflicts` carries only the true conflicted paths.
            shown = "\n".join(f"    {p}" for p in listing[:20])
            return ProduceResult(failure=LandOutcome(
                kind=LandKind.CONFLICT,
                conflicts=tuple(paths),
                detail=(
                    f"squash of '{branch}' conflicts with current main on "
                    f"{len(listing)} file(s):\n{shown}"
                ),
            ))
        if tree is None:
            return ProduceResult(failure=LandOutcome(
                kind=LandKind.CONFLICT,
                detail=f"squash of '{branch}' onto main failed (merge-tree)",
            ))
        if tree == git.out("rev-parse", f"{base}^{{tree}}"):
            # The old `git commit` refused an empty squash; keep that an error.
            return ProduceResult(failure=LandOutcome(
                kind=LandKind.CONFLICT,
                detail=(
                    f"commit failed:\nsquash of '{branch}' is empty — "
                    "nothing to commit"
                ),
            ))
        message = f"task: {title}"
        if attempt_id:
            message += f"\n\n{attempt_trailer_line(attempt_id)}"
        commit = git.run("commit-tree", tree, "-p", base, "-m", message)
        if not commit.ok:
            return ProduceResult(failure=LandOutcome(
                kind=LandKind.CONFLICT,
                detail=f"commit failed:\n{commit.detail}",
            ))
        sha = commit.stdout.strip()
        return ProduceResult(sha=sha, display_sha=git.out("rev-parse", "--short", sha) or sha)

    return produce


def cherry_produce(
    repo_root: Path, resolved_sha: str, attempt_id: str | None = None
) -> Callable[[str], ProduceResult]:
    """The resolve producer: replay the already-validated resolved commit onto
    the base via :func:`~nightshift.git.refs.replay_commit` (the shared
    plumbing cherry-pick). Redundant content (the rescue already replayed it
    onto the fresh tip) collapses to the base itself instead of an empty
    commit — that path can't carry the idempotency trailer, which is fine:
    redundant means the content already landed. Only a real merge conflict is
    worded as one; other failures (unknown commit, no parent) carry their own
    accurate detail."""

    def produce(base: str) -> ProduceResult:
        git = GitRunner(repo_root)

        def failure(detail: str) -> ProduceResult:
            return ProduceResult(failure=LandOutcome(kind=LandKind.CONFLICT, detail=detail))

        full = git.out("rev-parse", f"{resolved_sha}^{{commit}}")
        if full is None:
            return failure(
                f"cannot replay resolved commit: unknown commit '{resolved_sha}'"
            )
        if full == base:
            return ProduceResult(sha=full)
        replayed = replay_commit(
            git, base, full,
            extra_trailer=attempt_trailer_line(attempt_id) if attempt_id else None,
        )
        if replayed.sha is None:
            if replayed.conflict:
                return failure(
                    f"resolved commit {resolved_sha[:8]} conflicts with current "
                    f"origin/main:\n{replayed.detail}"
                )
            return failure(
                f"cannot replay resolved commit {resolved_sha[:8]}: {replayed.detail}"
            )
        return ProduceResult(sha=replayed.sha)

    return produce


def push_main(workspace: Path, repo: str, remote: str, sha: str) -> GitResult:
    """THE push-main helper: non-force push of ``sha`` to ``<remote>``'s
    ``main``. A primitive — the caller must hold the RepoLock (asserted)."""
    assert repo_lock(workspace, repo).is_held_by_current_thread(), (
        "push_main requires the caller to hold the RepoLock"
    )
    return GitRunner(workspace / repo).run("push", remote, f"{sha}:refs/heads/main")


def checkout_behind_detail(display_sha: str | None) -> str:
    """The operator-visible CHECKOUT_BEHIND notice (replaces the retired
    "changes stashed" messages)."""
    return (
        f"landed ({display_sha}); your uncommitted changes kept the checkout "
        "from advancing — it was left behind main (commit or stash them, then "
        "`git checkout main`)"
    )


def _cas_and_advance(
    repo_root: Path,
    checkout: CheckoutState,
    base: str,
    new: str,
    display: str | None,
) -> LandOutcome | None:
    """Steps 3+4 of the pipeline: CAS ``main`` ``base → new``, then advance
    the checkout best-effort. Returns ``None`` when the CAS lost a race (the
    caller re-produces from the fresh tip); otherwise LANDED or
    CHECKOUT_BEHIND (both successes — the ref is authoritative)."""
    if new != base and not update_main_cas(repo_root, new, base).ok:
        return None
    if advance_checkout(repo_root, checkout, new):
        return LandOutcome(kind=LandKind.LANDED, sha=display)
    return LandOutcome(
        kind=LandKind.CHECKOUT_BEHIND, sha=display,
        detail=checkout_behind_detail(display),
    )


def _noting_drops(outcome: LandOutcome, dropped: list[str]) -> LandOutcome:
    """Surface rescue casualties on the outcome — never silent: the SHAs ride
    ``dropped_commits`` and the human note is appended to ``detail`` (the
    Phase-0 "dropped SHAs" wording, unchanged)."""
    if not dropped:
        return outcome
    note = (
        "origin re-sync dropped conflicting local commit(s): "
        + ", ".join(sha[:12] for sha in dropped)
        + " (preserved unreachable in the reflog for manual recovery)"
    )
    return replace(
        outcome,
        dropped_commits=tuple(dropped),
        detail=f"{outcome.detail}\n{note}".strip(),
    )


def integrate_and_push(
    ctx: RepoContext,
    produce: Callable[[str], ProduceResult],
    *,
    mode: LandingMode,
    max_retries: int = 3,
) -> LandOutcome:
    """The ONE integrate-and-push loop, holding the repo's RepoLock throughout
    (an orchestration boundary). See the module docstring for the pipeline."""
    with repo_lock(ctx.workspace, ctx.repo):
        return integrate_and_push_locked(ctx, produce, mode=mode, max_retries=max_retries)


def integrate_and_push_locked(
    ctx: RepoContext,
    produce: Callable[[str], ProduceResult],
    *,
    mode: LandingMode,
    max_retries: int = 3,
) -> LandOutcome:
    """:func:`integrate_and_push` for callers already holding the RepoLock
    (``land()`` acquires it once around fetch/adopt/integrate)."""
    assert repo_lock(ctx.workspace, ctx.repo).is_held_by_current_thread(), (
        "integrate_and_push_locked requires the caller to hold the RepoLock"
    )
    repo_root = ctx.repo_root
    dropped: list[str] = []
    for attempt in range(max_retries + 1):
        # 1. Integrate the latest origin/main so the commit replays on top of
        #    everyone else's merged work (throttled on the first attempt; a
        #    retry after a rejected push must re-sync, rescuing any divergence).
        if ctx.sync and ctx.remote:
            sync_main_locked(
                ctx.workspace,
                ctx.repo,
                ctx.remote,
                min_interval_seconds=ctx.git_refresh_seconds if attempt == 0 else 0,
                force=attempt > 0,
                reset_divergence=attempt > 0 or ctx.rescue_divergence_on_first_sync,
                dropped_commits=dropped,
                throttle=ctx.throttle,
            )

        base = main_sha(repo_root)
        if base is None:
            return _noting_drops(LandOutcome(
                kind=LandKind.CONFLICT,
                detail="target repo has no main commit to land onto",
            ), dropped)

        # 2. Produce the commit object on the fresh tip (plumbing; main is
        #    untouched until the CAS below, so a failure or rejected push
        #    leaves nothing to unwind).
        produced = produce(base)
        if produced.failure is not None:
            return _noting_drops(produced.failure, dropped)
        new = produced.sha
        assert new is not None, "produce must return a sha or a failure"
        display = produced.display_sha or new

        checkout = checkout_state(repo_root)
        match mode:
            case LandingMode.PUSH:
                # Push BEFORE the local CAS: a rejection means origin advanced
                # under us — the produced commit is simply abandoned (it never
                # touched main) and the next pass re-syncs + re-produces.
                push = push_main(ctx.workspace, ctx.repo, ctx.remote or "origin", new)
                if not push.ok:
                    # Retry only helps when we actually re-sync; without a
                    # rendezvous remote a rejection is terminal.
                    if ctx.sync and ctx.remote and attempt < max_retries:
                        continue
                    rejected = f"push to origin main rejected after {attempt + 1} attempt(s)"
                    if attempt > 0:
                        rejected += " (origin keeps advancing)"
                    return _noting_drops(LandOutcome(
                        kind=LandKind.PUSH_REJECTED,
                        remote="push",
                        pushed=False,
                        detail=f"{rejected}:\n{push.detail}",
                    ), dropped)
                outcome = _cas_and_advance(repo_root, checkout, base, new, display)
                if outcome is None:
                    # Origin has the commit but local main moved mid-land (an
                    # operator commit raced us): leave local main alone — the
                    # next divergence-rescuing sync reconciles by replay (the
                    # periodic sync refuses divergence) — and report the land.
                    outcome = LandOutcome(
                        kind=LandKind.LANDED, sha=display,
                        detail=(
                            f"landed on origin ({display}); local main moved "
                            "during the land and will reconcile on the next "
                            "land or forced sync"
                        ),
                    )
                return _noting_drops(
                    replace(outcome, remote="push", pushed=True), dropped
                )
            case LandingMode.NONE | LandingMode.PR:
                outcome = _cas_and_advance(repo_root, checkout, base, new, display)
                if outcome is None:
                    # Local main moved under us (an operator commit mid-land):
                    # re-produce from the fresh tip.
                    if attempt < max_retries:
                        continue
                    return _noting_drops(LandOutcome(
                        kind=LandKind.PUSH_REJECTED,
                        detail=(
                            f"local main kept advancing during the land — "
                            f"update-ref rejected after {max_retries + 1} attempt(s)"
                        ),
                    ), dropped)
                if mode is LandingMode.PR:
                    pr = ctx.pr
                    assert pr is not None, "PR mode requires ctx.pr"
                    outcome = open_pr(
                        ctx.workspace, ctx.repo, pr, new,
                        replace(outcome, remote="pr"),
                    )
                return _noting_drops(outcome, dropped)
            case _:
                assert_never(mode)
    raise AssertionError("unreachable: the retry loop always returns")


def open_pr(
    workspace: Path,
    repo: str,
    pr: PrSpec,
    sha: str,
    outcome: LandOutcome,
) -> LandOutcome:
    """Push ``task/<stem>`` at the freshly-landed commit and open a PR.

    Defaults ``automerge=true`` / ``draft=false`` (overridable per task). The
    branch points at the squash commit on main so the PR is exactly the landed
    change. GitHub auth lives only on the manager (``gh``). Best-effort: a
    push/gh failure is recorded on ``detail`` (``pushed=False``) but never
    unwinds the already-landed local commit."""
    assert repo_lock(workspace, repo).is_held_by_current_thread(), (
        "open_pr requires the caller to hold the RepoLock"
    )
    repo_root = workspace / repo
    pr_branch = f"task/{queue_slug(pr.queue)}-{pr.task}"
    push = GitRunner(repo_root).run(
        "push", "-f", "origin", f"{sha}:refs/heads/{pr_branch}"
    )
    if not push.ok:
        return replace(
            outcome,
            pushed=False,
            detail=(
                f"{outcome.detail}\nlocal land ok ({outcome.sha}); "
                f"PR branch push failed: {push.detail}"
            ).strip(),
        )
    outcome = replace(outcome, pushed=True)
    argv = [
        "gh", "pr", "create",
        "--head", pr_branch,
        "--base", "main",
        "--title", f"task: {pr.title}",
        "--body", f"Automated nightshift land for `{pr.task}`.",
    ]
    if pr.draft:
        argv.append("--draft")
    create = subprocess.run(argv, cwd=repo_root, capture_output=True, text=True)
    if create.returncode != 0:
        return replace(
            outcome,
            detail=(
                f"{outcome.detail}\nlocal land ok ({outcome.sha}); "
                f"gh pr create failed: "
                f"{(create.stderr or create.stdout).strip()[:300]}"
            ).strip(),
        )
    pr_url = create.stdout.strip().splitlines()[-1] if create.stdout.strip() else None
    if pr.automerge and not pr.draft and pr_url:
        subprocess.run(
            ["gh", "pr", "merge", "--auto", "--squash", pr_url],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
    return replace(outcome, pr_url=pr_url)
