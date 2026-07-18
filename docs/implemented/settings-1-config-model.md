# Nightshift — Settings Editor, Part 1: Config Model & Storage Unification

**Subject:** Make the **dataclasses the single source of truth** for all manager, worker, and player/UI configuration — shape, defaults, nesting, and editor metadata — and unify storage into three self-describing files under `.nightshift/` (plus `.env` for secrets). This replaces today's sprinkled `cfg.get("…", inline_default)` access and the hand-maintained `SCHEMA`/`DEFAULTS` list.

**Status:** Proposed — design for unimplemented work. Where this doc and the code disagree once implemented, the code governs and this doc should be updated.

**Series:** This is **Part 1 of 3** for the gear-menu Settings editor:
1. **Config model & storage unification** (this doc) — the foundation: typed, metadata-bearing config with one file per surface.
2. [Registry & admin API](settings-2-admin-api.md) — derive a `FieldSpec` registry by walking the dataclasses; serve/validate it over `/api/settings`.
3. [Multi-tier settings UI](settings-3-multitier-ui.md) — the VS-Code-style full-page editor on the manager and a settings page on the worker.

Parts 2 and 3 depend on this part and add **no** new config knowledge of their own — they consume the model defined here.

**Primary sources (to change):** `src/nightshift/manager/config.py` (`ManagerConfig`, `Cadences` — extend with metadata + absorb the operator block), `src/nightshift/worker/config.py` (`WorkerConfig` — add metadata), `src/nightshift/server/settings.py` (replace `SCHEMA`/`DEFAULTS`/`validate_settings` with a `PlayerConfig` dataclass + new file path), `src/nightshift/spawn_daily.py` (`load_config`/`save_config_value`/`resolve_config` repointed to the new manager file; inline operator-key defaults removed in favor of `OperatorConfig`), a new `src/nightshift/config/` package (the dataclasses + shared load/save/`.env` helpers), `src/nightshift/server/app.py` + `src/nightshift/manager/app.py` (read the new files), a new **`src/nightshift/assets/config/` templates dir** (`manager.json` / `worker.json` / `player.json` samples) + a committed **`.env.example`**, a **`nightshift init` scaffold command** (`python -m nightshift init`) wired to a **`just init`** recipe, `src/nightshift/_paths.py` (+`CONFIG_TEMPLATES_DIR`), `justfile`, `.gitignore`, `docs/configuration-reference.md`, `docs/setup-guide.md`, and the repo's legacy example `config.json` + the inline `config.json.local` example (deleted, replaced by the templates + the new files).

---

## 0. The one idea

Configuration today is **modeled in three different ways and stored in three ad-hoc places**:

- The `manager` block + cadences + `tasks_repo`/`wip_ref_prefix` are typed (`ManagerConfig`, `Cadences`), but the **~22 top-level operator/task-policy keys** (`max_per_day`, `automerge`, `forbidden_paths`, `scheduled_models_allow`, …) are **not modeled at all** — they're read ad-hoc via `cfg.get("model", "claude-sonnet-4-6")` scattered across `spawn_daily.py` / `engine.py` / `scheduler.py`, with inline defaults **that already disagree** (e.g. `resolve_frontmatter` defaults `automerge` to `True` while `config.json` ships `false`).
- The worker config is typed (`WorkerConfig`) and stored in `config.json.local`.
- The player/UI settings are a **hand-maintained `SCHEMA: list[dict]` + `DEFAULTS: dict` + `validate_settings()`** in `server/settings.py`, stored in `.nightshift/settings.json`.

This part fixes the **model and the storage**, in that order, because the editor (Parts 2–3) becomes trivial once every setting is a typed field that knows its own label, category, type, default, and destination file.

The deliverable of Part 1 is **invisible to the UI**: identical runtime behavior, but every editable setting is now a dataclass field carrying its editor metadata, persisted to exactly one of `.nightshift/manager.json`, `.nightshift/worker.json`, `.nightshift/player.json`, or `.env` (secrets).

---

## 1. Design tenets

1. **Loading and editing are different artifacts.** Loading (consumed by the manager/worker at startup) already works through the dataclass resolvers. Editing (Parts 2–3) needs field metadata (label/category/apply/secret) and write-back routing. We attach the editing metadata **to the loading model** so the two cannot drift, but we do **not** collapse them into one object or retrofit `pydantic-settings`.
2. **Dataclasses are the single source of truth for shape, defaults, and nesting.** No parallel `SCHEMA` list restating types/defaults. The `FieldSpec` registry (Part 2) is a *derived projection* of the dataclasses, not a second structure.
3. **Editor metadata is co-located with the field.** Via `dataclasses.field(metadata={…})`, so a field's label/category/apply/secret can never separate from its type/default.
4. **One file per surface; secrets never in a checked-in file.** `.nightshift/{manager,worker,player}.json` are committed; `.env` holds secrets and is gitignored.
5. **Clean cutover, no compatibility layer.** Nothing is running yet. We create the new files, move secrets to `.env`, delete the legacy files, and point the loaders **only** at the new locations. No read-fallback, no auto-migration, no `just migrate` recipe.
6. **`workspace` is a launch input, not a setting.** It *locates* the config files, so it can't live in them. It is resolved from `--workspace` / `NIGHTSHIFT_WORKSPACE` and injected into the running `ManagerConfig`/`WorkerConfig`; the editor shows it read-only.

---

## 2. Storage layout (after)

All three JSON files live at `<workspace>/.nightshift/` and are **committed**. Each file *is* its surface — there is no wrapper key (the old `{"manager": {…}}` envelope is gone; `manager.json` is the manager config).

```
<workspace>/
├── .nightshift/
│   ├── manager.json     # manager + operator/task-policy config (committed)
│   ├── worker.json      # this box's worker config (committed)
│   └── player.json      # operator UI/player preferences (committed)
└── .env                 # secrets only (gitignored): NIGHTSHIFT_SHARED_SECRET, NIGHTSHIFT_PG_DSN, …
```

**All settings live in the single `.nightshift/` directory** (the user's explicit "one place" choice). This is *settings only*. The worker's local run-history store remains `<workspace>/.nightshift-worker/` (`LOCAL_DIR` in `worker/local_store.py`) — that's machine-local **runtime state**, not configuration, and stays gitignored and outside `.nightshift/`. (Folding it under `.nightshift/` is a possible later cleanup, explicitly out of scope here.)

**Resolution precedence (low → high), unchanged in spirit:** dataclass default → JSON file → environment (`NIGHTSHIFT_*`, loaded from `.env`). Environment always wins; the editor surfaces when a field is env-shadowed (§6).

### Deletions and moves (one-time, performed as part of this work)

| Legacy | New |
|---|---|
| `<workspace>/config.json` (`manager` block + top-level keys) | `<workspace>/.nightshift/manager.json` (flattened; `cadences` stays nested) |
| `<workspace>/config.json.local` | `<workspace>/.nightshift/worker.json` |
| `<workspace>/.nightshift/settings.json` | `<workspace>/.nightshift/player.json` |
| `manager.shared_secret`, `manager.dsn` (were in `config.json`) | `.env` as `NIGHTSHIFT_SHARED_SECRET`, `NIGHTSHIFT_PG_DSN` |

> **Out of scope (untouched):** the **per-queue** and content-store `config.json` files (`ORDER_CONFIG`/`PLAYLIST_CONFIG`, `<tasks_root>/<queue>/config.json`, `<tasks_root>/config.json`) and the **user-level** `~/.nightshift/config.json`. The user-level file held only the persisted `workspace`, which now comes from `--workspace`/`NIGHTSHIFT_WORKSPACE`; remove the `workspace`-persistence path (`save_user_config_value("workspace", …)`) and its UI write, leaving `workspace` a pure launch input.

### `.gitignore`

Two existing rules conflict with "the three JSON files are committed" and must change:

- Line ~222 ignores `config.json.local` — **remove it** (its replacement `worker.json` is committed).
- Line ~232 ignores **all of** `.nightshift/` ("UI player settings + other manager-local runtime state"). Since the three config files now live there *and are committed* while the rest of `.nightshift/` stays machine-local, switch to a selective rule:

```gitignore
# Nightshift runtime state stays ignored, but the committed config files do not.
.nightshift/*
!.nightshift/manager.json
!.nightshift/worker.json
!.nightshift/player.json
```

- Keep `.env` ignored (line ~151) — it now holds the only secrets.

> The mix of committed config + ignored runtime under one `.nightshift/` dir is intentional (the user's "all in one place" choice); the negation pattern is what makes it work. Verify no current runtime artifact is named `manager.json`/`worker.json`/`player.json` (none is).

---

## 3. The config package

New package `src/nightshift/config/` owns the dataclasses and the shared file/`.env` plumbing, so manager, worker, and player share one mechanism.

```
src/nightshift/config/
├── __init__.py
├── meta.py        # FieldMeta TypedDict + metadata() helper + required-key assertion
├── io.py          # load_json/save_json (one file), dotenv load + single-key .env writer
├── manager.py     # ManagerConfig, Cadences, OperatorConfig (+ a combined ManagerSettings)
├── worker.py      # WorkerConfig
└── player.py      # PlayerConfig
```

> Existing `manager/config.py` and `worker/config.py` either move here or re-export from here; pick whichever minimizes churn at call sites. The intent is one home for config types.

### 3.1 Field metadata

Editor metadata is attached per field. It is **untyped/stringly-keyed** by nature of `dataclasses.field(metadata=…)`, so we constrain and verify it:

```python
# config/meta.py
from typing import TypedDict, Literal, Any
import dataclasses

class FieldMeta(TypedDict, total=False):
    category: str               # required — the sub-tier, e.g. "Scheduling"
    label: str                  # required — human label, e.g. "Max per day"
    desc: str                   # required — one-line description
    apply: Literal["live", "next-task", "restart"]  # required — see §5
    type: str                   # optional override; else inferred from the field type
    options: list[str]          # for enums
    secret: bool                # True ⟹ store routes to .env (see §4)
    env: str                    # the NIGHTSHIFT_* var that overrides this field
    editable: bool              # default True; set False to exclude (e.g. workspace, raw)

_REQUIRED = ("category", "label", "desc", "apply")

def meta(**kw: Any) -> dict:
    """Build a validated metadata mapping for dataclasses.field(metadata=…)."""
    return {"nightshift": FieldMeta(**kw)}  # namespaced to avoid clashes

def assert_complete(*dataclasses_: type) -> None:
    """Fail-fast at import: every editable field carries the required keys."""
    for dc in dataclasses_:
        for f in dataclasses.fields(dc):
            m = f.metadata.get("nightshift")
            if m is None or m.get("editable") is False:
                continue
            missing = [k for k in _REQUIRED if k not in m]
            if missing:
                raise AssertionError(f"{dc.__name__}.{f.name} missing meta keys: {missing}")
```

- A field is **editable iff it carries `nightshift` metadata** and is not `editable=False`. So `WorkerConfig.raw`, `WorkerConfig.workspace`, `ManagerConfig.raw` carry no metadata (or `editable=False`) and are excluded.
- `assert_complete(...)` runs at module import (fail-fast) so a metadata-key typo or a forgotten field is caught at startup, not at edit time. (Mitigates the "stringly-keyed metadata" risk noted during design.)
- Downstream code in Part 2 reads `.metadata["nightshift"]` and projects it into a **typed `FieldSpec`**, so consumers stay typed even though the source metadata is a dict.

### 3.2 Example: a field with metadata

```python
# config/manager.py (illustrative slice of OperatorConfig)
from dataclasses import dataclass, field
from nightshift.config.meta import meta

@dataclass(frozen=True)
class OperatorConfig:
    max_per_day: int = field(default=200, metadata=meta(
        category="Scheduling", label="Max per day",
        desc="Dispatch cap for the daily-queue path.",
        apply="next-task", env="NIGHTSHIFT_MAX_PER_DAY"))
    automerge: bool = field(default=False, metadata=meta(
        category="Landing & Git", label="Automerge",
        desc="Default automerge for PR-mode landings.", apply="next-task"))
    forbidden_paths: tuple[str, ...] = field(default=(...), metadata=meta(
        category="Worker execution policy", label="Forbidden paths",
        desc="Regex paths a worker may never modify.", apply="next-task", type="regex_list"))
    # … the remaining operator keys (§7 inventory)
```

> Mutable defaults (lists/dicts) use immutable defaults on the frozen dataclass (tuples / `field(default_factory=…)`), converted to/from JSON arrays/objects in `io.py`.

### 3.3 The three surface models

- **`ManagerSettings`** = the manager file model, composed of:
  - server fields (`host`, `port`) [`Identity & connection`],
  - `Cadences` (nested → `manager.json` `"cadences": {…}`) [`Cadences`],
  - the existing `tasks_repo`, `wip_ref_prefix`, `landing_mode`, `rendezvous_remote`,
  - `OperatorConfig` (the ~22 absorbed keys), flattened to the top level of `manager.json`.
  - **Secrets** `shared_secret`, `dsn` are declared with `secret=True` and therefore route to `.env`, not `manager.json` (§4). They remain attributes of the resolved in-memory object.
- **`WorkerConfig`** — unchanged fields + metadata; `backend` gains the `worker_backend` role (manager no longer carries it, §7.4); `shared_secret` is `secret=True` → `.env`.
- **`PlayerConfig`** — replaces `SCHEMA`/`DEFAULTS`: `theme`, `transport_mode`, `repeat_interval`, `port`. `validate_settings()` becomes per-field validation derived from the dataclass (enums via `options`, `repeat_interval` via the existing `parse_duration`, `port` range).

`assert_complete(ManagerSettings, Cadences, OperatorConfig, WorkerConfig, PlayerConfig)` is called once at package import.

---

## 4. Secrets → `.env`

`secret=True` on a field overrides its `store` to `.env` regardless of owning class. Secrets identified: `shared_secret` (both manager and worker) and `dsn` (manager).

- **Read:** resolved from the environment (`NIGHTSHIFT_SHARED_SECRET`, `NIGHTSHIFT_PG_DSN`). The `.env` file is loaded into `os.environ` at process start.
- **Write (Part 2):** a single-key `.env` writer in `config/io.py` upserts `KEY=value`, preserving other lines and comments. JSON files never contain secret keys.
- **Manager must load `.env`.** Today only the worker calls `_load_dotenv`. Lift it into `config/io.py` as `load_dotenv(workspace)` and call it at the start of both manager and worker launch (and the server entrypoint), so secrets-in-`.env` resolve uniformly. `os.environ.setdefault` semantics are preserved (a real env var still wins over `.env`).

> **Consequence for the model:** `ManagerConfig` previously did `env or block.get("shared_secret")`. After this change the `block.get(...)` fallback is removed (the JSON file no longer carries the secret); resolution is purely `NIGHTSHIFT_SHARED_SECRET` (from env/`.env`), default `None`.

---

## 5. Apply semantics (declaration only)

Each field declares how a change takes effect. Part 1 only **records** this; Parts 2–3 act on it.

| `apply` | Meaning | Examples |
|---|---|---|
| `live` | Takes effect immediately in the running process | `theme`, `transport_mode`, `repeat_interval` |
| `next-task` | Re-read from disk by `resolve_config`/work-order assembly on the next dispatch — no restart | `max_per_day`, `automerge`, `draft`, `forbidden_paths`, diff caps, `auto_resolve`, resolve overrides |
| `restart` | Baked at process launch | `host`, `port`, `cadences.*`, `shared_secret`, `dsn`, `tasks_repo`, `wip_ref_prefix`, `landing_mode`, `rendezvous_remote`, all worker identity/connection/UI fields |

> The `apply` value must be **verified against each field's actual read-site during implementation** — it is a claim about behavior, and a test (§8) asserts the classification for a representative set. `live` is just a declaration; the hot-apply path is built in Parts 2–3.

---

## 6. Env-shadow model

Because environment variables override the JSON files, a stored value can differ from the effective value. The model must expose, per field:

- `stored` — the value in the JSON file (or `.env` for secrets), i.e. what the editor writes.
- `effective` — the value after env overrides (what the process actually uses).
- `env_shadowed` — `True` when an env var currently overrides the stored value, plus the `env` var name (from metadata).

Part 1 provides the resolution primitives that make this computable (the loaders already layer env over file); Part 2 surfaces it in the API and Part 3 warns in the UI. Secret fields are a special case: `.env` *is* their store, so they are never reported as "shadowed."

---

## 7. Field inventory (the authoritative map)

Source of truth for Parts 2–3. `apply` is the initial classification (verify per read-site). Secrets marked → `.env`.

### 7.1 Manager → `.nightshift/manager.json`

| Field (JSON key) | Category | Type | Default | apply | Notes |
|---|---|---|---|---|---|
| `host` | Identity & connection | string | `0.0.0.0` | restart | env `NIGHTSHIFT_MANAGER_HOST` |
| `port` | Identity & connection | int | `8800` | restart | env `NIGHTSHIFT_MANAGER_PORT` |
| `shared_secret` | Identity & connection | string (secret) | `null` | restart | **→ `.env`** `NIGHTSHIFT_SHARED_SECRET` |
| `dsn` | Identity & connection | string (secret) | `null` | restart | **→ `.env`** `NIGHTSHIFT_PG_DSN` |
| `cadences.poll_seconds` | Cadences | float | `5.0` | restart | nested |
| `cadences.heartbeat_seconds` | Cadences | float | `10.0` | restart | nested |
| `cadences.lease_ttl_seconds` | Cadences | float | `120.0` | restart | nested |
| `cadences.worker_stale_seconds` | Cadences | float | `45.0` | restart | nested |
| `cadences.refresh_ms` | Cadences | int | `20000` | restart | nested |
| `max_per_day` | Scheduling | int | `200` | next-task | |
| `max_concurrent_queues` | Scheduling | int | `2` | next-task | |
| `max_nights_before_parking` | Scheduling | int | `2` | next-task | |
| `scheduled_models_allow` | Scheduling | string_list | (5 ids) | next-task | scheduling filter |
| `default_model` | Scheduling | string | `auto` | next-task | env `NIGHTSHIFT_DEFAULT_MODEL` |
| `model` | Scheduling | string | (file) | next-task | legacy compat path |
| `cursor_model` | Scheduling | string | (file) | next-task | legacy compat path |
| `landing_mode` | Landing & Git | enum(none/push/pr) | `none` | restart | env `NIGHTSHIFT_LANDING_MODE` |
| `rendezvous_remote` | Landing & Git | string | `origin` | restart | env `NIGHTSHIFT_RENDEZVOUS_REMOTE`; `null` disables |
| `wip_ref_prefix` | Landing & Git | string | `nightshift-wip` | restart | env `NIGHTSHIFT_WIP_REF_PREFIX`; validated by `normalize_wip_prefix` |
| `tasks_repo` | Landing & Git | string | `nightshift-tasks` | restart | env `NIGHTSHIFT_TASKS_REPO` |
| `automerge` | Landing & Git | bool | `false` | next-task | **standardized false** (see §9) |
| `draft` | Landing & Git | bool | `false` | next-task | |
| `forbidden_paths` | Worker execution policy | regex_list | (file) | next-task | baked into work order |
| `forbidden_template_paths` | Worker execution policy | regex_list | (file) | next-task | template/decomposition runs |
| `diff_cap_lines` | Worker execution policy | int | `1500` | next-task | |
| `diff_cap_exempt_paths` | Worker execution policy | regex_list | (file) | next-task | |
| `max_fix_attempts` | Worker execution policy | int | `6` | next-task | |
| `auto_resolve` | Conflict resolution | bool | `true` | next-task | manager owns main/PR + launches resolve job |
| `max_resolve_attempts` | Conflict resolution | int | `2` | next-task | |
| `resolve_model` | Conflict resolution | string | `null` | next-task | optional override |
| `resolve_backend` | Conflict resolution | string | `null` | next-task | optional override |

### 7.2 Worker → `.nightshift/worker.json`

| Field | Category | Type | Default | apply | Notes |
|---|---|---|---|---|---|
| `worker_id` | Identity & connection | string | `<host>-<pid>` | restart | env `NIGHTSHIFT_WORKER_ID` |
| `backend` | Identity & connection | enum(backends) | `claude-code` | restart | env `NIGHTSHIFT_WORKER_BACKEND`; **the** backend selector (§7.4) |
| `manager_url` | Identity & connection | string | `http://localhost:8800` | restart | env `NIGHTSHIFT_MANAGER_URL` |
| `shared_secret` | Identity & connection | string (secret) | `null` | restart | **→ `.env`** `NIGHTSHIFT_SHARED_SECRET` |
| `rendezvous_remote` | Identity & connection | string | `null` | restart | env `NIGHTSHIFT_RENDEZVOUS_REMOTE` |
| `queues` | Routing | string_list | any (`[]`) | restart | env `NIGHTSHIFT_WORKER_QUEUES` (CSV) |
| `priorities` | Routing | int_list | any (`[]`) | restart | env `NIGHTSHIFT_WORKER_PRIORITIES` (CSV) |
| `models` | Routing | string_list | `[]` | restart | env `NIGHTSHIFT_WORKER_MODELS` (CSV) |
| `mcps` | Routing | string_list | `[]` | restart | env `NIGHTSHIFT_WORKER_MCPS` (CSV) |
| `model_aliases` | Models | str_map | `{}` | restart | `{requested: actual}` |
| `auto_model` | Models | str_map | per-backend | restart | overrides `auto` per backend |
| `max_model` | Models | str_map | per-backend | restart | overrides `max` per backend |
| `ui_host` | UI & Network | string | `0.0.0.0` | restart | env `NIGHTSHIFT_WORKER_UI_HOST` |
| `ui_port` | UI & Network | int | `8810` | restart | env `NIGHTSHIFT_WORKER_UI_PORT` |

> `refresh_ms` is set by the manager at checkin (not operator-edited); mark `editable=False`. `workspace` is the launch input → `editable=False`.

### 7.3 Player → `.nightshift/player.json`

| Field | Category | Type | Default | apply |
|---|---|---|---|---|
| `theme` | Appearance | enum(light/dark) | `dark` | live |
| `transport_mode` | Transport | enum(oneshot/auto/repeat) | `auto` | live |
| `repeat_interval` | Transport | duration | `30m` | live |
| `port` | Server | int | `8799` | restart |

### 7.4 `worker_backend` resolution

`worker_backend` is dropped from the manager config (leftover from the combined-process era). The backend selector lives only as `WorkerConfig.backend` in `worker.json`. Any reader that consumed the manager-side `worker_backend` (e.g. `server/app.py`'s `/api/backends` "current" via `load_settings(...).get("worker_backend")`, and the GH-actions/local-runner compat path) is repointed to the worker surface. If a single-host/server mode still needs a "selected backend," it reads `worker.json`'s `backend`. Removing the key from `player.json`/`config.json` and repointing those readers is part of this work; verify there is exactly one source after the change.

---

## 8. Tests

1. **Defaults-drift guard** — assert each `OperatorConfig`/`ManagerSettings`/`WorkerConfig`/`PlayerConfig` default equals the value the **old** inline `cfg.get("key", DEFAULT)` call-sites used. Build the expected table from an audit of the current call sites (`spawn_daily.py`, `engine.py`, `scheduler.py`, `server/settings.py`). Where the old default was inconsistent (the `automerge` `True`/`false` split, §9), assert the **resolved** single value and document the chosen one. This test is what makes "single source of truth" real on day one.
2. **Metadata completeness** — `assert_complete(...)` runs at import; a unit test imports the package and asserts no `AssertionError`, and that every editable field has a unique `(surface, json-path)` and a known `apply`/`type`.
3. **Load/save round-trip per file** — write a model to each JSON file, reload, assert equality; assert nested `cadences` round-trips; assert env overrides layer correctly over file values.
4. **Secrets isolation** — saving a model never writes `shared_secret`/`dsn` into any `.json`; the `.env` writer upserts a single key while preserving other lines/comments; loading reads the secret back from env.
5. **Apply classification** — table-test a representative set of fields against their declared `apply`, with a comment pointing at the read-site that justifies it (guards against silent reclassification).
6. **No legacy paths remain** — grep-style test (or import test) asserting nothing reads `<workspace>/config.json` / `config.json.local` / `.nightshift/settings.json` anymore.

---

## 9. The `automerge` default decision

Today: `config.json` ships `automerge: false`; `docs/configuration-reference.md` documents `false`; but `resolve_frontmatter` falls back to `config.get("automerge", True)`. We standardize on **`false`** (matches shipped config + docs; conservative for landings). After this change the per-task fallback reads `OperatorConfig.automerge` (default `false`) instead of an inline `True`, so the operator default and the per-task fallback are one value. The drift test (§8.1) asserts this explicitly.

---

## 10. Repo templates & first-time setup

A fresh workspace has no `.nightshift/` yet, and the loaders read **only** the new files (no fallback). So the repo must ship samples and a one-command scaffold, or no one can set Nightshift up. This **replaces** today's "copy the repo-root `config.json` and hand-write `config.json.local`" story.

### 10.1 Shipped templates (package data)

Add `src/nightshift/assets/config/` (resolved via `asset("config", …)`, alongside `ui/`, `templates/`, `migrations/`), with `CONFIG_TEMPLATES_DIR = ASSETS_DIR / "config"` in `_paths.py`:

```
src/nightshift/assets/config/
├── manager.json   # sample manager config (flat + nested cadences; NO secrets)
├── worker.json    # sample worker config
└── player.json    # sample player/UI config
```

And a committed **`.env.example`** at the repo root for secrets + launch env.

**`manager.json` (sample):**
```json
{
  "host": "0.0.0.0",
  "port": 8800,
  "landing_mode": "none",
  "rendezvous_remote": "origin",
  "tasks_repo": "nightshift-tasks",
  "wip_ref_prefix": "nightshift-wip",
  "cadences": { "poll_seconds": 5.0, "heartbeat_seconds": 10.0, "lease_ttl_seconds": 120.0, "worker_stale_seconds": 45.0, "refresh_ms": 20000 },
  "default_model": "auto",
  "scheduled_models_allow": ["claude-sonnet-4-6", "claude-opus-4-8"],
  "max_per_day": 200,
  "max_concurrent_queues": 2,
  "max_nights_before_parking": 2,
  "automerge": false,
  "draft": false,
  "diff_cap_lines": 1500,
  "diff_cap_exempt_paths": ["^tests/fixtures/", "^docs/", "\\.md$"],
  "forbidden_paths": ["^\\.github/workflows/", "^CLAUDE\\.md$", "^AGENTS\\.md$"],
  "forbidden_template_paths": ["^tools/nightshift/templates/"],
  "max_fix_attempts": 6,
  "auto_resolve": true,
  "max_resolve_attempts": 2,
  "resolve_model": null,
  "resolve_backend": null
}
```

**`worker.json` (sample):**
```json
{
  "worker_id": "vm-1",
  "backend": "claude-code",
  "manager_url": "http://localhost:8800",
  "rendezvous_remote": null,
  "queues": [],
  "priorities": [],
  "models": ["claude-opus-4-8", "claude-sonnet-4-6"],
  "mcps": [],
  "model_aliases": {},
  "auto_model": {},
  "max_model": {},
  "ui_host": "0.0.0.0",
  "ui_port": 8810
}
```

**`player.json` (sample):**
```json
{ "theme": "dark", "transport_mode": "auto", "repeat_interval": "30m", "port": 8799 }
```

**`.env.example` (committed):**
```bash
# Workspace that parents your target repos + the nightshift-tasks content store.
NIGHTSHIFT_WORKSPACE=$HOME/workspaces
# Where workers reach the manager.
NIGHTSHIFT_MANAGER_URL=http://localhost:8800

# --- Secrets (never committed; copy to .env and fill in) ---
# NIGHTSHIFT_SHARED_SECRET=
# NIGHTSHIFT_PG_DSN=postgresql://nightshift@localhost:5432/nightshift
# Backend credentials (whichever backend you run):
# ANTHROPIC_API_KEY=
# GEMINI_API_KEY=
```

> The samples are illustrative; the *authoritative* defaults are the dataclass defaults (§7). The regex lists here are abbreviated — keep them short and point at the reference for the full set. JSON has no comments, so per-key guidance lives in the docs (§11) and the registry (Part 2).

### 10.2 `nightshift init` / `just init`

A scaffold command creates a workspace's config from the templates:

- `python -m nightshift init [--workspace <dir>]` (default workspace from `--workspace`/`NIGHTSHIFT_WORKSPACE`/cwd, §1):
  - creates `<workspace>/.nightshift/` if absent;
  - copies each `asset("config", "<name>.json")` to `<workspace>/.nightshift/<name>.json` **only if it doesn't already exist** (never clobbers operator edits);
  - creates `<workspace>/.env` from `.env.example` if absent;
  - prints a per-file `created` / `skipped (exists)` summary and the resolved workspace.
- `just init` wraps it, forwarding `--workspace "$NIGHTSHIFT_WORKSPACE"` like the other recipes.

This is **scaffolding for a fresh workspace**, distinct from the one-time developer migration in §2 (which deletes legacy files and has no recipe). `init` is idempotent and additive: safe to re-run, never destructive.

## 11. Documentation updates

Both docs currently describe the old files; update them in lockstep with the cutover.

### 11.1 `docs/setup-guide.md`

- **"The workspace"** — change "The operator `config.json` is read from `<workspace>/config.json`" → settings now live in `<workspace>/.nightshift/{manager,worker,player}.json`; secrets in `<workspace>/.env`. Replace the line "Put your operator `config.json` at that workspace root … the copy shipped in this repo is an example you can copy there and tune" with: run `just init` to scaffold `<workspace>/.nightshift/` from the shipped templates, then edit.
- **Quickstart step 1 (Configure the environment)** — keep launch/secret vars in `.env` (`NIGHTSHIFT_WORKSPACE`, `NIGHTSHIFT_MANAGER_URL`, `NIGHTSHIFT_PG_DSN`, `NIGHTSHIFT_SHARED_SECRET`, backend creds). Add a **step 0/1.5: `just init`** to create `.nightshift/{manager,worker,player}.json` + `.env`.
- **Quickstart step 4 (Start a worker)** — replace the `config.json.local` JSON block and the "`<workspace>/config.json.local`, worker-local and never committed" phrasing with `<workspace>/.nightshift/worker.json` (committed; secrets/per-box overrides still come from env). Update the example accordingly.
- **"Add a second worker" (same VM / remote)** — replace `config.json.local` references with `worker.json` (and keep the "env wins; give per-box overrides as `NIGHTSHIFT_*`" guidance).
- **Common operations table + the `just --list` aside** — add `just init` (scaffold a workspace's config).

### 11.2 `docs/configuration-reference.md`

- **"Where configuration lives" table** — replace rows:
  - `<workspace>/config.json` (Manager) → `<workspace>/.nightshift/manager.json` (Manager; committed; **no `manager` wrapper** — keys are top-level, `cadences` nested).
  - `<workspace>/config.json.local` (Worker, "No (gitignored)") → `<workspace>/.nightshift/worker.json` (Worker; **committed**).
  - add `<workspace>/.nightshift/player.json` (Operator UI prefs; committed).
  - `.env` row stays (now the **only** home for secrets: `NIGHTSHIFT_SHARED_SECRET`, `NIGHTSHIFT_PG_DSN`).
- **Precedence line** — "built-in default, then the `.nightshift/*.json` file, then `.env`, then the process environment."
- **"Manager configuration"** — drop the `{ "manager": { … } }` envelope example; show `manager.json` directly (flat + nested `cadences`). Note `shared_secret`/`dsn` are **not** in this file — they live in `.env`. Drop `worker_backend` from the manager key table (§7.4).
- **"Worker configuration"** — `config.json.local` → `worker.json`; `shared_secret` → `.env`; `backend` is the sole backend selector.
- **New subsection "Templates & first-time setup"** — document `src/nightshift/assets/config/` + `.env.example` + `just init` (idempotent, copy-if-absent).

> Other docs that mention `config.json` / `config.json.local` (`NIGHTSHIFT.md`, `nightshift.md`, the spec docs, the top-level `README.md`) are updated opportunistically; the two guides above are the required, in-scope edits.

## 12. Risks & non-goals

- **Risk: behavior change from repointing readers.** Removing inline defaults and routing through the dataclass must preserve current effective values exactly. Mitigation: the drift guard (§8.1) and a deliberate audit of every `cfg.get(...)` site. Per the agreed scope, this part **introduces the model and makes it authoritative for defaults/storage**; migrating every runtime consumer to read through the dataclass object (rather than the resolved dict) is allowed but bounded — where a consumer keeps reading the merged dict, the dict is produced *from* the dataclass so defaults still come from one place.
- **Risk: `.env` not loaded by the manager.** Mitigation: shared `load_dotenv` wired into every entrypoint (§4) + a test that a secret in `.env` resolves in a manager-style load.
- **Non-goal:** the registry generator, the API, and any UI — those are Parts 2 and 3. Part 1 ships with **no** user-visible change.
- **Non-goal:** per-queue/content-store config and `~/.nightshift/config.json` restructuring (§2).
