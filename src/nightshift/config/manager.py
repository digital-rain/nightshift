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
from nightshift.lifecycle import LandingMode


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
    git_refresh_seconds: float = field(default=15.0, metadata=meta(
        category="Cadences", label="Git refresh seconds",
        desc=(
            "Minimum interval between origin/main fetch checks per target repo. "
            "Each check fetches origin/main and fast-forwards local main only when "
            "the remote tip moved; otherwise the manager waits this long before "
            "checking again. 0 disables throttling (not recommended)."),
        apply="restart"))


@dataclass(frozen=True)
class OperatorConfig:
    """Task-policy keys formerly scattered as top-level config.json values."""

    max_per_day: int = field(default=200, metadata=meta(
        category="Scheduling", label="Max per day",
        desc="Dispatch cap for the daily-queue path.",
        env="NIGHTSHIFT_MAX_PER_DAY"))
    max_concurrent_queues: int = field(default=2, metadata=meta(
        category="Scheduling", label="Max concurrent queues",
        desc="Max queues served concurrently."))
    max_nights_before_parking: int = field(default=2, metadata=meta(
        category="Scheduling", label="Max nights before parking",
        desc="Nights a failing task retries before being parked.",
        ))
    scheduled_models_allow: tuple[str, ...] = field(
        default=("claude-code/claude-sonnet-4-6", "claude-code/claude-opus-4-8"),
        metadata=meta(
            category="Scheduling", label="Scheduled models allow",
            desc="Filter: only auto-schedule tasks pinned to these provider/model ids.",
            type="string_list", validate="model_id_list"))
    default_model: str = field(default="auto", metadata=meta(
        category="Scheduling", label="Default model",
        desc="Model a brief inherits when it sets no model:.",
        env="NIGHTSHIFT_DEFAULT_MODEL",
        validate="model_id_or_keyword"))

    landing_mode: LandingMode = field(default=LandingMode.NONE, metadata=meta(
        category="Landing & Git", label="Landing mode",
        desc="Remote policy: none (local only), push, or pr.",
        apply="restart", env="NIGHTSHIFT_LANDING_MODE",
        options=[mode.value for mode in LandingMode]))
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
        desc="Default automerge for PR-mode landings."))
    draft: bool = field(default=False, metadata=meta(
        category="Landing & Git", label="Draft",
        desc="Default draft state for PR-mode landings."))
    autostash_operator_work: bool = field(default=True, metadata=meta(
        category="Landing & Git", label="Autostash operator work",
        desc="Stash uncommitted operator work before a local landing.",
        ))
    max_push_retries: int = field(default=3, metadata=meta(
        category="Landing & Git", label="Max push retries",
        desc=(
            "How many times a land re-syncs origin/main and re-squashes when the "
            "push is rejected because origin advanced (optimistic concurrency)."),
        ))
    validate_on_integrate: bool = field(default=False, metadata=meta(
        category="Landing & Git", label="Validate on integrate",
        desc=(
            "Re-run the validate command on the integrated tree before pushing "
            "when origin drifted but the squash was textually clean (guards "
            "against semantic conflicts). Off by default."),
        ))

    forbidden_paths: tuple[str, ...] = field(
        default=("^\\.github/workflows/", "^CLAUDE\\.md$", "^AGENTS\\.md$"),
        metadata=meta(
            category="Worker execution policy", label="Forbidden paths",
            desc="Regex paths a worker may never modify.",
            type="regex_list"))
    forbidden_template_paths: tuple[str, ...] = field(
        default=("^tools/nightshift/templates/",),
        metadata=meta(
            category="Worker execution policy", label="Forbidden template paths",
            desc="Paths forbidden specifically in template/decomposition runs.",
            type="regex_list"))
    diff_cap_lines: int = field(default=1500, metadata=meta(
        category="Worker execution policy", label="Diff cap lines",
        desc="Default max changed lines for a task's result.",
        ))
    diff_cap_exempt_paths: tuple[str, ...] = field(
        default=("^tests/fixtures/", "^docs/", "\\.md$"),
        metadata=meta(
            category="Worker execution policy", label="Diff cap exempt paths",
            desc="Paths excluded from the diff cap.",
            type="regex_list"))
    max_fix_attempts: int = field(default=6, metadata=meta(
        category="Worker execution policy", label="Max fix attempts",
        desc="Fix retries (dispatch path)."))
    validate_cmd: str = field(default="just validate", metadata=meta(
        category="Worker execution policy", label="Validate command",
        desc=(
            "System-wide default validate command run after each task. "
            "Per-queue overrides take precedence. Empty string disables "
            "validation globally."),
        json_key="validate"))
    quarantine_threshold: int = field(default=2, metadata=meta(
        category="Worker execution policy", label="Quarantine threshold",
        desc=(
            "Consecutive runs of one task that make no progress (no commit "
            "landed, or a worker error) before it is quarantined: held in the "
            "queue but skipped by every worker so a confused task cannot burn "
            "budget in a re-execution loop. 0 disables quarantine."),
        env="NIGHTSHIFT_QUARANTINE_THRESHOLD"))

    auto_resolve: bool = field(default=True, metadata=meta(
        category="Conflict resolution", label="Auto resolve",
        desc="Hand out resolve work-orders on conflict/validation failure.",
        ))
    max_resolve_attempts: int = field(default=2, metadata=meta(
        category="Conflict resolution", label="Max resolve attempts",
        desc="Resolve retries before parking."))
    resolve_model: str | None = field(default=None, metadata=meta(
        category="Conflict resolution", label="Resolve model",
        desc="Optional model override for resolve runs.",
        validate="model_id"))
    resolve_backend: str | None = field(default=None, metadata=meta(
        category="Conflict resolution", label="Resolve backend",
        desc="Optional backend override for resolve runs."))
    max_concurrent_resolves: int = field(default=1, metadata=meta(
        category="Conflict resolution", label="Max concurrent resolves",
        desc=(
            "Cap on simultaneous out-of-process resolve jobs per repo. Resolve "
            "agent work runs unlocked and concurrent with normal dispatch; this "
            "bounds thrash. The final merge is always serialized."),
        ))


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
        desc="Task-policy keys.", editable=False, flatten=True))

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
        git_refresh_seconds=_as_float(
            cad_data.get("git_refresh_seconds", cad_data.get("origin_sync_seconds")),
            15.0,
        ),
    )

    # Parsed once at the config boundary: an unknown mode fails the load loudly
    # (ValueError) instead of silently degrading to "none".
    landing_mode = LandingMode(
        os.environ.get("NIGHTSHIFT_LANDING_MODE")
        or data.get("landing_mode")
        or "none"
    )

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
        scheduled_models_allow=_as_tuple(
            data.get("scheduled_models_allow") or data.get("scheduled_models"),
            ("claude-code/claude-sonnet-4-6", "claude-code/claude-opus-4-8")),
        default_model=(
            os.environ.get("NIGHTSHIFT_DEFAULT_MODEL")
            or data.get("default_model")
            or "auto"),
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
        max_push_retries=_as_int(data.get("max_push_retries"), 3),
        validate_on_integrate=bool(data.get("validate_on_integrate", False)),
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
        validate_cmd=str(
            data.get("validate")
            if "validate" in data
            else data.get("validate_cmd", "just validate")
        ),
        quarantine_threshold=_as_int(
            os.environ.get("NIGHTSHIFT_QUARANTINE_THRESHOLD")
            or data.get("quarantine_threshold"),
            2),
        auto_resolve=bool(data.get("auto_resolve", True)),
        max_resolve_attempts=_as_int(data.get("max_resolve_attempts"), 2),
        resolve_model=data.get("resolve_model"),
        resolve_backend=data.get("resolve_backend"),
        max_concurrent_resolves=_as_int(data.get("max_concurrent_resolves"), 1),
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
            "git_refresh_seconds": settings.cadences.git_refresh_seconds,
        },
        "default_model": settings.operator.default_model,
        "scheduled_models_allow": list(settings.operator.scheduled_models_allow),
        "max_per_day": settings.operator.max_per_day,
        "max_concurrent_queues": settings.operator.max_concurrent_queues,
        "max_nights_before_parking": settings.operator.max_nights_before_parking,
        "automerge": settings.operator.automerge,
        "draft": settings.operator.draft,
        "autostash_operator_work": settings.operator.autostash_operator_work,
        "max_push_retries": settings.operator.max_push_retries,
        "validate_on_integrate": settings.operator.validate_on_integrate,
        "diff_cap_lines": settings.operator.diff_cap_lines,
        "diff_cap_exempt_paths": list(settings.operator.diff_cap_exempt_paths),
        "forbidden_paths": list(settings.operator.forbidden_paths),
        "forbidden_template_paths": list(settings.operator.forbidden_template_paths),
        "max_fix_attempts": settings.operator.max_fix_attempts,
        "validate": settings.operator.validate_cmd,
        "quarantine_threshold": settings.operator.quarantine_threshold,
        "auto_resolve": settings.operator.auto_resolve,
        "max_resolve_attempts": settings.operator.max_resolve_attempts,
        "resolve_model": settings.operator.resolve_model,
        "resolve_backend": settings.operator.resolve_backend,
        "max_concurrent_resolves": settings.operator.max_concurrent_resolves,
    }
    save_json(manager_json_path(workspace), data)
