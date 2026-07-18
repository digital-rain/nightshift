# Nightshift VSCode Extension — Synthesized Design

**Subject:** The design for a VSCode extension companion to the Nightshift Manager, synthesized from a six-candidate design arena over the feasibility review's Options 1 and 3.
**Companion:** [`2026-07-18-vscode-extension.arena-note.md`](2026-07-18-vscode-extension.arena-note.md) records how this recommendation was produced.
**Parent:** [`../reviews/2026-07-18-vscode-extension-feasibility.md`](../reviews/2026-07-18-vscode-extension-feasibility.md).
**Date:** 2026-07-18
**Status:** Proposed.

---

## 1. Design stance

The extension is an **ambient companion plus wrapper**: a status-bar pulse and disciplined interrupts fed by SSE (the arena's winning stance), with the full operator UI available in-editor as a framed webview (the winning wrapper), and brief authoring support as the one deeply native feature (the corrected authoring graft). It is a pure client of the existing HTTP + SSE operator contract.

Constants, non-negotiable:

- The Manager stays a Python service; **zero manager-side changes are required** by any phase below.
- The Manager's lifetime is never tied to an editor window. Attach-first; spawn only on explicit request, detached; never kill on deactivate.
- No second dashboard. The browser UI (framed or external) remains the rich surface; native UI is limited to glanceable state, interrupts, and authoring.

## 2. Verified contract facts (load-bearing)

All candidate claims were checked against source; the synthesis rests on these:

1. **Auth:** `NIGHTSHIFT_SHARED_SECRET` gates only the worker API — `_require_secret` (`X-Nightshift-Secret` header) is a dependency on the five `/api/worker/*` routes (`manager/api_worker.py:314,501-697`). The operator API and static UI have **no auth today**. The extension therefore sends no secret, ships no secret setting, and documents the secure posture as "operator port local, trusted, or SSH-tunneled." If operator auth ever ships, it must be same-origin (cookie/session), because the browser UI's `EventSource` cannot send custom headers — a header gate on `/api/*` would break the shipped UI (arena finding, candidate A3's rejected proposal).
2. **SSE:** `/api/events` (`api_operator.py:1126`) sends a snapshot on connect (`workers`, `leases`, `runs`, `blocked`, `cursor`), then delta events; the browser (`assets/ui/manager-events.js`) **refetches on structural kinds rather than applying deltas**. The extension mirrors this: snapshot and cheap GETs (`/api/leases`, `/api/blocked`) own the counts; deltas only mark dirty and trigger debounced refetch. Delta arithmetic is a rejected design (races land/resolve; misses frontmatter-held blocked rows).
3. **UI framing:** the operator UI uses only relative URLs (`fetch("/api/…")`, `EventSource("/api/events")` at `manager-events.js:78`), so it works unmodified inside an iframe; SSE reconnect comes free from the browser's native `EventSource`.
4. **Launch:** `just manager [port]` runs `.venv/bin/python -m nightshift.manager --workspace … [--port …]` (`justfile:34-40`). `just restart` kills whatever owns the port (`justfile:74`); `just expunge` kills all nightshift processes — neither is a safe programmatic default.
5. **Brief schema:** beyond the detail-editor whitelist (`task_files.py:612` `_EDITABLE_META_KEYS`), the manager reads `make_pr` (`api_worker.py:340`, `reconciler.py:257`), `after` (`scheduler.py:191`), `mcp` (`scheduler.py:217`), and `turns` (`work_orders.py:76`) from frontmatter. `POST /api/tasks` (`api_operator.py:435`) is the canonical creation path (writes + commits into `nightshift-tasks/`, supports `enhance`); the `.tasks/` repo inbox is an import target, not an authoring path.

## 3. Component design

```
nightshift-vscode/
  package.json
  src/
    extension.ts        activate: commands, status bar, SSE client wiring
    config.ts           managerUrl resolution: setting → .nightshift/worker.json → localhost:8800
    client.ts           typed fetch helpers over /api/* (no secret; AbortController timeouts)
    sse.ts              fetch-stream SSE parser: reconnect w/ jittered backoff, staleness clock
    ambient.ts          counts store (snapshot + refetch), StatusBarItem render
    notify.ts           toast policy: dedupe, rate cap, quiet hours, morning digest
    panel.ts            singleton webview: iframe via asExternalUri, CSP, down-state
    spawn.ts            (Phase 4) attach-or-offer-to-start, detached spawn, log tail
    briefs.ts           (Phase 5) frontmatter completions/diagnostics/CodeLens, enqueue
```

Key decisions, each taken from the arena winner on that axis:

- **Webview (from A1):** `enableScripts: false`; the only interactivity in the down-state is `command:` URIs (`enableCommandUris: true`) — retry and settings links with zero webview JS. `retainContextWhenHidden: true` is mandatory: without it every tab switch destroys the iframe's `EventSource`. CSP grants `frame-src`/`child-src` for exactly the resolved manager origin, nothing else. The iframe `src` is always passed through `vscode.env.asExternalUri`, which is a no-op locally and auto-forwards the port under Remote-SSH/WSL/Containers/Codespaces. `extensionKind: ["workspace", "ui"]` so URL resolution and probes run next to the workspace.
- **SSE client (from B1):** Node `fetch` streaming + a ~40-line `data:`-frame parser; no `eventsource` dependency (weaker header/reconnect control). Backoff 1→2→4…30s with jitter. Staleness: if no frame or keep-alive comment for `stalenessMs` (default 45s ≈ 3 heartbeats), the status bar shows `stale` even if TCP looks open.
- **Status bar (from B1):** `$(moon) NS · ▶2 · ⚠1` (running/blocked; error count only when non-zero). `running = leases.length`, `blocked = blocked.length` from snapshot or `GET /api/leases` / `GET /api/blocked`; **not** `/api/stats` (lifetime aggregates, wrong grain). Click opens the panel.
- **Notification policy (from B1, verbatim):** toasts only for `task_blocked` and error-status `task_result`; never for completions. Dedupe key `${kind}:${queue}:${task}` (60 min). Rate cap (default 6/hour; overflow goes to the digest). Quiet hours (default 22:00–08:00 local) buffer everything; the first live connection after quiet hours flushes the buffer as **one** morning digest ("overnight: 3 blocked · 2 errors · 12 completed") with an Output-channel detail listing. The digest cursor persists in `globalState`.
- **Activation:** `workspaceContains:.nightshift/manager.json` / `workspaceContains:.nightshift/worker.json` plus the commands. No `*`, no unconditional `onStartupFinished` — unrelated workspaces pay nothing.

## 4. Endpoint map (all verified in source)

| Use | Endpoint | Phase |
|---|---|---|
| Health probe / cadence | `GET /api/info` (`api_operator.py:1150`) | 1 |
| Framed UI (everything it does) | `/` static + its own `/api/*` calls | 1 |
| Live convergence | `GET /api/events` SSE (`:1126`) | 2 |
| Running count | `GET /api/leases` (`:1037`) | 2 |
| Blocked count / detail | `GET /api/blocked` (`:1043`) | 2 |
| Recent errors for digest | `GET /api/runs?since=…` (`:945`) | 3 |
| Enqueue brief | `POST /api/tasks` (`:435`, `?enhance=true` supported) | 5 |
| Value completions | `GET /api/models` (`:1010`, returns `{models: […]}`), `GET /api/repos` (`api_playlists.py:259`), `GET /api/workflows` (`api_workflows.py:125`, object keyed by name), `GET /api/playlists` (`api_playlists.py:72`), `GET /api/task-defaults` (`:404`) | 5 |
| Follow a run | SSE `run_started`/`run_finished` filtered by task name; `GET /api/runs/{run_id}/{task}/log` (`:1057`) polled at `max(refresh_ms, 5s)` | 5 |

Response shapes are mirrored in one `types.ts`; a ~20-line probe added to `tools/smoke.py` asserts the endpoints the extension uses exist and parse (maintenance containment, from B2).

## 5. Lifecycle

- **Attach-first, always.** Activation probes `/api/info` (1.5s timeout). Healthy → attach. Down → honest `offline` status; the panel's down-state offers Retry / Start Manager / Edit Settings.
- **Offer-to-start (Phase 4), never auto-start.** On explicit command: spawn `uv run python -m nightshift.manager --workspace <ws> [--port <p>]` (or `just manager`) with `detached: true, shell: false, stdio: ["ignore", logFd, logFd]` + `unref()` — the child leads its own process group and survives window close by construction. Readiness = a valid `/api/info` response, never a log substring. A final probe immediately before spawn closes the double-start race; A2's full cross-window election/ownership machinery is **rejected** as disproportionate for a single-operator tool.
- **Never kill.** `deactivate()` closes the SSE stream and nothing else. Restart is delegated to a visible terminal running `just restart <port>` (which the operator already owns), not to extension-held process handles. `just expunge` is never invoked programmatically.
- **Multi-window:** each window may hold its own SSE connection (the hub is multi-subscriber); the digest cursor is `globalState`-scoped so only the first window to flush claims the morning summary.

## 6. Brief authoring (Phase 5, the corrected B3 graft)

- **No new language id.** Providers register against `{ language: "markdown", pattern: "**/nightshift-tasks/**/*.md" }` (plus `.tasks/**/*.md` read-only), so briefs keep every Markdown feature the operator already has. (B3's `contributes.languages` takeover is rejected — it would strip Markdown tooling from brief files.)
- **Completions/diagnostics** cover the verified key set: the editable keys from `_EDITABLE_META_KEYS` plus the scheduler/landing-read keys (`make_pr`, `after`, `mcp`, `turns`). Engine-owned keys (`workflow_step`, `workflow_visits`) and manager-written state flags surface as read-only warnings when hand-edited. Value completions come live from `/api/models` (unwrap `.models`), `/api/repos`, `/api/workflows` (object keys), cached per session, degrading to static literals offline.
- **Enqueue** via CodeLens / command → queue quick-pick (`/api/playlists`) → `POST /api/tasks` with optional `enhance`. The created task name lands in a one-level "My recent tasks" tree; the follower matches `run_started` events by task name (known ambiguity if a title is reused — accepted for v1, flagged for an optional additive correlation-id later).

## 7. Phased delivery (each independently landable)

| Phase | Content | Source | Effort |
|---|---|---|---|
| 1 | Wrapper: open-panel command, URL resolution, `/api/info` probe, iframe + CSP + `asExternalUri`, down-state, open-external escape hatch | A1 | 1–2 days |
| 2 | Ambient: SSE client, counts store, status bar with staleness | B1 | 2 days |
| 3 | Interrupts: toast policy, quiet hours, morning digest, mute | B1 | 2 days |
| 4 | Attach-or-offer-to-start: detached spawn, launch log Output channel, restart-via-terminal | A2 (reduced) | 2–3 days |
| 5 | Brief authoring: completions, diagnostics, enqueue, my-tasks follow | B3 (corrected) | 3–4 days |

Total ~10–14 days; Phase 1 alone already beats Simple Browser (auto-discovery, Remote-SSH correctness, down-state recovery).

## 8. Rejected designs (with reasons)

- **Extension-host reverse proxy (A3):** a second server whose main payoff (header injection) serves an auth scheme that cannot ship as proposed — gating `/api/*` with `X-Nightshift-Secret` breaks the browser UI's `EventSource` and static assets. Revisit only if the third-box-over-plain-HTTP topology (webview mixed-content) becomes real.
- **Full supervision platform (A2's registry/elections/identity tokens):** ~2 weeks of process forensics guarding against races a single operator on one box does not hit; the safe core (attach-first, detached spawn, never kill) is kept.
- **Four TreeViews + mutations (B2):** a second operator product with real rot risk (its own estimate: 15–20 days). Its maintenance-containment ideas (types mirror, smoke probe) are kept; a tiny read-only "needs attention" tree is deferred until status-bar evidence proves the browser hop is a felt cost.
- **Plaintext `sharedSecret` setting (B1/B2):** omitted entirely — there is nothing to send it to today; when operator auth exists, use `SecretStorage`.
- **Delta-arithmetic counts, `eventsource` npm dependency, auto-start on activation, kill-on-close:** all rejected per the verified contract facts above.

## 9. Optional future manager-side change (additive, not required)

Extend `/api/info` with `instance_id` (random per boot), `started_at`, `pid`, and `workspace_fingerprint` (from A2). This upgrades Phase 4's attach attribution from "some healthy Nightshift answers on this port" to a proven instance identity, and gives the wrapper a precise "wrong service on port" diagnostic. Two lines of response assembly; existing fields unchanged.
