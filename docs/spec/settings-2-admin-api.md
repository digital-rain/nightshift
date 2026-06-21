# Nightshift — Settings Editor, Part 2: Registry & Admin API

**Subject:** Derive a typed **`FieldSpec` registry** by walking the config dataclasses from [Part 1](settings-1-config-model.md), and serve it over a **tiered `/api/settings`** that returns categories → fields with `stored`/`effective`/`env_shadowed`/`apply`, and accepts a **delta `PUT`** that validates only the changed fields, routes each to its file (or `.env`), hot-applies `live` fields, and reports which changes need a restart.

**Status:** Proposed — design for unimplemented work. Where this doc and the code disagree once implemented, the code governs and this doc should be updated.

**Series:** Part **2 of 3**. Depends on Part 1 (the config model + storage). Consumed by [Part 3](settings-3-multitier-ui.md) (the UI). This part adds **no new config knowledge** — it projects and serves the Part 1 model.

**Primary sources (to change):** new `src/nightshift/config/registry.py` (the generator + `FieldSpec` + JSON-schema emit) and `src/nightshift/config/validate.py` (delta validation + write routing), `src/nightshift/server/app.py` (replace the current `/api/settings` GET/PUT for single-host/server mode), `src/nightshift/manager/app.py` (replace its `/api/settings`), `src/nightshift/worker/ui_app.py` (add `/api/settings` GET/PUT over `worker.json`). Reuses `config/io.py` (file + `.env` writers) and the dataclasses from Part 1.

---

## 0. The one idea

Part 1 made every editable setting a dataclass field that knows its category, label, type, default, `apply`, `secret`, and owning file. Part 2 turns that into a **single derived registry** and a **uniform API** that the same UI code can drive whether it's talking to the manager, the worker, or single-host/server mode.

The registry is a **projection**, never a parallel source: it is computed by walking `dataclasses.fields()` and reading `.metadata["nightshift"]`. Add a field in Part 1 → it appears in the API automatically.

---

## 1. `FieldSpec` — the typed projection

```python
# config/registry.py
from dataclasses import dataclass
from typing import Any, Literal

FieldType = Literal[
    "string", "int", "float", "bool", "enum", "duration",
    "string_list", "int_list", "regex_list", "str_map",
]
ApplyMode = Literal["live", "next-task", "restart"]
Store = Literal["manager.json", "worker.json", "player.json", ".env"]

@dataclass(frozen=True)
class FieldSpec:
    key: str               # dotted path within the file, e.g. "cadences.poll_seconds"
    surface: Literal["manager", "worker", "player"]
    store: Store           # derived from surface; secret ⟹ ".env"
    category: str          # the sub-tier
    label: str
    desc: str
    type: FieldType        # from metadata override, else inferred from the field type
    apply: ApplyMode
    default: Any
    options: list[str] | None = None  # enums
    secret: bool = False
    env: str | None = None            # NIGHTSHIFT_* var, for env-shadow detection
```

Downstream code (this part + Part 3) is fully typed against `FieldSpec`; the stringly-keyed `dict` metadata exists only inside the dataclass field and is validated at import (Part 1 §3.1).

## 2. The generator

```python
def build_registry() -> list[FieldSpec]:
    """Walk the Part 1 dataclasses → ordered FieldSpec list.
    Skips fields without `nightshift` metadata or with editable=False."""
```

Rules:

- **Type:** use `meta["type"]` if present, else infer from the field's annotation (`bool`→`bool`, `int`→`int`, `float`→`float`, `str`→`string`/`enum` when `options` present, `tuple[str,…]`→`string_list`, `dict[str,str]`→`str_map`). `duration`/`regex_list`/`int_list` always come from the explicit `type` override.
- **Dotted keys:** nested dataclasses (e.g. `Cadences` inside the manager model) yield `cadences.<field>`; the writer maps the dotted key back into nested JSON.
- **Store:** `surface → file`; `secret=True` overrides to `.env`.
- **Order:** preserve declaration order within a surface; categories render in first-seen order (so the dataclass field order *is* the UI order — one more reason the model is the source of truth).

`emit_json_schema(specs) -> dict` produces a JSON-schema document for the frontend/tests (the one genuinely useful thing Pydantic-as-source-of-truth would have given us, kept without making Pydantic the source).

## 3. `GET /api/settings`

Returns the registry grouped into tiers/categories with live values. Uniform across all apps; the app decides which surfaces it exposes:

| App | Surfaces exposed |
|---|---|
| `manager/app.py` | `manager` + `player` |
| `worker/ui_app.py` | `worker` |
| `server/app.py` (single-host) | `manager` + `worker` + `player` (it runs both roles in one process) |

A surface is only returned if its file/role is owned by that process; the UI renders whatever tiers come back, so no client change is needed per app.

```jsonc
{
  "tiers": [
    {
      "surface": "manager",
      "categories": [
        {
          "name": "Cadences",
          "fields": [
            {
              "key": "cadences.poll_seconds",
              "label": "Worker poll interval",
              "desc": "Worker idle poll interval (sent to workers at checkin).",
              "type": "float",
              "apply": "restart",
              "store": "manager.json",
              "default": 5.0,
              "stored": 5.0,            // value in the file
              "effective": 5.0,         // value after env overrides
              "env": "—",
              "env_shadowed": false,
              "secret": false
            }
          ]
        }
      ]
    }
  ],
  "schema": { /* emit_json_schema(...) */ }
}
```

- **`stored` vs `effective` vs `env_shadowed`:** computed via Part 1 §6. `stored` is what the editor edits; `effective` is what the process uses; `env_shadowed` flags when an env var currently wins (UI warns).
- **Secrets:** never return the value. Return `"stored": null` plus `"is_set": true|false` and `"secret": true`, so the UI shows "set / not set" with a write-only field. (`.env` is the store; secrets are never "shadowed.")

## 4. `PUT /api/settings` — delta only

The body is a **partial, surface-keyed envelope** carrying only changed fields (the canonical shape; used uniformly by every app):

```jsonc
{ "manager": { "cadences.poll_seconds": 7.5, "automerge": true },
  "player":  { "theme": "light" } }
```

Only the surfaces an app owns are accepted; an unknown surface or dotted key → `400`. (A single flat `{dotted_key: value}` map is **not** accepted — the surface envelope removes any ambiguity when the same key name could exist on two surfaces, e.g. `shared_secret`.)

Pipeline:

1. **Resolve specs** for the incoming keys; unknown key → `400`.
2. **Validate the delta** against each field's `FieldSpec`:
   - Build a **transient** validator from the registry — either per-spec validators (type + constraints: enum membership, int/float range, `duration` via `parse_duration`, `regex_list` compiles each pattern, `str_map` is `{str:str}`, `wip_ref_prefix` via `normalize_wip_prefix`) **or** an ad-hoc Pydantic model constructed on the fly from the specs being written. Do **not** keep a standing Pydantic mirror of the whole config.
   - Collect all errors; any error → `400` with a per-field message and **write nothing** (atomic per request).
3. **Route writes by `store`:** group by file; for each JSON file, load → set nested dotted keys → write once (preserve sibling keys + ordering). Secret keys go to the `.env` writer (one upsert per key). A request that touches `manager.json` + `.env` writes both only after all validation passes.
4. **Apply:**
   - `live` fields hot-apply in-process (e.g. `theme`/`transport_mode`/`repeat_interval` update the player runtime; emit the existing `settings_changed` SSE).
   - `next-task`/`restart` fields are persisted; no live mutation.
5. **Respond** with the re-read `GET` payload plus:
   ```jsonc
   { "ok": true, "restart_required": ["manager.cadences.poll_seconds"], "applied_live": ["player.theme"] }
   ```
   so the UI can show a "restart to apply" banner for the listed keys.

> **Atomicity note:** validate-all-then-write keeps a bad field from partially applying. Cross-file writes (JSON + `.env`) are sequenced after validation; a write failure mid-sequence is surfaced as `500` with the list of files already written (best-effort, logged) — acceptable since nothing is concurrently running and the editor re-reads state on response.

## 5. Worker API addition

`worker/ui_app.py` is read-only today. Add:

- `GET /api/settings` → registry for the `worker` surface, values from `worker.json` (+ env-shadow + `.env` secret "is_set").
- `PUT /api/settings` → same delta pipeline, writing `worker.json` / `.env`.

All worker fields are `apply: restart` (the worker reads config at launch), so the PUT response always returns them under `restart_required`; the UI shows "restart this worker to apply." No hot-apply path on the worker.

## 6. Tests

1. **Registry is a faithful projection** — every editable Part 1 field appears exactly once with correct `store`/`type`/`apply`; declaration order preserved; `assert_complete` covered.
2. **GET shape** — tiers/categories/fields; `stored`/`effective`/`env_shadowed` correct under an injected env override; secrets return `is_set` and never the value.
3. **PUT validation** — bad enum/range/duration/regex/map each → `400`, nothing written; valid delta writes only the touched files; sibling keys preserved; nested `cadences.*` writes to the right place.
4. **Secret routing** — PUT of a secret writes `.env` only, never a `.json`; GET reflects `is_set`.
5. **Apply reporting** — `live` field reported in `applied_live` and observable in-process; `restart` field reported in `restart_required` and not hot-applied.
6. **JSON-schema** — `emit_json_schema` validates a known-good payload and rejects a known-bad one (frontend contract test).
7. **Per-surface scoping** — worker endpoint exposes only worker fields; manager endpoint exposes manager + player; no cross-surface leakage.

## 7. Non-goals

- No UI (Part 3).
- No new settings; the API is a pure projection of Part 1.
- No live-apply for `restart`/worker fields — the API only *reports* restart-required; actually hot-reloading the manager/worker process is explicitly out of scope.
