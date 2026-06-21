"""Manager configuration model — ManagerSettings, Cadences, OperatorConfig.

ManagerSettings is the top-level model for ``.nightshift/manager.json``.
It composes Cadences (nested under ``cadences``) and OperatorConfig (flattened
to the top level). Secrets (shared_secret, dsn) are declared with
``secret=True`` and route to ``.env``, not manager.json.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nightshift.config.io import load_dotenv, load_json, manager_json_path, save_json
from nightshift.config.meta import meta
from nightshift.engine import WIP_REF_PREFIX, normalize_wip_prefix


@dataclass(frozen=True)
class Cadences:
    """Timing knobs shared by manager + workers (seconds, except refresh_ms)."""

    poll_seconds: float = field(default=5.0, metadata=meta(
        category="Cadences", label="Poll seconds",
        desc="Worker idle poll interval.", apply="restart"))
    heartbeat_seconds: float = field(default=10.0, metadata=meta(
        category="Cadences", label="Heartbeat seconds",
        desc="Worker→manager heartbeat interval that keeps a lease alive.",
        apply="restart"))
    lease_ttl_seconds: float = field(default=120.0, metadata=meta(
        category="Cadences", label="Lease TTL seconds",
        desc="Lease lifetime before the manager reclaims it.", apply="restart"))
    worker_stale_seconds: float = field(default=45.0, metadata=meta(
        category="Cadences", label="Worker stale seconds",
        desc="Silence after which a worker is marked offline.", apply="restart"))
    refresh_ms: int = field(default=20000, metadata=meta(
        category="Cadences", label="Refresh ms",
        desc="UI safety-poll fallback (SSE is the primary live channel).",
        apply="restart"))


@dataclass(frozen=True)
class OperatorConfig:
    """Task-policy keys formerly scattered as top-level config.json values."""

    max_per_day: int = field(default=200, metadata=meta(
        category="Scheduling", label="Max per day",
        desc="Dispatch cap for the daily-queue path.",
        apply="next-task", env="NIGHTSHIFT_MAX_PER_DAY"))
    max_concurrent_queues: int = field(default=2, metadata=meta(
        category="Scheduling", label="Max concurrent queues",
        desc="Max queues served concurrently.", apply="next-task"))
    max_nights_before_parking: int = field(default=2, metadata=meta(
        category="Scheduling", label="Max nights before parking",
        desc="Nights a failing task retries before being parked.",
        apply="next-task"))
    scheduled_models: tuple[str, ...] = field(
        default=("claude-sonnet-4-6", "claude-opus-4-8"),
        metadata=meta(
            category="Scheduling", label="Scheduled models",
            desc="Pin-only allow-set for explicit model: in briefs.",
            apply="next-task", type="string_list"))
    default_model: str = field(default="auto", metadata=meta(
        category="Scheduling", label="Default model",
        desc="Model a brief inherits when it sets no model:.",
        apply="next-task", env="NIGHTSHIFT_DEFAULT_MODEL"))
    model: str | None = field(default=None, metadata=meta(
        category="Scheduling", label="Model (legacy)",
        desc="Legacy compat path for model selection.",
        apply="next-task"))
    cursor_model: str | None = field(default=None, metadata=meta(
        category="Scheduling", label="Cursor model (legacy)",
        desc="Legacy compat path for cursor model selection.",
        apply="next-task"))

    landing_mode: str = field(default="none", metadata=meta(
        category="Landing & Git", label="Landing mode",
        desc="Remote policy: none (local only), push, or pr.",
        apply="restart", env="NIGHTSHIFT_LANDING_MODE",
        options=["none", "push", "pr"]))
    rendezvous_remote: str | None = field(default="origin", metadata=meta(
        category="Landing & Git", label="Rendezvous remote",
        desc="Git remote name for cross-machine landing; null disables.",
        apply="restart", env="NIGHTSHIFT_RENDEZVOUS_REMOTE"))
    wip_ref_prefix: str = field(default="nightshift-wip", metadata=meta(
        category="Landing & Git", label="WIP ref prefix",
        desc="Cross-machine WIP namespace for validated branches.",
        apply="restart", env="NIGHTSHIFT_WIP_REF_PREFIX"))
    tasks_repo: str = field(default="nightshift-tasks", metadata=meta(
        category="Landing & Git", label="Tasks repo",
        desc="Name of the content-store repo holding briefs + queue config.",
        apply="restart", env="NIGHTSHIFT_TASKS_REPO"))
    automerge: bool = field(default=False, metadata=meta(
        category="Landing & Git", label="Automerge",
        desc="Default automerge for PR-mode landings.", apply="next-task"))
    draft: bool = field(default=False, metadata=meta(
        category="Landing & Git", label="Draft",
        desc="Default draft state for PR-mode landings.", apply="next-task"))
    autostash_operator_work: bool = field(default=True, metadata=meta(
        category="Landing & Git", label="Autostash operator work",
        desc="Stash uncommitted operator work before a local landing.",
        apply="next-task"))

    forbidden_paths: tuple[str, ...] = field(
        default=("^\\.github/workflows/", "^CLAUDE\\.md$", "^AGENTS\\.md$"),
        metadata=meta(
            category="Worker execution policy", label="Forbidden paths",
            desc="Regex paths a worker may never modify.",
            apply="next-task", type="regex_list"))
    forbidden_template_paths: tuple[str, ...] = field(
        default=("^tools/nightshift/templates/",),
        metadata=meta(
            category="Worker execution policy", label="Forbidden template paths",
            desc="Paths forbidden specifically in template/decomposition runs.",
            apply="next-task", type="regex_list"))
    diff_cap_lines: int = field(default=1500, metadata=meta(
        category="Worker execution policy", label="Diff cap lines",
        desc="Default max changed lines for a task's result.",
        apply="next-task"))
    diff_cap_exempt_paths: tuple[str, ...] = field(
        default=("^tests/fixtures/", "^docs/", "\\.md$"),
        metadata=meta(
            category="Worker execution policy", label="Diff cap exempt paths",
            desc="Paths excluded from the diff cap.",
            apply="next-task", type="regex_list"))
    max_fix_attempts: int = field(default=6, metadata=meta(
        category="Worker execution policy", label="Max fix attempts",
        desc="Fix retries (dispatch path).", apply="next-task"))

    auto_resolve: bool = field(default=True, metadata=meta(
        category="Conflict resolution", label="Auto resolve",
        desc="Hand out resolve work-orders on conflict/validation failure.",
        apply="next-task"))
    max_resolve_attempts: int = field(default=2, metadata=meta(
        category="Conflict resolution", label="Max resolve attempts",
        desc="Resolve retries before parking.", apply="next-task"))
    resolve_model: str | None = field(default=None, metadata=meta(
        category="Conflict resolution", label="Resolve model",
        desc="Optional model override for resolve runs.", apply="next-task"))
    resolve_backend: str | None = field(default=None, metadata=meta(
        category="Conflict resolution", label="Resolve backend",
        desc="Optional backend override for resolve runs.", apply="next-task"))


@dataclass(frozen=True)
class ManagerSettings:
    """The manager file model — .nightshift/manager.json.

    Composes server fields, Cadences (nested), and OperatorConfig (flattened).
    Secrets route to .env via ``secret=True``.
    """

    host: str = field(default="0.0.0.0", metadata=meta(
        category="Server & Network", label="Host",
        desc="Bind address for the manager HTTP server.",
        apply="restart", env="NIGHTSHIFT_MANAGER_HOST"))
    port: int = field(default=8800, metadata=meta(
        category="Server & Network", label="Port",
        desc="Bind port (operator UI + worker/operator API).",
        apply="restart", env="NIGHTSHIFT_MANAGER_PORT"))
    shared_secret: str | None = field(default=None, metadata=meta(
        category="Server & Network", label="Shared secret",
        desc="If set, every worker call must send a matching header.",
        apply="restart", secret=True, env="NIGHTSHIFT_SHARED_SECRET"))
    dsn: str | None = field(default=None, metadata=meta(
        category="Server & Network", label="Database DSN",
        desc="Nightshift's own Postgres DSN; unset = in-memory store.",
        apply="restart", secret=True, env="NIGHTSHIFT_PG_DSN"))

    cadences: Cadences = field(default_factory=Cadences, metadata=meta(
        category="Cadences", label="Cadences",
        desc="Timing knobs shared by manager + workers.",
        apply="restart", editable=False))

    # OperatorConfig fields are flattened into the JSON — we compose them
    # as a nested object for code ergonomics, serialized flat.
    operator: OperatorConfig = field(default_factory=OperatorConfig, metadata=meta(
        category="Scheduling", label="Operator config",
        desc="Task-policy keys.", apply="next-task", editable=False))

    # Internal: the raw dict from disk for pass-through access by legacy readers.
    raw: dict[str, Any] = field(default_factory=dict, metadata=meta(
        category="Internal", label="Raw", desc="Raw dict.",
        apply="restart", editable=False))


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return default


def load_manager_settings(workspace: Path) -> ManagerSettings:
    """Resolve manager config from ``.nightshift/manager.json`` + env.

    Environment overrides (``NIGHTSHIFT_*``) always win over file values.
    Secrets (shared_secret, dsn) are resolved purely from env/.env — they
    are never read from the JSON file.
    """
    load_dotenv(workspace)
    data = load_json(manager_json_path(workspace))

    cad_data = data.get("cadences", {}) if isinstance(data.get("cadences"), dict) else {}
    cadences = Cadences(
        poll_seconds=_as_float(cad_data.get("poll_seconds"), 5.0),
        heartbeat_seconds=_as_float(cad_data.get("heartbeat_seconds"), 10.0),
        lease_ttl_seconds=_as_float(cad_data.get("lease_ttl_seconds"), 120.0),
        worker_stale_seconds=_as_float(cad_data.get("worker_stale_seconds"), 45.0),
        refresh_ms=_as_int(cad_data.get("refresh_ms"), 20000),
    )

    landing_mode = (
        os.environ.get("NIGHTSHIFT_LANDING_MODE")
        or data.get("landing_mode")
        or "none"
    )
    if landing_mode not in ("none", "push", "pr"):
        landing_mode = "none"

    env_rendezvous = os.environ.get("NIGHTSHIFT_RENDEZVOUS_REMOTE")
    if env_rendezvous:
        rendezvous_remote: str | None = env_rendezvous
    elif "rendezvous_remote" in data:
        rv = data.get("rendezvous_remote")
        rendezvous_remote = str(rv) if rv else None
    else:
        rendezvous_remote = "origin"

    raw_wip_prefix = (
        os.environ.get("NIGHTSHIFT_WIP_REF_PREFIX")
        or data.get("wip_ref_prefix")
        or WIP_REF_PREFIX
    )
    try:
        wip_ref_prefix = normalize_wip_prefix(raw_wip_prefix)
    except ValueError:
        wip_ref_prefix = WIP_REF_PREFIX

    operator = OperatorConfig(
        max_per_day=_as_int(data.get("max_per_day"), 200),
        max_concurrent_queues=_as_int(data.get("max_concurrent_queues"), 2),
        max_nights_before_parking=_as_int(data.get("max_nights_before_parking"), 2),
        scheduled_models=_as_tuple(
            data.get("scheduled_models"),
            ("claude-sonnet-4-6", "claude-opus-4-8")),
        default_model=(
            os.environ.get("NIGHTSHIFT_DEFAULT_MODEL")
            or data.get("default_model")
            or "auto"),
        model=data.get("model"),
        cursor_model=data.get("cursor_model"),
        landing_mode=landing_mode,
        rendezvous_remote=rendezvous_remote,
        wip_ref_prefix=wip_ref_prefix,
        tasks_repo=(
            os.environ.get("NIGHTSHIFT_TASKS_REPO")
            or data.get("tasks_repo")
            or "nightshift-tasks"),
        automerge=bool(data.get("automerge", False)),
        draft=bool(data.get("draft", False)),
        autostash_operator_work=bool(data.get("autostash_operator_work", True)),
        forbidden_paths=_as_tuple(
            data.get("forbidden_paths"),
            ("^\\.github/workflows/", "^CLAUDE\\.md$", "^AGENTS\\.md$")),
        forbidden_template_paths=_as_tuple(
            data.get("forbidden_template_paths"),
            ("^tools/nightshift/templates/",)),
        diff_cap_lines=_as_int(data.get("diff_cap_lines"), 1500),
        diff_cap_exempt_paths=_as_tuple(
            data.get("diff_cap_exempt_paths"),
            ("^tests/fixtures/", "^docs/", "\\.md$")),
        max_fix_attempts=_as_int(data.get("max_fix_attempts"), 6),
        auto_resolve=bool(data.get("auto_resolve", True)),
        max_resolve_attempts=_as_int(data.get("max_resolve_attempts"), 2),
        resolve_model=data.get("resolve_model"),
        resolve_backend=data.get("resolve_backend"),
    )

    return ManagerSettings(
        host=os.environ.get("NIGHTSHIFT_MANAGER_HOST") or data.get("host") or "0.0.0.0",
        port=_as_int(
            os.environ.get("NIGHTSHIFT_MANAGER_PORT") or data.get("port"), 8800),
        shared_secret=os.environ.get("NIGHTSHIFT_SHARED_SECRET") or None,
        dsn=os.environ.get("NIGHTSHIFT_PG_DSN") or None,
        cadences=cadences,
        operator=operator,
        raw=data,
    )


def save_manager_settings(workspace: Path, settings: ManagerSettings) -> None:
    """Persist a ManagerSettings to ``.nightshift/manager.json``.

    Secrets (shared_secret, dsn) are never written to the JSON file.
    """
    data: dict[str, Any] = {
        "host": settings.host,
        "port": settings.port,
        "landing_mode": settings.operator.landing_mode,
        "rendezvous_remote": settings.operator.rendezvous_remote,
        "tasks_repo": settings.operator.tasks_repo,
        "wip_ref_prefix": settings.operator.wip_ref_prefix,
        "cadences": {
            "poll_seconds": settings.cadences.poll_seconds,
            "heartbeat_seconds": settings.cadences.heartbeat_seconds,
            "lease_ttl_seconds": settings.cadences.lease_ttl_seconds,
            "worker_stale_seconds": settings.cadences.worker_stale_seconds,
            "refresh_ms": settings.cadences.refresh_ms,
        },
        "default_model": settings.operator.default_model,
        "scheduled_models": list(settings.operator.scheduled_models),
        "max_per_day": settings.operator.max_per_day,
        "max_concurrent_queues": settings.operator.max_concurrent_queues,
        "max_nights_before_parking": settings.operator.max_nights_before_parking,
        "automerge": settings.operator.automerge,
        "draft": settings.operator.draft,
        "autostash_operator_work": settings.operator.autostash_operator_work,
        "diff_cap_lines": settings.operator.diff_cap_lines,
        "diff_cap_exempt_paths": list(settings.operator.diff_cap_exempt_paths),
        "forbidden_paths": list(settings.operator.forbidden_paths),
        "forbidden_template_paths": list(settings.operator.forbidden_template_paths),
        "max_fix_attempts": settings.operator.max_fix_attempts,
        "auto_resolve": settings.operator.auto_resolve,
        "max_resolve_attempts": settings.operator.max_resolve_attempts,
        "resolve_model": settings.operator.resolve_model,
        "resolve_backend": settings.operator.resolve_backend,
    }
    if settings.operator.model is not None:
        data["model"] = settings.operator.model
    if settings.operator.cursor_model is not None:
        data["cursor_model"] = settings.operator.cursor_model
    save_json(manager_json_path(workspace), data)
