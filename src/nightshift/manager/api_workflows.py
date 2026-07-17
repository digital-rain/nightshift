"""Workflow-editor manager API — definition CRUD + validate, prompt CRUD,
and hot reload (workflow-editor spec §3–§4).

All write endpoints operate on **operator files only**
(``<workspace>/.nightshift/workflows/`` and ``.nightshift/prompts/``); shipped
definitions and prompts are immutable package assets, exposed read-only with
provenance so the UI can offer duplicate-to-edit. Validation never lives in
the browser: every candidate round-trips through the engine's own
``parse_workflow`` (plus the endpoint-layer prompt-reference check), so the
editor can never save a definition the loader would reject.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from nightshift._paths import asset
from nightshift.manager.wire import EmitFn
from nightshift.workflows import (
    StepKind,
    WorkflowError,
    check_prompt_refs,
    load_workflows,
    operator_prompts_dir,
    operator_workflows_dir,
    parse_workflow,
    prompt_exists,
)


# One path segment, no traversal: letters/digits then letters/digits/._- (a
# leading dot — and thus ".."/hidden files — is impossible by construction).
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _safe(name: str) -> bool:
    return bool(_SAFE_NAME.match(name))


def register_workflow_editor_api(
    app: FastAPI,
    *,
    workspace: Path,
    _emit: EmitFn,
) -> None:
    """Register the workflow-editor endpoints onto the shared FastAPI app.

    The loaded definition set lives on ``app.state.workflows``; every
    successful write re-runs :func:`load_workflows` and swaps that reference
    atomically (§3.1). A failed merged reload (a *different* operator file
    hand-broken since startup) keeps the previous in-memory set — strictly
    last-known-good — and surfaces the error to the caller.
    """

    op_defs_dir = operator_workflows_dir(workspace)
    op_prompts_dir = operator_prompts_dir(workspace)

    # ----- provenance helpers ------------------------------------------- #

    def _shipped_defs() -> dict[str, dict]:
        """Raw shipped definitions keyed by content name (package assets are
        always well-formed; a malformed one would have failed startup)."""
        out: dict[str, dict] = {}
        shipped = asset("workflows")
        if shipped.is_dir():
            for path in sorted(shipped.glob("*.json")):
                try:
                    raw = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                name = raw.get("name")
                if isinstance(name, str) and name:
                    out[name] = raw
        return out

    def _operator_def_path(name: str) -> Path:
        return op_defs_dir / f"{name}.json"

    def _operator_def_names() -> set[str]:
        if not op_defs_dir.is_dir():
            return set()
        return {p.stem for p in op_defs_dir.glob("*.json")}

    def _shipped_prompt_names() -> set[str]:
        shipped = asset("prompts")
        if not shipped.is_dir():
            return set()
        return {p.name for p in shipped.glob("*.md")}

    def _operator_prompt_names() -> set[str]:
        if not op_prompts_dir.is_dir():
            return set()
        return {p.name for p in op_prompts_dir.glob("*.md")}

    def _validate_candidate(raw: Any) -> str | None:
        """The full save-path validation: ``parse_workflow`` plus the
        endpoint-layer prompt-reference check. Returns the error message, or
        ``None`` when the candidate is valid."""
        try:
            wf = parse_workflow(raw)
            check_prompt_refs(wf, lambda n: prompt_exists(workspace, n))
        except WorkflowError as err:
            return str(err)
        return None

    def _reload() -> str | None:
        """Re-run the merged load and swap ``app.state.workflows`` (one atomic
        reference assignment on the event loop). On failure the previous set
        is kept — a bad file on disk must not take down dispatch that was
        working a moment ago. Returns the error message, or ``None``."""
        try:
            app.state.workflows = load_workflows(workspace)
        except WorkflowError as err:
            return str(err)
        return None

    # ----- definitions ---------------------------------------------------- #

    @app.get("/api/workflows")
    async def get_workflows() -> JSONResponse:
        """The loaded definitions with provenance:
        ``{name: {steps, source, shadows_shipped}}`` (workflow-editor §3)."""
        shipped = set(_shipped_defs())
        operator = _operator_def_names()
        out = {}
        for name, wf in app.state.workflows.items():
            source = "operator" if name in operator else "shipped"
            out[name] = {
                "steps": [s.id for s in wf.steps],
                "source": source,
                "shadows_shipped": source == "operator" and name in shipped,
            }
        return JSONResponse(out)

    @app.get("/api/workflows/{name}")
    async def get_workflow(name: str) -> JSONResponse:
        """The full raw definition JSON + provenance. For a shadowed name the
        operator version is returned and the shipped original rides along as
        ``shipped_definition`` (the UI's diff-against-shipped view)."""
        if not _safe(name):
            return JSONResponse({"error": "invalid workflow name"}, status_code=400)
        shipped = _shipped_defs()
        op_path = _operator_def_path(name)
        if op_path.is_file():
            try:
                raw = json.loads(op_path.read_text())
            except (OSError, json.JSONDecodeError) as err:
                return JSONResponse(
                    {"error": f"operator file is not valid JSON: {err}"},
                    status_code=409,
                )
            return JSONResponse({
                "name": name,
                "definition": raw,
                "source": "operator",
                "shadows_shipped": name in shipped,
                "shipped_definition": shipped.get(name),
            })
        if name in shipped:
            return JSONResponse({
                "name": name,
                "definition": shipped[name],
                "source": "shipped",
                "shadows_shipped": False,
                "shipped_definition": None,
            })
        return JSONResponse({"error": "unknown workflow"}, status_code=404)

    @app.post("/api/workflows/validate")
    async def validate_workflow(request: Request) -> JSONResponse:
        """Dry-run validation of a candidate definition dict — the editor's
        debounced live-validation call. Never touches disk."""
        try:
            raw = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"ok": False, "error": "body is not valid JSON"})
        error = _validate_candidate(raw)
        if error is not None:
            return JSONResponse({"ok": False, "error": error})
        return JSONResponse({"ok": True})

    @app.put("/api/workflows/{name}")
    async def put_workflow(name: str, request: Request) -> JSONResponse:
        """Validate; on success write ``.nightshift/workflows/<name>.json``
        canonically formatted (indent-2 + trailing newline, so hand edits and
        editor edits produce identical diffs), then hot-reload (§3.1)."""
        if not _safe(name):
            return JSONResponse({"error": "invalid workflow name"}, status_code=400)
        try:
            raw = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "body is not valid JSON"}, status_code=400)
        if not isinstance(raw, dict) or raw.get("name") != name:
            return JSONResponse(
                {"error": "definition 'name' must match the URL path"},
                status_code=400,
            )
        error = _validate_candidate(raw)
        if error is not None:
            return JSONResponse({"error": error}, status_code=400)
        op_defs_dir.mkdir(parents=True, exist_ok=True)
        _operator_def_path(name).write_text(json.dumps(raw, indent=2) + "\n")
        reload_error = _reload()
        if reload_error is not None:
            return JSONResponse(
                {"error": f"saved, but reload failed: {reload_error}"},
                status_code=409,
            )
        await _emit("workflows_changed", payload={"name": name})
        return JSONResponse({
            "ok": True,
            "name": name,
            "source": "operator",
            "shadows_shipped": name in _shipped_defs(),
        })

    @app.delete("/api/workflows/{name}")
    async def delete_workflow(name: str) -> JSONResponse:
        """Remove the operator file; hot-reload. Deleting a shadow *restores*
        the shipped definition. Refuses (404) for shipped-only names."""
        if not _safe(name):
            return JSONResponse({"error": "invalid workflow name"}, status_code=400)
        op_path = _operator_def_path(name)
        if not op_path.is_file():
            return JSONResponse(
                {"error": "no operator definition with that name"},
                status_code=404,
            )
        op_path.unlink()
        reload_error = _reload()
        if reload_error is not None:
            return JSONResponse(
                {"error": f"deleted, but reload failed: {reload_error}"},
                status_code=409,
            )
        await _emit("workflows_changed", payload={"name": name})
        return JSONResponse({
            "ok": True,
            "restored_shipped": name in _shipped_defs(),
        })

    # ----- prompts ---------------------------------------------------------- #

    def _prompt_filename(name: str) -> str | None:
        """Normalize an API prompt name to its ``<name>.md`` filename, or
        ``None`` for an unsafe segment."""
        if not _safe(name):
            return None
        return name if name.endswith(".md") else f"{name}.md"

    @app.get("/api/workflow-prompts")
    async def get_workflow_prompts() -> JSONResponse:
        """Every known prompt with provenance — feeds the step card's prompt
        picker (workflow-editor §4)."""
        shipped = _shipped_prompt_names()
        operator = _operator_prompt_names()
        out = {}
        for name in sorted(shipped | operator):
            source = "operator" if name in operator else "shipped"
            out[name] = {
                "source": source,
                "shadows_shipped": source == "operator" and name in shipped,
            }
        return JSONResponse(out)

    @app.get("/api/workflow-prompts/{name}")
    async def get_workflow_prompt(name: str) -> JSONResponse:
        fname = _prompt_filename(name)
        if fname is None:
            return JSONResponse({"error": "invalid prompt name"}, status_code=400)
        shipped = _shipped_prompt_names()
        op_path = op_prompts_dir / fname
        if op_path.is_file():
            shipped_body = (
                asset("prompts", fname).read_text() if fname in shipped else None
            )
            return JSONResponse({
                "name": fname,
                "text": op_path.read_text(),
                "source": "operator",
                "shadows_shipped": fname in shipped,
                "shipped_body": shipped_body,
            })
        if fname in shipped:
            return JSONResponse({
                "name": fname,
                "text": asset("prompts", fname).read_text(),
                "source": "shipped",
                "shadows_shipped": False,
                "shipped_body": None,
            })
        return JSONResponse({"error": "unknown prompt"}, status_code=404)

    @app.put("/api/workflow-prompts/{name}")
    async def put_workflow_prompt(name: str, request: Request) -> JSONResponse:
        """Write ``.nightshift/prompts/<name>.md``. A prompt is prose — the
        only structural check is non-empty. No reload machinery: nothing
        caches prompt bodies; the next ``build_work_order`` reads the file."""
        fname = _prompt_filename(name)
        if fname is None:
            return JSONResponse({"error": "invalid prompt name"}, status_code=400)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "body is not valid JSON"}, status_code=400)
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            return JSONResponse(
                {"error": "prompt text must not be empty"}, status_code=400
            )
        op_prompts_dir.mkdir(parents=True, exist_ok=True)
        (op_prompts_dir / fname).write_text(text)
        await _emit("workflows_changed", payload={"prompt": fname})
        return JSONResponse({
            "ok": True,
            "name": fname,
            "source": "operator",
            "shadows_shipped": fname in _shipped_prompt_names(),
        })

    @app.delete("/api/workflow-prompts/{name}")
    async def delete_workflow_prompt(name: str) -> JSONResponse:
        """Operator files only; refuses while any *loaded* definition
        references the name (definitions are the dependents — refuse early
        rather than let the next reload trip the §3 check)."""
        fname = _prompt_filename(name)
        if fname is None:
            return JSONResponse({"error": "invalid prompt name"}, status_code=400)
        op_path = op_prompts_dir / fname
        if not op_path.is_file():
            return JSONResponse(
                {"error": "no operator prompt with that name"}, status_code=404
            )
        # A shadow's delete falls back to the shipped body, so references to a
        # still-shipped name stay satisfied; refuse only when removal would
        # leave a loaded doc step dangling.
        if fname not in _shipped_prompt_names():
            referencing = sorted({
                wf.name
                for wf in app.state.workflows.values()
                for step in wf.steps
                if step.kind is StepKind.DOC and step.prompt == fname
            })
            if referencing:
                return JSONResponse(
                    {"error": (
                        f"prompt '{fname}' is referenced by workflow(s): "
                        + ", ".join(referencing)
                    )},
                    status_code=409,
                )
        op_path.unlink()
        await _emit("workflows_changed", payload={"prompt": fname})
        return JSONResponse({
            "ok": True,
            "restored_shipped": fname in _shipped_prompt_names(),
        })
