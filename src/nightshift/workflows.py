"""The workflow definition layer (pure).

A *workflow* is declarative data: a named sequence of steps forming a small
state machine with signal-routed edges. This module loads, validates, and
interprets those definitions — it never touches the store, git, or HTTP. The
manager drives the cursor; this module only answers questions about the graph.

Spec: ``docs/spec/2026-07-16-workflows.md`` §3 (vocabulary, role resolution).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from nightshift._paths import asset


class StepKind(StrEnum):
    """The three step kinds (§3.1)."""

    DOC = "doc"
    CODE = "code"
    SPLIT = "split"


# Terminal cursor destination. A workflow step routes to a step id or to $end.
END = "$end"

# Sentinel distinguishing "max_turns key absent" (inherit the task/queue turns)
# from an explicit ``null`` (unbounded → None) or an int override (§3.1).
_INHERIT = object()


class WorkflowError(ValueError):
    """Raised on load/validation failure — loud, never swallowed."""


@dataclass(frozen=True)
class WorkflowStep:
    id: str
    kind: StepKind
    role: str
    inputs: tuple[str, ...]
    prompt: str | None = None
    output: str | None = None
    max_turns: int | None | object = _INHERIT
    signals: dict[str, str] = field(default_factory=dict)
    next: str = END
    max_visits: int = 1
    # True when ``max_visits`` was explicit in the JSON (needed for the cycle
    # check; steps not on a cycle default to 1 without declaring it).
    max_visits_declared: bool = False


@dataclass(frozen=True)
class WorkflowDef:
    name: str
    steps: tuple[WorkflowStep, ...]

    def step(self, step_id: str) -> WorkflowStep:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise WorkflowError(f"workflow {self.name!r}: no step {step_id!r}")

    @property
    def first(self) -> WorkflowStep:
        return self.steps[0]

    def has_step(self, step_id: str) -> bool:
        return any(s.id == step_id for s in self.steps)


# Sentinel meaning "next defaults to the following list step" — resolved after
# the whole list is parsed (we need neighbour ids).
_NEXT_IN_LIST = object()


# --------------------------------------------------------------------------- #
# Parsing + validation
# --------------------------------------------------------------------------- #


def _parse_step(raw: dict, index: int, total: int) -> WorkflowStep:
    if not isinstance(raw, dict):
        raise WorkflowError(f"step #{index} is not an object")
    step_id = raw.get("id")
    if not isinstance(step_id, str) or not step_id:
        raise WorkflowError(f"step #{index} missing string 'id'")
    kind_raw = raw.get("kind")
    try:
        kind = StepKind(kind_raw)
    except ValueError as err:
        raise WorkflowError(f"step {step_id!r}: invalid kind {kind_raw!r}") from err
    role = raw.get("role")
    if not isinstance(role, str) or not role:
        raise WorkflowError(f"step {step_id!r}: missing string 'role'")

    inputs_raw = raw.get("inputs", [])
    if not isinstance(inputs_raw, list) or not all(isinstance(i, str) for i in inputs_raw):
        raise WorkflowError(f"step {step_id!r}: 'inputs' must be a list of strings")
    inputs = tuple(inputs_raw)

    # max_turns tri-state: absent → inherit; explicit null → None; int → override.
    if "max_turns" not in raw:
        max_turns: int | None | object = _INHERIT
    else:
        mt = raw["max_turns"]
        if mt is None:
            max_turns = None
        elif isinstance(mt, int) and not isinstance(mt, bool):
            max_turns = mt
        else:
            raise WorkflowError(f"step {step_id!r}: 'max_turns' must be int or null")

    signals_raw = raw.get("signals", {})
    if not isinstance(signals_raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in signals_raw.items()
    ):
        raise WorkflowError(f"step {step_id!r}: 'signals' must be a token→step map")
    signals = dict(signals_raw)

    # next defaults to the following step, or $end for the last.
    if "next" in raw:
        nxt = raw["next"]
        if not isinstance(nxt, str):
            raise WorkflowError(f"step {step_id!r}: 'next' must be a string")
        next_dest: object = nxt
    else:
        next_dest = END if index == total - 1 else _NEXT_IN_LIST

    max_visits_raw = raw.get("max_visits")
    if max_visits_raw is None:
        max_visits_declared = False
        max_visits = 1
    else:
        if not isinstance(max_visits_raw, int) or isinstance(max_visits_raw, bool):
            raise WorkflowError(f"step {step_id!r}: 'max_visits' must be an int")
        max_visits_declared = True
        max_visits = max_visits_raw

    prompt = raw.get("prompt")
    output = raw.get("output")
    if prompt is not None and not isinstance(prompt, str):
        raise WorkflowError(f"step {step_id!r}: 'prompt' must be a string")
    if output is not None and not isinstance(output, str):
        raise WorkflowError(f"step {step_id!r}: 'output' must be a string")

    return WorkflowStep(
        id=step_id,
        kind=kind,
        role=role,
        inputs=inputs,
        prompt=prompt,
        output=output,
        max_turns=max_turns,
        signals=signals,
        next=next_dest,  # type: ignore[arg-type]  # sentinel resolved below
        max_visits=max_visits,
        max_visits_declared=max_visits_declared,
    )


def parse_workflow(raw: dict) -> WorkflowDef:
    """Parse + validate a single workflow definition dict.

    Raises :class:`WorkflowError` on any structural or semantic violation.
    """
    if not isinstance(raw, dict):
        raise WorkflowError("workflow definition must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise WorkflowError("workflow definition missing string 'name'")
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise WorkflowError(f"workflow {name!r}: 'steps' must be a non-empty list")

    total = len(steps_raw)
    parsed = [_parse_step(s, i, total) for i, s in enumerate(steps_raw)]

    # Resolve _NEXT_IN_LIST sentinels to the following step id.
    resolved: list[WorkflowStep] = []
    for i, step in enumerate(parsed):
        next_dest = step.next
        if next_dest is _NEXT_IN_LIST:
            next_dest = parsed[i + 1].id
            step = WorkflowStep(
                id=step.id,
                kind=step.kind,
                role=step.role,
                inputs=step.inputs,
                prompt=step.prompt,
                output=step.output,
                max_turns=step.max_turns,
                signals=step.signals,
                next=next_dest,
                max_visits=step.max_visits,
                max_visits_declared=step.max_visits_declared,
            )
        resolved.append(step)

    wf = WorkflowDef(name=name, steps=tuple(resolved))
    _validate(wf)
    return wf


def _destinations(step: WorkflowStep) -> list[str]:
    """Every place the cursor may go from ``step``: signal targets + next."""
    return [*step.signals.values(), step.next]


def _validate(wf: WorkflowDef) -> None:
    ids = {s.id for s in wf.steps}
    if len(ids) != len(wf.steps):
        raise WorkflowError(f"workflow {wf.name!r}: duplicate step ids")

    for step in wf.steps:
        # Doc steps require prompt + output; code/split must not set them.
        if step.kind is StepKind.DOC:
            if not step.prompt or not step.output:
                raise WorkflowError(
                    f"step {step.id!r}: doc steps require 'prompt' and 'output'"
                )
        else:
            if step.prompt is not None or step.output is not None:
                raise WorkflowError(
                    f"step {step.id!r}: {step.kind.value} steps must not set "
                    "'prompt'/'output'"
                )

        # Destinations must name existing steps or $end.
        for dest in _destinations(step):
            if dest != END and dest not in ids:
                raise WorkflowError(
                    f"step {step.id!r}: destination {dest!r} names no step"
                )

        # Split steps must route only to $end.
        if step.kind is StepKind.SPLIT:
            for dest in _destinations(step):
                if dest != END:
                    raise WorkflowError(
                        f"step {step.id!r}: split steps must route to $end "
                        f"(found {dest!r})"
                    )

    # inputs may only name "brief" or an output produced by an earlier step.
    produced: set[str] = {"brief"}
    for step in wf.steps:
        for inp in step.inputs:
            if inp not in produced:
                raise WorkflowError(
                    f"step {step.id!r}: input {inp!r} is not 'brief' nor an "
                    "earlier step's output"
                )
        if step.output:
            produced.add(step.output)

    # The default path (following `next` from the first step) must reach a
    # code or split step.
    _check_default_path_terminates(wf)

    # Any step reachable from itself must declare max_visits explicitly.
    _check_cycles_declare_visits(wf)


def _check_default_path_terminates(wf: WorkflowDef) -> None:
    seen: set[str] = set()
    cur = wf.first.id
    while cur != END:
        if cur in seen:
            break
        seen.add(cur)
        step = wf.step(cur)
        if step.kind in (StepKind.CODE, StepKind.SPLIT):
            return
        cur = step.next
    raise WorkflowError(
        f"workflow {wf.name!r}: default path never reaches a code or split step"
    )


def _check_cycles_declare_visits(wf: WorkflowDef) -> None:
    edges: dict[str, list[str]] = {}
    for step in wf.steps:
        edges[step.id] = [d for d in _destinations(step) if d != END]

    def reachable_from(start: str) -> set[str]:
        seen: set[str] = set()
        stack = list(edges.get(start, []))
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(edges.get(node, []))
        return seen

    for step in wf.steps:
        if step.id in reachable_from(step.id) and not step.max_visits_declared:
            raise WorkflowError(
                f"step {step.id!r}: reachable from itself — must declare "
                "'max_visits'"
            )


# --------------------------------------------------------------------------- #
# Loading (shipped + operator shadow)
# --------------------------------------------------------------------------- #


def load_workflows(workspace: Path) -> dict[str, WorkflowDef]:
    """Load shipped ``assets/workflows/*.json`` shadowed by
    ``<workspace>/.nightshift/workflows/*.json``. Operator files override
    shipped ones of the same name."""
    defs: dict[str, WorkflowDef] = {}
    shipped_dir = asset("workflows")
    if shipped_dir.is_dir():
        for path in sorted(shipped_dir.glob("*.json")):
            wf = _load_file(path)
            defs[wf.name] = wf
    op_dir = workspace / ".nightshift" / "workflows"
    if op_dir.is_dir():
        for path in sorted(op_dir.glob("*.json")):
            wf = _load_file(path)
            defs[wf.name] = wf
    return defs


def _load_file(path: Path) -> WorkflowDef:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as err:
        raise WorkflowError(f"failed to load {path}: {err}") from err
    return parse_workflow(raw)


# --------------------------------------------------------------------------- #
# Role → model resolution (§3.2)
# --------------------------------------------------------------------------- #


def _clean(value: object | None) -> str:
    return str(value).strip() if value is not None else ""


def resolve_role_model(
    role: str,
    *,
    brief_meta: dict,
    queue_config: dict,
    planner_model: str,
    default_model: str,
) -> str | None:
    """Resolve a step's model per the §3.2 ladder, first match wins.

    Returns ``None`` when unresolvable (the caller marks the task blocked).
    """
    workflow_models = queue_config.get("workflow_models") or {}
    queue_pin = _clean(workflow_models.get(role))

    if role == "implementor":
        brief = _clean(brief_meta.get("model"))
        candidates = [brief, queue_pin, _clean(default_model)]
    elif role == "planner":
        brief = _clean(brief_meta.get("planner_model"))
        candidates = [brief, queue_pin, _clean(planner_model), _clean(default_model)]
    else:
        # Any other key: queue workflow_models then default; no planner fallback.
        candidates = [queue_pin, _clean(default_model)]

    for cand in candidates:
        if cand:
            return cand
    return None


# --------------------------------------------------------------------------- #
# Routing + visit accounting
# --------------------------------------------------------------------------- #


def make_resolver(defs: dict[str, WorkflowDef], planner_model: str, default_model: str):
    """A :class:`nightshift.manager.scheduler.WorkflowResolver` closure over the
    loaded definitions + manager config. Given a brief's frontmatter and the
    queue config, resolves ``(workflow, step_id, model)`` for the current step
    (the brief's ``workflow_step`` or the definition's first step), or
    ``(None, None, error)`` on any authoring error (unknown definition/step,
    unresolvable role)."""

    def resolve(
        meta: dict, queue_config: dict
    ) -> tuple[str, str, str] | tuple[None, None, str]:
        name = str(meta.get("workflow") or "").strip()
        wf = defs.get(name)
        if wf is None:
            return (None, None, f"unknown workflow '{name}'")
        step_id = str(meta.get("workflow_step") or "").strip() or wf.first.id
        if not wf.has_step(step_id):
            return (None, None, f"workflow '{name}' has no step '{step_id}'")
        step = wf.step(step_id)
        model = resolve_role_model(
            step.role,
            brief_meta=meta,
            queue_config=queue_config,
            planner_model=planner_model,
            default_model=default_model,
        )
        if model is None:
            return (
                None, None,
                f"workflow '{name}' step '{step_id}': cannot resolve model for "
                f"role '{step.role}'",
            )
        return (name, step_id, model)

    return resolve


def step_max_turns(step: WorkflowStep, inherited: int | None) -> int | None:
    """Resolve a step's turn budget against the task/queue ``inherited`` value
    per the absent/int/null rule (§3.1): absent → inherit; explicit null →
    unbounded (None); int → override."""
    if step.max_turns is _INHERIT:
        return inherited
    return step.max_turns  # type: ignore[return-value]  # int | None here


def route(step: WorkflowStep, signal: str | None) -> str:
    """Destination step id or END. A declared signal wins; otherwise follow
    ``next``. Undeclared signals are ignored (route via next)."""
    if signal is not None and signal in step.signals:
        return step.signals[signal]
    return step.next


def parse_visits(raw: str | None) -> dict[str, int]:
    """Parse ``"plan:1,implement:2"`` into ``{"plan": 1, "implement": 2}``."""
    out: dict[str, int] = {}
    if not raw:
        return out
    for chunk in str(raw).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        key, _, val = chunk.partition(":")
        key = key.strip()
        if not key:
            continue
        try:
            out[key] = int(val.strip())
        except ValueError:
            continue
    return out


def format_visits(visits: dict[str, int]) -> str:
    """Format ``{"plan": 1, "implement": 2}`` into ``"plan:1,implement:2"``."""
    return ",".join(f"{k}:{v}" for k, v in visits.items())


__all__ = [
    "END",
    "StepKind",
    "WorkflowDef",
    "WorkflowError",
    "WorkflowStep",
    "format_visits",
    "load_workflows",
    "make_resolver",
    "parse_visits",
    "parse_workflow",
    "resolve_role_model",
    "route",
    "step_max_turns",
]
