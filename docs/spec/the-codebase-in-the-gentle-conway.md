# Nightshift React/Vite UI — Complete the Port, Restore Fidelity, Refactor Shared Components

## Context

The `react-vite-ui` branch ports the Nightshift **manager** and **worker** UIs from
bespoke vanilla JS/HTML/CSS (served by uvicorn) to React 19 / Vite 8 / TS / Tailwind 4,
**side-by-side** with the original (`just manager`/`just worker` = legacy; `just
manager-react`/`just worker-react` = new). The backend `/api` surface is unchanged —
the React UI is purely a new front end on the existing player API.

The initial pass (commit `e913448` + fixes) is a **foundation + shared kit only**, not a
port. It is well-built but thin: it replaced the legacy manager's whole "iPhone-Music"
metaphor with five plain top-nav tabs and dropped most chrome and functionality. The
legacy manager is ~5,400 lines of `app.js` + 1,700 lines of CSS; the React manager wires
~5 screens to data. Concretely the port is **missing**: the Now/now-playing screen, the
bottom mini-player + tab bar, transport modes (oneshot/auto/repeat), the P0–P5 play
filter, the phase stepper, drag-to-reorder, Playlists, Repos, every modal/dialog
(new-task, new-queue, add-from/add-to), the run-log viewer, manual conflict resolve,
queue dedication, and the donut/ring stat charts.

**Goal (per user direction):** **full music-metaphor fidelity**, **full functional
parity** with the legacy manager + worker, and a **balanced refactor** — extract a shared
component the moment a second screen needs it, building highest-value screens first, so
both apps compose from one library and bespoke code is minimized.

All work happens in `src/nightshift/assets/ui-react/`. **No Python/backend changes** are
required — every endpoint already exists (verified against `manager/app.py`,
`worker/ui_app.py`). The existing shared kit (`primitives.tsx`, `fields.tsx`,
`TaskListItem.tsx`, `TaskList.tsx`, `TaskDetail.tsx`, `DetailTakeover.tsx`,
`StatsPage.tsx`, `SettingsEditor.tsx`, `AppShell.tsx`, `useSse.ts`, the query hooks, the
`endpoints.ts` namespaces, `format.ts`, `rowAdapters.tsx`) is sound and is the base we
build on.

> **Worktree discipline (per memory):** do this on a dedicated branch/worktree off
> `react-vite-ui`, validate there, then squash-merge — never edit `main` directly. When
> validating, the worktree `.venv` may symlink to main's editable install; run any backend
> checks with `PYTHONPATH=$PWD/src:$PWD/tests`. Verify built output against the **committed**
> tree (the React `.gitignore` previously over-matched a `lib/` dir).

---

## Design overview

Three layers, built in order, extracting shared pieces as the second consumer appears:

1. **Harden the shared kit** (small, high-leverage) — the primitives every screen needs.
2. **Restore the manager music-metaphor shell + screens** (the bulk).
3. **Bring the worker to parity** (mostly reuse), then polish + cutover.

### Phase 0 — Shared-kit hardening (foundation gaps)

These are cheap and unblock everything. Files under `src/components/`, `src/lib/`.

- **Status tones** — `primitives.tsx` `STATUS_TONE` is missing legacy statuses. Add
  `pending`/`running`→accent, `paused`/`quarantined`/`aborted`→warn, `stopped`/`skipped`→
  neutral, `completed`→ok, `error`→err. Keep `StatusBadge` the single source of truth.
- **Icon set** — new `src/components/icons.tsx`: the legacy 24×24 SVGs as components
  (play, pause, skip, stop, chevron, back-chevron, eye/eye-off, grip, gear, plus, sort,
  the three transport-mode glyphs, home/now/queue/playlists/history nav glyphs). Port
  verbatim from `ui/index.html` + `app.js` (`PLAY_SVG`, `PAUSE_SVG`, `CHEVRON_SVG`,
  `EYE_ICON`, …) so stroke widths/shapes match exactly.
- **PhaseStepper** — new `src/components/PhaseStepper.tsx`: Worker → Validate → Commit
  dots + connecting bars, states grey/active(blue, pulsing)/done(green)/failed(red).
  Mirror legacy `.stepper`/`.step`/`.step-dot`/`.step-bar` + `@keyframes pulse`.
- **SpinRing** — extract the now-playing row spinner (legacy `spin-ring`, 0.7s linear)
  into a tiny component; reserve its 14px slot in rows so there's no layout shift.
- **Markdown** — new `src/lib/markdown.tsx` (or a `<Markdown>` component): port
  `escapeHtml` + `renderMarkdown` from `app.js` (block + inline: headings, lists, code,
  blockquote, hr, bold/italic/code/links). Add a `.markdown-body` rule block to
  `theme.css` matching the legacy spacing. Used by TaskDetail's brief **Markdown |
  Preview** toggle.
- **Modal** — new `src/components/Modal.tsx`: the `.modal`/`.modal-card` shell (overlay,
  centered card, head with title + close, `.modal-actions` footer, soft shadow, ESC/
  backdrop close). Every dialog below uses it.
- **SegmentedControl** — new `src/components/Segmented.tsx`: the `.mode-group`/`.mode-opt`
  and `.pf-opt` pattern (bordered button row, `.active`/`.on` = accent-soft). Reused by
  transport-mode selector and the priority filter.
- **DonutChart + StatCard** — extend `StatsPage.tsx`: a dependency-free SVG donut
  (`stroke-dasharray` arcs, center label) + legend, plus the headline `StatCard`
  (value/label/sub). Port the four chart builders from `app.js` (`proportionDonut`,
  `failureModesDonut`, `modelCallsDonut`, `modelCostDonut`, `avgTokensDonut`). No chart
  library — keep the bundle lean.
- **CmpTable** — generalize the existing `ComparisonTable` so the Workers screen's four
  tables (by backend/worker/model/queue, columns Runs/Done/Err/LOC/Avg s/Turns/Tokens/
  Cost) render from one component (today only two are wired).
- **Theme toggle + brand-tag** — add the legacy `data-theme` dark/light toggle (small
  control in the shell) and the dynamic brand-tag behavior (turns accent when a playlist
  is active; "· N running" suffix).

### Phase 1 — Manager music-metaphor shell

Rework `manager/ManagerApp.tsx` + `AppShell.tsx` into the legacy two-strip layout.

- **Top bar**: brand/logo (winged-moon) + brand-tag, the transport-**mode** segmented
  control (oneshot/auto/repeat), the **play-priority filter** (ALL/P0…P5), a **+ Add**
  button (opens new-task), and a **gear menu** (Settings / Workers / Repos).
- **Bottom region (two strips)**: a **mini-player** (`▶`/`❚❚` play-pause, skip, stop —
  big `tbtn` icons wired to `useTransport`) over a **bottom tab nav** (Home / Now / Queue
  / Playlists / History) with the legacy `.navbtn` glyphs + active accent.
- **View state** expands to the legacy set: `home | now | queue | playlists | history |
  settings | workers | repos | detail | playlist-info | stats`. Settings/Workers/Repos
  open from the gear menu (not the tab strip), exactly as legacy. Keep it as local state
  now; optionally add hash deep-linking in Phase 4.
- Wire **mode/priority/transport** to `manager.transport()`,
  `manager.setPlayPriorities()`, and the `/api/state` + `/api/active` reads
  (`useActiveState`, plus new `useQueueState`, `usePlayPriorities` hooks in
  `managerQueries.ts`). `useSse()` already converges these — extend its invalidation to
  the new query keys.

### Phase 2 — Manager screens (full parity)

Build on the shell + kit. New files under `src/components/` and/or `manager/screens/`.

- **Now screen** (`NowScreen`): execution card (title + `.md` filename, model badge,
  play/pause, live "Worker · 14m 03s" 1s ticker via `elapsedSeconds`, **PhaseStepper**,
  collapsible **Log** panel tailing the live log) **or** the idle hero (big play, "Idle"
  + next-task suggestion). On wide screens, the legacy two-column Now+detail layout. Data
  from `/api/state` + the SSE `task_log`/`task_status` stream; reuse the worker's log-tail
  pattern.
- **Queue screen**: upgrade the current `TaskList` to the legacy `#screen-queue` — queue
  chrome (active-queue name + Add menu), **UP NEXT** count + **sort toggle**
  (manual↔priority, `manager.setSort`), **drag-to-reorder** (native HTML5 DnD →
  `manager.reorder()`; disabled when priority-sorted), multi-select (shift-click), and a
  per-row **actions menu** (add-to-playlist, info, play next/last, enable/disable, remove,
  set priority). Add a small `useDragOrder` hook + a row context-menu component; both are
  reused by Playlists rows.
- **Detail takeover** (extend `TaskDetail.tsx`): full editable pane with the brief
  **Markdown|Preview** toggle, the read-only metadata grid (File/Status/Repo/Model/Phase),
  the expando **Log**/**Result**/**Run details** sections (port the `.xpanel` expando into
  a shared `<Expando>`), and PhaseStepper. **New-task** = same surface with an empty draft
  (back = discard); wire `manager.createTask()` (the `+` button) — currently unwired.
- **Playlists screen**: list with task-count badges, **Show hidden** toggle, **Rescan**
  (`manager.rescanPlaylists()`), **+ New** (new-queue modal), and per-row controls
  (hide/unhide, info → **playlist-info takeover**, add-task, menu). Hooks already exist
  (`usePlaylists`, `createPlaylist`, `updatePlaylist`, `deletePlaylist`).
- **Repos screen**: workspace path, warnings for queues bound to absent repos, known-repos
  list (available/absent + tasks-store tag), per-queue repo bindings (`getQueueConfig`/
  `setQueueRepo`), **Rescan** (`/api/repos/rescan`). Add `useRepos` + `useReposRescan`
  hooks (`/api/repos` is not yet consumed).
- **History screen**: add the **Stats** and **Clear** actions; rows already adapt via
  `runToRow`. Clicking a run opens a **read-only detail** with its **log viewer**
  (`manager.log()` paged fetch — new `useRunLog`).
- **Stats screen** (takeover from History): StatCards (Tasks/Avg time/Success rate/LOC) +
  the DonutChart row (outcomes, failure modes, model usage, model cost) + by-model table.
  Reuses Phase-0 charts.
- **Workers screen**: keep the list + blocked section; add **queue dedication** editor
  (`getDedication`/`setDedication` — new hooks) and all **four** CmpTables.
- **Modals**: new-task (or reuse detail draft), **new-queue**, **add-from-playlist**,
  **add-to-playlist** — all via the shared `<Modal>`; wire to existing endpoints.

### Phase 3 — Worker parity

The worker reuses ~everything; gaps are mostly screens it already lacks.

- **Now**: rebuild as the rich legacy card — queue/repo/phase badges, model/started/
  branch/worktree metadata rows, **PhaseStepper**, and the auto-scrolling live **log**
  `<pre>` (reuse the Now log component). Data from `/api/now` (`log_tail`).
- **History detail**: clicking a worker run opens the **read-only TaskDetail** (back to
  history) — reuse the same takeover.
- **Stats**: replace tiles-only `WorkerStatsView` with the full StatCards + DonutCharts
  (worker has no fleet comparisons, so no CmpTables) — reuse Phase-0 charts.
- **Header/nav**: worker ID + backend badge + the shared theme toggle; nav tabs Now/
  History/Settings (+ restart control when surfaced).
- Settings already shares `SettingsEditor` — no change beyond the hardened field set.

### Phase 4 — Polish, parity sweep, cutover

- **Animations/fidelity**: spin-ring on the now-playing row, pulse on the active phase
  step, chevron rotation on expandos, the 300ms "button wink", `tabular-nums` on all
  timing/counts/cost, monospace filenames, sticky save-bars with the legacy shadow.
- **Responsive**: Now two-column ≥721px / takeover below; settings sidebar 240→180px
  ≤600px; `safe-area-inset-bottom` on the bottom strips.
- **Parity sweep**: diff React screens against the legacy `index.html`/`app.js`/`worker`
  feature-by-feature; close stragglers (markdown preview, keyboard gestures, render-guard
  during list rebuilds).
- **Optional**: hash-based deep-linking for view restoration.
- **Build/cutover verification** (no cutover of legacy yet — side-by-side stays):
  `npm run build` → `dist-manager`/`dist-worker`; confirm both serve via
  `NIGHTSHIFT_UI_DIR`/`NIGHTSHIFT_WORKER_UI_DIR` against a running backend.

---

## Critical files

**Reuse / extend (existing):**
- `src/components/primitives.tsx` — status tones, buttons, Pill, Spinner.
- `src/components/{TaskList,TaskListItem,TaskDetail,DetailTakeover,fields}.tsx` — rows,
  editor, takeover, form fields.
- `src/components/StatsPage.tsx` — extend with DonutChart, StatCard, generalized CmpTable.
- `src/components/SettingsEditor.tsx` — already shared; only the hardened field set.
- `src/app/AppShell.tsx` — rework into top-bar + bottom two-strip shell.
- `src/hooks/{managerQueries,workerQueries,useSse,useSettingsSave}.ts` — add hooks for
  queue-state, play-priorities, repos, dedication, run-log; extend SSE invalidation.
- `src/api/endpoints.ts`, `src/api/types.ts` — endpoints already declared; add any missing
  request/response types (repos, dedication, run-log paging).
- `src/lib/{format,rowAdapters,cn}.ts(x)` — formatting + row adapters.

**New shared components:**
- `src/components/icons.tsx`, `PhaseStepper.tsx`, `Modal.tsx`, `Segmented.tsx`,
  `Expando.tsx`, `SpinRing.tsx`, `RowMenu.tsx`; `src/lib/markdown.tsx`;
  `src/hooks/useDragOrder.ts`.

**New manager screens (in `manager/` or `manager/screens/`):**
- `NowScreen.tsx`, `PlaylistsScreen.tsx`, `PlaylistInfo.tsx`, `ReposScreen.tsx`,
  `StatsScreen.tsx`, and the four modals; rework `ManagerApp.tsx` for the shell + view set.

**Worker:**
- rework `worker/WorkerApp.tsx` Now/Stats/History-detail to reuse the shared pieces.

**Reference (legacy, do not modify):**
- `src/nightshift/assets/ui/{index.html,app.js,style.css,workers.js,manager-events.js}`,
  `src/nightshift/assets/ui-worker/{index.html,app.js,worker.css}` — the fidelity source
  of truth (palette, class names, SVGs, animations, screen structure).

---

## Verification

Per-phase, run the app for real (memory: validate built output against the committed tree;
watch the `.gitignore`/venv-symlink traps):

1. **Build/typecheck**: `cd src/nightshift/assets/ui-react && npm run build` (runs `tsc -b`
   + both Vite builds) → `dist-manager/` and `dist-worker/` produced clean.
2. **Manager dev**: terminal A `just manager`; terminal B `just manager-react` (:5173,
   proxies `/api`→:8800). Walk every screen: Now (start a task, watch the phase stepper +
   live log + ticker), Queue (drag-reorder, sort toggle, multi-select, row menu, + Add →
   create task), Playlists (create/hide/rescan, playlist-info, add-from/add-to), Repos
   (bindings + rescan), History → Stats (donuts render) + log viewer, Workers (4 tables +
   dedication), Settings (dirty/save/restart). Confirm transport mode + priority filter +
   mini-player play/pause/skip/stop drive `/api/transport` and converge live via SSE across
   two browser tabs.
3. **Worker dev**: `just worker` + `just worker-react` (:5273→:8810). Now card (badges,
   metadata, stepper, auto-scroll log), History → read-only detail, Stats donuts, Settings.
4. **Fidelity pass**: side-by-side legacy (`just manager`) vs React at matching widths —
   compare palette, spacing, badges, animations, dark/light toggle. Spot-check the
   distinctive details (spin-ring, pulse, button-wink, tabular-nums, monospace filenames).
5. **Prod static serving**: `NIGHTSHIFT_UI_DIR=…/dist-manager just manager` and
   `NIGHTSHIFT_WORKER_UI_DIR=…/dist-worker just worker` — confirm the built bundles serve
   and the `/api` calls resolve same-origin.
