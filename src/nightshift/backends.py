"""Pluggable worker backends — the "shim".

A *backend* turns a built prompt into worker activity inside the task's git
worktree, streaming output lines through ``emit_log`` and honouring early
abort (skip / stop). The engine picks one per run by name; this is the single
seam where Nightshift decides *who* does the work.

These are registered:

- ``claude-code``   — Claude Code CLI (default; fully **agentic**: edits files, runs bash).
- ``cursor``        — Cursor's headless agent (``cursor-agent``; **agentic**).
- ``gemini``        — Google's Gemini CLI (``gemini``; **agentic**).
- ``anthropic``     — the Anthropic Messages API directly (**single-shot completion**,
  NOT agentic — it streams a model response but does not edit files).
- ``ollama``        — a local Ollama model (**single-shot completion**, NOT agentic).
- ``ollama-cloud``  — a cloud-hosted Ollama model on ``ollama.com`` (**single-shot
  completion**, NOT agentic); same native API as ``ollama`` but reached over HTTPS
  with a Bearer ``OLLAMA_API_KEY``.

The API backends exist so we can measure raw model latency/throughput
against the agent CLIs and to give a foundation for a future tool loop. Because
they don't edit files, a run using them finishes as "no changes" (the engine's
no-commit guard) rather than landing a commit.

This module references the :mod:`nightshift.prompts` / :mod:`nightshift.preflight`
*modules* (not bare names) for the claude helpers it reuses so monkeypatching
(and the no-import-cycle property) both hold: the legacy runner imports this
module lazily, from inside ``run_task``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from nightshift import preflight, prompts


EmitLog = Callable[[str], None]
ShouldAbort = Callable[[], "str | None"]
# Invoked with the worker subprocess pid once it launches (subprocess backends
# only); lets the engine record a liveness signal for stale-run reconciliation.
OnWorkerStart = Callable[[int], None]

# Conventional returncode used by :func:`_stream_subprocess` when the worker
# binary itself could not be launched (distinct from a non-zero worker exit).
LAUNCH_FAILED = 127


def _httpx_timeout(seconds: float | None) -> Any:
    """An httpx timeout: a finite per-op bound, or no timeout when unset/<=0."""
    return httpx.Timeout(seconds) if seconds and seconds > 0 else None


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
    # Global per-worker wall-clock bound for this run (seconds). None/0 = none.
    timeout: float | None = None


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

    ``cache_read_input_tokens``/``cache_creation_input_tokens`` are the subset
    of ``input_tokens`` served from / written to the vendor's prompt cache
    (Anthropic-shaped; ``None`` when the backend doesn't report cache activity
    at all — Ollama, or an Anthropic call with no cache breakpoints). ``usage``
    is the raw vendor-shaped usage payload (per-turn detail for the harness,
    per-model splits for Claude Code, thinking/tool tokens for Gemini, …),
    kept for detail beyond what the normalized columns hold.
    """

    returncode: int
    aborted: str | None = None
    error: str | None = None
    turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    usage: dict[str, Any] | None = None
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


def _usage_cache_tokens(usage: dict[str, Any] | None) -> tuple[int | None, int | None]:
    """Pull (cache_read, cache_creation) token counts from an Anthropic-shaped
    ``usage`` blob. ``None`` (not 0) when the backend didn't report the field
    at all, so a real zero (cache enabled but missed) stays distinguishable
    from "this vendor doesn't report cache activity"."""
    if not isinstance(usage, dict):
        return None, None
    read = usage.get("cache_read_input_tokens")
    creation = usage.get("cache_creation_input_tokens")
    read = int(read) if read is not None else None
    creation = int(creation) if creation is not None else None
    return read, creation


class AgentStreamParser:
    """Defensive parser for an agent CLI's ``stream-json`` output.

    Each subprocess line is fed in; the parser returns the human-readable text to
    surface in the live log (so the Now screen stays useful) and quietly captures
    turns / token usage / cost from the final ``result`` event. Anything that is
    not JSON is passed through verbatim, so an older CLI that ignores the
    ``--output-format`` flag still streams readable output — it just yields no
    telemetry. Schema differences between Claude Code and cursor-agent are
    tolerated: usage/turns/cost are read wherever they appear.

    The raw ``usage`` blob (cache splits, and whatever else the CLI includes
    alongside it, e.g. Claude Code's per-model ``modelUsage``) is kept
    verbatim in ``usage_payload`` for detail beyond the normalized fields.
    """

    def __init__(self) -> None:
        self.turns: int | None = None
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.cache_read_input_tokens: int | None = None
        self.cache_creation_input_tokens: int | None = None
        self.cost_usd: float | None = None
        self.usage_payload: dict[str, Any] | None = None

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
        read, creation = _usage_cache_tokens(usage)
        if read is not None:
            self.cache_read_input_tokens = read
        if creation is not None:
            self.cache_creation_input_tokens = creation
        if isinstance(usage, dict):
            self.usage_payload = usage
        model_usage = ev.get("modelUsage")
        if isinstance(model_usage, dict):
            self.usage_payload = {**(self.usage_payload or {}), "modelUsage": model_usage}

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
        result.cache_read_input_tokens = self.cache_read_input_tokens
        result.cache_creation_input_tokens = self.cache_creation_input_tokens
        result.usage = self.usage_payload
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
    for d in prompts.EXTRA_BIN_DIRS:
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
    timeout: float | None = None,
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
    aborted: dict[str, str | None] = {"reason": None}
    done = threading.Event()
    deadline = (time.monotonic() + timeout) if timeout and timeout > 0 else None

    def _watch_abort() -> None:
        while not done.wait(0.25):
            reason = should_abort()
            if reason is None and deadline is not None and time.monotonic() > deadline:
                reason = "timeout"
            if reason is not None:
                aborted["reason"] = reason
                preflight.kill_process_group(proc)
                return

    watcher = threading.Thread(target=_watch_abort, daemon=True)
    watcher.start()
    try:
        for line in proc.stdout:
            _emit(line)
            reason = should_abort()
            if reason is None and deadline is not None and time.monotonic() > deadline:
                reason = "timeout"
            if reason is not None:
                aborted["reason"] = reason
                preflight.kill_process_group(proc)
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
    timeout: float | None = None,
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
    deadline = (time.monotonic() + timeout) if timeout and timeout > 0 else None

    def _watch_abort() -> None:
        while not done.wait(0.25):
            reason = should_abort()
            if reason is None and deadline is not None and time.monotonic() > deadline:
                reason = "timeout"
            if reason is not None:
                aborted["reason"] = reason
                preflight.kill_process_group(proc)
                return

    watcher = threading.Thread(target=_watch_abort, daemon=True)
    watcher.start()
    try:
        for line in proc.stdout:
            chunks.append(line)
            reason = should_abort()
            if reason is None and deadline is not None and time.monotonic() > deadline:
                reason = "timeout"
            if reason is not None:
                aborted["reason"] = reason
                preflight.kill_process_group(proc)
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
    input folds prompt + cached, output uses candidate (response) tokens.
    Gemini's ``cached`` figure is a cache *read* count (there's no cache-write
    concept in its API, so ``cache_creation_input_tokens`` stays ``None``).
    Gemini reports no dollar cost, so ``cost_usd`` stays ``None``. The raw
    ``stats.models`` subtree rides along in ``usage`` — it also carries
    ``thoughts``/``tool`` token counts the normalized fields don't hold.
    """
    models = (data.get("stats") or {}).get("models") or {}
    turns = 0
    inp = 0
    out = 0
    cache_read = 0
    for m in models.values():
        if not isinstance(m, dict):
            continue
        turns += int((m.get("api") or {}).get("totalRequests", 0) or 0)
        tok = m.get("tokens") or {}
        cached = int(tok.get("cached", 0) or 0)
        inp += int(tok.get("prompt", 0) or 0) + cached
        out += int(tok.get("candidates", tok.get("response", 0)) or 0)
        cache_read += cached
    return {
        "turns": turns or None,
        "input_tokens": inp or None,
        "output_tokens": out or None,
        "cache_read_input_tokens": cache_read if models else None,
        "cache_creation_input_tokens": None,
        "usage": models or None,
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
        argv = prompts.build_claude_argv(spec.prompt, spec.model, spec.max_turns)
        argv[0] = prompts.resolve_claude_bin(spec.config)
        return _stream_subprocess(
            argv, cwd=spec.cwd, env=spec.env, emit_log=emit_log,
            should_abort=should_abort, on_start=on_worker_start,
            parser=AgentStreamParser(),
            timeout=spec.timeout,
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
            timeout=spec.timeout,
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
            timeout=spec.timeout,
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
        usage: dict[str, Any] = {}
        try:
            with httpx.stream(
                "POST",
                "https://api.anthropic.com/v1/messages",
                json=body,
                headers=headers,
                timeout=_httpx_timeout(spec.timeout),
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
                        # Carries the cache splits (cache_control isn't set on
                        # this single-shot call, so they're typically 0, but
                        # are folded/reported like any other Anthropic usage).
                        usage.update(event.get("message", {}).get("usage", {}) or {})
                    elif etype == "message_delta":
                        usage.update(event.get("usage", {}) or {})
        except httpx.HTTPError as exc:
            return WorkerResult(returncode=1, error=f"anthropic request failed: {exc}")
        emit_log("\n")
        input_tokens, output_tokens = _usage_tokens(usage)
        cache_read, cache_creation = _usage_cache_tokens(usage)
        # Single-shot completion: one "turn"; cost left to the rollup (token-only).
        return WorkerResult(
            returncode=0, turns=1,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
            usage=usage or None,
        )


def _ollama_generate(
    *,
    host: str,
    model: str,
    prompt: str,
    emit_log: EmitLog,
    should_abort: ShouldAbort,
    label: str,
    headers: dict[str, str] | None = None,
    error_hint: str = "",
    timeout: float | None = None,
) -> WorkerResult:
    """Stream a single-shot completion from an Ollama ``/api/generate`` endpoint.

    Shared by the local (:class:`OllamaBackend`) and cloud
    (:class:`OllamaCloudBackend`) backends — the wire protocol is identical; only
    the ``host`` and (for cloud) the Bearer ``headers`` differ. ``label`` prefixes
    error messages and ``error_hint`` is appended to transport failures (e.g. a
    "is `ollama serve` running?" hint for the local daemon).
    """
    body = {"model": model, "prompt": prompt, "stream": True}
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage: dict[str, Any] = {}
    try:
        with httpx.stream(
            "POST", f"{host}/api/generate", json=body, headers=headers,
            timeout=_httpx_timeout(timeout),
        ) as resp:
            if resp.status_code >= 400:
                resp.read()
                return WorkerResult(
                    returncode=1, error=f"{label} HTTP {resp.status_code}: {resp.text[:200]}"
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
                # Ollama reports a per-stream error inline (e.g. unknown model);
                # surface it rather than finishing as a silent empty success.
                if event.get("error"):
                    return WorkerResult(returncode=1, error=f"{label}: {event['error']}")
                if event.get("done"):
                    if event.get("prompt_eval_count") is not None:
                        input_tokens = int(event["prompt_eval_count"])
                        usage["prompt_eval_count"] = input_tokens
                    if event.get("eval_count") is not None:
                        output_tokens = int(event["eval_count"])
                        usage["eval_count"] = output_tokens
                    break
    except httpx.HTTPError as exc:
        return WorkerResult(
            returncode=1, error=f"{label} request failed: {exc}{error_hint}"
        )
    emit_log("\n")
    # Token counts but no dollar cost; single-shot → one turn. Ollama reports
    # no cache split at all (not even a KV-cached-prefix concept in the API),
    # so cache_read/cache_creation stay None rather than a misleading 0.
    return WorkerResult(
        returncode=0, turns=1,
        input_tokens=input_tokens, output_tokens=output_tokens,
        usage=usage or None,
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
        model = spec.config.get("ollama_model") or spec.model or "llama3.1"
        emit_log(f"  [ollama] {model} @ {host}: single-shot completion (non-agentic)\n")
        return _ollama_generate(
            host=host, model=model, prompt=spec.prompt,
            emit_log=emit_log, should_abort=should_abort,
            label="ollama", error_hint=" (is `ollama serve` running?)",
            timeout=spec.timeout,
        )


class OllamaCloudBackend:
    name = "ollama-cloud"
    agentic = False
    description = (
        "Ollama Cloud (ollama.com) — single-shot completion (NOT agentic). "
        "Hosted models via the native Ollama API over HTTPS; requires OLLAMA_API_KEY."
    )

    def available(self, config: dict[str, Any] | None = None) -> bool:
        cfg = config or {}
        return bool(cfg.get("ollama_cloud_api_key") or os.environ.get("OLLAMA_API_KEY"))

    def run(
        self,
        spec: WorkerSpec,
        emit_log: EmitLog,
        should_abort: ShouldAbort,
        on_worker_start: OnWorkerStart | None = None,
    ) -> WorkerResult:
        key = (
            spec.config.get("ollama_cloud_api_key")
            or spec.env.get("OLLAMA_API_KEY")
            or os.environ.get("OLLAMA_API_KEY")
        )
        if not key:
            return WorkerResult(returncode=2, error="OLLAMA_API_KEY is not set")
        host = str(spec.config.get("ollama_cloud_host", "https://ollama.com")).rstrip("/")
        model = spec.config.get("ollama_cloud_model") or spec.model or "gpt-oss:120b"
        emit_log(f"  [ollama-cloud] {model} @ {host}: single-shot completion (non-agentic)\n")
        return _ollama_generate(
            host=host, model=model, prompt=spec.prompt,
            emit_log=emit_log, should_abort=should_abort,
            headers={"Authorization": f"Bearer {key}"},
            label="ollama-cloud",
            timeout=spec.timeout,
        )


class NightshiftAgentBackend:
    """In-process agentic harness (the ``nightshift`` provider).

    The bare model half is ``<vendor>/<upstream-model>`` (e.g.
    ``anthropic/claude-sonnet-4-6``); ``select_run_backend`` strips the
    ``nightshift/`` prefix, so ``spec.model`` already carries the vendor half.
    The harness runs an owned tool loop and applies edits with a deterministic
    SEARCH/REPLACE applier — no apply-model round-trip. Heavy code lives in
    :mod:`nightshift.agent`; this class is the thin backend-contract wiring and
    imports it lazily to stay clear of the ``backends``↔``agent`` cycle (agent
    imports ``_usage_tokens``/``_httpx_timeout`` from here).
    """

    name = "nightshift"
    agentic = True
    description = (
        "In-process agentic harness (owned tool loop + deterministic "
        "SEARCH/REPLACE; vendor in the model half, e.g. "
        "nightshift/anthropic/claude-sonnet-4-6)."
    )

    def available(self, config: dict[str, Any] | None = None) -> bool:
        """Available if any supported vendor's credentials are present.

        The precise per-vendor error (which vendor, which key) is raised at
        ``run`` time; this mirrors ``OllamaCloudBackend.available``.
        """
        cfg = config or {}
        if cfg.get("ollama_cloud_api_key") or os.environ.get("OLLAMA_API_KEY"):
            return True
        if os.environ.get("ANTHROPIC_API_KEY"):
            return True
        return bool(shutil.which("ollama"))

    def run(
        self,
        spec: WorkerSpec,
        emit_log: EmitLog,
        should_abort: ShouldAbort,
        on_worker_start: OnWorkerStart | None = None,
    ) -> WorkerResult:
        from nightshift.agent.loop import (
            DEFAULT_MAX_TOKENS,
            DEFAULT_MAX_TURNS,
            load_charter,
            run_loop,
        )
        from nightshift.agent.tools import build_registry
        from nightshift.agent.transport import TransportError, complete, split_vendor

        vendor, upstream = split_vendor(spec.model)
        if not vendor or not upstream:
            return WorkerResult(
                returncode=2,
                error=f"nightshift model must be <vendor>/<model>, got {spec.model!r}",
            )

        knobs = dict(spec.config.get("nightshift", {})) if isinstance(
            spec.config.get("nightshift"), dict
        ) else {}
        knobs.setdefault("max_tokens", DEFAULT_MAX_TOKENS)
        tools_enabled = knobs.get("tools_enabled") or None
        context_policy = str(knobs.get("context_policy", "spans"))

        emit_log(f"  [nightshift] {vendor}/{upstream}: agentic loop\n")
        registry = build_registry(
            spec.cwd,
            timeout=spec.timeout,
            tools_enabled=tools_enabled,
            context_policy=context_policy,
            should_abort=lambda: should_abort(),
        )

        def _complete(messages, tools, knobs_, **kw):
            return complete(messages, tools, knobs_, env=spec.env, **kw)

        try:
            loop = run_loop(
                transport_complete=_complete,
                registry=registry,
                charter=load_charter(),
                brief=spec.prompt,
                model=spec.model,
                knobs=knobs,
                max_turns=spec.max_turns if spec.max_turns is not None else DEFAULT_MAX_TURNS,
                timeout=spec.timeout,
                should_abort=should_abort,
                emit_log=emit_log,
            )
        except TransportError as exc:
            return WorkerResult(returncode=1, error=str(exc))

        if loop.error is not None:
            return WorkerResult(returncode=1, error=loop.error, turns=loop.turns)
        input_tokens, output_tokens = _usage_tokens(loop.usage)
        cache_read, cache_creation = _usage_cache_tokens(loop.usage)
        usage_payload = dict(loop.usage)
        if loop.per_turn_usage:
            usage_payload["per_turn"] = loop.per_turn_usage
        return WorkerResult(
            returncode=0,
            aborted=loop.aborted,
            turns=loop.turns,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
            usage=usage_payload or None,
            cost_usd=None,
        )


_BACKENDS: tuple[Any, ...] = (
    ClaudeCodeBackend(),
    CursorAgentBackend(),
    GeminiCLIBackend(),
    AnthropicBackend(),
    OllamaBackend(),
    OllamaCloudBackend(),
    NightshiftAgentBackend(),
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


def known_providers() -> set[str]:
    """Set of valid provider tokens (== backend names)."""
    return {b.name for b in _BACKENDS}


def require_backend(provider: str) -> Any:
    """Return the backend for ``provider`` or raise ``KeyError`` (no fallback).

    Unlike :func:`get_backend`, this never silently falls back to the default —
    an unknown provider in a qualified model id is an operator error we surface.
    """
    registry = _by_name()
    if provider not in registry:
        raise KeyError(provider)
    return registry[provider]


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
