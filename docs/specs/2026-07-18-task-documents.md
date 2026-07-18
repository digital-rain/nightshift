# Nightshift — Task documents (`docs:` — paths-first, binary-native, delivery-by-reference)

**Subject:** A first-class way for a task brief to declare the **documents** an agent should read while working the task — a design spec, an API contract, a prior investigation, a data dictionary, a style guide, **and small binary docs (images, PDF)** such as screenshots, diagrams, and mockups. The design is a **deliberate hybrid with an opinionated default**: documents are *referenced by repo-relative path* (canonical, no copy), pinned by git **blob sha** at first dispatch and read by the worker from its worktree object store; *attachments* are a narrow, visibly-heavier escape hatch for bytes that live in no repo. **Nothing — text or binary — is ever embedded on the wire.**
**Status:** Proposal (greenfield, binary-first). Follows the workflows spec (`2026-07-16-workflows.md`) conventions; where this and the code disagree, the code governs.
**Default:** No documents. A task without `docs:`/`attachments:` behaves **byte-identically to today** — the work order gains no keys, the prompt header gains no lines, materialization does nothing. Single-shot, `enhance`, `split`, `loop`, and `workflow` are untouched.
**Relationship:** Documents are the *operator-supplied* sibling of engine-produced **workflow artifacts** (`2026-07-16-workflows.md §5`). Both become read-only files materialized into run-scratch and named in the prompt header through the same rail (`materialize_artifacts` → `materialize_docs`, `_artifact_header`, the tasks-repo executor lane). This proposal reuses that rail and extends it in exactly one dimension: it becomes **byte-agnostic** (opaque bytes, real extensions, delivered by reference) instead of text-only.

---

## 0. The one idea

Nightshift is a **git-native** engine: tasks land commits in a target repo pinned at `base_ref`, doc steps already run read-only at that same `base_ref`, and operators already think in repo paths (`docs/spec/auth.md`, `docs/mockup.png`). So the canonical document — text or image — *already lives in the repo*, versioned and drift-free. The task should **name** it, the manager should **pin** it (blob sha at `base_ref`), and the worker should **read** it from its own worktree object store (`git cat-file blob <sha>`):

```yaml
docs: [docs/spec/auth.md, docs/design/login-mockup.png]
```

Attachments — bytes stored *with* the task in `nightshift-tasks` under `<task>.docs/` — are the **escape hatch, not the peer**. They exist for the one case paths cannot serve: content that lives in no repo (a pasted investigation, an emailed screenshot). They are a *visibly different* affordance (`attachments:`, a heavier UI action) so an operator never reaches for one by reflex. The mental model is one sentence:

> **By default your docs live in the repo — reference them by path. Attach only what has nowhere else to live.**

**Binary is first-class from day one, and it is what makes the design's spine load-bearing rather than stylistic.** You cannot base64 a PNG into a JSON work order, an image has no `range:` line-slice, and duplicating large media is exactly what the (non-LFS) tasks repo can't absorb. So the delivery mechanism is **always by-reference — never embed** (text *and* binary): the worker reads a pinned blob (`git cat-file blob <sha>`) from a store it already has. Text and a 400 KB mockup travel identically — as a `{sha, media}` pin the worker resolves locally. The engine never parses content; it pins, custodies, delivers a read-only file with its real extension, annotates binaries in the header, and routes. Whether a model can *use* an image is the backend's concern (a vision model reads it; a text-only one ignores it, §7).

## 1. Frontmatter contract

Two operator keys — one primary, one escape hatch — plus one engine-owned pin.

| Field | Type | Owner | Meaning |
|---|---|---|---|
| `docs` | string **or** list of (string \| doc-spec object) | operator | **Primary.** Repo-relative paths (relative to the target-repo worktree root) resolved and pinned at `base_ref`. String form is the dead-simple common case; object form adds per-doc options (§1.1). Default unset. |
| `attachments` | list of (string \| doc-spec object) | operator | **Escape hatch.** Task-local files (text or small binary) whose bytes live in the tasks repo under `<task>.docs/`. A different key with a different verb — you *attach* a file, you *reference* a path. Default unset. |
| `docs_pin` | map (engine-rendered) | **engine** | Per referenced entry (path *and* attachment), the git **blob sha** + sniffed `media` + `bytes` at first dispatch, so an entry's dispatch-time content is recoverable even if the branch moves. Written through the tasks-repo executor's engine-meta lane; added to `ENGINE_META_KEYS`, **not** `_EDITABLE_META_KEYS`; UI renders read-only. Absent until first dispatch. |

### 1.1 `docs` — string or object (text and image, one shape)

95% of tasks want `docs: [path, path]` and should type exactly that. The object form covers the rare per-doc knob without a second key. **A text path and an image path are the same shape** — nothing privileges text:

```yaml
# Common case — dead simple, the shape we nudge every operator toward:
docs:
  - docs/spec/auth.md                      # text
  - docs/design/login-mockup.png           # image — same syntax, media sniffed → image/png

# Object form — only when you need a per-doc option:
docs:
  - docs/spec/auth.md                      # bare string still allowed in the same list
  - path: docs/very-large-runbook.md
    range: "1-120"                          # TEXT ONLY line range (§5); illegal on binary
  - path: contracts/openapi.yaml
    as: "the API contract"                  # override the prompt-header label
    steps: [plan]                           # workflow: restrict to named steps (§7)
```

Normalization (parse time): a bare string `"docs/spec/auth.md"` becomes `{path: "docs/spec/auth.md"}`. `media` and `bytes` are **not** operator-writable — the engine sniffs them at pin time (extension + content magic) and records them in `docs_pin`.

| Object field | Type | Default | Meaning |
|---|---|---|---|
| `path` | string | (required) | Repo-relative path, resolved at `base_ref`. Must normalize inside the worktree root; `..`/absolute → parse error (§7). A `workspace:` prefix escapes to a sibling repo in the workspace. |
| `range` | string `"A-B"` | whole file | **TEXT ONLY.** Materialize only lines A–B (inclusive, 1-based), applied after read; header notes it. A `range` on a binary doc is a parse/resolution error — **binary has no lines and is bounded purely by the byte cap** (§5). |
| `as` | string | derived from basename | Human label in the prompt header (`The <as> is: <path>`). |
| `steps` | list of step ids | all steps | Workflow only: restrict this doc to the named steps (§7). Ignored on single-shot tasks. |

### 1.2 `attachments` — the escape hatch (text or small binary)

```yaml
attachments:
  - investigation-2026-07-10.md            # text, committed under <task>.docs/
  - path: emailed-screenshot.png           # small binary — an image with no repo home
    as: "the bug repro screenshot"
```

**The two keys are intentionally asymmetric in ergonomics.** `docs` accepts the barest syntax because it is what an operator types most. `attachments` requires you to have *first put a file into the tasks repo* (via the UI's explicit attach action, §6) — there is no way to "just type an attachment." **That asymmetry is the nudge** — the friction gradient encodes "prefer paths" into the ergonomics, not just the prose. It matters more for images: a mockup already in `docs/design/` should be referenced, not re-uploaded.

### 1.3 Ownership split & compositions

`docs` and `attachments` are operator-owned and join `_EDITABLE_META_KEYS`. `docs_pin` is engine-owned and joins `ENGINE_META_KEYS` (extended from the string-only workflow cursor to a nested mapping — the `set_engine_meta` lane already round-trips YAML values).

- `docs` / `attachments` + **any existing field** — orthogonal.
- `docs` / `attachments` + `workflow` — first-class: docs flow to steps per §7 (default: every step; opt-in narrowing via `steps:`). A `plan-review-implement` workflow pointed at `docs: [docs/spec/auth.md, docs/mockup.png]` has every step read the same pinned spec and the same pinned mockup.
- `docs` / `attachments` + `loop` / `evergreen` — supported; each iteration/cycle re-materializes. Evergreen **clears `docs_pin`** each cycle (re-pins to the then-current `base_ref`); attachments and the frontmatter are **retained** (operator inputs are cycle-independent, unlike engine artifacts which reset).

## 2. Storage & custody

Two custody models, matching the two mechanisms — and the asymmetry is the point.

### 2.1 `docs` (paths) — *no custody*

The engine stores **nothing**. Bytes live where they already live: in the target repo, versioned in *that* repo's history — a git blob whether it is a `.md` or a `.png`. The only engine-owned datum is `docs_pin` (§4), which records blob shas, media types, and sizes — never content. There is exactly one copy of `docs/design/login-mockup.png` in the universe, and it is the repo's. A 400 KB diagram referenced by twelve tasks is stored once; an attachments-default would copy it twelve times into the non-LFS tasks repo.

### 2.2 `attachments` — custody in the tasks repo, byte-native

Attachment bytes live next to the brief under a **`<task>.docs/`** directory — parallel to the engine's `<task>.artifacts/` convention (workflows §5) but a *distinct* directory, and **byte-native** (real extensions, opaque bytes) rather than text-only:

```
nightshift-tasks/<queue>/
  07.add-oauth.md                 # the brief
  07.add-oauth.docs/              # operator attachments (this proposal)
    investigation-2026-07-10.md   #   text
    emailed-screenshot.png        #   small binary, stored opaque, real extension
  07.add-oauth.artifacts/         # engine-produced workflow artifacts (existing)
    plan.md
```

- **Writes** go through the tasks-repo executor — the same serialized lane that commits frontmatter flags and workflow artifacts. `task_files.write_attachment` mirrors `write_artifact` but is **byte-native** (`data: bytes`, real extension, not forced `<name>.md`). One commit per attach/replace/remove.
- **Cap: the effective `document_cap_bytes`** (§5.1 — operator setting clamped to a code-constant ceiling). Over-cap attach is rejected at the API boundary with a typed `document_too_large` error; unsupported media type with `unsupported document type`. Never silently truncated.
- **Media allow-list at attach time** (§5.3): text always; images (`png`/`jpeg`/`gif`/`webp`) + `pdf` by default. `media` is sniffed (extension + content magic) and recorded.
- **Overwrite/versioning:** re-attaching a same-named file overwrites in place; the superseded version is ordinary tasks-repo git history — the artifact model exactly.
- **Lifecycle:** `<task>.docs/` is deleted **with the brief** on terminal consumption (land / split-harvest / operator delete), folded into the brief-removal commit — the same fold `delete_task` / `drop_completed_task` do for `<task>.artifacts/`. On an **evergreen reset**, attachments are **retained** (operator input, not per-cycle engine state); only `docs_pin` is cleared. On quarantine, retained alongside the brief.

### 2.3 Why not store path-doc bytes too (symmetry)?

Copying every referenced doc into `<task>.docs/` would make custody uniform but would (a) duplicate large in-repo media into the non-LFS tasks repo, (b) reintroduce the drift git already solves (the copy and the canonical doc diverge), and (c) turn every task into a tasks-repo bloater. The asymmetry is the feature: **canonical stays canonical; only homeless bytes get stored.**

## 3. Materialization & the work order

**One materialization path for all docs; nothing embedded on the wire.** The divergence is only *where the bytes come from* (worktree object store for paths; local tasks checkout for attachments), resolved manager-side. The worker sees a uniform list of read-only files and does not care about source-kind or media. *(Grafts: the uniform materializer + invisible-kind header from Candidate 3; the media-derived-extension delivery framing from Candidate 5.)*

### 3.1 Resolution (manager-side, at dispatch) — pin-only, never embed

`build_work_order` gains a `docs` resolution pass producing a `docs` config block. **Delivery is by-reference for everything** — the worker already has both stores (the worktree at `base_ref`, and the tasks checkout), so no content, text or binary, rides the wire:

- **`docs` (path) entries:** resolve the path against the target repo at `base_ref` via `git rev-parse <base_ref>:<path>` → the **blob sha** (not the dirty working tree), sniff `media`/`bytes`, record the sha into `docs_pin` (§4) on first resolution, and emit a **pin-only** entry (`sha` + `path` + `media` [+ `range` for text]). The worker materializes from that sha out of its worktree object store.
- **`attachments` entries:** resolve the tasks-repo blob sha for `<task>.docs/<name>`, sniff `media`/`bytes`, pin it, and emit a **`blob_ref`** entry (the `<task>.docs/` path). The worker reads it from its local tasks checkout.

The resulting block (embedded in the config blob, mirroring `workflow.artifacts` in shape, but carrying **no content**):

```json
"docs": [
  { "name": "auth.md", "label": "the auth spec", "kind": "path", "source": "target",
    "media": "text/markdown", "path": "docs/spec/auth.md", "ref": "base_ref", "sha": "9f2c…", "range": "1-120" },
  { "name": "login-mockup.png", "label": "the login mockup", "kind": "path", "source": "target",
    "media": "image/png", "path": "docs/design/login-mockup.png", "ref": "base_ref", "sha": "b1d0…", "bytes": 402118 },
  { "name": "emailed-screenshot.png", "label": "the bug repro screenshot", "kind": "attach", "source": "tasks",
    "media": "image/png", "blob_ref": "07.add-oauth.docs/emailed-screenshot.png", "sha": "0f1e…", "bytes": 51044 }
]
```

Every entry carries a `sha`; **no `text`, no bytes, for either kind or either source.** `source` says which object store to read; `media` drives delivery + header annotation; `range` (text only) is applied worker-side. The work order stays tiny regardless of doc size or type — a 4 MB PDF (under a raised cap) adds ~150 bytes, not 5.3 MB of base64.

### 3.2 Worker materialization & prompt header

The worker calls `materialize_docs` (a byte-native `materialize_artifacts` sibling) to write each doc as a **read-only file in run-scratch, outside any worktree** — exactly how brief and artifacts are materialized. It differs from the text-only artifact materializer in three byte-agnostic ways:

1. **Sources bytes by reference** — a path-doc via `git cat-file blob <sha>` from the worktree object store (text and binary identically); an attachment via `git cat-file blob <sha>` / a read of `blob_ref` from the local tasks checkout. It verifies the read bytes' sha matches the entry's `sha` (integrity + correct-blob check; mismatch → typed `document_unavailable`, routes RETRY_ELSEWHERE — *graft: Candidate 5's integrity check*).
2. **Preserves the real extension** in the scratch filename (`…doc-<name>.png`, `…doc-<name>.pdf`, `…doc-<name>.md`) — derived from `media` — so a vision backend and the OS recognize it.
3. **Applies `range:`** (text only) after reading, before writing the scratch copy. `chmod 0o444` on the result.

The prompt header names them, reusing `_artifact_header`; **source-kind and wire-encoding are invisible** to the agent, but a binary is annotated so it opens the file with the right tool:

```
Reference documents (read-only, read these before exploring):
  - The auth spec (lines 1–120) is: <scratch>/task-local-main-07.add-oauth.doc-auth.md
  - The login mockup is: <scratch>/…doc-login-mockup.png            [image/png — open with an image-capable tool]
  - The bug repro screenshot is: <scratch>/…doc-emailed-screenshot.png  [image/png — open with an image-capable tool]
```

`nightshift-local.md` (and the workflow doc/code charters) gain one conditional paragraph, mirroring the existing plan-charter language ("read the manifest files first"): *"When reference documents are listed, read them first; treat them as authoritative context before exploring the repo. A document annotated with an image/PDF media type is binary — open it with a tool that can read that type; if you cannot, note that in your output and proceed with the remaining context."* The last clause is the text-only-backend contract (§7) stated in-prompt.

Because a path-doc materializes from its pinned blob sha, the worker sees the exact pinned content even if its local branch is stale, and the scratch copy lives outside the worktree, so an agent editing `docs/spec/auth.md` as part of the task never mutates its own reference copy mid-run.

## 4. Cross-machine / recoverability / staleness — the blob-sha pin

The core invariant holds identically for text and binary because both resolve through **a pinned blob sha**, and the land pipeline never rewrites history (so blob shas stay reachable).

1. **Resolution pins to `base_ref` via the blob sha, not by embedding.** The manager resolves each entry to a blob sha (`git rev-parse <base_ref>:<path>` for path docs; the tasks-repo blob sha for attachments) and freezes it into the work order and `docs_pin`. The worker reads that sha from a store it already has — the worktree object store (populated by the `prepare_worktree_base` fetch it already does) or the local tasks checkout. A cross-machine worker whose local branch is behind still gets the exact pinned bytes, because a content-addressed sha is reachable once `base_ref` is fetched. **Recoverable by construction:** a crashed/re-dispatched step reconstructs every doc from `nightshift-tasks` (`docs`/`attachments` + `docs_pin` + `<task>.docs/`) + the target repo at `base_ref`; nothing lives only in a live session, and nothing depends on the transient work-order payload carrying content.

   > On the reachability risk C1 raised (a large path-doc blob might not be present cross-machine): it is reachable via the same `prepare_worktree_base` fetch the code step already depends on — the pinned commit is an ancestor of `origin/main`, so its trees and blobs come down with that fetch. Documents ride the fetch worktree setup already performs; no new fetch machinery. The residual risk (an operator force-pushing the target repo's history out from under a pinned task) is outside Nightshift's controlled land path, which never rewrites history.

2. **`docs_pin` survives branch movement.** Content-addressed and immutable: `git cat-file blob 9f2c…` returns the dispatch-time bytes regardless of where branches now point. The manager re-materializes from the pinned sha on every subsequent dispatch/step, so **every step of a workflow sees byte-identical doc content** — text and image alike — even if the doc changed on the branch between plan and implement.

   ```yaml
   docs_pin:
     docs/spec/auth.md:           { sha: 9f2c1ab, media: text/markdown, bytes: 18240 }
     docs/design/login-mockup.png:{ sha: b1d0c88, media: image/png,     bytes: 402118 }
     attach:emailed-screenshot.png:{ sha: 0f1e2d3, media: image/png,    bytes: 51044 }
   ```

3. **Snapshot-on-first-dispatch, not live-refresh.** Docs are pinned once and held for the task's life (a plan step and the implement step must read the same spec/mockup). Evergreen resets clear `docs_pin` so each cycle re-pins to the then-current `base_ref`.

**Fallback when a pinned blob is unreachable** (shouldn't happen given no-history-rewrite; defined for safety): retry resolution at the current `base_ref` by path; on success re-pin (log `docs_pin_refreshed`); on failure the task goes **blocked** with `referenced doc '<path>' not found at base_ref` — an operator/authoring condition, not a retryable run error.

## 5. Size / token discipline — the cap

**A hard per-document byte cap is the design, not a limitation.** Two reasons make it correct: (1) attachment bytes live in `nightshift-tasks`, a normal git repo that is **not** LFS-backed, so GitHub's non-LFS blob limit is the physical bound; and (2) task docs target the **small-to-medium** regime. **The cap bounds images directly** — an image has no `range:` escape, so it fits under the cap or it is rejected (never truncated — a truncated PNG is not a PNG). The only requirement is that the cap be *operator-tunable within a code-defined ceiling*, never a magic literal.

### 5.1 The document cap — configurable setting under a code-constant ceiling

- **Ceiling — a code constant, not a hard-coded check.** `DOCUMENT_CAP_CEILING_BYTES` (a module constant sited next to the existing `_DOCUMENT_MAX_BYTES = 256 * 1024` in `manager/api_worker.py`) is the absolute maximum the engine will *ever* store or deliver, chosen to sit comfortably under the GitHub non-LFS blob limit (e.g. `5 * 1024 * 1024`). No cap setting may exceed it; the setting is clamped to it on load. **Every cap check references the constant, never a literal.**
- **Setting — operator-tunable, starts low.** `document_cap_bytes` is a new operator setting (manager `manager.json`, queue-overridable, env `NIGHTSHIFT_DOCUMENT_CAP_BYTES`) with a **conservative default well below the ceiling** (256 KB — the current artifact-cap value) so an operator *raises* it toward the ceiling as their corpus warrants (an image-heavy operator raises it early — a mockup easily exceeds 256 KB), or lowers it to keep prompts lean. Loaded via the settings registry exactly like `diff_cap_lines` (default, `category="Worker execution policy"`, `env=…`); surfaced in the Settings UI. **Effective cap = `min(document_cap_bytes, DOCUMENT_CAP_CEILING_BYTES)`.**
- **Enforced at the earliest point.** Attachments are checked at **attach time** (the API boundary) — the tasks repo never receives an over-cap blob; path docs are checked at **resolution time** in `build_work_order` against the sha's `git cat-file -s` size, *before* materializing. Over-cap **rejects/blocks, never truncates**: a path doc → blocked `referenced doc '<path>' is <N> KB, over the document cap (<cap> KB) — declare a range (text) or raise document_cap_bytes`; for binary the message drops the `range` hint (`… reduce the image or raise document_cap_bytes`).

### 5.2 `range:` is text-only; the cap is the whole story for binary

`range: "A-B"` materializes only the declared span (header notes it) — the primary large-*text* lever, and the reason a path beats "paste it into the brief." **It does not apply to binary:** there is no "first 120 lines" of a PNG, so a `range` on a binary entry is a parse/resolution error. An over-cap image is rejected; the operator's remedy is to resize/recompress it (an image-editing act, outside the engine) or raise the setting toward the ceiling.

### 5.3 Media allow-list + aggregate budget + per-step selection

- **`allowed_doc_media_types`** (operator setting, manager + queue-overridable), default `["text/*", "application/json", "application/yaml", "image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"]`. The engine sniffs each doc's media type (extension + content magic — *classification, not content parsing*) and rejects anything off the list with `unsupported document type '<media>'`. A magic-byte mismatch (a `.png` that isn't a PNG) is rejected. Text is always allowed; the binary set is the deliberately small "images mostly, plus PDF" set. Widening is an operator choice; the byte cap still bounds each entry.
- **Per-task aggregate budget** `document_budget_bytes` (queue-config, default e.g. 4 MB, clamped under a ceiling multiple): the sum of a task's (or a workflow step's) resolved doc bytes, so an operator can't reference twenty in-cap images into one prompt. Over-budget → blocked, naming the total and the budget. Because delivery is by-reference the work order stays tiny regardless — this budget bounds what the *worker materializes and the agent reads*, not wire size.
- **Per-step selection (workflow).** A doc's `steps: [plan]` restricts it to named steps, so a heavy API contract or diagram reaches the planner without bloating every implement retry. Default (no `steps:`) = every step. This is the workflows §8.3 lever ("a step receives only its declared inputs") applied to operator docs.
- **Cacheability.** Docs append to the task-varying tail of the prompt (after the byte-stable charter), preserving the prefix-cache discipline (workflows §8.2). Pinned bytes are identical across a workflow's steps, so cross-step cache hits are possible for text; a re-read image is re-encoded by the backend each turn (backend concern).

## 6. UI (create panel + detail pane)

The UI **nudges toward paths** by ergonomic asymmetry, and makes binary first-class: **type a path, or attach a file — and if it's an image, you see it.** (This is the first place `app.js` grows real file upload; today all fields are text.)

**Create panel:**
- A prominent **"Reference documents"** field with **repo-path autocomplete** — completing against the target repo tree at `base_ref` (a `GET /api/repos/{repo}/paths?prefix=` endpoint backed by `git ls-tree`, filtered to `allowed_doc_media_types`). Enter adds a chip; an image path chip shows an **inline thumbnail** rendered from the pinned blob (`GET /api/repos/{repo}/blob?sha=…`). Invalid paths (absent at `base_ref`) flag inline in amber *before* create. A `range` field appears only for text chips.
- A visually secondary **"Attach a file"** control (button + drag-drop + clipboard-paste — a pasted screenshot is the marquee case) captioned *"Only for content that isn't in any repo."* The picker's `accept` is populated from the allow-list; over-cap/unsupported uploads reject inline. Image attachments show a client-side `FileReader` thumbnail before the task is even created; a running total against `document_budget_bytes` shows as a small meter.

**Detail pane:**
- A **Documents** section listing both kinds with distinct iconography — a **link/branch icon** for paths (showing `path @ pinned-sha`), a **paperclip icon** for attachments — plus a media icon (doc/image/PDF).
- **Image preview inline** for both kinds (thumbnail → lightbox); text opens a rendered read-only view (range-sliced); PDF opens in an embedded viewer / download.
- Path chips expose a **"re-pin to current base_ref"** affordance; when a path-doc's pinned sha differs from its content at the current `base_ref`, the chip shows an amber **"source drifted — pinned to older version"** badge with one-click re-pin (*graft: Candidate 3/5's drift badge* — staleness becomes an auditable choice, not a silent fact).
- Add/remove; removing a path edits `docs:`, removing an attachment edits `attachments:` *and* deletes the file from `<task>.docs/`. `docs_pin` renders read-only.

## 7. Edge cases

| Case | Behavior |
|---|---|
| **Missing path (path not at `base_ref`)** | Task **blocked**, `referenced doc '<path>' not found at base_ref`. Never a silent skip. Surfaces inline in the detail pane. |
| **Missing attachment (named but absent from `<task>.docs/`)** | Blocked, `attached document '<name>' is missing from the task` (a frontmatter/consistency error; the UI keeps them in sync). |
| **Doc renamed/deleted in repo after pin** | The pinned **blob sha** still resolves via `git cat-file` regardless of path (content-addressed), so a pinned task keeps working. Only *first* resolution needs the path. Re-pin against the new path if the operator wants the moved doc. |
| **Over-cap image** | Rejected — no range escape. Path doc → blocked at pin (`… reduce the image or raise document_cap_bytes`); attachment → rejected at attach (`document_too_large`). Never truncated. |
| **Over-cap text** | Blocked/rejected with the "declare a range or raise document_cap_bytes" message; `range:` slices at materialize time, but the *stored* attachment must still fit the cap (attach the slice, or raise the cap). |
| **Unsupported media type** | Rejected at attach (attachment) or resolution (path) with the sniffed type named (§5.3). Magic-byte mismatch also rejected. |
| **Huge doc (past the ceiling)** | Even a maxed-out `document_cap_bytes` can't admit it (clamped to `DOCUMENT_CAP_CEILING_BYTES`). Blocked/rejected. Out of scope by construction — the tasks repo is not LFS-backed. |
| **Doc shared by many tasks** | Path docs: the *celebrated* case — N tasks reference the same canonical blob, each pins its own sha at its own dispatch; zero duplication of large media in the tasks repo. Attachments are per-task copies by design; sharing large content is a reason to prefer a path. |
| **Doc changes between dispatch and land / between steps** | The pin freezes dispatch-time bytes; every step re-materializes from the same sha ⇒ byte-identical across a workflow. A later re-dispatch re-pins to the newer sha. The sha is the contract, for text and image alike. |
| **Workflow: which steps see which docs** | Default: every step sees every declared doc. Opt-in narrowing via `steps: [ids]`. Orthogonal to workflow `inputs:` (which route *artifacts*); docs and artifacts are separate header sections. |
| **Decomposition — do children inherit docs?** | **Both, self-contained.** `harvest_split_output` copies parent `docs:` into each child's frontmatter and **re-pins** each child at its own dispatch (canonical, no byte copy); it copies parent `<task>.docs/` **bytes** into each child's `<child>.docs/` (snapshot-per-child, since the parent may be consumed). Both remain recoverable cross-machine. Opt-out per child in the split charter. |
| **Text-only backend receiving an image doc** | Fully defined and benign. The engine is byte-type-agnostic: it delivers the image file and annotates it `[image/png — open with an image-capable tool]` regardless of backend. A vision model reads it; a text-only model reads the annotation, notes it can't open the binary (charter clause, §3.2), and proceeds with the remaining context. The engine never routes image-docs to vision workers (that would couple doc-media to scheduling) and never fails the task (an unused input is not an error). If the operator wants the image *used*, they pin `model:` to a vision model — an ordinary routing choice, not new machinery. |
| **`range` on a binary doc** | Parse/resolution error → blocked `range: is not valid on binary document '<name>' (<media>)`. |
| **Path escapes the repo (`../`, absolute, symlink off-tree)** | Rejected (normalize-then-`startswith` guard, the same check `worktree_dir` uses; `git rev-parse <base_ref>:<path>` is confined to the repo tree): blocked `referenced doc '<path>' is outside the repo`. |

## 8. Touch points (implementation checklist)

Mirrors the workflows spec §12 style.

- **`config/` (frontmatter parse)** — normalize `docs` (string → `{path}`; object form `path`/`range`/`as`/`steps`) and `attachments`; validate repo-relative + traversal guard; **error if `range` is set on a doc whose sniffed media is binary**.
- **`config/manager.py` (settings registry)** — new operator settings: `document_cap_bytes` (default `256 * 1024`, `category="Worker execution policy"`, `env="NIGHTSHIFT_DOCUMENT_CAP_BYTES"`, serialized like `diff_cap_lines`), `allowed_doc_media_types` (list), `document_budget_bytes`; all manager + queue-overridable (queue `config.json`). Effective cap helper = `min(document_cap_bytes, DOCUMENT_CAP_CEILING_BYTES)`.
- **`manager/api_worker.py`** — add the `DOCUMENT_CAP_CEILING_BYTES` module constant beside `_DOCUMENT_MAX_BYTES`, chosen under the GitHub non-LFS blob limit; the doc-cap check references the effective cap (setting clamped to the ceiling), never a literal.
- **`manager/work_orders.py`** — `build_work_order` docs-resolution pass (§3.1): for path docs resolve the blob **sha** at `base_ref` (`git rev-parse <base_ref>:<path>`) + sniff `media`/`bytes`; for attachments resolve the tasks-repo blob sha; record/read `docs_pin`; enforce per-doc cap + `allowed_doc_media_types` + aggregate budget; filter by `steps`; emit **pin-only / `blob_ref`** entries (`kind`/`source`/`path`/`media`/`ref`/`sha`/`range`) — **no content on the wire, text or binary**; blocked-reason returns for missing / over-cap / unsupported / traversal / range-on-binary.
- **`task_files.py`** —
  - byte-native attachment family: `attachments_dir(...)`, `write_attachment(..., data: bytes)`, `read_attachment(...) -> bytes`, `delete_attachment(...)`; `sniff_media_type(name, head_bytes)` helper.
  - `materialize_docs(...)` — byte-native `materialize_artifacts` sibling: `git cat-file blob <sha>` from the target worktree (path) or tasks checkout (attachment), preserve real extension, `chmod 0o444`, sha-verify (mismatch → `document_unavailable`), text `range` slice post-read.
  - `drop_completed_task` / `delete_task` — fold `<task>.docs/` removal into the brief-removal commit; **retain** on evergreen reset.
  - `harvest_split_output` — copy parent `docs:` into children (re-pinned) and copy `<task>.docs/` bytes into each child's `<child>.docs/`.
  - `_EDITABLE_META_KEYS` += `docs`, `attachments`; `ENGINE_META_KEYS` += `docs_pin` (nested-mapping extension of the string-only cursor).
- **`prompts.py`** — Reference-documents header block (label + scratch path, `(lines A-B)` for ranged text, `[<media> — open with a …-capable tool]` for binary; source/kind invisible); the conditional "read reference docs first, and if you can't open a binary note it and proceed" paragraph in `nightshift-local.md` and the workflow doc/code charters. Extend `_artifact_header` or add `_docs_header`.
- **`worker/execute.py`** — after brief/artifact materialization, call `materialize_docs` for single-shot, code, and doc/split steps; pass `{label: (path, media)}` to the header builder. (Doc/split steps already cut a worktree at `base_ref`, so `git cat-file blob <sha>` is available.)
- **`resolve_runner.py`** — materialize docs for resolve runs too (same pins), so a resolve attempt sees identical context.
- **`transitions.py`** — evergreen reset effect clears `docs_pin`, retains `<task>.docs/` + `docs`/`attachments` frontmatter; terminal consumption removes `<task>.docs/` alongside `<task>.artifacts/`.
- **`lifecycle.py`** — carry the resolved `docs` set on the work order/attempt; add `document_unavailable` (environment) and `document_too_large` / `unsupported document type` (authoring) to the failure taxonomy.
- **`manager/api_operator.py`** — `POST/PUT/DELETE /api/tasks/{task}/attachments` (executor lane, multipart accepts text + allowed binary, effective-cap + allow-list + budget guards, records the pin); `GET /api/repos/{repo}/paths?prefix=` (autocomplete at `base_ref`); `GET /api/repos/{repo}/blob?sha=` (path-doc preview) and `GET /api/tasks/{task}/docs/{name}` (attachment preview) for image thumbnails; `POST /api/tasks/{task}/docs/repin` (clear/refresh `docs_pin`, single or all).
- **`manager/scheduler.py`** — none (docs don't affect capability matching; the candidate's existing `base_ref` is reused for pinning; image-docs are deliberately *not* routed to vision workers).
- **`assets/ui/app.js`** — create-panel Reference-docs autocomplete field + secondary Attach control (first `<input type="file">` / `FormData` / `FileReader`); image thumbnails + cap/budget meter; detail-pane Documents section (path vs attachment + media iconography, image/PDF preview, view/re-pin/remove, drift badge); read-only `docs_pin`.
- **`docs/user/configuration-reference.md`** — `docs`, `attachments` (operator), `docs_pin` (engine) frontmatter rows; the `document_cap_bytes` setting (+ env + queue override) and the `DOCUMENT_CAP_CEILING_BYTES` ceiling note; `allowed_doc_media_types`; `document_budget_bytes`; the pinning/recoverability model; the note that all docs (text + binary, path + attachment) are delivered by reference and never embedded.
- **Tests** — string/object `docs` normalization; path resolution at `base_ref` (not dirty tree); **path-docs pin-only on the wire (no content) and materialize via `git cat-file blob <sha>`**; **binary round-trips both kinds** (path via sha, attachment via `blob_ref`/sha) — materialized read-only with the real extension, header carries the media annotation, engine never decodes it; sha-verify on materialize (tampered blob → `document_unavailable`); pin recorded on first dispatch and reused across workflow steps (byte-identical text *and* image); pin blob recoverable after branch moves / rename / stale local branch (once `base_ref` fetched); `allowed_doc_media_types` rejects an unsupported type + magic-byte mismatch; over-cap image rejected (no range hint) / over-cap text blocked (range hint); `document_cap_bytes` clamped to `DOCUMENT_CAP_CEILING_BYTES` when set above; raising the setting admits a previously-over-cap doc; aggregate `document_budget_bytes` block (counts binary bytes); attachment attach/replace/remove commits + guards; `<task>.docs/` deleted with brief, retained on evergreen reset; per-step `steps:` selection; `range:` slice + header note (text only; illegal on binary); decomposition inherits `docs` (re-pinned) **and** attachments (snapshot-per-child, bytes copied); `superseded` drift badge surfaces when the pinned sha diverges from HEAD; text-only backend gets an image → header annotated, task not failed; a task with neither field produces a byte-identical work order to today (regression guard).

## 9. Implementation order

1. `config`/`task_files` parse + normalization + `_EDITABLE_META_KEYS`/`ENGINE_META_KEYS` + byte-native attachment CRUD + `sniff_media_type` + `materialize_docs` (text + binary from the outset) + tests.
2. `config/manager.py` cap/allow-list/budget settings + `DOCUMENT_CAP_CEILING_BYTES` constant + clamp + tests.
3. `manager/work_orders.py` resolution pass (pin-only / `blob_ref`) + `docs_pin` + budgets + blocked-reason returns + tests.
4. `prompts.py` header (binary annotation) + `worker/execute.py` wiring (single-shot, code, doc/split) + sha-verify + tests.
5. `transitions.py` evergreen reset + `harvest_split_output` inheritance (paths re-pinned + attachment bytes copied) + tests.
6. Operator API (attach CRUD, path autocomplete, blob/preview endpoints, repin) + `app.js` UI (file upload, image preview, cap meter, drift badge) + config docs.

Steps 1–4 are the functional core; **binary is in from step 1** (no text-first phase); the no-`docs:` path is byte-identical throughout.

## 10. Non-goals

- **Live path resolution without a pin.** A workflow's plan and implement steps must read the same spec/image; live re-read breaks determinism and recoverability. The blob-sha pin gives canonical-by-path authoring with immutable-by-sha execution.
- **Embedding document content (text or binary) in the work order.** Everything rides by-reference (`sha` / `blob_ref`); the worker has both stores. A PNG can't be a JSON string, and embedding text is redundant copying — one model, uniformly by-reference. *(This is a deliberate departure from the artifact rail, which embeds text; artifacts have no other home, documents do.)*
- **A cross-task content-addressed doc store with refcounts/GC** (Candidate 5's model). Genuinely elegant for dedup, but it is net-new machinery (a store, refcounting, a GC pass) and the lowest-fidelity to existing patterns; path docs already give free sharing (the canonical blob) and git delta-compresses identical attachment blobs. Deferred unless attachment sharing proves a real cost — it composes cleanly later (an attachment becomes a store object) if it does.
- **Automatic refresh / watch-the-file.** Re-pin is always an explicit operator act — a durable batch engine must not silently change a task's inputs.
- **Routing image-docs to vision-capable workers.** Deliberately not done (§7): doc-media stays decoupled from scheduling; the model decides what it can use.
- **LFS / a large-binary tier.** The tasks repo is a normal git repo; the cap + ceiling keep it under the non-LFS limit. Media past the ceiling is out of scope by construction, not omission.
- **Engine-side chunking / summarization / embedding / agent-side paging into an over-cap doc.** The engine never parses content; size is disciplined by the configurable per-doc byte cap (under a code-constant ceiling), `range:` (text), the aggregate budget, and per-step selection. An over-cap file is served by a `range:` slice (text), a resize (image), or rejected — never by the engine paging a stored blob.
- **A fully-symmetric unified `docs:`-only field with an `attach:`/`repo:` scheme** (Candidate 3's model). Its orthogonal source×media framing is the cleanest *conceptual* model and directly informed §3's single media-aware materializer — but a single field hides the one decision that matters (canonical-in-repo vs task-local), letting operators drift toward attaching by ergonomic accident. The asymmetric two-field surface keeps the "prefer paths" nudge physical while sharing one delivery mechanism underneath (where the real complexity is). Materialization is symmetric (the worker treats both uniformly — free); authoring and custody stay asymmetric (the recommendation, encoded).
