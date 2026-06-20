"""Pluggable worker backends — the "shim".

A *backend* turns a built prompt into worker activity inside the task's git
worktree, streaming output lines through ``emit_log`` and honouring early
abort (skip / stop). The engine picks one per run by name; this is the single
seam where Nightshift decides *who* does the work.

Four are registered:

- ``claude``    — Claude Code CLI (default; fully **agentic**: edits files, runs bash).
- ``cursor``    — Cursor's headless agent (``cursor-agent``; **agentic**).
- ``anthropic`` — the Anthropic Messages API directly (**single-shot completion**,
  NOT agentic — it streams a model response but does not edit files).
- ``ollama``    — a local Ollama model (**single-shot completion**, NOT agentic).

The two API backends exist so we can measure raw model latency/throughput
against the agent CLIs and to give a foundation for a future tool loop. Because
they don't edit files, a run using them finishes as "no changes" (the engine's
no-commit guard) rather than landing a commit.

This module intentionally does **not** import :mod:`nightshift.engine` at the
top level for the claude helpers it reuses; instead it references the engine
*module* so monkeypatching (and the no-import-cycle property) both hold:
``engine`` imports this module lazily, from inside ``run_task``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from nightshift import engine


EmitLog = Callable[[str], None]
ShouldAbort = Callable[[], "str | None"]
# Invoked with the worker subprocess pid once it launches (subprocess backends
# only); lets the engine record a liveness signal for stale-run reconciliation.
OnWorkerStart = Callable[[int], None]

# Conventional returncode used by :func:`_stream_subprocess` when the worker
# binary itself could not be launched (distinct from a non-zero worker exit).
LAUNCH_FAILED = 127


@dataclass
class WorkerSpec:
    """Everything a backend needs to do one task's worth of work."""

    task: str
    prompt: str
    model: str
    max_turns: int | None
    cwd: Path
    env: dict[str, str]
    config: dict[str, Any]


@dataclass
class WorkerResult:
    """Outcome of a backend run.

    ``returncode`` mirrors a process exit code (0 = ok). ``aborted`` is set to
    ``"skipped"``/``"stopped"`` when the controller asked the worker to stop.
    ``error`` carries a human-readable reason for a launch/transport failure.

    The telemetry fields (``turns``, ``input_tokens``, ``output_tokens``,
    ``cost_usd``) are best-effort: a backend that can report them does, the rest
    leave them ``None``. They flow through the run record so the manager can roll
    them up per worker / model / backend / queue. Single-shot API backends report
    ``turns=1``; the agent CLIs report their own turn count.
    """

    returncode: int
    aborted: str | None = None
    error: str | None = None
    turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


def _usage_tokens(usage: dict[str, Any] | None) -> tuple[int | None, int | None]:
    """Pull (input, output) token counts from a backend ``usage`` blob.

    Input folds in cache-creation/read tokens (Anthropic splits them out) so the
    figure reflects total input throughput; missing fields stay ``None``.
    """
    if not isinstance(usage, dict):
        return None, None
    inp = usage.get("input_tokens")
    if inp is not None:
        inp = int(inp) + int(usage.get("cache_creation_input_tokens", 0) or 0) + int(
            usage.get("cache_read_input_tokens", 0) or 0
        )
    out = usage.get("output_tokens")
    out = int(out) if out is not None else None
    return inp, out


class AgentStreamParser:
    """Defensive parser for an agent CLI's ``stream-json`` output.

    Each subprocess line is fed in; the parser returns the human-readable text to
    surface in the live log (so the Now screen stays useful) and quietly captures
    turns / token usage / cost from the final ``result`` event. Anything that is
    not JSON is passed through verbatim, so an older CLI that ignores the
    ``--output-format`` flag still streams readable output — it just yields no
    telemetry. Schema differences between Claude Code and cursor-agent are
    tolerated: usage/turns/cost are read wherever they appear.
    """

    def __init__(self) -> None:
        self.turns: int | None = None
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.cost_usd: float | None = None

    def _capture(self, ev: dict[str, Any]) -> None:
        if "num_turns" in ev and ev["num_turns"] is not None:
            self.turns = int(ev["num_turns"])
        cost = ev.get("total_cost_usd", ev.get("cost_usd"))
        if cost is not None:
            self.cost_usd = float(cost)
        usage = ev.get("usage")
        if usage is None and isinstance(ev.get("message"), dict):
            usage = ev["message"].get("usage")
        inp, out = _usage_tokens(usage)
        if inp is not None:
            self.input_tokens = inp
        if out is not None:
            self.output_tokens = out

    def feed(self, line: str) -> str:
        text = line.strip()
        if not text:
            return ""
        try:
            ev = json.loads(text)
        except json.JSONDecodeError:
            return line  # not JSON — pass through unchanged
        if not isinstance(ev, dict):
            return ""
        self._capture(ev)
        etype = ev.get("type")
        if etype == "assistant" and isinstance(ev.get("message"), dict):
            return _assistant_text(ev["message"])
        return ""

    def apply(self, result: WorkerResult) -> WorkerResult:
        result.turns = self.turns
        result.input_tokens = self.input_tokens
        result.output_tokens = self.output_tokens
        result.cost_usd = self.cost_usd
        return result


def _assistant_text(message: dict[str, Any]) -> str:
    """Render an assistant message's content blocks into log text."""
    parts: list[str] = []
    for block in message.get("content", []) or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            parts.append(str(block["text"]))
        elif block.get("type") == "tool_use" and block.get("name"):
            parts.append(f"\n[tool: {block['name']}]\n")
    return "".join(parts)


def resolve_bin(name: str, override: str | None) -> str:
    """Resolve an executable: explicit override (``~`` expanded), then ``$PATH``,
    then the common bin dirs a non-login shell misses; else the bare name."""
    if override:
        return os.path.expanduser(str(override))
    found = shutil.which(name)
    if found:
        return found
    for d in engine._EXTRA_BIN_DIRS:
        cand = Path(d) / name
        if cand.exists():
            return str(cand)
    return name


def _stream_subprocess(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    emit_log: EmitLog,
    should_abort: ShouldAbort,
    on_start: OnWorkerStart | None = None,
    parser: AgentStreamParser | None = None,
) -> WorkerResult:
    """Run ``argv``, streaming combined stdout/stderr line-by-line to
    ``emit_log`` and terminating early if ``should_abort`` returns a reason.
    ``on_start`` is called with the child pid once it launches.

    When ``parser`` is given, each raw line is routed through it: the parser
    returns the readable text to emit and captures turns/token telemetry, which
    is attached to the returned :class:`WorkerResult`."""

    def _emit(raw: str) -> None:
        emit_log(parser.feed(raw) if parser is not None else raw)
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except (FileNotFoundError, OSError) as exc:
        return WorkerResult(
            returncode=LAUNCH_FAILED,
            error=f"could not launch worker ({argv[0]!r}): {exc}",
        )
    if on_start is not None:
        on_start(proc.pid)
    assert proc.stdout is not None
    # A worker can stop emitting output while still alive (e.g. blocked on a long
    # tool call), so streaming alone can't observe a stop. Poll abort on a side
    # thread and kill the whole process group when it fires.
    aborted: dict[str, str | None] = {"reason": None}
    done = threading.Event()

    def _watch_abort() -> None:
        while not done.wait(0.25):
            reason = should_abort()
            if reason is not None:
                aborted["reason"] = reason
                engine._kill_process_group(proc)
                return

    watcher = threading.Thread(target=_watch_abort, daemon=True)
    watcher.start()
    try:
        for line in proc.stdout:
            _emit(line)
            reason = should_abort()
            if reason is not None:
                aborted["reason"] = reason
                engine._kill_process_group(proc)
                break
        returncode = proc.wait()
    finally:
        done.set()
        watcher.join(timeout=1)
    result = WorkerResult(returncode=returncode, aborted=aborted["reason"])
    return parser.apply(result) if parser is not None else result


def _run_buffered(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    should_abort: ShouldAbort,
    on_start: OnWorkerStart | None = None,
) -> tuple[int, str, str | None, str | None]:
    """Run ``argv`` to completion, **buffering** combined stdout/stderr instead of
    streaming it. Returns ``(returncode, output, aborted_reason, launch_error)``.

    Used by backends whose telemetry only arrives as a single end-of-run JSON
    blob (Gemini CLI ``--output-format json``), where line streaming would split
    the JSON. Abort polling + process-group kill match :func:`_stream_subprocess`.
    """
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True,
        )
    except (FileNotFoundError, OSError) as exc:
        return LAUNCH_FAILED, "", None, f"could not launch worker ({argv[0]!r}): {exc}"
    if on_start is not None:
        on_start(proc.pid)
    assert proc.stdout is not None
    chunks: list[str] = []
    aborted: dict[str, str | None] = {"reason": None}
    done = threading.Event()

    def _watch_abort() -> None:
        while not done.wait(0.25):
            reason = should_abort()
            if reason is not None:
                aborted["reason"] = reason
                engine._kill_process_group(proc)
                return

    watcher = threading.Thread(target=_watch_abort, daemon=True)
    watcher.start()
    try:
        for line in proc.stdout:
            chunks.append(line)
            reason = should_abort()
            if reason is not None:
                aborted["reason"] = reason
                engine._kill_process_group(proc)
                break
        returncode = proc.wait()
    finally:
        done.set()
        watcher.join(timeout=1)
    return returncode, "".join(chunks), aborted["reason"], None


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort parse of a JSON object from ``text`` that may carry leading
    warnings/log noise before the blob. Returns ``None`` if nothing parses."""
    text = text.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def parse_gemini_stats(data: dict[str, Any]) -> dict[str, Any]:
    """Pull turns/token telemetry from a Gemini CLI ``--output-format json`` blob.

    The ``stats.models[*]`` map carries per-model ``api`` (request counts) and
    ``tokens`` (prompt/candidates/cached/…). Turns ≈ total model API requests;
    input folds prompt + cached, output uses candidate (response) tokens. Gemini
    reports no dollar cost, so ``cost_usd`` stays ``None``.
    """
    models = (data.get("stats") or {}).get("models") or {}
    turns = 0
    inp = 0
    out = 0
    for m in models.values():
        if not isinstance(m, dict):
            continue
        turns += int((m.get("api") or {}).get("totalRequests", 0) or 0)
        tok = m.get("tokens") or {}
        inp += int(tok.get("prompt", 0) or 0) + int(tok.get("cached", 0) or 0)
        out += int(tok.get("candidates", tok.get("response", 0)) or 0)
    return {
        "turns": turns or None,
        "input_tokens": inp or None,
        "output_tokens": out or None,
        "cost_usd": None,
    }


def build_gemini_argv(prompt: str, model: str, config: dict[str, Any]) -> list[str]:
    """Argv for the Gemini CLI (``gemini``) headless run.

    ``-p`` is headless/non-interactive (prompt is the flag's value); ``--yolo``
    auto-approves the agent's edit/shell tools so a headless run doesn't hang on
    approval; ``--output-format json`` makes the CLI print a final JSON blob with
    a ``stats`` block we mine for telemetry. ``gemini_model`` overrides the model;
    ``gemini_extra_args`` appends extra flags.
    """
    model = config.get("gemini_model") or model
    argv = ["gemini", "-p", prompt, "--yolo", "--output-format", "json"]
    if model and model not in ("auto", "max"):
        argv += ["--model", model]
    extra = config.get("gemini_extra_args")
    if extra:
        argv += list(extra)
    return argv


def build_cursor_argv(prompt: str, model: str, config: dict[str, Any]) -> list[str]:
    """Argv for Cursor's headless agent (``cursor-agent``).

    ``-p`` is print/non-interactive (full tool access incl. write+shell);
    ``--force`` + ``--trust`` skip the approval/trust prompts that would hang a
    headless run; the prompt is the trailing positional argument. ``cursor_model``
    overrides the model (Cursor uses names like ``sonnet-4``/``gpt-5``, not the
    task's Claude id), and ``cursor_extra_args`` appends any extra flags.
    """
    model = config.get("cursor_model") or model
    # stream-json so we can capture turn/token telemetry from the agent's events;
    # AgentStreamParser falls back to raw passthrough if a CLI build ignores it.
    argv = ["cursor-agent", "-p", "--force", "--trust", "--output-format", "stream-json"]
    if model:
        argv += ["--model", model]
    extra = config.get("cursor_extra_args")
    if extra:
        argv += list(extra)
    argv.append(prompt)  # prompt is the trailing positional argument
    return argv


class ClaudeCodeBackend:
    name = "claude-code"
    agentic = True
    description = "Claude Code CLI — fully agentic (edits files, runs bash). Default."

    def available(self, config: dict[str, Any] | None = None) -> bool:
        return bool(shutil.which("claude") or (config or {}).get("claude_bin"))

    def run(
        self,
        spec: WorkerSpec,
        emit_log: EmitLog,
        should_abort: ShouldAbort,
        on_worker_start: OnWorkerStart | None = None,
    ) -> WorkerResult:
        argv = engine.build_claude_argv(spec.prompt, spec.model, spec.max_turns)
        argv[0] = engine.resolve_claude_bin(spec.config)
        return _stream_subprocess(
            argv, cwd=spec.cwd, env=spec.env, emit_log=emit_log,
            should_abort=should_abort, on_start=on_worker_start,
            parser=AgentStreamParser(),
        )


class CursorAgentBackend:
    name = "cursor"
    agentic = True
    description = "Cursor headless agent (cursor-agent) — agentic. Requires the Cursor CLI."

    def available(self, config: dict[str, Any] | None = None) -> bool:
        return bool(shutil.which("cursor-agent") or (config or {}).get("cursor_bin"))

    def run(
        self,
        spec: WorkerSpec,
        emit_log: EmitLog,
        should_abort: ShouldAbort,
        on_worker_start: OnWorkerStart | None = None,
    ) -> WorkerResult:
        argv = build_cursor_argv(spec.prompt, spec.model, spec.config)
        argv[0] = resolve_bin("cursor-agent", spec.config.get("cursor_bin"))
        return _stream_subprocess(
            argv, cwd=spec.cwd, env=spec.env, emit_log=emit_log,
            should_abort=should_abort, on_start=on_worker_start,
            parser=AgentStreamParser(),
        )


class GeminiCLIBackend:
    name = "gemini"
    agentic = True
    description = (
        "Google Gemini CLI (gemini) — agentic (edits files, runs tools via "
        "--yolo). Direct vendor path for comparing against Gemini-via-Cursor. "
        "Requires the Gemini CLI + an authenticated account / GEMINI_API_KEY."
    )

    def available(self, config: dict[str, Any] | None = None) -> bool:
        return bool(shutil.which("gemini") or (config or {}).get("gemini_bin"))

    def run(
        self,
        spec: WorkerSpec,
        emit_log: EmitLog,
        should_abort: ShouldAbort,
        on_worker_start: OnWorkerStart | None = None,
    ) -> WorkerResult:
        argv = build_gemini_argv(spec.prompt, spec.model, spec.config)
        argv[0] = resolve_bin("gemini", spec.config.get("gemini_bin"))
        # Gemini's JSON telemetry only arrives as one end-of-run blob, so unlike
        # the stream-json backends the readable output is shown on completion.
        emit_log(
            f"  [gemini] {spec.model}: buffered JSON run "
            "(telemetry-rich; response shown on completion)\n"
        )
        rc, output, aborted, launch_err = _run_buffered(
            argv, cwd=spec.cwd, env=spec.env,
            should_abort=should_abort, on_start=on_worker_start,
        )
        if launch_err is not None:
            return WorkerResult(returncode=LAUNCH_FAILED, error=launch_err)
        if aborted is not None:
            return WorkerResult(returncode=0, aborted=aborted)

        data = _extract_json_object(output)
        if data is not None:
            response = data.get("response")
            if response:
                emit_log(str(response) + "\n")
            tele = parse_gemini_stats(data)
            error = data.get("error") if isinstance(data.get("error"), dict) else None
            if rc != 0 and error:
                return WorkerResult(
                    returncode=rc or 1,
                    error=f"gemini {error.get('type', 'error')}: {error.get('message', '')}".strip(),
                    **tele,
                )
            return WorkerResult(returncode=rc, **tele)

        # No parseable JSON (e.g. an older CLI without --output-format) — surface
        # the raw output so the operator still sees what happened.
        emit_log(output)
        return WorkerResult(returncode=rc)


class AnthropicBackend:
    name = "anthropic"
    agentic = False
    description = (
        "Anthropic Messages API directly — single-shot completion "
        "(NOT agentic; no file edits). Latency baseline."
    )

    def available(self, config: dict[str, Any] | None = None) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    def run(
        self,
        spec: WorkerSpec,
        emit_log: EmitLog,
        should_abort: ShouldAbort,
        on_worker_start: OnWorkerStart | None = None,
    ) -> WorkerResult:
        key = spec.env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return WorkerResult(returncode=2, error="ANTHROPIC_API_KEY is not set")
        model = spec.config.get("anthropic_model") or spec.model
        max_tokens = int(spec.config.get("anthropic_max_tokens", 4096))
        emit_log(f"  [anthropic] {model}: single-shot completion (non-agentic; no file edits)\n")
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "stream": True,
            "messages": [{"role": "user", "content": spec.prompt}],
        }
        input_tokens: int | None = None
        output_tokens: int | None = None
        try:
            with httpx.stream(
                "POST",
                "https://api.anthropic.com/v1/messages",
                json=body,
                headers=headers,
                timeout=None,
            ) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    return WorkerResult(
                        returncode=1,
                        error=f"anthropic HTTP {resp.status_code}: {resp.text[:300]}",
                    )
                for line in resp.iter_lines():
                    reason = should_abort()
                    if reason is not None:
                        return WorkerResult(returncode=0, aborted=reason)
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type")
                    if etype == "content_block_delta":
                        text = event.get("delta", {}).get("text", "")
                        if text:
                            emit_log(text)
                    elif etype == "message_start":
                        usage = event.get("message", {}).get("usage", {})
                        if usage.get("input_tokens") is not None:
                            input_tokens = int(usage["input_tokens"])
                    elif etype == "message_delta":
                        usage = event.get("usage", {})
                        if usage.get("output_tokens") is not None:
                            output_tokens = int(usage["output_tokens"])
        except httpx.HTTPError as exc:
            return WorkerResult(returncode=1, error=f"anthropic request failed: {exc}")
        emit_log("\n")
        # Single-shot completion: one "turn"; cost left to the rollup (token-only).
        return WorkerResult(
            returncode=0, turns=1,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )


class OllamaBackend:
    name = "ollama"
    agentic = False
    description = (
        "Local Ollama model — single-shot completion (NOT agentic). "
        "Requires a running `ollama serve`."
    )

    def available(self, config: dict[str, Any] | None = None) -> bool:
        return shutil.which("ollama") is not None or bool((config or {}).get("ollama_host"))

    def run(
        self,
        spec: WorkerSpec,
        emit_log: EmitLog,
        should_abort: ShouldAbort,
        on_worker_start: OnWorkerStart | None = None,
    ) -> WorkerResult:
        host = str(spec.config.get("ollama_host", "http://localhost:11434")).rstrip("/")
        model = spec.config.get("ollama_model", "llama3.1")
        emit_log(f"  [ollama] {model} @ {host}: single-shot completion (non-agentic)\n")
        body = {"model": model, "prompt": spec.prompt, "stream": True}
        input_tokens: int | None = None
        output_tokens: int | None = None
        try:
            with httpx.stream("POST", f"{host}/api/generate", json=body, timeout=None) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    return WorkerResult(
                        returncode=1, error=f"ollama HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                for line in resp.iter_lines():
                    reason = should_abort()
                    if reason is not None:
                        return WorkerResult(returncode=0, aborted=reason)
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    chunk = event.get("response", "")
                    if chunk:
                        emit_log(chunk)
                    if event.get("done"):
                        if event.get("prompt_eval_count") is not None:
                            input_tokens = int(event["prompt_eval_count"])
                        if event.get("eval_count") is not None:
                            output_tokens = int(event["eval_count"])
                        break
        except httpx.HTTPError as exc:
            return WorkerResult(
                returncode=1,
                error=f"ollama request failed: {exc} (is `ollama serve` running?)",
            )
        emit_log("\n")
        # Local model: token counts but no dollar cost; single-shot → one turn.
        return WorkerResult(
            returncode=0, turns=1,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )


_BACKENDS: tuple[Any, ...] = (
    ClaudeCodeBackend(),
    CursorAgentBackend(),
    GeminiCLIBackend(),
    AnthropicBackend(),
    OllamaBackend(),
)

DEFAULT_BACKEND = "claude-code"


def _by_name() -> dict[str, Any]:
    return {b.name: b for b in _BACKENDS}


def backend_names() -> list[str]:
    return [b.name for b in _BACKENDS]


def get_backend(name: str | None) -> Any:
    """Return the backend by ``name``, falling back to the default."""
    registry = _by_name()
    return registry.get(name or DEFAULT_BACKEND) or registry[DEFAULT_BACKEND]


def list_backends(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Describe every backend for the shim API (name, description, agentic,
    and whether it's currently usable on this machine)."""
    return [
        {
            "name": b.name,
            "description": b.description,
            "agentic": b.agentic,
            "available": b.available(config),
        }
        for b in _BACKENDS
    ]
