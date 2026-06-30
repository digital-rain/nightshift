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
- **primitives** — Pill, StatusBadge, buttons, EmptyState, Spinner, ErrorState.

## Develop

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

This is the **foundation + shared component kit**, not a 1:1 port of all ~18k
lines of the legacy UIs. The two app shells wire the shared kit to real data and
demonstrate every shared piece end-to-end (Queue, History, Stats, Workers,
Settings, task edit, SSE convergence, transport). Legacy chrome still to port on
top of these same pieces: the playlists/repos screens, the add-from/add-to and
new-playlist modals, drag-to-reorder, the priority-filter + transport-mode
groups, deep-linking, and log streaming. See `docs/REACT_UI.md`.
```
