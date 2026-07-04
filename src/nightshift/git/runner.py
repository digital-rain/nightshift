"""GitRunner — the single subprocess seam (git greenfield §2).

The ONLY place a git subprocess is spawned. One error policy per call site,
chosen explicitly:

* :meth:`GitRunner.run`  — never raises on a non-zero exit; the caller branches
  on the returned :class:`GitResult` (or deliberately discards it, with a
  comment saying why the call is best-effort).
* :meth:`GitRunner.out`  — queries: stripped stdout on success, ``None`` on
  failure.
* :meth:`GitRunner.must` — failure is exceptional: raises
  :class:`~nightshift.git.errors.GitError` carrying the trimmed detail.

Set ``NIGHTSHIFT_GIT_TRACE=1`` to log every invocation (argv, returncode,
duration) via the ``nightshift.git`` logger.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from nightshift.git.errors import GitError


_log = logging.getLogger("nightshift.git")

# Cap on the human-readable failure detail — the historical
# ``(stderr or stdout).strip()[:300]`` idiom, defined once.
_DETAIL_LIMIT = 300


@dataclass(frozen=True)
class GitResult:
    """Outcome of one git invocation."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def detail(self) -> str:
        """Trimmed human-readable failure detail (stderr, falling back to
        stdout), the way every call site historically summarized git errors."""
        return (self.stderr or self.stdout).strip()[:_DETAIL_LIMIT]


class GitRunner:
    """Runs git in a repo. The ONLY place ``subprocess`` appears in the git layer."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def run(self, *args: str, env: Mapping[str, str] | None = None) -> GitResult:
        """Run git, returning the result regardless of exit code. Never raises
        on a non-zero exit (a missing ``git`` binary still raises ``OSError``).

        ``env`` overlays the process environment for this one invocation — the
        seam for index-redirection plumbing (``GIT_INDEX_FILE``) so tree
        surgery never touches the repo's real index.
        """
        argv = ("git", *args)
        start = time.monotonic()
        proc = subprocess.run(  # noqa: S603 — fixed "git" executable
            argv,
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            env={**os.environ, **env} if env else None,
        )
        result = GitResult(
            argv=argv,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
        if os.environ.get("NIGHTSHIFT_GIT_TRACE"):
            _log.info(
                "git %s cwd=%s rc=%d %.0fms",
                " ".join(args),
                self.repo_root,
                result.returncode,
                (time.monotonic() - start) * 1000,
            )
        return result

    def out(self, *args: str) -> str | None:
        """Query policy: stripped stdout on success, ``None`` on failure."""
        result = self.run(*args)
        return result.stdout.strip() if result.ok else None

    def must(self, *args: str) -> GitResult:
        """Exceptional-failure policy: raise :class:`GitError` on a non-zero exit."""
        result = self.run(*args)
        if not result.ok:
            raise GitError(
                f"git {' '.join(args)} failed (exit {result.returncode}): "
                f"{result.detail}",
                argv=result.argv,
                returncode=result.returncode,
            )
        return result
