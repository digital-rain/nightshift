# Nightshift — Settings Editor, Part 3: Multi-Tier Settings UI

**Subject:** Replace the small flat Settings modal with a **full-page, multi-tier settings editor** (VS Code / operator-console style: tier + category sidebar → grouped, typed fields → sticky Save bar) driven entirely by the [Part 2](settings-2-admin-api.md) API, and add a matching Settings page to the minimal worker SPA.

**Status:** Proposed — design for unimplemented work. Where this doc and the code disagree once implemented, the code governs and this doc should be updated.

**Series:** Part **3 of 3**. Depends on Parts 1–2. Adds **no new config knowledge** — it renders whatever the registry/API returns.

**Primary sources (to change):** `src/nightshift/assets/ui/index.html` (replace the `#settings-modal` with a full-page `view="settings"`), `src/nightshift/assets/ui/app.js` (the gear menu already routes `settings`; change `openSettings()` to switch to the settings view; new schema-driven renderer + typed control widgets + save/dirty tracking), `src/nightshift/assets/ui/style.css` (settings view + sidebar + controls + badges), and the worker SPA `src/nightshift/assets/ui-worker/{index.html,app.js,worker.css}` (add a gear/Settings entry + a compact version of the same renderer over the worker surface).

---

## 0. The one idea

The current Settings is a cramped modal with a flat field list and a Raw-JSON textarea. Part 3 makes it the **operator-console layout from the reference screenshot**: a left sidebar with **two tiers** (surface → category), a scrollable right pane of grouped, typed controls, a search box, env-shadow/restart/secret affordances, and an explicit Save bar. The renderer is generic — it draws the Part 2 payload — so it never encodes config knowledge.

## 1. Shell: full-page view (not a modal)

- The gear menu's **Settings…** switches the main pane to `view="settings"` (peer of the existing `workers`/`repos` views), rather than opening `#settings-modal`. Remove the old modal markup/handlers once the view is in.
- Layout:
  - **Left sidebar (tiers):** top-level groups `Manager` / `Player` (manager app) — each expands to its categories (`Server & Network`, `Cadences`, `Scheduling`, `Landing & Git`, `Worker execution policy`, `Conflict resolution`, …). Worker app shows the single `Worker` tier with its categories.
  - **Right pane:** the selected category's fields rendered as labeled rows (label + description + control + per-field affordances). A top **search box** filters fields across all categories by label/key/description (VS Code behavior); a match jumps/scrolls and highlights.
  - **Sticky Save bar** (bottom): "N unsaved changes", **Discard**, **Save**. Disabled when clean.

## 2. Typed controls (by `FieldSpec.type`)

| Type | Control |
|---|---|
| `bool` | toggle switch |
| `enum` | `<select>` of `options` |
| `int` / `float` | number input (with min/max when known) |
| `duration` | text input with inline validity hint (`45s`, `30m`, `1h30m`) |
| `string` | text input |
| `string_list` / `int_list` | **chip/list editor** — add/remove rows, reorder; ints validated |
| `regex_list` | chip/list editor; each pattern validated (compiles) with an inline error marker |
| `str_map` | **key/value row editor** — add/remove `{key: value}` rows (e.g. `model_aliases`) |
| `string (secret)` | password field showing **"set / not set"**; write-only (never displays the value); leaving it blank keeps the existing secret |

Every field row shows its **real config key** in a dim monospace tag (e.g. `cadences.poll_seconds`) and a badge:

- `restart` (amber) — `apply: restart`
- `live` (green) — `apply: live`
- `secret` (pink) — secret field (stored in `.env`)

When a field is **env-shadowed**, show an inline warning: *"Overridden by `NIGHTSHIFT_…`; editing the file won't change the running value until the env var is unset."* The control still edits the stored value.

## 3. Save / dirty model

- Editing marks the field dirty (diff against `stored`); the Save bar counts dirty fields.
- **Save** sends a **delta `PUT`** (only dirty fields, §Part 2) — not the whole config.
- On success: re-render from the response; if `restart_required` is non-empty, show a persistent banner listing those settings ("Restart the manager/worker to apply: …"); `applied_live` fields take effect immediately (e.g. theme re-skins on save, as today).
- On `400`: show per-field errors inline (map server messages to field rows); keep the form dirty.
- **Raw JSON escape hatch:** a collapsible per-category (or per-file) `<details>` "Raw JSON" pane showing the file's current stored values; editing it is an alternative submission path (parsed and merged into the delta on Save). This preserves the current power-user affordance while the typed controls are the default.

## 4. Worker SPA settings page

The worker UI (`ui-worker/`) is currently Now + History with a top nav and no gear. Add:

- A **Settings** entry to `.worker-nav` (or a gear button in the topbar) → a `view="settings"` section.
- A compact instance of the same renderer over the **worker** surface (`/api/settings` on the worker app). All worker fields are `restart`, so the page always shows a "restart this worker to apply" banner after Save. Reuse the shared `style.css` controls (already mounted at `/shared`) so the look matches the operator console.

## 5. Accessibility & polish

- Sidebar is a `role="tablist"`/tree; arrow-key navigation between categories; the right pane scrolls independently.
- Each control has a programmatic label (`aria-describedby` → description).
- Search has a clear/escape affordance; empty results show an empty state.
- Theme: reuse existing CSS variables (`--bg-elev`, `--border`, `--text-dim`, etc.) so it inherits light/dark.

## 6. Tests / verification

- **Renderer contract:** given a canned Part 2 payload, every `type` renders its control; badges and env-shadow warnings appear when flagged; secrets render write-only.
- **Dirty/delta:** changing two fields and saving sends exactly those two keys; Discard reverts to `stored`.
- **Error mapping:** a `400` with a per-field message renders inline on the right row and leaves the form dirty.
- **Restart banner:** a response with `restart_required` shows the banner; `applied_live` (theme) re-skins without reload.
- **Manual/browser smoke (per `control-ui` skill):** open the manager settings view, exercise a chip editor (`scheduled_models_allow`), a map editor (`model_aliases` on the worker), a secret field (shows set/not-set, never the value), and confirm Save → re-read.

## 7. Non-goals

- No config knowledge in the frontend — it is a pure renderer of the registry. Adding a setting is a Part 1 change only; it appears here automatically.
- No process hot-reload for `restart` fields (the UI only surfaces the requirement).
- No per-queue config here (that remains the existing queue-config surfaces); this editor is Manager / Worker / Player only.
