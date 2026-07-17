"""Work-order assembly — the JSON contract handed to a polling worker.

Split out of ``manager/api_worker.py`` in Phase 7 (module-size seam): these
helpers are pure functions of the content store + manager config, with no
endpoint or store wiring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nightshift import playlists as playlists_mod
from nightshift.config.manager import ManagerConfig
from nightshift.manager.scheduler import parse_required_mcps, queue_label
from nightshift.preflight import resolve_preflight_cmd
from nightshift.queue_config import format_validate_cmd, resolve_validate_cmd
from nightshift.spawn_daily import resolve_config, split_frontmatter
from nightshift.task_files import (
    read_artifacts,
    resolve_title,
    split_notes,
    split_original,
)
from nightshift.workflows import (
    StepKind,
    WorkflowDef,
    resolve_prompt_text,
    step_max_turns,
)


def task_meta(tasks_root: Path, task: str, queue: str | None) -> dict[str, Any]:
    """A task's frontmatter from the content store ({} for a missing brief)."""
    path = tasks_root / playlists_mod.tasks_rel(queue) / f"{task}.md"
    try:
        return split_frontmatter(path.read_text(errors="replace"))[0]
    except OSError:
        return {}


def build_work_order(
    workspace: Path,
    tasks_root: Path,
    task: str,
    queue: str | None,
    repo: str,
    lease_id: str,
    run_id: str,
    base_ref: str | None,
    cfg: ManagerConfig,
    workflow_defs: dict[str, WorkflowDef] | None = None,
) -> dict[str, Any]:
    """Assemble the JSON work order handed to a worker.

    The brief is read from the content store (``tasks_root``) and its body is
    embedded (frontmatter stripped) so the brief never enters the target repo.
    Every path is **workspace-relative**: ``repo`` is a bare child name and
    ``task_path`` is ``<tasks_repo>/<queue>/<task>.md``. ``base_ref`` is the
    target repo's canonical HEAD. Landing policy (landing/automerge/draft) and
    backend choice are intentionally *not* included — landing is manager-side,
    backend is worker-owned.
    """
    tasks_rel = playlists_mod.tasks_rel(queue)
    tasks_repo = tasks_root.name
    path = tasks_root / tasks_rel / f"{task}.md"
    text = path.read_text(errors="replace") if path.exists() else ""
    meta, body = split_frontmatter(text)
    # Workers only ever see the effective brief: notes and the preserved
    # pre-enhancement original are operator-side context, not spec.
    body, _original = split_original(body)
    body, notes = split_notes(body)
    merged = resolve_config(workspace, tasks_root, tasks_rel)

    model = meta.get("model") or cfg.default_model
    raw_turns = meta.get("turns", merged.get("max_turns"))
    validate_argv = resolve_validate_cmd(merged)
    preflight_argv = resolve_preflight_cmd(merged)
    config_blob = {
        "model": str(model).strip() or cfg.default_model,
        "validate": merged.get("validate"),
        "validate_cmd": format_validate_cmd(validate_argv),
        # Environment preflight (default `uv sync --frozen`); empty string in a
        # queue's config opts out. Formatted like validate_cmd for the worker.
        "preflight": merged.get("preflight"),
        "preflight_cmd": format_validate_cmd(preflight_argv),
        "diff_cap_lines": merged.get("diff_cap_lines"),
        "forbidden_paths": merged.get("forbidden_paths"),
        "max_turns": int(raw_turns) if raw_turns is not None else None,
        # MCP connectors the brief declares (informational for the worker; the
        # manager already routed to a worker that advertises this superset).
        "required_mcps": list(parse_required_mcps(meta)),
        # WIP namespace the worker publishes its cross-machine branch under. The
        # worker never reads the centralized config, so the manager hands it the
        # operator-configured prefix here (co-located workers ignore it).
        "wip_ref_prefix": cfg.wip_ref_prefix,
        # Ralph-loop mode: when true, the worker uses the iterative ralph-loop
        # prompt instead of the standard single-pass nightshift-local prompt.
        "loop": bool(meta.get("loop", False)),
        "loop_max_iterations": int(meta.get("loop_max_iterations", 0)),
        # Split (decomposition) mode: the worker writes subtask briefs into a
        # dedicated split output directory instead of implementing directly.
        "split": bool(meta.get("split", False)),
    }

    # Workflow block (§6.2): a workflow task's work order embeds the current
    # step's routing metadata + this step's declared input artifacts, and the
    # step's model/max_turns override the blob's.
    wf_name = str(meta.get("workflow") or "").strip()
    if workflow_defs and wf_name and wf_name in workflow_defs:
        wf = workflow_defs[wf_name]
        step_id = str(meta.get("workflow_step") or "").strip() or wf.first.id
        if wf.has_step(step_id):
            step = wf.step(step_id)
            artifacts = read_artifacts(
                tasks_root, task, step.inputs, tasks_rel=tasks_rel,
            )
            wf_block: dict[str, Any] = {
                "name": wf_name,
                "step": step.id,
                "kind": step.kind.value,
                "role": step.role,
                "artifacts": artifacts,
                "signals": list(step.signals.keys()),
            }
            if step.kind is StepKind.DOC:
                wf_block["prompt"] = step.prompt
                wf_block["output"] = step.output
                # Prompt custody (workflow-editor spec §4): the body is
                # resolved manager-side (operator file wins over the shipped
                # asset) and rides the order, so remote workers never need a
                # view of the manager's ``.nightshift/``. When resolution
                # fails the key is omitted and the worker falls back to its
                # own asset() read.
                prompt_text = resolve_prompt_text(workspace, step.prompt or "")
                if prompt_text is not None:
                    wf_block["prompt_text"] = prompt_text
            config_blob["workflow"] = wf_block
            # A split step reuses the worker's decomposition path.
            if step.kind is StepKind.SPLIT:
                config_blob["split"] = True
            # The step's max_turns overrides the inherited blob value.
            config_blob["max_turns"] = step_max_turns(step, config_blob["max_turns"])

    return {
        "lease_id": lease_id,
        "run_id": run_id,
        "task": task,
        "queue": queue_label(queue),
        "priority": int(meta.get("priority", 5)) if str(meta.get("priority", "")).strip() != "" else 5,
        "title": resolve_title(task, meta),
        "body": body.strip(),
        "notes": notes.strip() or None,
        # Whether this brief went through the enhance-on-create pass; carried
        # onto the attempt row so outcomes can be compared enhanced-vs-raw.
        "enhanced": bool(meta.get("enhanced", False)),
        "repo": repo,
        "task_path": f"{tasks_repo}/{tasks_rel}/{task}.md",
        "base_ref": base_ref,
        "config": config_blob,
    }
