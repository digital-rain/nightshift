"""Squash-merge a task branch onto local ``main`` (+ autostash and the
lines-of-code accounting for the landed squash commit).

Moved verbatim from ``engine.py`` in Phase 3 of the rebuild-in-place migration.
"""

from __future__ import annotations

from pathlib import Path

from nightshift.git import GitRunner
from nightshift.git.locks import landing_lock
from nightshift.git.refs import branch_exists
from nightshift.git.worktrees import worktree_branch


def _tracked_changes(repo_root: Path) -> list[str]:
    """Porcelain status lines for *tracked* changes in ``repo_root`` (ignores
    untracked ``??`` files, which only block a merge if they collide — and that
    case surfaces via git's own stderr instead)."""
    result = GitRunner(repo_root).run("status", "--porcelain")
    return [
        line for line in result.stdout.splitlines()
        if line.strip() and not line.startswith("??")
    ]


def porcelain_path(line: str) -> str:
    """Extract the working path from a ``git status --porcelain`` line.

    Lines are ``XY <path>``; a rename is ``R  <old> -> <new>`` — we want the
    destination. Surrounding quotes (git quotes paths with special chars) are
    stripped so callers see a clean repo-relative path."""
    path = line[3:] if len(line) > 3 else line.strip()
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip().strip('"')


def landing_blockers(repo_root: Path) -> list[str]:
    """Tracked changes in the target repo that should block a squash-merge.

    The content store is a *separate* repo, so briefs/queue config never live in
    ``repo_root`` and can't be a blocker. Every tracked change here is therefore
    genuine operator *code* WIP — return all of it (the land still stash/restores
    it when ``autostash`` is on)."""
    return _tracked_changes(repo_root)


AUTOSTASH_MESSAGE = "nightshift-autostash"


def stash_operator_work(repo_root: Path, paths: list[str]) -> str | None:
    """Set aside the operator's tracked code WIP (the given ``paths``) in the
    target repo for the land critical section. Returns the captured stash
    *commit sha*, or ``None`` when there was nothing to set aside.

    Stack-free by design: ``git stash create`` records the WIP as a commit object
    without touching the LIFO stash stack, so a human running ``git stash``
    mid-land can never perturb it. ``stash create`` does not revert the tree, so
    we then explicitly clean just the blocker ``paths``."""
    if not paths:
        return None
    git = GitRunner(repo_root)
    created = git.run("stash", "create", AUTOSTASH_MESSAGE)
    sha = created.stdout.strip()
    if not created.ok or not sha:
        return None
    # `stash create` captured the WIP but left the tree dirty; clean exactly the
    # blocker paths so the merge sees them at HEAD. Best-effort: the WIP is
    # already safely captured in ``sha``.
    git.run("checkout", "HEAD", "--", *paths)
    return sha


def restore_operator_work(repo_root: Path, sha: str, paths: list[str]) -> str | None:
    """Re-apply the set-aside WIP commit ``sha`` on top of the landed tree.
    Returns ``None`` on success, or a human-readable conflict detail when the
    apply conflicts with what the task just landed.

    On conflict the blocker ``paths`` are rolled back to the landed ``HEAD`` (so
    the tree is left clean, not littered with conflict markers) and the WIP is
    preserved on the stash stack under the ``nightshift-autostash`` message via
    ``git stash store`` so the operator can recover it by hand — never lost."""
    git = GitRunner(repo_root)
    result = git.run("stash", "apply", sha)
    if result.ok:
        return None
    # Conflict: clear the half-applied blocker paths and stash-store the sha so
    # the WIP is findable for a manual restore. Both are best-effort — the WIP
    # commit ``sha`` itself is what must survive, and it already exists.
    if paths:
        git.run("checkout", "HEAD", "--", *paths)
    git.run("stash", "store", "-m", AUTOSTASH_MESSAGE, sha)
    detail = (result.stderr.strip() or result.stdout.strip()
              or "git stash apply failed")
    return detail


def _reset_to_head(repo_root: Path) -> None:
    """Undo a half-applied squash so a failed merge never leaves ``repo_root`` in
    a conflicted/partly-staged state. Safe only because :func:`squash_to_main`
    refuses to start when the tree already has tracked changes."""
    # Best-effort: this is already the failure-recovery path; there is nothing
    # more to do if the reset itself fails.
    GitRunner(repo_root).run("reset", "--hard", "HEAD")


def conflicted_paths(repo_root: Path) -> list[str]:
    """Files left with unmerged (conflicted) entries in the index after a failed
    ``git merge --squash``. Must be read *before* :func:`_reset_to_head`."""
    result = GitRunner(repo_root).run("diff", "--name-only", "--diff-filter=U")
    return [line for line in result.stdout.splitlines() if line.strip()]


# --------------------------------------------------------------------------
# Lines-of-code accounting for the Stats page.
# --------------------------------------------------------------------------
# A landed task is a single squash commit; its "lines of code" figure is the
# churn (added + removed lines) of that commit, excluding noise the spec calls
# out: build files, docs, comments, and blank lines. Surfaced per-task so the
# Stats page can sum it across history.

# Path suffixes / basenames that are not "code": docs, build, lockfiles, data.
# Matched case-insensitively against the file's suffix and basename.
_NON_CODE_SUFFIXES = frozenset({
    ".md", ".markdown", ".rst", ".adoc", ".txt",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".lock",
    ".csv", ".tsv", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico",
})
_NON_CODE_BASENAMES = frozenset({
    "justfile", "makefile", "dockerfile", "build", "build.bazel",
    "workspace", "workspace.bazel", "package-lock.json", "yarn.lock",
    "pnpm-lock.yaml", "poetry.lock", "uv.lock", "requirements.txt",
    "go.sum", "cargo.lock",
})
_NON_CODE_BAZEL_SUFFIXES = frozenset({".bazel", ".bzl"})

# Directory prefixes whose contents are never code for LOC accounting:
# build/dist output, vendored deps, and the git dir. Matched case-insensitively
# against the full (forward-slashed) path. ``.worktrees/`` is excluded
# defensively (worktrees live under the workspace, not a landed commit, but a
# repo that nests one should never have it counted).
_NON_CODE_DIR_PREFIXES: tuple[str, ...] = (
    ".worktrees/",
    ".git/",
    "dist/",
    "build/",
    "node_modules/",
    "vendor/",
    ".venv/",
)

# Per-language line-comment prefixes, keyed by code-file suffix. A diff line
# whose content (after stripping leading whitespace) starts with one of these
# is a comment and excluded from the count.
_LINE_COMMENT_PREFIXES: dict[str, tuple[str, ...]] = {
    ".py": ("#",),
    ".pyi": ("#",),
    ".sh": ("#",),
    ".bash": ("#",),
    ".rb": ("#",),
    ".js": ("//",),
    ".jsx": ("//",),
    ".ts": ("//",),
    ".tsx": ("//",),
    ".mjs": ("//",),
    ".cjs": ("//",),
    ".css": ("/*",),
    ".scss": ("//", "/*"),
    ".go": ("//",),
    ".rs": ("//",),
    ".c": ("//", "/*"),
    ".h": ("//", "/*"),
    ".cpp": ("//", "/*"),
    ".java": ("//", "/*"),
    ".sql": ("--",),
}


def _is_code_path(path: str) -> bool:
    """True if ``path`` counts as code for LOC accounting — i.e. not a doc,
    build file, lockfile, or data/asset file (the categories the spec excludes)."""
    lowered = path.lower()
    # A path under any excluded directory (anywhere in the tree) is not code:
    # ``services/ui/dist/bundle.js``, ``a/build/x`` …
    for prefix in _NON_CODE_DIR_PREFIXES:
        if lowered.startswith(prefix) or ("/" + prefix) in lowered:
            return False
    name = path.rsplit("/", 1)[-1].lower()
    suffix = ""
    dot = name.rfind(".")
    if dot > 0:
        suffix = name[dot:]
    if name in _NON_CODE_BASENAMES:
        return False
    if suffix in _NON_CODE_SUFFIXES or suffix in _NON_CODE_BAZEL_SUFFIXES:
        return False
    return True


def _is_comment_line(content: str, suffix: str) -> bool:
    """True if a diff line's content is a blank line or a single-line comment for
    the file's language. Block-comment interiors are not tracked (a pragmatic
    line-prefix heuristic, not a parser), which is acceptable for a churn stat."""
    stripped = content.strip()
    if not stripped:
        return True
    prefixes = _LINE_COMMENT_PREFIXES.get(suffix)
    if not prefixes:
        return False
    return any(stripped.startswith(p) for p in prefixes)


def compute_code_loc(repo_root: Path, sha: str) -> int:
    """Lines of code churned by a single commit ``sha`` in ``repo_root`` (added +
    removed), excluding build files, docs, comments, and blank lines.

    Reads the commit's own diff (``git show <sha>``) so a squash commit is
    measured against its parent. Returns 0 on any git error or for the initial
    commit (no parent) so a missing figure never breaks a run record."""
    result = GitRunner(repo_root).run(
        "show", sha, "--format=", "--unified=0", "--no-color", "--no-renames"
    )
    if not result.ok:
        return 0
    return _count_diff_code_lines(result.stdout)


def _count_diff_code_lines(diff: str) -> int:
    """Count added/removed code lines in a ``git show``/``git diff`` body,
    excluding non-code paths, comments, and blank lines."""
    total = 0
    suffix = ""
    counts = False
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            # `diff --git a/<path> b/<path>` — use the destination path to decide
            # whether this file's hunks count.
            parts = line.split(" b/", 1)
            path = parts[1].strip() if len(parts) == 2 else ""
            counts = bool(path) and _is_code_path(path)
            dot = path.rfind(".")
            suffix = path[dot:].lower() if dot > 0 else ""
            continue
        if not counts:
            continue
        # Skip file/hunk headers; +++/--- are metadata, not content.
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+") or line.startswith("-"):
            if not _is_comment_line(line[1:], suffix):
                total += 1
    return total


def squash_failure_kind(recoverable: bool, detail: str) -> str:
    """Classify a :func:`squash_to_main` failure into a ``failure_kind``.

    A content conflict (overlapping edits) is ``merge_conflict``; everything else
    — a dirty ``main`` (transient), a failed commit, or a generic merge abort —
    is ``merge_rejected``.
    """
    if not recoverable and detail.startswith("merge conflict"):
        return "merge_conflict"
    return "merge_rejected"


def squash_to_main(
    workspace: Path,
    repo: str,
    task: str,
    title: str,
    *,
    queue: str | None = None,
    autostash: bool = True,
) -> tuple[str | None, str, bool]:
    """Merge the task's worktree branch as a single squash commit on the target
    repo's ``main`` (``repo_root = workspace / repo``).

    Returns ``(sha, "", False)`` on success, or ``(None, detail, recoverable)``
    on failure where ``detail`` is a human-readable reason and ``recoverable``
    says whether re-attempting the *same* squash could succeed once the user
    clears a blocker.

    Briefs/queue config live in the separate content store and are delivered via
    a run-scratch file, so the target repo only ever receives the implementation
    squash — there is nothing to snapshot up-front, and every tracked change in
    ``repo_root`` is genuine operator *code* WIP. That WIP is handled by
    ``autostash`` (default on, set per-queue via ``autostash_operator_work``):

    * ``autostash=True`` — the operator's tracked code changes are set aside with
      ``git stash`` for the brief merge+commit, then restored. A success may
      return ``(sha, detail, False)`` with a non-empty ``detail`` when restoring
      the set-aside work hit a conflict (the land still happened; the stash entry
      is preserved). Callers that key off ``sha is None`` treat this as success.
    * ``autostash=False`` — preserves the old behavior: a code blocker returns
      ``recoverable=True`` ("main has uncommitted changes").

    Two failure shapes still matter and are NOT the same:

    * **Transient blocker** (``recoverable=True``) — ``main`` has uncommitted code
      and autostash is off. Re-running after committing/stashing will work.
    * **Content conflict** (``recoverable=False``) — the branch and ``main`` made
      overlapping edits. ``git merge --squash`` aborts; re-running fails
      identically. We list the conflicting files for a human 3-way resolution.

    On any merge/commit failure the working tree is reset back to ``HEAD`` (and
    any set-aside work restored) so it is never left half-merged.

    The whole critical section (optional set-aside → merge → commit →
    reset-on-failure → restore) runs under :func:`landing_lock` (per
    workspace+repo), so concurrent queue runners (and a CLI process) serialize on
    the repo's index/HEAD/stash instead of seeing a half-merged tree.
    """
    repo_root = workspace / repo
    branch = worktree_branch(task, queue)

    if not branch_exists(repo_root, branch):
        return None, f"no task branch '{branch}' to merge (nothing to recover)", False

    with landing_lock(workspace, repo):
        blockers = landing_blockers(repo_root)
        wip_sha: str | None = None
        blocker_paths: list[str] = []
        if blockers:
            blocker_paths = [porcelain_path(line) for line in blockers]
            if autostash:
                wip_sha = stash_operator_work(repo_root, blocker_paths)
            if not autostash or wip_sha is None:
                shown = "\n".join(f"    {line}" for line in blockers[:20])
                extra = "" if len(blockers) <= 20 else f"\n    … and {len(blockers) - 20} more"
                return None, (
                    "main has uncommitted changes — commit or stash them before the "
                    f"squash-merge can run:\n{shown}{extra}"
                ), True

        restore_detail: str | None = None
        git = GitRunner(repo_root)
        try:
            merge = git.run("merge", "--squash", branch)
            if not merge.ok:
                conflicts = conflicted_paths(repo_root)
                _reset_to_head(repo_root)
                if conflicts:
                    shown = "\n".join(f"    {p}" for p in conflicts)
                    return None, (
                        f"merge conflict — '{branch}' and main made overlapping edits to "
                        f"{len(conflicts)} file(s):\n{shown}\n"
                        "This cannot be auto-recovered; resolve the 3-way merge by hand "
                        "(see `recover_task` docs) or drop the stale branch."
                    ), False
                detail = (
                    merge.stderr.strip()
                    or merge.stdout.strip()
                    or f"git merge --squash {branch} exited {merge.returncode}"
                )
                return None, f"merge --squash failed:\n{detail}", False

            commit = git.run("commit", "-m", f"task: {title}")
            if not commit.ok:
                detail = (
                    commit.stderr.strip()
                    or commit.stdout.strip()
                    or f"git commit exited {commit.returncode}"
                )
                _reset_to_head(repo_root)
                return None, f"commit failed:\n{detail}", False

            sha = git.run("rev-parse", "--short", "HEAD").stdout.strip()
        finally:
            if wip_sha:
                restore_detail = restore_operator_work(repo_root, wip_sha, blocker_paths)

    if restore_detail:
        return sha, (
            f"landed ({sha}), but your set-aside working changes could not be "
            f"reapplied cleanly — they are kept in `git stash` for manual "
            f"restore:\n{restore_detail}"
        ), False
    return sha, "", False
