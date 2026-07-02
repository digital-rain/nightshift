# Nightshift UI — React / Vite / TypeScript / Tailwind 4

A React rewrite of the Nightshift operator + worker UIs, built **side-by-side**
with the existing hand-written vanilla-JS UIs (`../ui`, `../ui-worker`). The
legacy UIs are untouched and keep working; this is a new static surface on the
same `/api/*` backend.

Same stack as Longitude's dashboard (React + Vite + TS + Tailwind), modernised:
React 19, Vite 8, Tailwind 4 (CSS-first `@theme`), and **TanStack Query** for the
SSE + polling data layer.

## Why this is a frontend-only change

All three Nightshift backends already serve a clean JSON + SSE API and mount the
UI as static files (no Jinja, no server-rendered HTML):

| Backend                       | Port | Mounts            |
| ----------------------------- | ---- | ----------------- |
| manager (`manager/app.py`)    | 8800 | `assets/ui`       |
| worker UI (`worker/ui_app.py`)| 8810 | `assets/ui-worker` + `/shared` → `assets/ui` |
| server (`server/app.py`)      | 8799 | `assets/ui`       |

So React is "just another static surface." No Python changes are required to
develop it; cutting over is a one-line change to where each app's `outDir`
points (or to the FastAPI mount).

## Layout

```
ui-react/
  manager/            Vite root for the operator UI (index.html, main.tsx, ManagerApp.tsx)
  worker/             Vite root for the worker UI
  src/
    api/              types.ts (the contract), client.ts (fetch wrapper), endpoints.ts
    hooks/            queryKeys, managerQueries, workerQueries, useSse
    components/       shared kit — see below
    lib/              format, cn, rowAdapters
    app/              AppShell, queryClient
    theme.css         Tailwind 4 @theme — design tokens ported from legacy style.css
  vite.config.ts      manager build → dist-manager, dev proxy → :8800
  vite.worker.config.ts  worker build → dist-worker, dev proxy → :8810
```

### Shared component kit (the point of this rewrite)

The manager and worker surfaces are deliberately thin; the reusable pieces live
in `src/components` and generalise across both:

- **TaskListItem / TaskList** + `lib/rowAdapters.tsx` — one row + list component
  drives the manager queue, manager history, and worker history. Each list
  supplies an *adapter* (`queueItemToRow`, `runToRow`, `workerRunToRow`) that
  maps its backend shape into a normalised `TaskRowModel`. Add a list by writing
  an adapter, not by branching the row.
- **StatsPage** (`StatTiles`, `ComparisonTable`, `ManagerStatsView`,
  `WorkerStatsView`) — the headline tiles serve both UIs; the comparison tables
  are reused by both the manager Stats screen and the Workers screen.
- **TaskDetail** + **DetailTakeover** + **fields** — the read/edit task surface.
- **SettingsEditor** — the tier/category/field editor; both backends return the
  same settings shape, so one editor drives both.
- **primitives** — Pill, StatusBadge (full legacy status vocabulary), buttons,
  EmptyState, Spinner, ErrorState.
- **icons** — the legacy 24×24 SVG set (transport, chevrons, nav glyphs, the
  three transport-mode glyphs, eye/eye-off, grip, gear, plus, sort) as components.
- **PhaseStepper** — Worker → Validate → Commit progression (active dot pulses).
  `stepsFromPhase(phase, status)` derives the model from a run.
- **DonutChart / StatCharts** — dependency-free SVG donuts (outcomes, failure
  modes, model usage, model cost) for both stats surfaces.
- **Segmented** — the transport-mode selector + priority-filter shell.
- **Modal / Expando / RowMenu / SpinRing** — dialog, collapsible panel, popup
  actions menu, now-playing spinner.
- **lib/markdown.tsx** — the brief Markdown|Preview renderer (ported from app.js).
- **hooks** — `useTheme` (data-theme dark/light), `useDragOrder` (native queue
  reorder).

## Run it (justfile recipes)

From the repo root — these wrap the npm scripts below:

```bash
just react-install          # one-time: install npm deps

# Dev (HMR), against a running backend in another terminal:
just manager                #   terminal 1: the real manager (:8800)
just manager-react          #   terminal 2: Vite dev UI (:5173), proxies /api → :8800
#   …or the worker pair:
just worker                 #   terminal 1: the real worker UI backend (:8810)
just worker-react           #   terminal 2: Vite dev UI (:5273), proxies /api → :8810

# Production: the REAL backend serves the built React bundle (no separate UI server).
just manager-react-prod     # builds dist-manager, launches the manager serving it (:8800)
just worker-react-prod      # builds dist-worker, launches the worker serving it  (:8810)
```

`*-react-prod` works by pointing the backend's static mount at the React build
via `NIGHTSHIFT_UI_DIR` / `NIGHTSHIFT_WORKER_UI_DIR` (see `nightshift._paths`).
The API is identical — the React build is just the static surface — so this is
the same backend you already run, with a different shell. Unset those env vars
(the default) and you get the legacy vanilla UI.

## Develop (raw npm, if not using just)

```bash
cd src/nightshift/assets/ui-react
npm install

# Run a Nightshift backend in another terminal, then:
npm run dev          # manager UI on :5173, proxies /api → :8800
npm run dev:worker   # worker UI  on :5273, proxies /api → :8810
```

## Build

```bash
npm run build          # both surfaces → dist-manager/ and dist-worker/
npm run build:manager  # just the manager
npm run build:worker   # just the worker
npm run typecheck      # tsc -b, no emit
```

Output uses **relative** asset paths (`base: './'`), so it serves correctly under
any static mount, verified against a plain static server (the same way FastAPI's
`StaticFiles` serves it).

## Cutting over (when you're ready, not yet)

The builds currently land in `dist-manager/` / `dist-worker/` so nothing is
overwritten. To switch the real UI to React, either:

1. Point the Vite `outDir`s at `../ui` and `../ui-worker` and rebuild, or
2. Point the FastAPI mounts (`nightshift._paths.UI_DIR` / `WORKER_UI_DIR`) at the
   `dist-*` dirs.

Until then this is a fully isolated, reviewable surface.

## Scope of this pass

This pass completes the **music-metaphor port** to near-parity with the legacy
manager + worker UIs, all composed from the shared kit above.

**Manager** — the iPhone-Music shell: top-bar transport-mode segmented control
(oneshot/auto/repeat), P0–P5 play-priority filter, + Add, theme toggle, and a
gear menu (Settings / Workers / Repos); a bottom mini-player (play/pause · skip ·
stop) over the tab strip (Now / Queue / Playlists / History). Screens: **Now**
(running card with model badge, 1s elapsed ticker, PhaseStepper, live log tail —
or an idle hero), **Queue** (drag-to-reorder, sort toggle, per-row actions menu,
+ Add), **task detail / new-task** (brief Markdown|Preview), **Playlists**
(hide/unhide, rescan, new-queue modal, playlist-info takeover), **Repos**
(workspace, absent-repo warnings, per-queue bindings, rescan), **History** →
read-only **run detail** with the reconstructed **log viewer** → **Stats**
(donut charts + tiles), **Workers** (fleet + blocked + queue-dedication editor +
all four comparison tables), **Settings**. Live SSE convergence throughout.

**Worker** — rich **Now** card (queue/repo/phase badges, model/started/branch/
worktree metadata, PhaseStepper, auto-scrolling live log), **History** with a
read-only run detail, **Stats** donuts, **Settings**, theme toggle.

### Known gaps (backend-blocked)

The **add-from / add-to-playlist** task-copy pickers are **not** wired: they
depend on `POST /api/queue/import` and `GET /api/playlists/{name}/tasks`, which
exist only on the legacy single-process server (`server/app.py`), not the
manager (`manager/app.py`). Wiring them requires adding those two endpoints to
the manager first — out of scope for this frontend-only pass. The new-queue and
playlist-info surfaces (which use endpoints the manager *does* expose) are wired.

See `docs/REACT_UI.md` for the full endpoint→component map.
```
