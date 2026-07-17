"""The worker interface — prompt construction, claude argv/bin resolution,
child-process environment, and the worker-output sentinels (result line,
``NIGHTSHIFT_BLOCKED``).

Moved verbatim from ``engine.py`` in Phase 3 of the rebuild-in-place migration.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from nightshift._paths import asset


def _artifact_header(artifact_files: dict[str, str] | None) -> str:
    """Header lines naming each materialized input artifact by role, e.g.
    ``The PLAN file is: <path>``. Empty when there are no artifacts."""
    if not artifact_files:
        return ""
    lines = [
        f"The {name.upper()} file is: {path}\n"
        for name, path in artifact_files.items()
    ]
    return "".join(lines)


def build_prompt(
    task: str,
    *,
    task_file: str,
    validate_cmd: str,
    loop: bool = False,
    loop_max_iterations: int = 0,
    split: bool = False,
    split_dir: str | None = None,
    artifact_files: dict[str, str] | None = None,
) -> str:
    """Build the worker prompt matching CI injection format.

    ``task_file`` is the path to the **already-materialised** run-scratch brief
    (see :func:`materialize_brief`) — a read-only file *outside* the target
    worktree, so the brief never enters the repo the agent lands in.
    ``validate_cmd`` is the queue's resolved validate command, injected as
    ``$VALIDATE`` so the worker runs the queue's own gate — matching the command
    the engine later enforces. The caller (engine or worker) resolves both from
    the queue config before calling, so this helper needs neither root.

    When ``loop`` is True, use the ralph-loop prompt instead of the standard
    nightshift-local prompt. ``loop_max_iterations`` (0 = unlimited) is
    injected as ``$MAX_ITERATIONS``.

    When ``split`` is True, the worker is in decomposition mode — ``$SPLIT`` is
    injected as ``true`` and ``$SPLIT_DIR`` points to the directory the worker
    writes subtask ``.md`` briefs into (see :func:`split_output_dir`).
    """
    if loop:
        prompt_file = asset("prompts", "nightshift-ralph-loop.md")
        prompt_body = prompt_file.read_text()
        return (
            f"Your task file is: {task_file}\n"
            f"The TASK variable is: {task}\n"
            f"The TASK_FILE variable is: {task_file}\n"
            f"The VALIDATE command is: {validate_cmd}\n"
            f"The MAX_ITERATIONS variable is: {loop_max_iterations}\n\n"
            f"{prompt_body}"
        )
    prompt_file = asset("prompts", "nightshift-local.md")
    prompt_body = prompt_file.read_text()
    header = (
        f"Your task file is: {task_file}\n"
        f"The TASK variable is: {task}\n"
        f"The TASK_FILE variable is: {task_file}\n"
        f"The VALIDATE command is: {validate_cmd}\n"
    )
    header += _artifact_header(artifact_files)
    if split:
        header += (
            f"The SPLIT variable is: true\n"
            f"The SPLIT_DIR variable is: {split_dir}\n"
        )
    return f"{header}\n{prompt_body}"


def build_doc_prompt(
    task: str,
    *,
    prompt_asset: str,
    task_file: str,
    artifact_files: dict[str, str],
    output_file: str,
    prompt_text: str | None = None,
) -> str:
    """Build a workflow doc-step prompt: a task-varying header (task file,
    artifact paths, ``$OUTPUT_FILE``) followed by the byte-stable charter body.

    ``prompt_text`` is the manager-resolved body riding the work order
    (workflow-editor spec §4 — operator prompts shadow shipped ones and remote
    workers cannot read the manager's ``.nightshift/``); when absent the body
    is read from the shipped ``assets/prompts/<prompt_asset>`` (wire compat
    with orders from an older manager). The body is never interpolated, so it
    caches across runs of the same step (spec §8.2)."""
    prompt_body = (
        prompt_text if prompt_text is not None
        else asset("prompts", prompt_asset).read_text()
    )
    header = (
        f"Your task file is: {task_file}\n"
        f"The TASK variable is: {task}\n"
        f"The TASK_FILE variable is: {task_file}\n"
    )
    header += _artifact_header(artifact_files)
    header += f"The OUTPUT_FILE variable is: {output_file}\n"
    return f"{header}\n{prompt_body}"


def build_claude_argv(
    prompt: str,
    model: str,
    max_turns: int | None,
    resume: str | None = None,
) -> list[str]:
    """Build the claude CLI argument vector.

    Uses ``--output-format stream-json --verbose`` so the run emits structured
    events: ``backends.AgentStreamParser`` renders the readable text for the live
    log and captures turn/token/cost telemetry from the final ``result`` event.
    The parser passes any non-JSON line through unchanged, so an older CLI that
    ignores these flags still streams output (just without telemetry).
    """
    argv = [
        "claude",
        "-p", prompt,
        "--model", model,
        "--allowedTools", "Bash,Edit,MultiEdit,Write,Read,Glob,Grep,LS",
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",
    ]
    if max_turns is not None:
        argv.extend(["--max-turns", str(max_turns)])
    # Session resume (spec §7.5): a worker-local hint to reuse the prior
    # session's context (prompt-cache hits). A hint, never a dependency — the
    # prompt still carries every declared input.
    if resume:
        argv.extend(["--resume", str(resume)])
    return argv


# Bin dirs that interactive/login shells commonly add but a service started
# from a non-login shell (e.g. the UI server) may be missing.
EXTRA_BIN_DIRS = (
    str(Path.home() / ".local/bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
)


def resolve_claude_bin(config: dict | None = None) -> str:
    """Resolve the ``claude`` executable robustly.

    Order: an explicit ``claude_bin`` in config, then ``$PATH`` (shutil.which),
    then common install dirs that non-login shells miss. Falls back to the bare
    name so the caller surfaces a clear "not found" error.
    """
    if config and config.get("claude_bin"):
        return os.path.expanduser(str(config["claude_bin"]))
    found = shutil.which("claude")
    if found:
        return found
    for d in EXTRA_BIN_DIRS:
        cand = Path(d) / "claude"
        if cand.exists():
            return str(cand)
    return "claude"


def worker_env(worktree: Path | str | None = None) -> dict[str, str]:
    """A child-process environment with the common bin dirs on PATH, so the
    worker (and the tools it shells out to) resolve even when the server was
    launched from a non-login shell.

    ``worktree`` (a task worktree dir): when given, ``<worktree>/src`` is
    prepended to ``PYTHONPATH`` so ``import nightshift`` resolves to the
    worktree's own source. Worktrees symlink the target repo's ``.venv``
    (:data:`SYMLINK_TARGETS`), whose editable install points at the *main*
    checkout's ``src`` — without this override, ``just validate`` (and the
    agent's own ``python`` runs) would exercise main's code instead of the
    branch under test, failing any task that adds code + a test for it.
    """
    env = os.environ.copy()
    parts = env.get("PATH", "").split(os.pathsep)
    for d in EXTRA_BIN_DIRS:
        if d not in parts:
            parts.append(d)
    env["PATH"] = os.pathsep.join(p for p in parts if p)
    if worktree is not None:
        wt_src = str(Path(worktree) / "src")
        if Path(wt_src).is_dir():
            existing = [
                p
                for p in env.get("PYTHONPATH", "").split(os.pathsep)
                if p and p != wt_src
            ]
            env["PYTHONPATH"] = os.pathsep.join([wt_src, *existing])
    return env


RESOLVE_PROMPT_FILE = asset("prompts", "nightshift-resolve.md")


def build_resolve_prompt(task: str, *, task_file: str, context: str) -> str:
    """Build the prompt for the resolve agent — the resolution charter plus the
    concrete reason the squash-merge to main failed.

    ``task_file`` is the path to the materialised run-scratch brief (outside the
    worktree), never a path inside the target repo."""
    prompt_body = RESOLVE_PROMPT_FILE.read_text()
    return (
        f"Your task file is: {task_file}\n"
        f"The TASK variable is: {task}\n\n"
        f"## Why the merge to main failed\n\n{context}\n\n"
        f"{prompt_body}"
    )


_PYTEST_SUMMARY = re.compile(r"(\d+)\s+passed")


def extract_result_line(validate_stdout: str, validate_stderr: str = "") -> str:
    """Derive a one-line result (e.g. ``All 1291 tests pass``) from validate output."""
    text = validate_stdout or ""
    last_match = None
    for match in _PYTEST_SUMMARY.finditer(text):
        last_match = match
    if last_match is not None:
        return f"All {last_match.group(1)} tests pass"
    for line in reversed((text + "\n" + (validate_stderr or "")).splitlines()):
        if line.strip():
            return line.strip()[:120]
    return "validate passed"


# Honest-failure sentinel: an agent that cannot complete a task makes **no
# commits** and emits a final log line ``NIGHTSHIFT_BLOCKED: <reason>`` (no
# ``.BLOCKED`` file is written anywhere). The worker/engine scan captured output
# for this marker to surface a ``blocked`` status + reason.
_BLOCKED_SENTINEL = re.compile(r"^\s*NIGHTSHIFT_BLOCKED:\s*(.*\S)?\s*$")


def extract_blocked_reason(text: str) -> str | None:
    """Return the reason from the **last** ``NIGHTSHIFT_BLOCKED: <reason>`` line in
    ``text``, or ``None`` when the sentinel is absent.

    A bare ``NIGHTSHIFT_BLOCKED:`` with no reason still counts as a block and
    yields a generic ``"blocked"`` reason. Scanning the whole captured log (last
    match wins) tolerates the marker appearing mid-stream before the agent's
    final summary."""
    reason: str | None = None
    for line in text.splitlines():
        match = _BLOCKED_SENTINEL.match(line)
        if match:
            reason = (match.group(1) or "").strip() or "blocked"
    return reason


# Workflow routing sentinel (spec §7.3): an agent emits ``NIGHTSHIFT_SIGNAL:
# <token>`` to route the workflow (e.g. ``plan-trivial``, ``review-clear``).
# Same last-match-wins mechanics as ``NIGHTSHIFT_BLOCKED``.
_SIGNAL_SENTINEL = re.compile(r"^\s*NIGHTSHIFT_SIGNAL:\s*(\S.*?)\s*$")


def extract_signal(text: str) -> str | None:
    """Return the token from the **last** ``NIGHTSHIFT_SIGNAL: <token>`` line in
    ``text``, or ``None`` when the sentinel is absent. A bare
    ``NIGHTSHIFT_SIGNAL:`` with no token is ignored (no routing)."""
    token: str | None = None
    for line in text.splitlines():
        match = _SIGNAL_SENTINEL.match(line)
        if match:
            captured = (match.group(1) or "").strip()
            if captured:
                token = captured
    return token
