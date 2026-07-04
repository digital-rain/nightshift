"""Sandboxed tool registry for the agentic loop (spec §5.2).

The loop hands the model a fixed set of tools; this module defines them, their
JSON-schema specs, and their handlers. Two properties matter for the rest of the
harness:

* **Sandbox (spec invariant 3).** Every path argument is resolved through
  :func:`_resolve_in_sandbox`, which rejects absolute paths and anything that
  escapes ``root`` (``..`` traversal, symlinks pointing outside). ``root`` is the
  worker's ``spec.cwd``. This mirrors the path-traversal intent of
  :mod:`nightshift.repos` / :func:`nightshift.playlists.is_valid_name`.
* **Immutability (spec invariant 7b).** The registry is built **once** per run
  from a fixed ``tools_enabled`` set and never mutated. ``tools`` sits at cache
  prefix position 0, so :meth:`ToolRegistry.specs` must be byte-stable across the
  run — hence the deterministic serialization (sorted names, ``sort_keys=True``).

Network-free except ``grep`` / ``run_bash``, which shell out.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nightshift.agent.apply import ApplyError, apply_edits, parse_blocks


# How much tool output we are willing to hand back to the model in one result.
# Bounds both context cost and a runaway command flooding the loop.
_MAX_OUTPUT_CHARS = 20_000
_TRUNCATION_NOTE = "\n…[truncated]"

# The full tool set; a run's registry is the subset named by ``tools_enabled``.
ALL_TOOLS = ("read_file", "list_dir", "grep", "edit_file", "write_file", "run_bash")


class SandboxError(Exception):
    """A path argument escaped the sandbox root (absolute, ``..``, or symlink)."""


def _resolve_in_sandbox(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root``, refusing any escape.

    Rejects absolute inputs outright, then resolves symlinks and ``..`` and
    confirms the result is still within ``root``. Returns the absolute resolved
    path; raises :class:`SandboxError` otherwise.
    """
    if rel is None or rel == "":
        raise SandboxError("empty path")
    candidate = Path(rel)
    if candidate.is_absolute():
        raise SandboxError(f"absolute path not allowed: {rel!r}")
    root_resolved = root.resolve()
    target = (root_resolved / candidate).resolve()
    if target != root_resolved and not target.is_relative_to(root_resolved):
        raise SandboxError(f"path escapes sandbox: {rel!r}")
    return target


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return text[:_MAX_OUTPUT_CHARS] + _TRUNCATION_NOTE


def _was_truncated(text: str) -> bool:
    return len(text) > _MAX_OUTPUT_CHARS


@dataclass(frozen=True)
class ToolResult:
    """A dispatch outcome. ``is_error`` true means the model should retry — e.g.
    an :class:`~nightshift.agent.apply.ApplyError` surfaced as text, not a crash.
    ``truncated`` true means the content was clipped at the output cap
    (``_MAX_OUTPUT_CHARS``) — telemetry for tuning the cap, not a failure."""

    content: str
    is_error: bool = False
    truncated: bool = False


# A handler takes the validated input dict and returns a ToolResult.
Handler = Callable[[dict[str, Any]], ToolResult]


@dataclass(frozen=True)
class _ToolDef:
    name: str
    description: str
    properties: dict[str, Any]
    required: list[str]
    handler: Handler

    def spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": self.properties,
                "required": self.required,
            },
        }


class ToolRegistry:
    """An immutable, per-run set of tools.

    Build via :func:`build_registry`. :meth:`specs` is deterministically ordered
    so the serialized tool block is byte-stable (cache prefix invariant 7a/7b).
    """

    def __init__(self, defs: dict[str, _ToolDef]) -> None:
        # Sorted by name so specs() and the JSON form are byte-stable.
        self._defs = {name: defs[name] for name in sorted(defs)}

    def names(self) -> list[str]:
        return list(self._defs)

    def specs(self) -> list[dict[str, Any]]:
        """Tool definitions, sorted by name (stable cache prefix)."""
        return [d.spec() for d in self._defs.values()]

    def specs_json(self) -> str:
        """The canonical serialized form — sorted keys, sorted tools.

        This is the exact byte sequence that should feed the cache-prefix; the
        transport may re-serialize for the wire, but stability is pinned here.
        """
        return json.dumps(self.specs(), sort_keys=True, separators=(",", ":"))

    def dispatch(self, name: str, tool_input: dict[str, Any]) -> ToolResult:
        """Run tool ``name`` with ``tool_input``; never raises for tool-level
        failures — those come back as ``is_error=True`` so the loop can let the
        model retry. An unknown name is itself a retryable tool error."""
        tool = self._defs.get(name)
        if tool is None:
            return ToolResult(f"unknown tool: {name!r}", is_error=True)
        try:
            return tool.handler(tool_input)
        except SandboxError as exc:
            return ToolResult(f"sandbox error: {exc}", is_error=True)
        except ApplyError as exc:
            return ToolResult(f"edit failed ({exc.kind}): {exc.message}", is_error=True)
        except (OSError, ValueError) as exc:
            return ToolResult(f"{name} failed: {exc}", is_error=True)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


def _number_lines(text: str, start: int) -> str:
    out = []
    for offset, line in enumerate(text.splitlines()):
        out.append(f"{start + offset:>6}\t{line}")
    return "\n".join(out)


def _read_file(root: Path, context_policy: str) -> Handler:
    def handler(args: dict[str, Any]) -> ToolResult:
        path = _resolve_in_sandbox(root, args["path"])
        if not path.is_file():
            return ToolResult(f"not a file: {args['path']!r}", is_error=True)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        start = int(args.get("start", 1))
        end = int(args.get("end", total))
        start = max(1, start)
        end = min(total, end)
        if start > end:
            return ToolResult(f"empty range {start}-{end}", is_error=True)
        # context_policy=spans nudges the model away from whole-file dumps: when
        # it asks for an unbounded read of a large file, hint at narrowing.
        hint = ""
        if (
            context_policy == "spans"
            and "start" not in args
            and "end" not in args
            and total > 400
        ):
            hint = (
                f"\n…[{total} lines; pass start/end to read a span "
                "instead of the whole file]"
            )
            end = min(total, 400)
        body = _number_lines("\n".join(lines[start - 1 : end]), start)
        return ToolResult(_truncate(body) + hint, truncated=_was_truncated(body))

    return handler


def _list_dir(root: Path) -> Handler:
    def handler(args: dict[str, Any]) -> ToolResult:
        path = _resolve_in_sandbox(root, args.get("path", "."))
        if not path.is_dir():
            return ToolResult(f"not a directory: {args.get('path', '.')!r}", is_error=True)
        entries = []
        for child in sorted(path.iterdir()):
            entries.append(child.name + ("/" if child.is_dir() else ""))
        listing = "\n".join(entries)
        return ToolResult(_truncate(listing), truncated=_was_truncated(listing))

    return handler


def _grep(root: Path, timeout: float | None) -> Handler:
    def handler(args: dict[str, Any]) -> ToolResult:
        pattern = args["pattern"]
        literal = bool(args.get("literal", False))
        sub = args.get("path", ".")
        search_root = _resolve_in_sandbox(root, sub)
        rg = shutil.which("rg")
        if rg is not None:
            argv = [rg, "--line-number", "--no-heading", "--color", "never"]
            if literal:
                argv.append("--fixed-strings")
            if args.get("glob"):
                argv += ["--glob", str(args["glob"])]
            argv += [pattern, str(search_root)]
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout if timeout and timeout > 0 else None,
            )
            # rg exit 1 = "no matches" (not an error); >1 is a real failure.
            if proc.returncode > 1:
                return ToolResult(f"grep failed: {proc.stderr.strip()}", is_error=True)
            return ToolResult(
                _truncate(proc.stdout) or "(no matches)",
                truncated=_was_truncated(proc.stdout),
            )
        return _grep_python(search_root, pattern, literal, args.get("glob"))

    return handler


def _grep_python(
    search_root: Path, pattern: str, literal: bool, glob: str | None
) -> ToolResult:
    """Pure-Python ``grep`` fallback used when ``rg`` is absent (also the path
    tests exercise without depending on ripgrep being installed)."""
    try:
        regex = re.compile(re.escape(pattern) if literal else pattern)
    except re.error as exc:
        return ToolResult(f"bad pattern: {exc}", is_error=True)
    files = (
        [search_root]
        if search_root.is_file()
        else sorted(search_root.rglob(glob or "*"))
    )
    out: list[str] = []
    for f in files:
        if not f.is_file():
            continue
        try:
            for lineno, line in enumerate(
                f.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if regex.search(line):
                    out.append(f"{f}:{lineno}:{line}")
        except OSError:
            continue
    matches = "\n".join(out)
    return ToolResult(
        _truncate(matches) or "(no matches)", truncated=_was_truncated(matches)
    )


def _edit_file(root: Path) -> Handler:
    def handler(args: dict[str, Any]) -> ToolResult:
        path = _resolve_in_sandbox(root, args["path"])
        if not path.is_file():
            return ToolResult(f"not a file: {args['path']!r}", is_error=True)
        original = path.read_text(encoding="utf-8", errors="replace")
        blocks = parse_blocks(args["edits"])  # ApplyError → caught in dispatch
        if not blocks:
            return ToolResult("no SEARCH/REPLACE blocks found", is_error=True)
        updated = apply_edits(original, blocks)  # atomic; raises on any failure
        path.write_text(updated, encoding="utf-8")
        return ToolResult(f"applied {len(blocks)} edit(s) to {args['path']}")

    return handler


def _write_file(root: Path) -> Handler:
    def handler(args: dict[str, Any]) -> ToolResult:
        path = _resolve_in_sandbox(root, args["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["contents"], encoding="utf-8")
        return ToolResult(f"wrote {args['path']}")

    return handler


def _run_bash(
    root: Path,
    timeout: float | None,
    should_abort: Callable[[], str | None] | None,
) -> Handler:
    def handler(args: dict[str, Any]) -> ToolResult:
        if should_abort is not None and should_abort():
            return ToolResult("aborted before run", is_error=True)
        try:
            proc = subprocess.run(
                args["command"],
                shell=True,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=timeout if timeout and timeout > 0 else None,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(f"command timed out after {timeout}s", is_error=True)
        body = (proc.stdout or "") + (proc.stderr or "")
        prefix = f"[exit {proc.returncode}]\n"
        return ToolResult(
            prefix + _truncate(body),
            is_error=proc.returncode != 0,
            truncated=_was_truncated(body),
        )

    return handler


def build_registry(
    root: Path,
    *,
    timeout: float | None = None,
    tools_enabled: list[str] | None = None,
    context_policy: str = "spans",
    should_abort: Callable[[], str | None] | None = None,
) -> ToolRegistry:
    """Build the immutable per-run registry.

    ``tools_enabled`` (default: all) is the allow-list, resolved **once** here so
    the tool set is fixed for the run. ``context_policy`` tunes ``read_file``;
    ``should_abort`` gates ``run_bash``.
    """
    enabled = set(tools_enabled) if tools_enabled is not None else set(ALL_TOOLS)
    catalog: dict[str, _ToolDef] = {
        "read_file": _ToolDef(
            "read_file",
            "Read a file (line-numbered). Pass start/end (1-based) to read a span.",
            {
                "path": {"type": "string"},
                "start": {"type": "integer"},
                "end": {"type": "integer"},
            },
            ["path"],
            _read_file(root, context_policy),
        ),
        "list_dir": _ToolDef(
            "list_dir",
            "List directory entries (directories end with '/').",
            {"path": {"type": "string"}},
            [],
            _list_dir(root),
        ),
        "grep": _ToolDef(
            "grep",
            "Search file contents. Set literal=true for a fixed string.",
            {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "glob": {"type": "string"},
                "literal": {"type": "boolean"},
            },
            ["pattern"],
            _grep(root, timeout),
        ),
        "edit_file": _ToolDef(
            "edit_file",
            "Edit a file via SEARCH/REPLACE blocks (aider fences). Atomic.",
            {"path": {"type": "string"}, "edits": {"type": "string"}},
            ["path", "edits"],
            _edit_file(root),
        ),
        "write_file": _ToolDef(
            "write_file",
            "Create or overwrite a file with the given contents.",
            {"path": {"type": "string"}, "contents": {"type": "string"}},
            ["path", "contents"],
            _write_file(root),
        ),
        "run_bash": _ToolDef(
            "run_bash",
            "Run a shell command in the sandbox root.",
            {"command": {"type": "string"}},
            ["command"],
            _run_bash(root, timeout, should_abort),
        ),
    }
    return ToolRegistry({name: catalog[name] for name in catalog if name in enabled})
