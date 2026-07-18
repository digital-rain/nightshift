# Wrapping the Nightshift Manager as a VSCode Extension — Feasibility Review

**Subject:** Can the Nightshift Manager be ported, wrapped, or converted into a VSCode plugin/extension — and if so, in what form?
**Scope:** `src/nightshift/manager/` (FastAPI app, operator API, SSE hub, scheduler, landing authority), `src/nightshift/assets/ui/` (operator UI), the manager's lifecycle and deployment model.
**Date:** 2026-07-18
**Verdict:** Yes — as a **wrapper**, not a port. The Manager is a long-lived Python service whose entire operator surface is already an HTTP + SSE contract; an extension should treat it as an external service and never as code living inside the extension host. A thin webview wrapper is a day of work; a native-UI companion is a worthwhile incremental follow-on; a TypeScript port is technically possible and strategically wrong.

---

## What the Manager actually is (and why it can't "become" an extension)

VSCode extensions run in a Node.js extension host. The Manager is:

- a Python **FastAPI server** on `:8800` (`manager/app.py`) with background loops (reconciler, scheduler arbitration);
- the **git landing authority** — per-repo locks, cross-process `flock`, `merge-tree` conflict preview, squash-to-main, an out-of-process conflict resolver (`resolve_job.py`);
- a **store** over Postgres (`PgStore`) or in-memory SQLite fallback;
- an **SSE hub** (`hub.py`) driving live operator-UI convergence;
- a static, zero-build **vanilla-JS operator UI** (`assets/ui/`) mounted via `StaticFiles`.

Two properties dominate the design space:

1. **The operator API is already the full integration contract.** Everything the browser UI does goes through `/api/*` (`api_operator.py`, `api_playlists.py`, `api_repo_tasks.py`) plus one `EventSource("/api/events")` (`assets/ui/manager-events.js:78`). The UI uses only relative URLs — no build step, no bundler assumptions — so it works unmodified when framed or proxied.
2. **The Manager's lifetime must not be tied to an editor window.** It is an *overnight* runner: workers poll it while no editor is open. Any design that makes the extension host own the process (activate-spawn / deactivate-kill) fights the product's core premise.

Both properties push the same direction: wrap, don't port.

## Options, cheapest first

### Option 0 — No extension: Simple Browser

`> Simple Browser: Show` → `http://localhost:8800` renders the operator UI in an editor tab today, zero code. This is the baseline any extension must beat.

### Option 1 — Thin wrapper extension (webview + iframe) — recommended first step

A webview panel whose content is essentially `<iframe src="{manager_url}">`. Because the UI's `fetch("/api/…")` calls and the SSE EventSource are all relative and same-origin *inside the iframe*, the full UI — live updates, settings editing, the shared analytics module — works unmodified. The extension contributes:

- a command + activity-bar icon to open the panel;
- a `nightshift.managerUrl` setting, defaulted from `.nightshift/worker.json`'s `manager_url` when the workspace has one;
- a connectivity check against `/api/info` with a friendly "manager not running" state.

Effort: ~200 lines, roughly a day. Zero changes to the Manager. The Manager keeps its own lifecycle.

### Option 2 — Process supervision (sidecar pattern), done carefully

Option 1 plus lifecycle management: on activation, probe `/api/info`; if nothing answers, offer to spawn `uv run python -m nightshift.manager` (or `just manager`) as a child process — the language-server pattern — with an output channel for logs and a "Restart Manager" command.

Constraints that shape this option:

- **Attach-if-running must be the primary mode.** Killing the Manager on window close contradicts the overnight use case. The right shape is *attach if running; offer to start if not; on deactivate, detach (leave it running) by default*.
- **Runtime prerequisites leak in.** The box needs Python 3.12+, `uv`, and `git`. A VSIX cannot sanely bundle a Python runtime; per-platform frozen binaries (PyInstaller et al.) are possible but add a release axis the project does not currently have.

### Option 3 — Native VSCode UI over the existing API

Skip the iframe for glanceable surfaces and build first-class VSCode features in TypeScript against `/api/*` + `/api/events`:

- a TreeView for queues / tasks / runs / workers;
- a status-bar item with running/blocked counts fed by SSE;
- notifications on `blocked` / `error` run outcomes;
- commands: "add task from selection", "enqueue file as brief"; CodeLens on `.tasks/` briefs;
- "reveal task branch" integration with the Source Control view.

The Manager needs zero changes — but this is a second UI to maintain (weeks, not days), and the rich pages (Statistics, workflow editor) would be wasteful to reimplement. The sensible shape is hybrid: native views for glanceable state and notifications, the webview (Option 1) for the rich pages.

### Option 4 — Full TypeScript port — ruled out

Rewriting the Manager inside the extension host means reimplementing the store/SQL layer, scheduler, landing pipeline (squash, merge-tree preview, repo locks, cross-process `flock`), reconciler loops, and SSE in Node — while the workers, the agentic harness, and the migrations stay Python. That is a permanently bifurcated codebase for no architectural gain, plus the lifecycle mismatch from the premise above. Not worth pursuing.

## Recommendation

1. **Now:** Option 1 — command, webview iframe, `managerUrl` setting, `/api/info` health check.
2. **Fold in:** Option 2's *attach-or-offer-to-start* behavior (never kill-on-close by default).
3. **Grow incrementally:** the two highest-value Option 3 features — status-bar counts and blocked/error notifications via SSE — then reassess whether deeper native UI earns its maintenance cost.

Design constant across all of it: **the Manager is an external service the extension talks to over HTTP/SSE**, never code that lives inside the extension.

## Open item

If `NIGHTSHIFT_SHARED_SECRET` gates the operator API in a given deployment, the iframe inherits whatever auth the browser UI performs today, but any native feature (Option 3) issues its own requests and must send the secret itself. Verify the auth story against a secured deployment before shipping the status-bar/notification features.

**Resolved 2026-07-18:** verified in source — `_require_secret` (`api_worker.py:314`) gates only the five `/api/worker/*` routes; the operator API and static UI are unauthenticated today. The extension sends no secret; the secure posture is a local/trusted/SSH-tunneled operator port. Note that a future operator gate cannot be the same custom header: the browser UI's `EventSource` cannot send one, so operator auth must be same-origin (cookie/session). Design follow-up: [`../spec/2026-07-18-vscode-extension.md`](../spec/2026-07-18-vscode-extension.md) + its arena note.
