# Task Documents — Implementation Plan

> **For agentic workers:** Execute one phase per session, in order. Each phase is self-contained: its **Read first** block lists everything you need in context; do not read beyond it. Each phase ends with its tests passing, `just validate` green, and one commit. Steps use checkbox (`- [ ]`) syntax for tracking. Implement exactly what this plan says — nothing more (no LFS, no vision routing, no content-addressed doc store, no embedding bytes on the wire).

**Goal:** Implement `docs/spec/2026-07-18-task-documents.md` — operator-declared reference documents (`docs:` paths + `attachments:` escape hatch), pinned by git blob sha at first dispatch, delivered by-reference (never embedded), byte-native (text + images/PDF from day one), materialized into run-scratch and named in the prompt header.

**Architecture:** Reuse the workflow-artifact rail (`materialize_*` → prompt header → tasks-repo executor lane) but diverge in one dimension: docs ride the wire as **pin-only / `blob_ref` entries** (sha + media + path), never as content. Path docs have no custody (bytes stay in the target repo); attachments live under `<task>.docs/` as opaque bytes. `docs_pin` is engine-owned frontmatter, snapshot-on-first-dispatch.

**Authority:** The spec (`docs/spec/2026-07-18-task-documents.md`) governs semantics. This plan governs sequencing, file boundaries, and interfaces. Where they disagree, the spec wins — and update whichever was wrong.

## Global constraints

- Python 3.12+, **no new dependencies** (stdlib `json` / `mimetypes` / magic-byte sniffing only — do not add PyYAML).
- Tests: `uv run pytest tests/<file> -x -q` per phase; `just validate` before each phase's final commit.
- **Binary from phase 1.** There is no text-first intermediate. Every materialize/attach/pin path accepts opaque bytes and real extensions.
- **Default is a no-op.** A task with neither `docs:` nor `attachments:` must produce a work order **byte-identical** to today, every phase. Guard this with a regression test in Phase 2; keep it green thereafter.
- **Nothing on the wire.** Work-order `config.docs` entries carry `sha` / `blob_ref` / `media` / `path` / `range` — never `text`, never base64, never raw bytes.
- Frontmatter today is **line-based, not YAML** (`spawn_daily.split_frontmatter`). Nested maps and YAML lists do not parse. This plan prescribes a **stdlib-only** parse/render extension (below) — do not invent a third frontmatter format.
- Imports at top of module, never inline. Exhaustive `match` with `assert_never` on new enums. Commit messages: `task: task-documents phase N — <summary>`.
- Do **not** touch `manager/scheduler.py` capability matching (spec §8: image-docs are deliberately not routed to vision workers).

## Frontmatter encoding contract (all phases)

`split_frontmatter` stays the line scanner. New keys use these shapes so the existing rewriter loops keep working:

| Key | Operator / engine | On-disk form | Parse helper |
|---|---|---|---|
| `docs` | operator | (a) single path string; (b) comma-separated paths; (c) JSON array of strings / objects — one line after `docs:`; (d) YAML-ish block list: `docs:` then indented `- path` / `- path: …` lines until the next top-level key | `parse_docs_field(raw) -> list[DocSpec]` |
| `attachments` | operator | Same shapes as `docs`, but entries name files under `<task>.docs/` (no repo path semantics) | `parse_attachments_field(raw) -> list[DocSpec]` |
| `docs_pin` | engine | **Single-line JSON object** after `docs_pin:` (stdlib `json.dumps`/`loads`). Keys are path strings or `attach:<name>`. Values are `{"sha","media","bytes"}`. | `parse_docs_pin(raw) -> dict[str, PinRecord]` |

`DocSpec` fields after normalize: `path: str`, `range: str | None`, `as: str | None`, `steps: list[str] | None`. Bare string → `{path: s}`.

`_render_meta_value` gains: `dict`/`list` → compact JSON (`json.dumps(value, separators=(",", ":"), sort_keys=True)`). Scalars unchanged. `set_engine_meta` / `set_task_meta` keep rewriting **one line per key**.

Block-list parse (shape d): when the value after `docs:` / `attachments:` is empty, consume subsequent fence lines that match `^\s+-\s+` until a non-indented, non-empty line. Implement this inside the parse helpers by accepting the full fence text + key, **or** by upgrading `split_frontmatter` to return raw fence lines for known list keys. Prefer a focused helper `parse_listish_frontmatter_value(fence_lines, key) -> object` used by callers that need `docs`/`attachments`; do not break existing scalar parse for other keys.

## Phase map and interface ledger

Sequential; each phase consumes only the **Produces** lines of earlier phases (restated in its own header).

| Phase | Delivers | Session context risk |
|---|---|---|
| 1 | Parse/normalize + settings + attachment CRUD + `materialize_docs` + failure kinds + `docs_pin` render | Medium — frontmatter encoding + binary I/O |
| 2 | `build_work_order` pin-only resolution + prompt header + worker/resolve wiring + evergreen/split lifecycle | **High** — dispatch + materialize integrity |
| 3 | Operator attach/preview/repin APIs + create/detail UI + user docs | Medium — first real file upload in `app.js` |

---

### Phase 1 — foundations: parse, settings, custody, materialize

**Read first:**
- Spec §1 (frontmatter contract), §2 (storage & custody), §5.1–5.3 (cap / media allow-list / budget — settings only; enforcement wiring is Phase 2 for path docs), §8 touch points for `config/`, `task_files.py`, `lifecycle.py`, `api_worker.py` constant site.
- This plan's **Frontmatter encoding contract** above.
- `src/nightshift/spawn_daily.py:155-175` (`split_frontmatter`).
- `src/nightshift/task_files.py:301-470` (`ENGINE_META_KEYS`, `artifacts_dir` / `write_artifact` / `delete_artifacts` / `set_engine_meta` / `materialize_artifacts`), `:612-633` (`_EDITABLE_META_KEYS`, `_render_meta_value`), `:481-507` (`drop_completed_task` / `delete_task` fold pattern).
- `src/nightshift/config/manager.py:145-171` (`diff_cap_lines` + `quarantine_threshold` as the settings-registry templates — category, env, load/save).
- `src/nightshift/manager/api_worker.py:152` (`_DOCUMENT_MAX_BYTES = 256 * 1024` — site the ceiling constant beside it; do **not** change the artifact-submit check that uses `_DOCUMENT_MAX_BYTES`).
- `src/nightshift/lifecycle.py:136-154` (`FailureKind`), `:359-396` (`RetryPolicy.on_failure` / `ENVIRONMENT_FAILURE_KINDS`).
- `src/nightshift/repo_tasks.py` — search `git cat-file blob` for the existing blob-read subprocess pattern to mirror.
- Nothing else. Do **not** edit `work_orders.py`, `prompts.py`, `execute.py`, or `app.js` in this phase.

**Files:**
- Create: `src/nightshift/docs_resolve.py` (pure helpers: parse, normalize, sniff, path guards, cap/allow-list predicates — no git, no HTTP).
- Modify: `src/nightshift/task_files.py`
- Modify: `src/nightshift/config/manager.py` (+ `src/nightshift/assets/config/manager.json` default keys)
- Modify: `src/nightshift/manager/api_worker.py` (constant only)
- Modify: `src/nightshift/lifecycle.py`
- Test: `tests/test_task_documents.py` (new); extend `tests/test_config_model.py` / `tests/test_task_queue_files.py` only if an existing assertion breaks on new editable keys.

**Produces (Phase 2+ consume exactly these):**

```python
# docs_resolve.py
from dataclasses import dataclass

DOCUMENT_CAP_CEILING_BYTES: int  # re-exported or imported from api_worker / a tiny shared const module
DEFAULT_DOCUMENT_CAP_BYTES = 256 * 1024
DEFAULT_DOCUMENT_BUDGET_BYTES = 4 * 1024 * 1024
DEFAULT_ALLOWED_DOC_MEDIA_TYPES = (
    "text/*", "application/json", "application/yaml",
    "image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf",
)

@dataclass(frozen=True)
class DocSpec:
    path: str
    range: str | None = None      # "A-B" text only
    as_: str | None = None        # field name `as` via metadata / property if needed
    steps: tuple[str, ...] | None = None

@dataclass(frozen=True)
class PinRecord:
    sha: str
    media: str
    bytes: int

def parse_docs_field(raw: object, *, fence_lines: list[str] | None = None) -> list[DocSpec]: ...
def parse_attachments_field(raw: object, *, fence_lines: list[str] | None = None) -> list[DocSpec]: ...
def parse_docs_pin(raw: object) -> dict[str, PinRecord]: ...
def render_docs_pin(pin: dict[str, PinRecord]) -> str: ...  # compact JSON

def normalize_repo_path(path: str) -> str:
    """Reject absolute, `..`, and escaping paths. Raise ValueError with the
    blocked message shape from spec §7."""

def sniff_media_type(name: str, head: bytes) -> str:
    """Extension + magic-byte classification. Raise ValueError on magic mismatch."""

def media_is_binary(media: str) -> bool: ...
def media_allowed(media: str, allowed: Sequence[str]) -> bool: ...
def effective_document_cap(document_cap_bytes: int) -> int:
    return min(document_cap_bytes, DOCUMENT_CAP_CEILING_BYTES)

def extension_for_media(media: str, fallback_name: str) -> str:
    """Real extension for scratch filenames (png/jpeg/gif/webp/pdf/md/…)."""

class DocumentError(ValueError):
    """Authoring/resolution error. `.code` in {
       'document_too_large', 'unsupported_document_type',
       'range_on_binary', 'path_outside_repo', 'not_found', 'missing_attachment'
    }; `.message` is the operator-facing blocked reason string from spec §7."""
    code: str
    message: str
```

```python
# task_files.py additions
ENGINE_META_KEYS |= {"docs_pin"}          # nested via JSON line; NOT in _EDITABLE_META_KEYS
_EDITABLE_META_KEYS |= {"docs", "attachments"}

def attachments_dir(tasks_root, task, tasks_rel="main") -> Path:
    # <tasks_root>/<tasks_rel>/<task>.docs/

def write_attachment(tasks_root, task, name, data: bytes, tasks_rel="main") -> Path:
    # opaque bytes, real extension preserved in `name`; one commit per write
    # (mirror write_artifact commit pattern; caller jobs must NOT double-commit)

def read_attachment(tasks_root, task, name, tasks_rel="main") -> bytes: ...
def delete_attachment(tasks_root, task, name, tasks_rel="main", *, commit=True) -> bool: ...
def list_attachments(tasks_root, task, tasks_rel="main") -> list[str]: ...

def materialize_docs(
    workspace: Path,
    repo: str,
    task: str,
    docs: Sequence[dict],   # work-order entries (Phase 2 shape); Phase 1 tests pass synthetic entries
    *,
    queue: str | None = None,
    tasks_root: Path | None = None,
    target_repo_root: Path | None = None,
) -> dict[str, Path]:
    """Byte-native sibling of materialize_artifacts.
    - kind/source path+target: git cat-file blob <sha> in target_repo_root (or workspace/repo)
    - kind/source attach+tasks: git cat-file blob <sha> OR read blob_ref under tasks_root
    - verify hashlib.sha1(b"blob {len}\\0" + data).hexdigest() == entry['sha']
      (git blob object id) OR verify raw content digest the pin recorded —
      pick ONE scheme, document it in the docstring, use it consistently with
      whatever Phase 2 writes into docs_pin (must be `git rev-parse` blob sha).
    - mismatch → raise DocumentUnavailable (environment; Phase 2 maps to FailureKind)
    - apply range (text only) after read; chmod 0o444; filename
      task-local-<queue>-<task>.doc-<stem><ext> from media/name
    Returns {entry_name: scratch_path}.
    """

# delete_task / drop_completed_task: also _remove_attachments_dir (fold, commit=False)
# evergreen: do NOT delete attachments_dir from any reset helper yet — Phase 2 wires reset
```

```python
# config/manager.py — OperatorConfig fields (category "Worker execution policy")
document_cap_bytes: int = 256 * 1024
    # meta(..., env="NIGHTSHIFT_DOCUMENT_CAP_BYTES")
allowed_doc_media_types: tuple[str, ...] = DEFAULT_ALLOWED_DOC_MEDIA_TYPES
document_budget_bytes: int = 4 * 1024 * 1024
# load/save/template + queue-merge via existing resolve_config _deep_merge (no extra plumbing
# if keys live in manager.json / queue config.json like diff_cap_lines)
```

```python
# api_worker.py
DOCUMENT_CAP_CEILING_BYTES = 5 * 1024 * 1024  # beside _DOCUMENT_MAX_BYTES; do not reuse the latter
# Prefer defining the constant once in docs_resolve.py and importing it here for the
# "sited beside _DOCUMENT_MAX_BYTES" comment/spec note, OR define here and import into
# docs_resolve — one source of truth only.
```

```python
# lifecycle.py
class FailureKind(StrEnum):
    ...
    DOCUMENT_UNAVAILABLE = "document_unavailable"  # environment → RETRY_ELSEWHERE
# Add DOCUMENT_UNAVAILABLE to the RETRY_ELSEWHERE match arm alongside WORKTREE_FAILED.
# Authoring failures (too large / unsupported / missing) are NOT new FailureKinds —
# they become TaskHoldKind.BLOCKED with a reason string at dispatch (Phase 2),
# matching workflow_error handling.
```

**Steps:**
- [ ] Write `tests/test_task_documents.py` covering, with failing tests first:
  - `docs` normalize: bare string; list of strings; object with `path`/`range`/`as`/`steps`; JSON-line form; block-list form.
  - `attachments` normalize (same shapes).
  - Traversal reject: `../`, absolute, symlink-escape style paths → `DocumentError(code=path_outside_repo)`.
  - `range` retained on text; `media_is_binary` + a helper `validate_range_for_media` rejects range on `image/png`.
  - `sniff_media_type`: `.md` → text; PNG magic → `image/png`; `.png` with non-PNG bytes → error; PDF magic.
  - `effective_document_cap` clamps above ceiling.
  - `allowed_doc_media_types` glob match (`text/*` admits `text/markdown`).
  - `write_attachment` / `read_attachment` / `delete_attachment` round-trip bytes (text + small PNG fixture); overwrite same name; commit creates a tasks-repo revision.
  - `delete_task` removes `<task>.docs/` alongside `<task>.artifacts/`.
  - `materialize_docs` for a synthetic path entry: given a temp git repo with a known blob sha, scratch file is `0o444`, real `.png` extension, sha-verify passes; tampered bytes raise unavailable.
  - `materialize_docs` text `range: "1-2"` slices lines; header-facing name preserved in return map.
  - `docs_pin` JSON render/parse round-trip via `set_engine_meta` (write pin, re-read frontmatter, `parse_docs_pin`).
  - `_EDITABLE_META_KEYS` accepts `docs`/`attachments`; `set_task_meta` rejects `docs_pin`; `set_engine_meta` accepts `docs_pin`.
  - Settings: `OperatorConfig().document_cap_bytes == 256*1024`; env override `NIGHTSHIFT_DOCUMENT_CAP_BYTES` loads (mirror quarantine_threshold test pattern).
- [ ] Run `uv run pytest tests/test_task_documents.py -x -q` — expect failures.
- [ ] Implement `docs_resolve.py`, `task_files` attachment family + `materialize_docs`, settings, ceiling constant, `FailureKind.DOCUMENT_UNAVAILABLE`.
- [ ] Tests pass; `just validate`; commit `task: task-documents phase 1 — parse, settings, attachment custody, materialize_docs`.

**Phase 1 done when:** attachment bytes round-trip under `<task>.docs/`; `materialize_docs` can produce a read-only scratch file from a git blob sha with integrity check; frontmatter keys parse; settings load; no work-order or UI changes yet.

---

### Phase 2 — dispatch pin, prompt header, worker wiring, lifecycle

**Read first:**
- Spec §3 (materialization & work order), §4 (blob-sha pin), §5 (enforcement at resolve/attach — path side here), §7 edge table (missing / over-cap / range-on-binary / workflow steps / decomposition / text-only backend), §8 rows for `work_orders.py`, `prompts.py`, `worker/execute.py`, `resolve_runner.py`, `transitions`/reset, `harvest_split_output`.
- Phase 1 **Produces**.
- `src/nightshift/manager/work_orders.py` (entire file — `build_work_order`).
- `src/nightshift/manager/api_worker.py:289-298` (`_workflow_reset_job`), `:318-390` (`_lease_and_build` + `build_work_order` call), `:585-594` (workflow_error → `TaskHoldKind.BLOCKED` pattern to mirror).
- `src/nightshift/prompts.py:18-116` (`_artifact_header`, `build_prompt`, `build_doc_prompt`).
- `src/nightshift/assets/prompts/nightshift-local.md` (Plan-files paragraph — mirror for reference docs); skim `assets/prompts/workflow-plan.md` header expectations only if present.
- `src/nightshift/worker/execute.py:168-180` and `:410-430` (brief/artifact materialize sites for doc and code paths).
- `src/nightshift/resolve_runner.py` — search `materialize_brief` (docs must materialize here too per spec).
- `src/nightshift/task_files.py:825-888` (`harvest_split_output`).
- Nothing in `app.js` / operator attach routes yet.

**Files:**
- Modify: `src/nightshift/docs_resolve.py` (add git-backed resolution helpers, or keep pure and put git calls in `work_orders.py` — prefer resolution orchestration in `work_orders.py` / a `resolve_task_docs(...)` next to it, sniff/cap checks calling Phase 1 predicates).
- Modify: `src/nightshift/manager/work_orders.py`
- Modify: `src/nightshift/manager/api_worker.py` (`_lease_and_build` block-on-docs-error; `_workflow_reset_job` clears `docs_pin`)
- Modify: `src/nightshift/prompts.py` + charter assets (`nightshift-local.md`; workflow doc/code charters that already say "read the manifest files first")
- Modify: `src/nightshift/worker/execute.py`
- Modify: `src/nightshift/resolve_runner.py`
- Modify: `src/nightshift/task_files.py` (`harvest_split_output` inheritance)
- Test: extend `tests/test_task_documents.py`; add/extend `tests/test_workflows_manager.py` or `tests/test_prompts.py` for header + no-docs regression.

**Produces:**

```python
# Resolution result (work_orders.py or docs_resolve.py)
@dataclass(frozen=True)
class DocsResolveResult:
    entries: tuple[dict, ...]          # pin-only work-order entries
    pin: dict[str, PinRecord]          # full docs_pin map to persist
    pin_dirty: bool                    # True when first pin or refresh wrote new shas
    blocked_reason: str | None         # if set, entries/pin ignored; caller holds task

def resolve_task_docs(
    *,
    workspace: Path,
    tasks_root: Path,
    task: str,
    queue: str | None,
    repo: str,
    base_ref: str,
    meta: dict,
    merged_config: dict,               # resolve_config output (caps / allow-list / budget)
    workflow_step_id: str | None,      # filter entries with steps: […]
) -> DocsResolveResult: ...
```

Work-order entry shape (exact keys; **no content**):

```json
{
  "name": "auth.md",
  "label": "the auth spec",
  "kind": "path",
  "source": "target",
  "media": "text/markdown",
  "path": "docs/spec/auth.md",
  "ref": "base_ref",
  "sha": "<git blob sha>",
  "range": "1-120",
  "bytes": 18240
}
```

Attachment entry: `kind: "attach"`, `source: "tasks"`, `blob_ref: "<task>.docs/<name>"`, `sha`, `media`, `bytes` — no `text`.

`build_work_order` changes:
1. After building `config_blob` (and workflow block if any), call `resolve_task_docs(...)`.
2. If `blocked_reason`: still return the order **or** raise a dedicated `DocsBlocked(reason)` — pick one and handle it in `_lease_and_build` so the task is `TaskHoldKind.BLOCKED` with that reason and **no lease** is handed out. Prefer raising `DocsBlocked` so the order never carries a partial docs list.
3. If ok and `entries` non-empty: `config_blob["docs"] = list(entries)`.
4. If `pin_dirty`: persist via tasks-repo executor `set_engine_meta(..., {"docs_pin": render_docs_pin(pin)})` before returning the order (same serialized lane as other engine meta). If pin already matches, do not rewrite.
5. If neither `docs` nor `attachments` in meta: skip the pass entirely — `config` keys identical to today.

Resolution rules (implement exactly):
- Path: `git rev-parse <base_ref>:<path>` → sha; `git cat-file -s <sha>` → bytes; sniff via `git cat-file blob <sha>` head bytes; enforce allow-list, per-doc cap (binary message omits range hint; text message includes it), aggregate budget; `range` illegal on binary.
- Attach: resolve blob sha for `<task>.docs/<name>` in tasks repo; same sniff/cap/allow-list/budget; missing file → blocked.
- Reuse existing `docs_pin` shas when present (do not re-resolve path at live HEAD); still filter by `steps` and re-emit entries from the pin + frontmatter.
- Unreachable pin fallback (spec §4): retry resolve at current `base_ref`; on success re-pin (`pin_dirty=True`); on failure blocked `referenced doc '<path>' not found at base_ref`.
- Workflow `steps:` filter: if `workflow_step_id` set and entry has `steps`, keep only when step id ∈ steps; no `steps` → all steps.

Prompt header (`_docs_header` or extend `_artifact_header` carefully — **keep artifact wording unchanged**):

```
Reference documents (read-only, read these before exploring):
  - The auth spec (lines 1–120) is: <scratch>/….doc-auth.md
  - The login mockup is: <scratch>/….doc-login-mockup.png  [image/png — open with an image-capable tool]
```

- Source/kind invisible. Binary lines get `[<media> — open with an image-capable tool]` (or `… PDF-capable tool` for `application/pdf`).
- Charter paragraph (conditional in spirit; always present as one short paragraph is OK if header absence makes it inert): *"When reference documents are listed, read them first; treat them as authoritative context before exploring the repo. A document annotated with an image/PDF media type is binary — open it with a tool that can read that type; if you cannot, note that in your output and proceed with the remaining context."*
- `build_prompt` / `build_doc_prompt` accept `doc_files: dict[str, tuple[str, str]] | None` mapping label → `(path, media)` (or a small dataclass). Append `_docs_header` after the artifact header.

Worker (`execute.py`): after brief/artifact materialization on **doc, code, and split** paths, if `order["config"].get("docs")`: call `materialize_docs(...)`; pass labels/media into the prompt builder. On `document_unavailable`, return `Outcome` with `FailureKind.DOCUMENT_UNAVAILABLE` (RETRY_ELSEWHERE).

`resolve_runner.py`: materialize the same `config.docs` pins (identical context).

Lifecycle:
- `_workflow_reset_job`: also `set_engine_meta(..., {"docs_pin": None})` (or clear key). Do **not** delete `<task>.docs/`. Do **not** clear `docs`/`attachments` frontmatter.
- `harvest_split_output`: for each child brief written, copy parent `docs:` / `attachments:` into child frontmatter (via `set_task_meta` or fence rewrite); copy `<parent>.docs/` bytes into `<child>.docs/` when present; do **not** copy `docs_pin` (children re-pin at their own first dispatch).

**Steps:**
- [ ] Failing tests first in `tests/test_task_documents.py` (and prompt/work-order tests as needed):
  - Path doc at `base_ref` → work order entry has `sha`, **no** `text`/bytes payload keys carrying content; `docs_pin` written on first build.
  - Second build reuses pin (same sha) even if file at HEAD changed (mutate file + commit on branch after pin).
  - Missing path → `DocsBlocked` / blocked reason `referenced doc '…' not found at base_ref`.
  - Over-cap image → blocked, message without range hint; over-cap text → message with range hint.
  - Unsupported media + magic mismatch → blocked.
  - Aggregate budget exceeded → blocked naming total + budget.
  - `range` on png → blocked.
  - Workflow `steps: [plan]` excluded from implement step's entries.
  - Attachment missing from `<task>.docs/` → blocked.
  - `materialize_docs` + `_docs_header` integration: binary annotation present; ranged text notes lines.
  - Sha mismatch at materialize → `DOCUMENT_UNAVAILABLE`.
  - Evergreen reset clears `docs_pin`, retains attachments dir + operator fields.
  - `harvest_split_output` children inherit `docs` + attachment bytes; no `docs_pin` on children.
  - **Regression:** task with no docs/attachments → `build_work_order` `config` deep-equals pre-feature fixture (or key set equal and no `docs` key).
- [ ] Implement resolution, lease blocking, pin persistence, prompts, execute/resolve wiring, reset, harvest.
- [ ] Tests pass; `just validate`; commit `task: task-documents phase 2 — pin-only work orders, header, worker materialize, lifecycle`.

**Phase 2 done when:** a task with `docs: [path-to-md, path-to-png]` pins both at dispatch, worker materializes both read-only from `git cat-file blob`, header annotates the image, workflow steps see byte-identical pins, evergreen re-pins next cycle, and no-docs tasks are unchanged.

---

### Phase 3 — operator API, UI, user docs

**Read first:**
- Spec §6 (UI), §8 rows for `api_operator.py`, `assets/ui/app.js`, `docs/user/configuration-reference.md`.
- Phase 1–2 **Produces** (attachment CRUD, resolve caps, `docs_pin`, preview needs sha).
- `src/nightshift/manager/api_operator.py` — `TaskCreate`/`TaskUpdate` models (~editable keys comment), existing `GET /api/tasks/{task}/artifacts` (~L382), task create/PATCH handlers.
- `src/nightshift/assets/ui/app.js:841-880` (`artifactsPanel` — pattern for a Documents panel), `:4200-4380` (`taskDetailContent`), `:4574-4685` (`openCreateTask` / `renderCreateScreen` / `renderCreateContent`).
- `docs/user/configuration-reference.md` — frontmatter + settings table style.
- Do not re-open worker execute paths unless a test forces a tiny fix.

**Files:**
- Modify: `src/nightshift/manager/api_operator.py`
- Modify: `src/nightshift/assets/ui/app.js` (+ `style.css` only if required for thumbnails/meter — keep minimal)
- Modify: `docs/user/configuration-reference.md`
- Test: extend `tests/test_task_documents.py` (API: attach guards, paths listing, blob fetch, repin) and/or `tests/test_nightshift_manager.py` for HTTP status/error codes. No browser test harness required — keep UI changes thin and mirror existing DOM helpers.

**Produces (HTTP contract):**

```
POST   /api/tasks/{task}/attachments          multipart file; queue query param as elsewhere
PUT    /api/tasks/{task}/attachments/{name}   replace
DELETE /api/tasks/{task}/attachments/{name}   deletes file + removes from attachments: frontmatter
GET    /api/tasks/{task}/docs/{name}          attachment bytes (preview); Content-Type from sniff
GET    /api/repos/{repo}/paths?prefix=&base_ref=   → [{path, media}, …] via git ls-tree; filter allow-list
GET    /api/repos/{repo}/blob?sha=            raw bytes for thumbnail (cap read to effective cap)
POST   /api/tasks/{task}/docs/repin           body: {"paths": ["…"] | null}  null = all path docs
GET    /api/tasks/{task}/documents            optional convenience: lists docs + attachments + docs_pin
                                              + drift: per path doc, pin.sha != rev-parse(base_ref:path)
```

API guards (attach):
- Effective cap → `400` with `{"error": "document_too_large", "detail": "…"}`.
- Allow-list / magic mismatch → `400` `unsupported_document_type`.
- Budget across existing attachments + new bytes → `400` with budget detail.
- Writes go through the tasks-repo executor lane (same pool as artifact writes). Prefer calling `write_attachment` **without** a second `commit_tasks` in the job (Phase 1 `write_attachment` already commits — do not repeat the artifact double-commit footgun).
- On successful attach: ensure `attachments:` frontmatter lists the name (`set_task_meta`).
- On delete: remove file + frontmatter entry; drop matching `attach:<name>` from `docs_pin` if present.

UI (create panel):
- Primary **"Reference documents"** control: text input + add; call `GET /api/repos/{repo}/paths?prefix=` for autocomplete (repo from draft). Chips for each path. Image chips: thumbnail via `/api/repos/{repo}/blob?sha=` after resolving sha (paths endpoint may return sha at `base_ref`, or a second cheap rev-parse endpoint — if paths returns only path/media, add `sha` to each paths row to avoid chatty blob lookups).
- `range` input only on chips whose media is text.
- Secondary **"Attach a file"** button + drag-drop + paste (`clipboardData.files`); caption exactly: `Only for content that isn't in any repo.` Use `<input type="file">` + `FormData`. Client-side `FileReader` thumbnail for images. Reject over-cap client-side using settings from the existing settings payload if present; still trust server errors.
- Small meter against `document_budget_bytes` (sum of known attachment sizes + path sizes when known).

UI (detail pane):
- **Documents** section (sibling to Artifacts): path rows with link/branch icon + `path @ <short-sha>`; attachment rows with paperclip icon; media icon by type.
- Image: thumbnail → click lightbox (`<dialog>` or existing modal pattern if any). Text: read-only fetch + markdown/plain render (range slice is already in pin materialization — for detail view, show file with note if ranged). PDF: open in new tab / download link.
- Amber **"source drifted — pinned to older version"** when drift API says so; button **"Re-pin to current base_ref"** → `POST …/docs/repin`.
- Remove path → PATCH frontmatter `docs`; remove attachment → `DELETE …/attachments/{name}`.
- `docs_pin` rendered read-only (collapsed JSON or short sha list).

Docs:
- `docs/user/configuration-reference.md`: rows for `docs`, `attachments`, `docs_pin`; settings `document_cap_bytes` (+ env + ceiling note), `allowed_doc_media_types`, `document_budget_bytes`; one short paragraph on by-reference delivery + pin-on-first-dispatch.

**Steps:**
- [ ] Failing API tests: attach png under cap; reject over-cap; reject `application/zip`; replace; delete removes bytes + frontmatter; paths prefix filtered; blob by sha; repin clears/refreshes `docs_pin` for named paths; drift true after mutating file at base_ref without repin.
- [ ] Implement API routes on the executor lane.
- [ ] Implement create-panel + detail-pane UI (paths-first asymmetry must be obvious in layout: Reference documents prominent, Attach secondary).
- [ ] Update configuration-reference.md.
- [ ] Tests pass; `just validate`; commit `task: task-documents phase 3 — attach API, path preview, UI, docs`.

**Phase 3 done when:** an operator can type a repo path (with autocomplete + image thumb) or attach a pasted screenshot, see both in the detail pane with drift/re-pin, and the Phase 2 engine path delivers them to the worker by reference.

---

## Out of scope (do not implement)

Copied from spec §10 — reject any impulse to add these:

- Embedding document content in the work order (text or binary).
- Live path resolution without a pin / automatic refresh.
- Routing image-docs to vision-capable workers.
- LFS / large-binary tier / engine-side chunking or summarization.
- Cross-task content-addressed doc store with refcounts/GC.
- Unifying `docs:` + `attachments:` into a single `attach:`/`repo:` field surface.

## Acceptance checklist (feature-complete after Phase 3)

- [ ] `docs: [md, png]` pins both; worker scratch files read-only with real extensions; header annotates binary.
- [ ] Attachments live under `<task>.docs/`; deleted with brief; retained on evergreen reset; `docs_pin` cleared on evergreen reset.
- [ ] Over-cap / unsupported / missing / traversal / range-on-binary → blocked or 400 as specified; never truncated.
- [ ] Workflow steps share byte-identical pins; `steps:` filters; split children inherit paths + attachment bytes and re-pin.
- [ ] No-docs task work order byte-identical to pre-feature.
- [ ] UI nudges paths; attach is secondary; drift badge + re-pin work.
