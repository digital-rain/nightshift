# Nightshift — Task documents (`docs:` — paths-first, attach only what has nowhere else to live)

**Subject:** A first-class way for a task brief to declare the **documents** an agent should read while working the task — a design spec, an API contract, a prior investigation, a data dictionary, a style guide. The design is a **deliberate hybrid with an opinionated default**: documents are *referenced by repo-relative path* (canonical, no copy, snapshot-pinned by blob sha), and *attachments* exist only as a narrow, visibly-heavier escape hatch for content that lives in no repo.
**Status:** Proposal. Follows the workflows spec (`2026-07-16-workflows.md`) conventions; where this and the code disagree, the code governs.
**Default:** No documents. A task without `docs:`/`attachments:` behaves **byte-identically to today** — the work order gains no keys, the prompt header gains no lines, materialization does nothing. Single-shot, `enhance`, `split`, `loop`, and `workflow` are untouched.
**Relationship:** Documents are the *operator-supplied* sibling of engine-produced **workflow artifacts** (`2026-07-16-workflows.md §5`). Both are read-only files materialized into run-scratch and named in the prompt header via the same machinery (`materialize_artifacts`, `_artifact_header`, the tasks-repo executor lane). This proposal reuses that rail wholesale rather than inventing a parallel one; the only divergence is custody and lifecycle.

---

## 0. The one idea

Nightshift is a **git-native** engine: tasks land commits in a target repo pinned at `base_ref`, doc steps already run read-only at that same `base_ref`, and operators already think in repo paths (`docs/spec/auth.md`, `ARCHITECTURE.md`). So the canonical document *already lives in the repo* — versioned, reviewed, drift-free. Copying it into the tasks repo would create a second copy that silently rots.

Therefore the **default and primary mechanism is a repo-relative path**:

```yaml
docs: [docs/spec/auth.md, ARCHITECTURE.md]
```

The manager resolves those against the target repo **at the task's pinned `base_ref`** and records the git **blob sha** of each as a snapshot pin; the worker reads the pinned blob from its own worktree (`git cat-file blob <sha>`, after the `base_ref` fetch it already does), materializes it read-only into run-scratch, and names it in the prompt header. Path content never rides the work order — text or binary, a path-doc is just a pinned blob the worker reads locally. No live re-read, no drift, one source of truth — and immutable execution because the pin is content-addressed.

Attachments — bytes stored *with* the task in the tasks repo — are the **escape hatch, not the peer**. They exist for the one case paths cannot serve: content that lives in no repo (a pasted investigation, an emailed spec, an external doc). They are a *visibly different* affordance (`attachments:`, a separate heavier UI action) so an operator never reaches for one by reflex. The mental model is one sentence:

> **By default your docs live in the repo — reference them by path. Attach only what has nowhere else to live.**

The engine never parses document content. It pins path resolution by blob sha, custodies attachment bytes, materializes both kinds identically, and routes them to the steps that declared them.

## 1. Frontmatter contract

Two operator keys — one primary, one escape hatch — plus one engine-owned pin.

| Field | Type | Owner | Meaning |
|---|---|---|---|
| `docs` | list of string **or** doc-spec object | operator | **Primary.** Repo-relative paths (relative to the target-repo worktree root) resolved and pinned at `base_ref`. String form is the dead-simple common case; object form adds per-doc options (§1.1). Default unset. |
| `attachments` | list of string (filenames) **or** doc-spec object | operator | **Escape hatch.** Task-local files whose bytes live in the tasks repo under `<task>.docs/` (§2.2). A different key with a different verb — you *attach* a file, you *reference* a path. Default unset. |
| `docs_pin` | map (engine-rendered) | **engine** | Snapshot pin (§4): per referenced path, the git blob sha of its content at first dispatch, so a path-doc's dispatch-time content is recoverable even if the branch moves. Written through the tasks-repo executor's engine-meta lane; added to `ENGINE_META_KEYS`, **not** `_EDITABLE_META_KEYS`; UI renders read-only. Absent until first dispatch. |

### 1.1 `docs` — string or object

95% of tasks want `docs: [path, path]` and should type exactly that. The object form covers the rare per-doc knob without a second key:

```yaml
# Common case — dead simple, the shape we nudge every operator toward:
docs:
  - docs/spec/auth.md
  - ARCHITECTURE.md

# Object form — only when you need a per-doc option:
docs:
  - docs/spec/auth.md                      # bare string still allowed in the same list
  - path: docs/very-large-runbook.md
    range: "1-120"                          # line range (§5 large-doc handling)
  - path: contracts/openapi.yaml
    as: "the API contract"                  # override the prompt-header label
    steps: [plan]                           # workflow: restrict to named steps (§7)
```

Normalization (parse time): a bare string `"docs/spec/auth.md"` becomes `{path: "docs/spec/auth.md"}`.

| Object field | Type | Default | Meaning |
|---|---|---|---|
| `path` | string | (required) | Repo-relative path, resolved at `base_ref`. Must normalize to inside the worktree root (§7). |
| `range` | string `"A-B"` | whole file | Materialize only lines A–B (inclusive, 1-based). Large-doc lever (§5). Recorded in the header so the agent knows it saw a slice. |
| `as` | string | derived from basename | Human label in the prompt header (`The <as> file is: <path>`). |
| `steps` | list of step ids | all steps | Workflow only: restrict this doc to the named steps (§7). Ignored on single-shot tasks. |

### 1.2 `attachments` — the escape hatch

```yaml
attachments:
  - investigation-2026-07-10.md            # bytes committed under <task>.docs/
  - path: pasted-error-log.txt
    as: "the failing run log"
```

**The two keys are intentionally asymmetric in ergonomics.** `docs` accepts the barest possible syntax because it is the one an operator types most. `attachments` requires you to have *first put a file into the tasks repo* (via the UI's explicit attach action, §6) — there is no way to "just type an attachment" the way you type a path. **That asymmetry is the nudge** — the friction gradient encodes "prefer paths" into the ergonomics, not just the prose.

### 1.3 Ownership split

`docs` and `attachments` are operator-owned and join `_EDITABLE_META_KEYS` (`task_files.py`). `docs_pin` is engine-owned and joins `ENGINE_META_KEYS` (extended from the string-only workflow cursor to a nested mapping — the `set_engine_meta` lane already round-trips YAML values). Operators author references; the engine owns the pin.

Compositions:

- `docs` / `attachments` + **any existing field** — orthogonal; no interaction with `model`, `after`, `split`, etc.
- `docs` / `attachments` + `workflow` — first-class: docs flow to steps per §7 (default: every step; opt-in narrowing via `steps:`). An operator points a `plan-review-implement` workflow at `docs: [docs/spec/auth.md]` and every step reads the canonical spec pinned to one sha.
- `docs` / `attachments` + `loop` / `evergreen` — supported; each iteration/cycle re-materializes the declared docs. Evergreen **clears `docs_pin`** each cycle so a janitor reads *this cycle's* docs (§4); attachments are **retained** across cycles.

## 2. Storage & custody

Two custody models, matching the two mechanisms — and the asymmetry is the point.

### 2.1 `docs` (paths) — *no custody*

The engine stores **nothing**. Bytes live where they already live: in the target repo, versioned in *that* repo's history. The only engine-owned datum is `docs_pin` (§4), which records blob shas — not content. There is exactly one copy of `docs/spec/auth.md` in the universe, and it is the repo's.

### 2.2 `attachments` — custody in the tasks repo, mirroring the artifacts convention

Attachment bytes live next to the brief under a **`<task>.docs/`** directory — deliberately parallel to the engine's `<task>.artifacts/` convention (workflows §5) but a *distinct* directory so operator-supplied docs and engine-produced artifacts never collide:

```
nightshift-tasks/<queue>/
  07.add-oauth.md                 # the brief
  07.add-oauth.docs/              # operator attachments (this proposal)
    investigation-2026-07-10.md
    pasted-error-log.txt
  07.add-oauth.artifacts/         # engine-produced workflow artifacts (existing)
    plan.md
```

- **Writes** go through the tasks-repo executor — the same serialized lane that commits frontmatter flags and workflow artifacts. `task_files.write_attachment` mirrors `write_artifact` (different dir suffix). One commit per attach/replace/remove.
- **Cap: the effective `document_cap_bytes`** (§5.1 — operator setting clamped to the `DOCUMENT_CAP_CEILING_BYTES` code constant; defaults to 256 KB, the current artifact-cap value). Over-cap attach is rejected at the API boundary with a typed `document_too_large` error (never silently truncated).
- **Text or small binary (§7 governs the split).** Text attachments (spec, note, log) are the common case. Small binary attachments — images mostly (`.png`, `.jpg`, `.gif`, `.webp`), also `.pdf` — are allowed and stored as opaque bytes with their real extension; the materialization rail is byte-agnostic (§3). The engine never parses either — it custodies bytes and hands the agent a read-only file; whether a given backend/model can *use* an image is the backend's concern (a vision-capable model reads it; a text-only one ignores it). `media_type` is sniffed (extension + content) and recorded so the header can annotate binaries and the worker can choose the right delivery path (§3.1).
- **Overwrite/versioning:** re-attaching a same-named file overwrites in place; the superseded version is ordinary tasks-repo git history — the artifact model exactly, no new versioning concept.
- **Lifecycle:** `<task>.docs/` is deleted **with the brief** on terminal consumption (land / split-harvest / operator delete), folded into the same commit as brief removal — the same fold `delete_task` / `drop_completed_task` already do for `<task>.artifacts/`. On an **evergreen cycle reset**, attachments are **retained** (unlike engine artifacts, which reset): an operator-attached doc is cycle-independent input, not per-cycle engine state. On quarantine, retained alongside the brief.

This keeps the tasks repo the single custody home for everything genuinely task-local, and — critically — makes attachments *feel* heavier than paths, because they are: you commit bytes, they travel with the task, they count against the cap.

## 3. Materialization & the work order

**One materialization path for all docs** — the divergence is *where the bytes come from* (path vs attachment), resolved manager-side, and *how they ride the wire* (embedded text vs `blob_ref` for binary), decided by `media_type`. The worker sees a uniform list of read-only files and does not care about kind or encoding. *(Grafts: the uniform materializer + invisible-kind header from Candidate 3; the `blob_ref` binary-delivery model from Candidate 5.)*

### 3.1 Resolution (manager-side, at dispatch)

`build_work_order` gains a `docs` resolution pass producing a `docs` config block. Delivery differs by kind, and — critically — **path-docs are never embedded** (text or binary): the worker already has the worktree at `base_ref`, so it reads them there.

- **`docs` (path) entries — resolve, pin, do not embed.** The manager resolves the path **against the target repo at `base_ref`** to capture its **blob sha** and `media`/`bytes`, records the sha into `docs_pin` (§4) on first resolution, and puts **only the pin** (`sha` + `path` + `media`) on the wire — no content, text or binary. The worker materializes from that sha out of its own object store (`git cat-file blob <sha>`), which it already has locally after the `base_ref` fetch it does anyway. This keeps the work order tiny regardless of doc size or type, and is why a path-doc is byte-type-agnostic: text and binary alike are just a pinned blob the worker reads from the worktree. `range:` (text only, §7) is recorded on the entry and applied by the worker after it reads the blob.
- **`attachments` entries — embedded or `blob_ref`.** Attachment bytes live in the tasks repo, which the worker also has, so the same "read locally" logic applies, with one convenience: small **text** attachments ride embedded (`text`) since they're already small and it saves a read; **binary** attachments ride as a `blob_ref` (the `<task>.docs/` path) read from the local tasks checkout, so a multi-MB image never inflates the work-order JSON (§5).

The resulting block (embedded in the existing config blob, mirroring `workflow.artifacts`):

```json
"docs": [
  { "name": "auth.md", "label": "the auth spec", "kind": "path", "media": "text",
    "path": "docs/spec/auth.md", "ref": "base_ref", "sha": "9f2c…" },
  { "name": "arch-diagram.png", "label": "the architecture diagram", "kind": "path",
    "media": "image/png", "path": "docs/arch-diagram.png", "ref": "base_ref", "sha": "b1d0…" },
  { "name": "investigation-2026-07-10.md", "label": "the investigation writeup",
    "kind": "attachment", "media": "text", "text": "…full text…" },
  { "name": "flow.png", "label": "the flow screenshot", "kind": "attachment",
    "media": "image/png", "bytes": 40211, "blob_ref": "07.add-oauth.docs/flow.png" }
]
```

A path-doc carries **no `text` and no bytes** — just its pinned `sha`. `kind` is informational (telemetry/UI); `media` drives delivery + header annotation; the worker treats path/attachment uniformly once bytes are in hand.

### 3.2 Worker materialization & prompt header

The worker calls `materialize_docs` (a `materialize_artifacts` sibling) to write each doc as a **read-only file in run-scratch, outside any worktree** — exactly how brief and artifacts are materialized. It differs from the text-only artifact materializer in three byte-agnostic ways: the scratch filename **preserves the real extension** (`…doc-<name>.png`, `…doc-<name>.pdf`, `…doc-<name>.md`); it sources bytes per the entry — a **path-doc** by `git cat-file blob <sha>` from the worktree's object store (text *and* binary, identically), an **attachment** from the embedded `text` or its `blob_ref` in the local tasks checkout; and it applies `range:` after reading a text path-doc. `chmod 0o444` on the result, as before. The prompt header names them, reusing `_artifact_header`; **kind is invisible** to the agent, but a binary is annotated so the agent opens it with the right tool:

```
Reference documents (read-only, read these before exploring):
  - The auth spec is: <scratch>/task-local-main-07.add-oauth.doc-auth.md
  - The architecture diagram is: <scratch>/…doc-arch-diagram.png   [image/png — open with an image-capable tool]
  - The investigation writeup is: <scratch>/…doc-investigation-2026-07-10.md
  - The flow screenshot is: <scratch>/…doc-flow.png   [image/png — open with an image-capable tool]
```

`nightshift-local.md` (and the workflow doc/code charters) gain one conditional sentence, mirroring the existing plan-charter language ("read the manifest files first"): *"When reference documents are listed, read them first; treat them as authoritative context for this task before exploring the repo."*

Because a path-doc is materialized from its **pinned blob sha** (`git cat-file blob <sha>`), the worker sees the exact pinned content even if its local branch is stale — the sha is content-addressed, and the worker fetches `base_ref` before it runs anyway (the same `prepare_worktree_base` step doc/code steps rely on), so the blob is reachable. The materialized file is a scratch copy outside the worktree, so an agent editing the repo (e.g. editing `docs/spec/auth.md` as part of the task) never mutates its own reference copy mid-run.

## 4. Cross-machine / recoverability / staleness — the snapshot pin

This is where a paths-default earns its keep *and* where it must not cut a corner. Three guarantees:

1. **Resolution pins to `base_ref`, always — via the blob sha, not by embedding.** The manager resolves a path against the canonical `base_ref` to capture its blob sha (§3.1), and the worker reads that sha from its own object store after the `base_ref` fetch it already performs — never from a possibly-dirty working tree, and never by shipping the bytes on the wire. A cross-machine worker whose local branch is behind still gets the exact pinned bytes because a content-addressed blob sha is reachable once `base_ref` is fetched. **Recoverable by construction:** a crashed/re-dispatched step reconstructs its doc inputs from tasks repo (`docs`/`attachments` frontmatter + `docs_pin` + `<task>.docs/`) + target repo at `base_ref`; nothing lives only in a live session, and nothing depends on the transient work-order payload carrying content.

2. **The blob-sha pin (`docs_pin`) survives branch movement.** On first dispatch the manager records the blob sha it resolved:

   ```yaml
   docs_pin:
     docs/spec/auth.md: 9f2c1ab
     ARCHITECTURE.md: 4d7e0c2
   ```

   The blob sha is content-addressed and immutable: as long as it is reachable in the target repo's object store, `git cat-file blob 9f2c1ab` returns the exact dispatch-time bytes regardless of where branches now point. Because Nightshift's land pipeline squashes onto `main` and never rewrites history, these blobs stay reachable. The manager re-materializes from the pinned sha on every subsequent dispatch/step of the same task, so **every step of a workflow sees byte-identical doc content** even if the doc changed on the branch between plan and implement.

3. **Snapshot-on-first-dispatch, not live-refresh.** Docs are pinned once, at first dispatch, held for the life of the task — the deliberate choice for determinism: a plan step and the implement step that consumes it must read the *same* spec. Evergreen resets clear `docs_pin` so each cycle re-pins to the then-current `base_ref` (a janitor reads today's docs).

**Fallback when a pinned blob is unreachable** (shouldn't happen given no-history-rewrite, but defined): the manager retries resolution at the current `base_ref` by path; on success it re-pins (logging `docs_pin_refreshed`); on failure (path also gone) the task goes **blocked** with `referenced doc '<path>' not found at base_ref` (§7). Blocked, not failed, because it is an operator/authoring condition, not a retryable run error.

## 5. Size / token discipline

**A hard per-document byte cap is the design, not a limitation.** Two independent reasons make a cap correct rather than a shortfall: (1) attachment bytes live in the `nightshift-tasks` git repo, which is **not** LFS-backed, so GitHub's own non-LFS blob ceiling is the real physical bound — the engine must stay well under it; and (2) a task document is meant for **small-to-medium** files (a spec, a contract, an investigation note), which is exactly the regime where embedding the whole file in the prompt is both affordable and the right ergonomics. Files that don't fit that regime want a `range:` slice, not a raw dump. So the cap is a feature, and the only real requirement is that it be *operator-tunable within a code-defined ceiling* rather than a magic literal.

### 5.1 The document cap — configurable setting under a code-constant ceiling

- **Ceiling — a code constant, not a hard-coded check.** `DOCUMENT_CAP_CEILING_BYTES` (a module constant, sited next to the existing `_DOCUMENT_MAX_BYTES = 256 * 1024` in `manager/api_worker.py`) is the absolute maximum the engine will *ever* store or embed, chosen to stay comfortably under the GitHub non-LFS blob limit. No cap setting may exceed it; the setting is clamped to it on load. The check site references the constant, never a literal.
- **Setting — operator-tunable, starts below the ceiling.** `document_cap_bytes` is a new operator setting (manager `manager.json`, queue-overridable, env `NIGHTSHIFT_DOCUMENT_CAP_BYTES`) with a **default well below the ceiling** (e.g. 256 KB — the current artifact-cap value, a safe conservative start) so an operator can *raise* it toward the ceiling as their corpus warrants, or lower it to keep prompts lean. Loaded via the settings registry exactly like `diff_cap_lines`; surfaced in the Settings UI. `min(document_cap_bytes, DOCUMENT_CAP_CEILING_BYTES)` is the effective cap.
- **Applies to both kinds, enforced at the earliest possible point.** Attachments are rejected at *attach time* (the API boundary) with a typed `document_too_large` error; path-docs are rejected at *resolution time* in `build_work_order`. A path-doc over the effective cap does **not** silently truncate — resolution fails and the task goes **blocked** with `referenced doc '<path>' exceeds the document cap (<N> KB > <cap> KB) — declare a range or raise document_cap_bytes`. The message names both the actual size and the current cap so the operator's two remedies (slice it, or raise the setting) are obvious. The cap is a byte cap, so it bounds images and PDFs directly (an image has no `range:` escape — it fits under the cap or it doesn't).
- **Allowed media types — a bounded allow-list.** `allowed_doc_media_types` (operator setting, default `["text/*", "image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"]`) governs which sniffed types the engine will store/deliver. Anything else is rejected with `unsupported document type '<media_type>'`. Text is always allowed; the binary entries are the deliberately small set worth carrying (images "mostly", per the small-binary intent). Widening the list is an operator choice; the byte cap still bounds each entry.

### 5.2 The other levers

- **`range: "A-B"`** materializes only the declared span; the header notes it (`The runbook (lines 1–120) is: …`). The primary large-doc lever, and the reason path-docs beat "just paste it into the brief" — a 4 MB runbook is referenced by its relevant 120 lines, never stored or embedded whole.
- **Total-docs soft budget.** A queue-config `docs_char_budget` (default ~200_000) caps the summed materialized doc text per work order; exceeding it is a blocked authoring error listing the offenders. Prevents attaching a whole `docs/` tree into one task even when each file is individually under the per-doc cap.
- **Per-step selection (workflow).** A doc's `steps: [plan]` restricts it to named steps, so a big API contract reaches the planner without bloating every implement retry. Default (no `steps:`) = every step (the simple mental model); narrowing is opt-in for cost. This is the workflows §8.3 lever ("a step receives only its declared inputs") applied to operator docs.
- **Cacheability.** Docs append to the task-varying tail of the prompt (after the byte-stable charter), preserving the prefix-cache discipline (workflows §8.2). Pinning means doc bytes are identical across a workflow's steps, so cross-step cache hits are possible.

**On "dynamic access to an over-cap doc" (the arena's flagged gap):** this is intentionally *not* solved and does not need to be. The engine never parses content (invariant), so it offers no agent-side paging/search *into a stored blob* — and it shouldn't, because the answer for a file too big to embed is `range:` (embed the relevant span) or, for a path-doc, the agent is already *in the worktree* and can open the file directly with its own tools. The cap plus `range:` plus the total budget fully cover the small-to-medium regime this feature targets; anything past the ceiling is out of scope by construction, not by omission.

## 6. UI

The UI is where the nudge lives. **Adding a path is one keystroke-cheap field; attaching a file is a distinct, heavier action.**

**Create panel:**
- A **"Reference docs"** field with **repo-path autocomplete** — as the operator types, it completes against the target repo's tree at `base_ref` (a `GET /api/repos/{repo}/paths?prefix=` endpoint the manager backs from `git ls-tree`). Enter adds a chip. Prominent, next to the model/priority controls. Invalid paths (absent at `base_ref`) flag inline in amber *before* the task is created.
- A visually secondary **"Attach task-local file"** control — a small "＋ Attach" button opening a file-picker / paste-box captioned *"Only for content that isn't in any repo."* The friction (a modal, a steering caption) is intentional.

**Detail pane:**
- A **Documents** section listing both kinds with distinct iconography: a **link/branch icon** for paths (showing `path @ pinned-sha`, click to view the pinned content rendered read-only; a **"re-pin to current base_ref"** affordance) and a **paperclip icon** for attachments (view rendered content, replace, remove).
- Path chips that fail resolution show the blocked reason inline.
- Removing a path edits `docs:` frontmatter; removing an attachment edits `attachments:` *and* deletes the file from `<task>.docs/` (through the executor).
- `docs_pin` renders read-only (engine-owned).
- **Drift indicator (graft: Candidate 5's `superseded` badge).** When a path-doc's pinned sha differs from the doc's content at the current `base_ref`, the chip shows an amber **"source drifted — pinned to older version"** badge with a one-click re-pin. This turns snapshot staleness from a silent fact into an auditable, deliberate operator choice — the piece a pure re-pin action was missing.

## 7. Edge cases

| Case | Behavior |
|---|---|
| **Missing doc (path not at `base_ref`)** | Task **blocked**, reason `referenced doc '<path>' not found at base_ref`. Never a silent skip (an agent working without a spec it was told exists is worse than a stopped task). Surfaces inline in the detail pane. |
| **Doc renamed/deleted in repo after pin** | The pinned **blob sha** still resolves via `git cat-file` regardless of path (content-addressed), so a pinned task keeps working. Only *first* resolution needs the path to exist. Re-pin (operator action) against the new path if the operator wants the moved doc. |
| **Binary / non-markdown** | Supported for both kinds (small images + PDF; §2.2). The engine never parses content — it sniffs `media_type`, stores/reads bytes, materializes a read-only file with the real extension, and annotates it in the header (`[image/png — open with an image-capable tool]`). A text-only backend simply ignores an image doc; a vision-capable one reads it. `range:` and line-count semantics don't apply to binary (§7 total-budget still does). A doc whose sniffed type is neither text nor an allowed binary (`allowed_doc_media_types`, §5.1) is rejected at attach/resolution with `unsupported document type '<media_type>'`. |
| **Huge doc** | Over the effective `document_cap_bytes` → blocked with the "declare a range or raise document_cap_bytes" message (§5.1); `range:` or raising the setting (up to the ceiling) is the remedy. Never silent truncation. |
| **Doc shared by many tasks** | The *celebrated* case for paths: N tasks each say `docs: [docs/spec/auth.md]` and each pins its own blob sha at its own dispatch time — zero duplication of bytes in the tasks repo, each task deterministic. This is precisely what attachments-default would fumble (N copies). |
| **Doc changes between dispatch and land** | The pin holds dispatch-time content for the whole task/workflow, so plan and implement agree. If the operator wants the new content mid-flight, they re-pin. Evergreen re-pins each cycle. |
| **Workflow: which steps see which docs** | Default: **every step** sees every declared doc. Opt-in narrowing via a doc's `steps: [ids]`. A doc is materialized into a step's work order only if the step is in its `steps` set (or the set is empty). Orthogonal to workflow `inputs:` (which route *artifacts*); docs and artifacts are separate header sections. |
| **Decomposition — do children inherit docs?** | **`docs` (paths): yes** — the manager copies parent `docs:` into each child's frontmatter at harvest, re-pinning each child to its own dispatch `base_ref` (children run in the same repo, so paths still resolve). **`attachments`: yes, by snapshot-per-child (graft: Candidate 5's rule).** `harvest_split_output` copies the parent's `<task>.docs/<name>` bytes into each child's `<child>.docs/<name>` and writes the `attachments:` entry, so children are self-contained and recoverable after the parent is consumed — closing Candidate 4's punt (which left children silently missing pasted context). Inheritance is opt-out per child in the split charter. |
| **Path escapes the repo (`../`, absolute)** | Rejected at parse/resolution (normalize-then-`startswith` guard, the same check `worktree_dir` uses): blocked `referenced doc '<path>' is outside the repo`. |

## 8. Touch points (implementation checklist)

Mirrors the workflows spec §12 style.

- **`task_files.py`** —
  - `read_attachments(...)` / `write_attachment(...)` / `delete_attachment(...)` / `attachments_dir(...)` — siblings of the `artifacts_*` family, `<task>.docs/` suffix (real extension preserved for binary), one commit each, effective-`document_cap_bytes` + `allowed_doc_media_types` guards (§5.1); `read_attachments` returns bytes (text decoded / binary raw) — no longer text-only. A `sniff_media_type` helper (extension + content).
  - `materialize_docs(...)` — thin `materialize_artifacts` sibling (scratch prefix `…doc-<name>.md`, `chmod 0o444`), or reuse with a prefix arg.
  - `delete_task` / `drop_completed_task` — fold `<task>.docs/` removal into the brief-removal commit; **retain** on evergreen reset.
  - `harvest_split_output` — copy parent `docs:` into children (re-pinned); copy parent `<task>.docs/` bytes into each child's `<child>.docs/` (snapshot-per-child, §7).
  - `_EDITABLE_META_KEYS` += `docs`, `attachments`; `ENGINE_META_KEYS` += `docs_pin` (nested-mapping extension of the string-only cursor).
- **`config/` (frontmatter parse)** — normalize `docs` (string → `{path}`), parse object form (`path`/`range`/`as`/`steps`), parse `attachments`; validate repo-relative + traversal guard.
- **`config/manager.py` (settings registry)** — new `document_cap_bytes` operator setting (default 256 KB = `256 * 1024`, category "Worker execution policy", `env=NIGHTSHIFT_DOCUMENT_CAP_BYTES`), loaded/serialized exactly like `diff_cap_lines`; queue `config.json` override (documented, no schema change). Effective cap = `min(document_cap_bytes, DOCUMENT_CAP_CEILING_BYTES)`.
- **`manager/api_worker.py`** — add the `DOCUMENT_CAP_CEILING_BYTES` module constant beside the existing `_DOCUMENT_MAX_BYTES`, chosen under the GitHub non-LFS blob limit; the doc-cap check references the effective cap (setting clamped to the ceiling), never a literal.
- **`manager/work_orders.py`** — `build_work_order` docs-resolution pass (§3.1): for path-docs, resolve the blob **sha** at `base_ref` (`git rev-parse <base_ref>:<path>`) + sniff `media`/`bytes`, record/read `docs_pin`, and emit **pin-only** entries (`{name,label,kind:"path",media,path,ref,sha}`) — **no content on the wire**; for attachments, emit embedded `text` (small text) or `blob_ref` (binary/large); apply `range` (worker-side, text); enforce per-doc cap + `allowed_doc_media_types` + total budget; blocked-reason returns for missing/oversized/unsupported-type/escaping.
- **`prompts.py`** — Reference-documents header block (label + scratch path, range note for text, `[<media_type> — open with a …-capable tool]` annotation for binary, invisible path/attachment kind); conditional "read reference docs first" paragraph in `nightshift-local.md` and the workflow doc/code charters.
- **`worker/execute.py` + `task_files.materialize_docs`** — after brief/artifact materialization, materialize each doc read-only outside the worktree with its **real extension**: a path-doc via `git cat-file blob <sha>` from the worktree object store (text + binary, identically), an attachment via embedded `text` or `blob_ref` from the local tasks checkout; apply `range:` after reading a text path-doc. Wire for single-shot, code, and doc/split steps; pass `{label: path}` (+ media annotations) to the header builder.
- **`transitions.py`** — evergreen reset effect clears `docs_pin`, retains `<task>.docs/` + `docs`/`attachments` frontmatter.
- **`manager/api_operator.py`** — `POST/PUT/DELETE /api/tasks/{task}/attachments` (executor lane, effective-`document_cap_bytes` + `allowed_doc_media_types` guards; multipart accepts text + allowed binary); `GET /api/repos/{repo}/paths?prefix=` (create-panel autocomplete at `base_ref`); `POST /api/tasks/{task}/docs/repin` (clear/refresh `docs_pin`, single or all); `GET /api/tasks/{task}/docs` (list + rendered view, drift flags).
- **`manager/scheduler.py`** — none (docs don't affect capability matching; the candidate's existing `base_ref` is reused for pinning).
- **`assets/ui/app.js`** — create-panel Reference-docs autocomplete field + secondary Attach control; detail-pane Documents section (path vs attachment iconography, view/re-pin/remove, `superseded` drift badge); read-only `docs_pin`.
- **`docs/user/configuration-reference.md`** — `docs`, `attachments` (operator), `docs_pin` (engine) frontmatter rows; the `document_cap_bytes` manager setting (+ env + queue override) and the `DOCUMENT_CAP_CEILING_BYTES` ceiling note; `allowed_doc_media_types`; the `docs_char_budget` queue key; the pinning/recoverability model; the note that path-docs (text + binary) are delivered by pinned sha and never embedded.
- **Tests** — string/object `docs` normalization; path resolution at `base_ref` (not dirty tree); **path-docs are pin-only on the wire (no `text`/bytes) and materialize via `git cat-file blob <sha>`**; pin recorded on first dispatch and reused across workflow steps (byte-identical); pin blob recoverable after branch moves / doc renamed / stale local branch (recoverable once `base_ref` fetched); **binary round-trips both kinds** (path via sha, attachment via `blob_ref`) — materialized read-only with the real extension, header carries the media annotation, engine never decodes it; `allowed_doc_media_types` rejects an unsupported type; missing/oversized/unsupported-type/escaping → blocked with correct reason; a doc over the effective cap → blocked (path) / rejected (attach) naming size + cap; `document_cap_bytes` clamped to `DOCUMENT_CAP_CEILING_BYTES` when set above it; raising the setting admits a previously-over-cap doc; attachment attach/replace/remove commits + guards; `<task>.docs/` deleted with brief, retained on evergreen reset; per-step `steps:` selection; `range:` slice + header note (text only); total budget block (counts binary bytes); decomposition inherits `docs` (re-pinned) **and** attachments (snapshot-per-child, binary bytes copied); `superseded` drift flag surfaces when the pinned sha diverges from HEAD; a task with neither field produces a byte-identical work order to today (regression guard).

## 9. Implementation order

1. `config`/`task_files` parse + normalization + `_EDITABLE_META_KEYS`/`ENGINE_META_KEYS` + `materialize_docs` + attachment CRUD (pure/near-pure) + tests.
2. `manager/work_orders.py` resolution pass + `docs_pin` recording + budgets + blocked-reason returns + tests.
3. `prompts.py` header + `worker/execute.py` wiring (single-shot, code, doc/split) + tests.
4. `transitions.py` evergreen reset + `harvest_split_output` inheritance (paths + snapshot-per-child) + tests.
5. Operator API (attach CRUD, path autocomplete, repin) + `app.js` UI (autocomplete, Documents section, drift badge).
6. Config key + docs.

Steps 1–3 are the functional core; the no-`docs:` path is byte-identical throughout.

## 10. Non-goals

- **Live path resolution without a pin.** A workflow's plan and implement steps must read the same spec; live re-read breaks determinism and recoverability. The blob-sha pin gives canonical-by-path authoring with immutable-by-sha execution.
- **A cross-task content-addressed attachment store with refcounts.** Sharing is served *for free* by `docs:` (the right home for anything worth sharing). One-off pastes are one-off by definition; deduping them adds a GC/refcount problem the `docs:` half already dissolves. (Git's own blob delta-compression recovers most storage anyway.)
- **Automatic refresh / watch-the-file.** Re-pin is always an explicit operator act — a durable batch engine must not silently change a task's inputs.
- **Binary/vision documents (v1).** Text-only for both kinds in v1; the header/materialize rail is designed so a `blob_ref`-delivered binary (Candidate 5's model) is a clean additive follow-up when a vision-capable backend justifies it.
- **Engine-side chunking / summarization / embedding / agent-side paging into an over-cap doc.** The engine never parses content; size is disciplined by the configurable per-doc cap (bounded by a code-constant ceiling under GitHub's non-LFS limit), `range:`, the total budget, and per-step selection. A file too large for the small-to-medium regime this feature targets is served by a `range:` slice (or read directly from the worktree by the agent, for path-docs) — not by the engine paging a stored blob. This is scope by construction, not an omission (§5).
- **A fully-symmetric unified `references:` field.** Rejected: symmetry hides the one decision that matters (canonical-in-repo vs task-local) behind uniform syntax, so operators drift toward whichever is syntactically nearest — and the nearest thing to "paste" is an attachment, re-creating the drift/dup problem by ergonomic accident. Materialization stays symmetric (the worker treats both uniformly — that symmetry is free); *authoring and custody* stay asymmetric (that asymmetry is the recommendation, encoded).
