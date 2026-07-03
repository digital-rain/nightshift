"""Ref-level git reads and the ref-move primitives of the plumbing land —
rev-parse, ancestry, branch existence, the ``update-ref`` CAS on canonical
``main``, and the best-effort checkout advance (git greenfield §3/§5).

Since Phase 6, refs are authoritative: a land moves ``refs/heads/main``
atomically and the operator checkout is advanced *best-effort* afterwards.
When the advance would clobber uncommitted operator work it is refused and
the checkout is left behind ``main`` (detached at its old commit) — the
CHECKOUT_BEHIND outcome. ``git status`` in the repo therefore never changes
across a land, whatever the outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nightshift.git import GitResult, GitRunner


MAIN_REF = "refs/heads/main"


def rev_parse(repo_root: Path, ref: str) -> str | None:
    return GitRunner(repo_root).out("rev-parse", ref)


def is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    """True when ``ancestor`` is reachable from ``descendant`` (inclusive)."""
    return GitRunner(repo_root).run("merge-base", "--is-ancestor", ancestor, descendant).ok


def branch_exists(repo_root: Path, branch: str) -> bool:
    return GitRunner(repo_root).run(
        "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"
    ).ok


def main_sha(repo_root: Path) -> str | None:
    """Canonical ``main`` tip. The branch ref is authoritative (a checkout
    left behind by a refused advance must not lie about main); a repo without
    a ``main`` branch falls back to ``HEAD`` (the pre-Phase-6 reading)."""
    return rev_parse(repo_root, MAIN_REF) or rev_parse(repo_root, "HEAD")


def update_main_cas(repo_root: Path, new: str, old: str) -> GitResult:
    """Atomically move ``refs/heads/main`` from ``old`` to ``new`` — git's own
    compare-and-swap. A non-zero exit means the ref was not at ``old`` (a
    concurrent move won the race); nothing was changed."""
    return GitRunner(repo_root).run("update-ref", MAIN_REF, new, old)


@dataclass(frozen=True)
class ReplayResult:
    """Outcome of one plumbing cherry-pick (:func:`replay_commit`). ``sha`` is
    the new tip — ``base`` itself when the replay was redundant — or ``None``
    on failure; ``conflict`` distinguishes a real merge conflict from an
    unreplayable commit (no parent, plumbing error)."""

    sha: str | None
    conflict: bool = False
    detail: str = ""


def replay_commit(
    git: GitRunner, base: str, sha: str, *, extra_trailer: str | None = None
) -> ReplayResult:
    """THE plumbing cherry-pick: replay one commit onto ``base`` (three-way,
    merge base = the commit's parent) without touching any checkout.

    Shared by the sync divergence rescue and the resolve producer:

    * clean → the new tip's sha;
    * redundant (its content already present on ``base``, e.g. a squash that
      origin re-squashed) → ``base`` unchanged;
    * conflicting or unreplayable → ``sha=None`` with an accurate ``detail``;
      nothing was created that needs unwinding (the source commit stays
      reachable from its original ref/reflog).

    ``extra_trailer`` (a full ``Key: value`` line) is appended to the replayed
    commit's message as a trailer block when not already present — the land
    idempotency trailer of the resolve path. The redundant path returns
    ``base`` untouched and cannot carry it.
    """
    parent = git.out("rev-parse", f"{sha}^")
    if parent is None:
        return ReplayResult(sha=None, detail=f"commit {sha[:12]} has no parent to replay from")
    merged = git.run("merge-tree", "--write-tree", f"--merge-base={parent}", base, sha)
    if not merged.ok:
        return ReplayResult(sha=None, conflict=True, detail=merged.detail or "merge conflict")
    lines = [ln for ln in merged.stdout.splitlines() if ln.strip()]
    if not lines:
        return ReplayResult(sha=None, detail="merge-tree produced no tree")
    tree = lines[0].strip()
    if tree == git.out("rev-parse", f"{base}^{{tree}}"):
        return ReplayResult(sha=base)
    message = git.out("log", "-1", "--format=%B", sha) or f"replay of {sha[:12]}"
    if extra_trailer and extra_trailer not in message:
        message = f"{message.rstrip()}\n\n{extra_trailer}"
    commit = git.run("commit-tree", tree, "-p", base, "-m", message)
    if not commit.ok:
        return ReplayResult(sha=None, detail=f"commit failed:\n{commit.detail}")
    return ReplayResult(sha=commit.stdout.strip())


@dataclass(frozen=True)
class CheckoutState:
    """The operator checkout as observed BEFORE a ref move: its commit and the
    branch HEAD is attached to (``ref`` is None when detached). Captured up
    front because once ``main`` moves, an attached HEAD reads the new tip."""

    sha: str | None
    ref: str | None

    @property
    def on_main(self) -> bool:
        return self.ref == MAIN_REF


def checkout_state(repo_root: Path) -> CheckoutState:
    git = GitRunner(repo_root)
    return CheckoutState(
        sha=git.out("rev-parse", "HEAD"),
        ref=git.out("symbolic-ref", "-q", "HEAD"),
    )


def advance_checkout(repo_root: Path, checkout: CheckoutState, new: str) -> bool:
    """Best-effort advance of the operator checkout after ``refs/heads/main``
    moved to ``new``. Returns True when the checkout now matches ``main``
    (or was never main's checkout); False → CHECKOUT_BEHIND.

    ``git read-tree -m -u <old> <new>`` is the two-tree merge at the core of
    ``reset --keep``: it carries uncommitted operator changes forward and
    refuses (touching nothing) when any dirty or untracked file would be
    clobbered. On refusal HEAD is detached at the old commit so index and
    working tree stay coherent — the checkout is simply left behind ``main``,
    with the operator's work exactly as it was.
    """
    if checkout.sha is None or checkout.sha == new:
        return True
    if checkout.ref is not None and not checkout.on_main:
        # The operator has some other branch checked out; main's move never
        # touches their tree. Nothing to advance.
        return True
    git = GitRunner(repo_root)
    if checkout.ref is None:
        # Already detached (typically a previous refused advance, possibly
        # deliberate operator archaeology) — never rewrite a detached
        # checkout. It stays behind until the operator checks out main.
        return False
    advance = git.run("read-tree", "-m", "-u", checkout.sha, new)
    if advance.ok:
        return True
    # Refused: the operator's uncommitted work overlaps the land. Detach HEAD
    # at the pre-land commit (best-effort) so the checkout stays coherent.
    git.run("update-ref", "--no-deref", "HEAD", checkout.sha)
    return False
