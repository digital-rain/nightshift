"""``squash_to_main`` — the legacy squash-land entry point (now a shim over the
Phase-6 plumbing pipeline) — plus the lines-of-code accounting for the landed
squash commit.

The autostash machinery that used to live here (``stash_operator_work``,
``restore_operator_work``, ``AUTOSTASH_MESSAGE``, the ``reset --hard`` unwind)
is deleted: the plumbing land never touches the working tree, so there is
nothing to set aside and nothing to unwind. The working-tree status helpers
(``landing_blockers``/``porcelain_path``) moved to :mod:`nightshift.preflight`.
"""

from __future__ import annotations

from pathlib import Path
from typing import assert_never

from nightshift.git import GitRunner
from nightshift.git.landing import RepoContext, integrate_and_push, squash_produce
from nightshift.git.worktrees import worktree_branch
from nightshift.lifecycle import LandingMode, LandKind


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
) -> tuple[str | None, str, bool]:
    """Merge the task's worktree branch as a single squash commit on the target
    repo's ``main`` (``repo_root = workspace / repo``) — the legacy entry point
    (``runner_legacy``/CLI recover), now a shim over
    :func:`nightshift.git.landing.integrate_and_push` in local-only mode.

    Returns the historical tuple: ``(sha, "", False)`` on success, or
    ``(None, detail, recoverable)`` on failure. The plumbing pipeline never
    touches the working tree, so:

    * operator WIP never blocks (the old ``autostash`` knob is gone); a land
      overlapping uncommitted work returns ``(sha, detail, False)`` with a
      CHECKOUT_BEHIND notice in ``detail`` — the land happened, the checkout
      was left behind ``main``;
    * a content conflict returns ``recoverable=False`` with the historical
      "merge conflict — …" wording (so :func:`squash_failure_kind` still
      classifies it ``merge_conflict``);
    * there is no half-merged state to unwind on any failure.
    """
    repo_root = workspace / repo
    branch = worktree_branch(task, queue)
    outcome = integrate_and_push(
        RepoContext(workspace, repo),
        squash_produce(repo_root, branch, title),
        mode=LandingMode.NONE,
    )
    match outcome.kind:
        case LandKind.LANDED:
            return outcome.sha, outcome.detail, False
        case LandKind.CHECKOUT_BEHIND:
            return outcome.sha, outcome.detail, False
        case LandKind.CONFLICT:
            if outcome.conflicts:
                # The historical wording, pinned by squash_failure_kind and the
                # legacy runner's operator messages.
                shown = "\n".join(f"    {p}" for p in outcome.conflicts)
                return None, (
                    f"merge conflict — '{branch}' and main made overlapping edits to "
                    f"{len(outcome.conflicts)} file(s):\n{shown}\n"
                    "This cannot be auto-recovered; resolve the 3-way merge by hand "
                    "(see `recover_task` docs) or drop the stale branch."
                ), False
            return None, outcome.detail, False
        case LandKind.PUSH_REJECTED:
            # Local-only mode: main kept moving under the land — transient.
            return None, outcome.detail, True
        case LandKind.NO_CHANGES | LandKind.ADOPTED | LandKind.TRANSPORT_FAILED:
            # Unreachable in local squash mode; report rather than crash.
            return None, outcome.detail or str(outcome.kind), False
        case _:
            assert_never(outcome.kind)
