"""Cross-queue next-task arbitration for the manager.

The scheduler answers one question per worker poll: *given everything currently
runnable across every queue, and the capabilities this worker advertised in its
poll request, which single task should it run next?*

Routing is **pull-based and capability-matched**. The poll request *is* the
filter: it carries the worker's queues, accepted priorities, advertised models,
and installed MCP connectors. A candidate is eligible iff it matches all of
them. Nothing is keyed off the worker's backend any more -- a pinned model
routes to whoever advertises that model id, regardless of which harness runs it.

Design:

* Per queue, reuse the engine's ``live_ordered_queue`` (which already applies
  the queue's sort mode, disabled filter, and play-priority filter) to get the
  ordered runnable stems, then drop anything currently **leased**, **blocked**
  (an unsatisfiable pin / connector), or **after-blocked** (a frontmatter
  ``after:`` dependency whose target task is still in the queue). The first
  survivor is that queue's *head*.
* Apply the worker's capability filter (queues, priorities, model membership,
  MCP superset) plus manager-side queue dedication as a pre-arbitration cut.
* Arbitrate across the surviving per-queue heads by ascending ``priority`` with a
  round-robin tiebreak (fewest-served queue wins) so no queue starves.

Everything here is pure given its inputs except :func:`build_candidates`, which
reads the canonical briefs from the content store (``tasks_root``). That split
keeps the arbitration unit testable without a repo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from nightshift import playlists, repos
from nightshift.spawn_daily import load_queue_config, split_frontmatter, task_priority
from nightshift.task_files import live_ordered_queue


# Worker-interpreted keywords that pin no specific model: any worker may take a
# task carrying one of these, so they never gate routing or mark a task blocked.
AGNOSTIC_MODELS = frozenset({"auto", "max", "", "default"})

# Unroutable-hold reason vocabulary (Phase 7). The reconciler auto-clears
# exactly the holds it set from :func:`unroutable`, recognizing them by these
# prefixes — so every reason built here MUST go through the constructors below,
# and the reconciler imports THESE prefixes (never restates the strings). A
# rewording that bypasses this fails tests/test_reconciler.py's drift check.
NO_CAPABLE_WORKER_PREFIX = "no live worker provides "
DEDICATED_OFFLINE_PREFIX = "queue '"
UNROUTABLE_REASON_PREFIXES: tuple[str, ...] = (
    NO_CAPABLE_WORKER_PREFIX,
    DEDICATED_OFFLINE_PREFIX,
)


def _no_capable_worker_reason(kind: str, name: str) -> str:
    return f"{NO_CAPABLE_WORKER_PREFIX}{kind} '{name}'"


def _dedicated_offline_reason(label: str, owners: list[str]) -> str:
    return (
        f"{DEDICATED_OFFLINE_PREFIX}{label}' is dedicated to offline "
        f"worker(s) {', '.join(owners)}"
    )


def is_agnostic_model(model: str | None) -> bool:
    """True when ``model`` pins nothing concrete (auto / max / unset)."""
    return (model or "").strip().lower() in AGNOSTIC_MODELS


def queue_label(queue: str | None) -> str:
    """Human/route label for a queue: ``main`` for the default queue, else the
    playlist name. Used for worker ``queues`` matching."""
    return queue or "main"


def _norm_set(values: list[str] | None) -> set[str]:
    """Lower-cased, whitespace-stripped set (advertised models / mcps)."""
    return {str(v).strip().lower() for v in (values or []) if str(v).strip()}


@dataclass(frozen=True)
class TaskCandidate:
    """One runnable task with the metadata the scheduler arbitrates on."""

    queue: str | None              # None = main queue
    task: str
    priority: int
    model: str                     # auto | max | explicit model id
    required_mcps: tuple[str, ...] = ()   # MCP connectors the brief declares
    after: tuple[str, ...] = ()    # frontmatter dependency stems (without .md)
    # Resolved target repo (task frontmatter ``repo:`` → queue default). ``None``
    # when resolution raised :class:`nightshift.repos.RepoConfigError`, in which
    # case ``repo_error`` carries the message so the manager can mark the task
    # ``blocked`` (an authoring error). Availability (repo present + ``.git``) is
    # a *separate* check the manager performs in ``worker_poll``.
    repo: str | None = None
    repo_error: str | None = None
    # Workflow awareness (§6.1): the definition name + current step id when the
    # brief carries a ``workflow:`` field, and an authoring error (unresolved
    # role / unknown definition / unknown step) surfaced exactly like
    # ``repo_error`` (the manager marks the task blocked).
    workflow: str | None = None
    workflow_step: str | None = None
    workflow_error: str | None = None

    @property
    def label(self) -> str:
        return queue_label(self.queue)


class WorkflowResolver(Protocol):
    """Resolves a workflow task's current step + model from its frontmatter and
    the queue config. Returns ``(workflow, step_id, resolved_model)`` on success
    or ``(None, None, error)`` on an authoring error."""

    def __call__(
        self, meta: dict, queue_config: dict
    ) -> tuple[str, str, str] | tuple[None, None, str]: ...


@dataclass(frozen=True)
class WorkerFilter:
    """A polling worker's capabilities, echoed in every poll request. ``queues``
    / ``priorities`` of ``None`` mean "any"; ``models`` / ``mcps`` of ``None`` or
    empty mean the worker advertises none (so it can only take agnostic / no-MCP
    tasks)."""

    worker_id: str
    queues: list[str] | None = None
    priorities: list[int] | None = None
    models: list[str] | None = None
    mcps: list[str] | None = None

    def _model_ok(self, cand: TaskCandidate) -> bool:
        if is_agnostic_model(cand.model):
            return True
        return cand.model.strip().lower() in _norm_set(self.models)

    def _mcps_ok(self, cand: TaskCandidate) -> bool:
        if not cand.required_mcps:
            return True
        return _norm_set(cand.required_mcps).issubset(_norm_set(self.mcps))

    def accepts(
        self,
        cand: TaskCandidate,
        *,
        dedication: dict[str, list[str]] | None = None,
    ) -> bool:
        if self.queues is not None and cand.label not in self.queues:
            return False
        # Manager-side queue dedication: a dedicated queue's tasks are only
        # offered to its bound worker(s); other workers are cut here even if they
        # otherwise match. A bound worker still serves its other queues normally.
        if dedication:
            owners = dedication.get(cand.label)
            if owners and self.worker_id not in owners:
                return False
        if self.priorities is not None and cand.priority not in self.priorities:
            return False
        if not self._model_ok(cand):
            return False
        if not self._mcps_ok(cand):
            return False
        return True


@dataclass
class SchedulerState:
    """Mutable arbitration memory carried across polls (round-robin fairness)."""

    served: dict[str, int] = field(default_factory=dict)

    def record(self, queue: str | None) -> None:
        label = queue_label(queue)
        self.served[label] = self.served.get(label, 0) + 1


def _after_deps(meta: dict) -> tuple[str, ...]:
    """Parse the ``after:`` dependency from frontmatter into a tuple of stems.

    Accepts a single stem (``after: 04.1.setup``) or a comma-separated list
    (``after: 04.1.setup, 04.2.schema``) for DAG fan-in. Each entry is
    stripped and ``.md`` suffixes are removed.
    """
    raw = meta.get("after")
    if not raw:
        return ()
    parts = str(raw).split(",")
    out: list[str] = []
    for p in parts:
        stem = p.strip().removesuffix(".md")
        if stem:
            out.append(stem)
    return tuple(out)


def _normalize_model(meta: dict, default_model: str) -> str:
    model = meta.get("model")
    if model is None or str(model).strip() == "":
        return default_model
    return str(model).strip()


def parse_required_mcps(meta: dict) -> tuple[str, ...]:
    """Parse the frontmatter ``mcp:`` declaration into a normalized tuple.

    The line-based frontmatter parser stores the value as a plain string, so we
    accept a comma-separated list with optional surrounding brackets/quotes:
    ``mcp: slack, github`` or ``mcp: [slack, github]``.
    """
    raw = meta.get("mcp")
    if raw is None:
        return ()
    text = str(raw).strip().strip("[]")
    out: list[str] = []
    for part in text.split(","):
        item = part.strip().strip("'\"").strip()
        if item:
            out.append(item)
    return tuple(out)


def build_candidates(
    tasks_root: Path,
    queue: str | None,
    *,
    default_model: str = "auto",
    workflow_resolver: WorkflowResolver | None = None,
) -> list[TaskCandidate]:
    """Read a queue's runnable tasks (in execution order) into candidates.

    Reuses :func:`live_ordered_queue` for ordering + the disabled/play-priority
    filters, then parses each task's frontmatter for priority / model / mcp /
    after, and resolves the target ``repo`` (task frontmatter override → the
    queue's default ``repo`` from its ``config.json``). A malformed/unset repo
    reference is *not* fatal here: the candidate carries ``repo=None`` plus
    ``repo_error`` and the manager marks it ``blocked``. Repo *availability* is
    checked by the manager (``worker_poll``), so it stays testable without a
    workspace on disk.
    """
    tasks_rel = playlists.tasks_rel(queue)
    queue_config = load_queue_config(tasks_root, tasks_rel)
    queue_repo = queue_config.get("repo")
    out: list[TaskCandidate] = []
    for stem in live_ordered_queue(tasks_root, tasks_rel):
        path = tasks_root / tasks_rel / f"{stem}.md"
        try:
            meta = split_frontmatter(path.read_text(errors="replace"))[0]
        except OSError:
            meta = {}
        repo: str | None = None
        repo_error: str | None = None
        try:
            repo = repos.resolve_repo(meta.get("repo"), queue_repo)
        except repos.RepoConfigError as err:
            repo_error = str(err)

        model = _normalize_model(meta, default_model)
        workflow: str | None = None
        workflow_step: str | None = None
        workflow_error: str | None = None
        # A workflow task's candidate model is its *current step's* resolved
        # model (§6.1); the resolver reads the definitions + manager cfg + queue
        # workflow_models. Non-workflow tasks (or no resolver) are unchanged.
        if workflow_resolver is not None and str(meta.get("workflow") or "").strip():
            wf, step, model_or_err = workflow_resolver(meta, queue_config)
            if wf is None:
                workflow_error = model_or_err
            else:
                workflow = wf
                workflow_step = step
                model = model_or_err

        out.append(
            TaskCandidate(
                queue=queue,
                task=stem,
                priority=task_priority(meta),
                model=model,
                required_mcps=parse_required_mcps(meta),
                after=_after_deps(meta),
                repo=repo,
                repo_error=repo_error,
                workflow=workflow,
                workflow_step=workflow_step,
                workflow_error=workflow_error,
            )
        )
    return out


def queue_head(
    candidates: list[TaskCandidate],
    *,
    worker: WorkerFilter,
    leased: set[tuple[str | None, str]],
    blocked: set[tuple[str | None, str]],
    present_tasks: set[str],
    dedication: dict[str, list[str]] | None = None,
) -> TaskCandidate | None:
    """First eligible candidate in a single queue's order, or None.

    A candidate is skipped when it is leased, blocked, after-blocked (its
    ``after:`` target file is still present in the queue), or rejected by the
    worker's capability filter / queue dedication.
    """
    for cand in candidates:
        key = (cand.queue, cand.task)
        if key in leased or key in blocked:
            continue
        if cand.after and any(dep in present_tasks for dep in cand.after):
            continue
        if not worker.accepts(cand, dedication=dedication):
            continue
        return cand
    return None


def arbitrate(
    heads: list[TaskCandidate],
    state: SchedulerState,
) -> TaskCandidate | None:
    """Pick one task across per-queue heads: ascending priority, then the
    least-served queue (round-robin), then queue label for determinism."""
    if not heads:
        return None
    return min(
        heads,
        key=lambda c: (c.priority, state.served.get(c.label, 0), c.label),
    )


def pick_next(
    candidates_by_queue: dict[str | None, list[TaskCandidate]],
    *,
    worker: WorkerFilter,
    leased: set[tuple[str | None, str]],
    blocked: set[tuple[str | None, str]],
    state: SchedulerState,
    dedication: dict[str, list[str]] | None = None,
) -> TaskCandidate | None:
    """End-to-end arbitration over already-built per-queue candidate lists.

    ``present_tasks`` (for ``after:`` resolution) is the set of every task stem
    currently in *any* queue -- a dependency is satisfied once its file is gone.
    """
    present_tasks = {c.task for cands in candidates_by_queue.values() for c in cands}
    heads: list[TaskCandidate] = []
    for cands in candidates_by_queue.values():
        head = queue_head(
            cands,
            worker=worker,
            leased=leased,
            blocked=blocked,
            present_tasks=present_tasks,
            dedication=dedication,
        )
        if head is not None:
            heads.append(head)
    chosen = arbitrate(heads, state)
    if chosen is not None:
        state.record(chosen.queue)
    return chosen


def unroutable(
    candidates_by_queue: dict[str | None, list[TaskCandidate]],
    *,
    available_models: set[str],
    available_mcps: set[str],
    dedication: dict[str, list[str]] | None = None,
    online_workers: set[str] | None = None,
) -> list[tuple[TaskCandidate, str]]:
    """Candidates that no live worker can ever currently serve, with a reason.

    The manager marks these **blocked** (with the reason) instead of leaving them
    pending forever. A candidate is unroutable when:

    * its pinned model is advertised by no live worker, or
    * a connector it requires is advertised by no live worker, or
    * its queue is dedicated to worker(s) that are all offline.

    ``auto``/``max`` candidates are never unroutable on the model axis.
    """
    avail_models = {m.strip().lower() for m in available_models}
    avail_mcps = {m.strip().lower() for m in available_mcps}
    online = online_workers or set()
    out: list[tuple[TaskCandidate, str]] = []
    for cands in candidates_by_queue.values():
        for cand in cands:
            reason: str | None = None
            if (
                not is_agnostic_model(cand.model)
                and cand.model.strip().lower() not in avail_models
            ):
                reason = _no_capable_worker_reason("model", cand.model)
            else:
                missing = [
                    m for m in cand.required_mcps if m.strip().lower() not in avail_mcps
                ]
                if missing:
                    reason = _no_capable_worker_reason("connector", missing[0])
                elif dedication:
                    owners = dedication.get(cand.label)
                    if owners and not any(o in online for o in owners):
                        reason = _dedicated_offline_reason(cand.label, owners)
            if reason is not None:
                out.append((cand, reason))
    return out


__all__ = [
    "AGNOSTIC_MODELS",
    "UNROUTABLE_REASON_PREFIXES",
    "SchedulerState",
    "TaskCandidate",
    "WorkerFilter",
    "WorkflowResolver",
    "arbitrate",
    "build_candidates",
    "is_agnostic_model",
    "parse_required_mcps",
    "pick_next",
    "queue_head",
    "queue_label",
    "unroutable",
]
