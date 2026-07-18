# Nightshift — Workflow editor (visual definition authoring in the operator UI)

**Subject:** A visual editor in the operator UI for creating and editing workflow definitions — the step cards, edges, roles, budgets, and prompts of `docs/spec/2026-07-16-workflows.md` — writing the same `.nightshift/workflows/*.json` files an operator could author by hand, with the engine's own validator as the live source of truth.
**Status:** Implemented. Where this doc and the code disagree, the code governs and this doc should be updated.
**Default:** Nothing changes for operators who never open the editor. Definitions remain plain JSON files; hand-editing them stays fully supported (the editor is a view over the files, not a replacement custody model). Shipped definitions remain read-only package assets.
**Relationship:** Builds on the implemented workflows spec (`2026-07-16-workflows.md`), which listed "a workflow editor" as a v1 non-goal and deliberately kept the vocabulary small "so every workflow stays inspectable and a future editor stays tractable" (§3.1). This spec is that editor. It adds **no vocabulary** — no new step kinds, fields, or routing semantics. It makes exactly one engine-semantics change (prompt custody moves manager-side, §4), which is required for the editor to author new doc steps and stands on its own merits.

---

## 0. The one idea

The workflow vocabulary is three step kinds, `next`, `signals`, and `max_visits` — a state machine small enough that a structured form can express *all* of it. So the editor is not a free-form canvas; it is **an ordered list of step cards with a live graph preview**, where every edge is chosen from a closed set (existing step ids + `$end`) via a picker, never drawn by hand. Validation is never reimplemented in the browser: every edit round-trips through the engine's own `parse_workflow` on the manager, so the editor can never save a definition the loader would reject, and the two can never drift.

Files stay the source of truth. The editor reads and writes `.nightshift/workflows/<name>.json` (and `.nightshift/prompts/<name>.md`) through a small CRUD API; the JSON on disk is canonical, diff-able, and remains hand-editable. An operator who keeps `.nightshift/` in git gets versioning for free — the editor adds none.

## 1. Motivation

Workflows shipped with exactly one authoring path: write JSON by hand, restart the manager, and learn about your first mistake from a startup crash (`load_workflows` fail-loud). Three compounding frictions:

- **No feedback loop.** Validation runs once at manager start and raises on the *first* error. Authoring a five-step workflow with a back-edge means several restart cycles.
- **Prompts are sealed.** A doc step's `prompt` names a shipped asset under the package's `assets/prompts/` — resolved on the *worker* via `_paths.asset()`. Operators cannot supply their own prompt, so a custom definition can only recombine the five shipped charters. That caps custom workflows at rearrangements of plan-review-implement.
- **The graph is invisible.** `next` defaults, signal edges, and cycle/`max_visits` interactions are easy to get wrong in raw JSON and impossible to see. The queue badge renders the step path *after* a task is running — nothing renders it at authoring time.

The editor closes all three: live validation, operator prompt custody, and a rendered graph while you type.

## 2. Today's state (what this spec changes)

- `load_workflows(workspace)` merges shipped `assets/workflows/*.json` with operator `.nightshift/workflows/*.json` (operator shadows shipped) — loaded **once** at manager startup into `app.state.workflows`; no reload path exists.
- `parse_workflow` raises `WorkflowError` on the first violation; there is no collect-all-errors mode and no way to validate without loading.
- Prompt bodies are read on the worker: `prompts.build_doc_prompt` calls `asset("prompts", prompt_asset)`. A remote worker has no view of the manager's `.nightshift/`, so operator prompts are structurally impossible today. Definition validation does not check that `prompt` names an existing asset — a typo surfaces as a worker-side read error at dispatch.
- The operator API is read-only: `GET /api/workflows` returns `{name: [ordered step ids]}` (create-panel picker + queue badge); `GET /api/tasks/<id>/artifacts` reads committed artifacts. No write, validate, or delete endpoints.
- The UI (`assets/ui/app.js`, framework-free vanilla JS, no build step) has a definition *picker* in the create panel and a step badge on queue rows — no authoring surface.

## 3. Definition custody and API

All endpoints operate on **operator files only** (`<workspace>/.nightshift/workflows/`). Shipped definitions are immutable package assets, exposed read-only with a duplicate-to-edit affordance (§5). Writing an operator file with a shipped definition's name shadows it — the existing `load_workflows` semantics, now surfaced deliberately in the UI with a "shadows shipped definition" warning rather than left as a foot-gun.

| Endpoint | Semantics |
|---|---|
| `GET /api/workflows` | **Extended in place** (both existing consumers — picker and badge — are ours to update): `{name: {steps: [ids], source: "shipped"\|"operator", shadows_shipped: bool}}`. |
| `GET /api/workflows/{name}` | The full raw definition JSON + provenance. For a shadowed name, returns the operator version and includes the shipped original under `shipped_definition` (the UI's "diff against shipped" view). |
| `POST /api/workflows/validate` | Dry-run: body is a candidate definition dict; runs `parse_workflow` **plus the endpoint-layer checks below**; returns `{ok: true}` or `{ok: false, error: "…"}`. Never touches disk. This is the editor's debounced live-validation call. |
| `PUT /api/workflows/{name}` | Validate (same path as above); on success write `.nightshift/workflows/<name>.json` canonically formatted (`json.dumps(indent=2)` + trailing newline — hand edits and editor edits produce identical diffs), then hot-reload (§3.1). `name` in the body must equal the path segment. |
| `DELETE /api/workflows/{name}` | Remove the operator file; hot-reload. Deleting a shadow *restores* the shipped definition — the UI says so. Refuses (404) for shipped-only names. |

**Endpoint-layer checks**, on top of `parse_workflow` (which stays pure and file-agnostic): every doc step's `prompt` must name an existing prompt — shipped or operator (§4). This closes today's unvalidated-prompt-reference gap for editor saves *and* is added to `load_workflows`' startup path for hand-authored files, where it fails loud like every other definition error.

Validation stays **first-error** in v1. `parse_workflow` raises on the first violation; the editor surfaces that one error and the operator fixes it — with a ~300 ms debounce the loop is tight enough that collect-all-errors is a nicety, not a need. Restructuring `_validate` to accumulate is noted as a follow-up, not done here.

Concurrent-edit protection is a **non-goal** (§7): Nightshift is a single-operator tool; last write wins.

### 3.1 Hot reload

After every successful `PUT`/`DELETE`, the handler re-runs `load_workflows(workspace)` and assigns `app.state.workflows` — one atomic reference swap on the event loop, no locking. Every consumer already reads `app.state.workflows` per-request (`make_resolver` is constructed per poll in `api_worker`; `build_work_order` takes the dict as an argument), so the next dispatch simply sees the new graph. No restart, no cache invalidation.

If the merged reload fails (a *different* operator file on disk is broken — hand-edited badly since startup), the endpoint returns that error and **keeps the previous in-memory set**: a bad file on disk must not take down dispatch that was working a moment ago. This makes the in-memory set strictly last-known-good; startup remains fail-loud as today.

## 4. Prompt custody (the one engine change)

Operator prompts live in `<workspace>/.nightshift/prompts/<name>.md`, shadowing shipped `assets/prompts/` by filename — the exact convention definitions already use.

The delivery problem: prompts are currently read on the **worker** from package assets, and remote workers cannot see the manager's `.nightshift/`. The fix follows the pattern the workflows spec already established for artifacts and briefs — *context rides the work order*:

- `build_work_order` resolves the doc step's prompt **manager-side** (operator file first, shipped asset fallback) and embeds the body in the workflow block as `prompt_text`, alongside the existing `prompt` name (kept for telemetry/`result_line` readability).
- `worker/execute.py` / `build_doc_prompt` prefer `prompt_text` when present and fall back to the `asset()` read when absent — wire-compatible with orders from an older manager during a rolling upgrade.
- Size is a non-issue (charters are a few KB; artifacts already ride the same blob at up to 256 KB each).
- Cacheability is untouched: the prompt body is byte-identical across runs of the same step whichever side reads it, and `build_doc_prompt`'s header-then-charter layout doesn't change (spec §8.2 discipline holds — the editor UI reminds prompt authors of the rule: no task-varying content in the charter body).

Prompt API, mirroring definitions:

| Endpoint | Semantics |
|---|---|
| `GET /api/workflow-prompts` | `{name: {source, shadows_shipped}}` for shipped + operator prompts. Feeds the step card's prompt picker. |
| `GET /api/workflow-prompts/{name}` | The markdown body + provenance (+ `shipped_body` when shadowing). |
| `PUT /api/workflow-prompts/{name}` | Write `.nightshift/prompts/<name>.md`. No structural validation — a prompt is prose; the only check is non-empty. |
| `DELETE /api/workflow-prompts/{name}` | Operator files only; refuses if any *loaded* definition references the name (definitions are the dependents; a dangling reference would fail §3's check on next reload anyway — refuse early instead). |

Prompt edits need no reload machinery: nothing caches prompt bodies; the next `build_work_order` reads the file.

## 5. The editor UI

A full-screen panel (same shell as the analytics view) reached from two places: a **Workflows** entry in the header nav, and an *edit/new* affordance beside the create panel's definition picker. Vanilla JS + SVG, no framework, no build step, no third-party graph library — consistent with the existing UI's constraints.

**Layout: step cards left, live graph right.**

- **Left pane — the ordered step list.** One card per step, drag-to-reorder (list order matters: it defines default `next` chaining and the inputs-from-earlier-steps rule). Each card:
  - `id` (text), `kind` (segmented `doc | code | split`), `role` (combo: `planner`/`implementor` suggested, free text allowed — §3.2's "any other key" is legal vocabulary).
  - Kind-dependent fields, shown/hidden live: doc → prompt picker (from `GET /api/workflow-prompts`, with an inline "edit prompt…" opening the prompt editor, §5.1) + `output` name; code/split → neither (the vocabulary forbids them).
  - `inputs`: checkboxes over the closed set {`brief`} ∪ {outputs of *earlier* cards} — recomputed on reorder, so the earlier-step rule is unviolatable by construction rather than caught by validation.
  - `max_turns`: tri-state control `inherit | unbounded | n` mapping to absent / `null` / int — the JSON's absent-vs-null distinction made explicit instead of invisible.
  - `signals`: rows of token → destination picker; `next`: destination picker. **Every destination is picked from step ids + `$end` — edges are never drawn by hand.** This is the deliberate anti-canvas decision: destinations form a small closed set, so drag-to-connect buys zero expressiveness and costs the entire hit-testing/routing/z-order complexity budget of a canvas editor.
  - `max_visits`: numeric, auto-surfaced (highlighted, required) when the live graph detects the step is on a cycle — the validator's rule, shown before the validator has to say it.
- **Right pane — the graph, render-only.** An SVG of the definition as it stands: nodes as the step chips the queue badge already uses (doc/code/split styling), solid arrows for `next`, dashed labeled arrows for signal edges, a distinct `$end` terminal node. Cycles get a visual treatment (the back-edge curved and badged with the target's `max_visits`). Layout is layered left-to-right following list order — with a hard vocabulary cap of a handful of steps, naive layered layout is sufficient; no layout engine. Clicking a node scrolls/focuses its card. The graph is a *view*, never an input surface.
- **Validation strip.** Every edit (debounced ~300 ms) POSTs the candidate to `/api/workflows/validate`; the current error (or a green check) renders in a strip above Save, and the offending card is outlined when the error names a step id. Save is disabled while invalid or while validation is in flight. The browser duplicates **no** validation rules — the two constructive exceptions above (inputs checkboxes, cycle-surfaced `max_visits`) prevent errors rather than judge them.
- **Raw JSON toggle.** A two-way JSON view of the same definition (edit either representation; parse errors in the raw view block switching back). Keeps the file honest as the source of truth and gives hand-editors a migration path in.
- **Provenance and lifecycle.** Shipped definitions open read-only with a **Duplicate** action (prefilled copy, name required to change). Saving an operator definition under a shipped name shows the shadow warning; the list view marks shadows and offers "view diff against shipped" / "delete (restores shipped)". Deleting a definition warns with the count of queued/blocked tasks whose frontmatter references it (the UI already holds the task list; the check is client-side) — deletion is allowed anyway, and those tasks take the existing `unknown workflow` blocked path.

### 5.1 The prompt editor

A plain markdown editor (textarea + the UI's existing rendered-markdown viewer side by side), opened from a step card or the prompts list. Shipped prompts are read-only with Duplicate, same as definitions. A static callout restates the charter discipline: byte-stable body, no task-varying content — the header injects paths and variables (spec §8.2). No structural validation beyond non-empty; prompt quality is prose quality.

## 6. Edit semantics for in-flight tasks

Editing a definition while tasks run under it must be safe, not clever:

- **In-flight attempts are unaffected.** The attempt row snapshots the step's routing block at dispatch (workflows Phase 5) and the work order embeds the step's prompt and artifacts; a running attempt completes entirely under the definition it was dispatched with.
- **Edits apply from the next cursor move.** Dispatch and submit resolve against `app.state.workflows` at that moment. A task mid-workflow picks up the edited graph the next time the scheduler or submit path consults it.
- **A cursor on a removed/renamed step blocks the task** — the existing resolver path already returns `workflow '<name>' has no step '<id>'` and the task goes blocked with that reason. The operator remedy is to restore the step (or delete/re-create the task); the editor's delete-step interaction warns when queued tasks' `workflow_step` frontmatter currently names that step, same client-side check as definition deletion.
- **No migration machinery.** Visit counters keyed to renamed steps simply reset (a renamed step is a new step; its count starts fresh on next entry). Documenting this beats building rename-tracking.

## 7. Non-goals

- **A drag-to-connect canvas.** Destinations are a closed picker set (§5); the graph stays render-only. Revisit only if the vocabulary ever grows past what forms express.
- **Versioning, history, rollback.** `.nightshift/` is the operator's tree; keeping it in git is their one-line choice. The editor writes canonical JSON precisely so those diffs read well.
- **Concurrent-editor conflict handling.** Single-operator tool; last write wins. No etags, no locks.
- **Workflow simulation / dry-run execution.** The validate endpoint checks structure; whether a workflow *works* is discovered by running it on a scratch task, as today.
- **Editing shipped assets in place, or a template gallery/marketplace.** Shipped definitions and prompts stay immutable; Duplicate is the entire template story.
- **New vocabulary.** No conditional edges, no per-step env, no non-markdown prompts. The editor renders §3.1 exactly.

## 8. Touch points (implementation checklist)

- `workflows.py` — startup prompt-reference check (a `prompt_exists` callable threaded into `load_workflows`, or a post-load pass in `manager/app.py`; keep `parse_workflow` pure).
- `manager/api_operator.py` — definition CRUD + validate endpoints, prompt CRUD endpoints, hot-reload with last-known-good retention (§3.1), extended `GET /api/workflows` payload.
- `manager/work_orders.py` — manager-side prompt resolution, `prompt_text` in the workflow block.
- `worker/execute.py` + `prompts.py` — prefer `prompt_text`, `asset()` fallback.
- `assets/ui/app.js` + `style.css` (or a new `workflow-editor.js` beside `analytics.js` — the UI already splits panels into files) — editor panel, step cards, SVG graph, prompt editor, picker/badge updates for the extended `GET /api/workflows` shape.
- `docs/user/` — authoring guide: the file conventions, shadowing, the charter discipline for custom prompts.
- Flip `2026-07-16-workflows.md` §11's editor line to point here.

## 9. Tests

- API: PUT round-trip (write → reload → `GET /api/workflows` reflects it → dispatch resolves it); invalid definition → 4xx with the `WorkflowError` message, disk untouched, in-memory set unchanged; PUT with a broken *sibling* file on disk keeps last-known-good; DELETE restores shipped shadow; name/path mismatch rejected; prompt-reference check (unknown prompt → validation error; operator prompt satisfies it); prompt DELETE refused while referenced.
- Prompt custody: work order embeds `prompt_text` (operator file wins over shipped); worker uses embedded text; worker falls back to `asset()` when absent; operator prompt reaches a doc-step run end-to-end.
- Startup: hand-authored operator file with a bad prompt reference fails loud, matching definition-error behavior.
- Edit-during-flight: cursor on a step removed by an edit → task blocked with the has-no-step reason; in-flight attempt under the old snapshot submits cleanly.
- UI (manual, per house practice): author a new two-doc-step + code-step workflow with a signal edge and a back-edge (editor must demand `max_visits`), a custom prompt, save, create a task with it, watch it run.

## 10. Implementation order

1. Prompt custody (`work_orders.py` embedding + worker fallback + startup reference check) — the standalone engine change; useful even editor-less (hand-authored operator prompts start working).
2. Definition + prompt CRUD/validate API with hot reload.
3. Editor UI: step cards + validation strip + raw JSON toggle (usable milestone without the graph).
4. Graph preview SVG; prompt editor panel.
5. Docs + spec status flip.
