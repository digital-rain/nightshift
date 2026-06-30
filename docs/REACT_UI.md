# React/Vite UI migration

Status: **foundation + shared component kit landed on branch `react-vite-ui`,
built side-by-side, not yet cut over.** This doc is the plan + remaining-work
tracker. The new code lives under `src/nightshift/assets/ui-react/` (see its
README for layout and commands).

## Decision record

- **Stack**: React 19, Vite 8, TypeScript (strict), Tailwind 4 (CSS-first
  `@theme`), TanStack Query. Matches Longitude's React/Vite/TS/Tailwind shape,
  modernised to current versions, plus react-query because Nightshift's SSE +
  polling data model fits it well.
- **Side-by-side**: builds to `dist-manager/` / `dist-worker/`. The legacy
  vanilla UIs (`assets/ui`, `assets/ui-worker`) are untouched and keep serving.
- **No backend changes**: all three FastAPI apps already expose a clean `/api/*`
  JSON + SSE layer and serve the UI via `StaticFiles`. React is a new static
  surface on the same API. (If that clean API layer had *not* existed, building
  it would have been the P0 step — but it does.)

## Why "shared component kit" not "1:1 port"

The manager UI is ~17.5k lines of vanilla JS, the worker ~1.3k. Much of that is
the same idea rendered twice. This pass extracts the reusable abstractions so
both surfaces are thin compositions:

| Concern            | Legacy (manager / worker)                  | Shared component                         |
| ------------------ | ------------------------------------------ | ---------------------------------------- |
| list of tasks      | queue rows / history rows (built twice)    | `TaskList` + `TaskListItem` + adapters   |
| a row              | bespoke innerHTML per list                 | `TaskRowModel` + `lib/rowAdapters`       |
| statistics         | `renderStats` / worker stats (twice)       | `StatsPage` (tiles + `ComparisonTable`)  |
| task read/edit     | detail takeover (manager)                  | `TaskDetail` + `DetailTakeover` + fields |
| settings editor    | settings tree (manager) + w-settings (wkr) | `SettingsEditor` (one, both backends)    |
| status/labels      | `.pill` / status colours                   | `primitives` (Pill, StatusBadge, …)      |
| live convergence   | `manager-events.js` + debounce-refetch     | `useSse` (snapshot seed + delta invalidate) |

## Verified

- `npm install` clean (48 pkgs, 0 vuln).
- `tsc -b` passes (strict).
- `vite build` passes for both surfaces (~77 / ~75 KB gzip).
- Built output served via a plain static server (mimics `StaticFiles`): index,
  hashed JS/CSS, and brand assets all 200; CSS contains the Tailwind reset and
  the ported `--color-*` tokens; asset refs are relative (mount-path agnostic).

## Remaining work (port on top of the shared kit)

Manager screens/chrome not yet ported (each reuses the shared pieces above):

- [ ] Now / mini-player view (current task + log tail)
- [ ] Playlists screen + new-playlist modal + add-from / add-to pickers
- [ ] Repos screen (workspace, known repos, queue bindings, rescan)
- [ ] Playlist-info takeover (reuses `DetailTakeover`)
- [ ] Drag-to-reorder the queue (`useReorderQueue` exists; needs a DnD binding)
- [ ] Priority-filter + transport-mode segmented groups in the top bar
- [ ] Hash deep-linking (`#task=…`) and back-is-cancel routing semantics
- [ ] Log streaming (`/api/runs/{run}/{task}/log` offset polling)
- [ ] Queue dedication editor; blocked-task recovery actions

Worker screens: Now / History / Stats / Settings are wired. Still:

- [ ] Run-detail takeover from a history row
- [ ] Capability strip (models / mcp) niceties, restart banner

Cross-cutting:

- [ ] Settings `PUT` delta shaping — the editor currently forwards a flat
      `{key: value}` working delta; map it to the nested
      surface→category→field body the backend expects, and surface
      `applied_live` / `restart_required` from the response.
- [ ] Light-theme toggle wiring (`data-theme` attribute switch is already
      supported by the token CSS; just needs a control).
- [ ] Tests (Longitude's UI has none; consider vitest + testing-library here).
- [ ] Cutover: repoint `outDir` (or the FastAPI mount) once at parity.
