# Arena synthesis note — VSCode extension design

Companion to `2026-07-18-vscode-extension.md`. Records how the recommendation was produced.
Parent brief: `../reviews/2026-07-18-vscode-extension-feasibility.md` (its Options 1 and 3 were the two themes).

## Rubric (weighted, /35)
1. Fit to the Manager's reality (×1.5) · 2. Operator value over Simple Browser (×1.5) · 3. Lifecycle & failure handling (×1) · 4. Scope discipline & honesty (×1) · 5. Extension-platform correctness (×1) · 6. Incrementality & maintainability (×1).
Disqualifiers: TypeScript port of the manager, editor-tied manager lifetime, ignoring auth.

## Candidates (6, parallel, distinct stances & model families)
| # | Theme / frame | Model | Judge | My call |
|---|---|---|---|---|
| A1 | Wrapper: pure-iframe minimalist | Sonnet 5 | 29.5 | 31 — best wrapper; `enableScripts:false` + `command:` URIs + `retainContextWhenHidden` is the zero-maintenance shape |
| A2 | Wrapper: supervisor lifecycle | GPT-5.6 | 26 | 30 — safest process design ever offered here, but a platform of its own; judge's "launch command error" charge was itself wrong (`justfile:40` runs `python -m nightshift.manager`) |
| A3 | Wrapper: bridge/proxy | Gemini 3.1 Pro | 16.5 | 20 — proxy is disproportionate; its operator-auth header gate would break the browser UI (EventSource can't send headers) |
| B1 | Native: ambient/glanceable | Grok 4.5 | 31.75 | 33 — verified the SSE convergence model correctly; the quiet-hours/digest notification policy is the arena's single best artifact |
| B2 | Native: full workbench | Opus 4.6 | 20.75 | 27 — all endpoints real, good containment ideas, but a second operator product; undeclared `eventsource` polyfill |
| B3 | Native: authoring-first | Kimi K2.7 | 10 (capped) | 29 — **cap overturned**: the "invented" keys are real (see Verification); real faults are two response-shape errors and a language-id takeover |

Cross-judge: read-only GPT-5.6 Terra. We agreed on the two theme winners and the overall winner; we disagreed on B3 (see below).

## Base
**B1 (ambient) + A1 (wrapper), merged.** They are complementary halves: B1 deliberately punts rich pages to the browser; A1 supplies exactly that surface in-editor for ~2 days of work. Together: glanceable state + interrupts + the full UI, ~1 week, zero manager changes.

## Grafts (with sources)
- **A2 → the safe lifecycle core:** attach-before-start, detached spawn (`detached: true` + `unref()` + fd-backed logs), readiness = `/api/info` not log grep, never kill, restart via visible terminal. Its election/ownership machinery was cut as disproportionate; its additive `/api/info` identity fields survive as the spec's only optional manager change.
- **B3 → brief authoring**, corrected: providers on a markdown `documentSelector` pattern instead of a new language id; schema from the verified read set (editable keys + `make_pr`/`after`/`mcp`/`turns`); `/api/models` unwrapped as `{models: […]}`; `/api/workflows` as object keys.
- **B2 → maintenance containment:** one `types.ts` mirroring wire shapes + a ~20-line endpoint probe in `tools/smoke.py`; lazy hydration.
- **A1 → the Remote/SSH seam:** every iframe URL through `vscode.env.asExternalUri`; `extensionKind: ["workspace","ui"]`.

## Rejections (highest-signal part of the record)
- **A3's proxy + operator header gate:** gating `/api/*` with `X-Nightshift-Secret` breaks the shipped browser UI (EventSource, static assets). Operator auth, if it ever ships, must be same-origin session/cookie — a manager feature, not an extension workaround.
- **Delta-arithmetic status counts:** the browser refetches on structural SSE kinds rather than applying deltas (`manager-events.js`); counting from `run_started`/`task_blocked` alone races land/resolve and misses frontmatter-held blocked rows.
- **`eventsource` npm dependency:** fetch-stream parsing gives header and reconnect control with no dep.
- **Plaintext `sharedSecret` setting (B1/B2):** nothing accepts it today; `SecretStorage` when operator auth exists.
- **B2's four-TreeView workbench and B3's fleet views:** second-product rot risk; deferred pending status-bar evidence.
- **Auto-start on activation / kill-on-close (all candidates rejected it too):** violates the overnight-runner premise.

## Verification (including two judge-error corrections)
- Auth ground truth confirmed twice (me + judge): `_require_secret` only on `/api/worker/*` (`api_worker.py:314,501-697`); operator API ungated.
- **Judge error 1 — B3's cap overturned:** `make_pr` is read at `api_worker.py:340` and `reconciler.py:257`; `after` at `scheduler.py:191`; `mcp` at `scheduler.py:217`; `turns` at `work_orders.py:76`. The judge checked only `_EDITABLE_META_KEYS` (`task_files.py:612`), which is the detail-editor whitelist, not the manager's read schema. B3's genuine errors (kept as docks): `/api/models` returns `{models: […]}` (`api_operator.py:1010`), `/api/workflows` returns an object keyed by name (`api_workflows.py:125`), and the `contributes.languages` takeover of `.md`.
- **Judge error 2 — A2/A3's launch command was correct:** `justfile:40` runs `{{py}} -m nightshift.manager`, exactly what they proposed.
- B2's endpoint table spot-checked against the route registrations in `api_operator.py` / `api_playlists.py`: all real.
- Dropouts: none (6/6 completed; 12/12 files delivered).
- Resolved from the parent review's open item: the auth question is now answered in source — the iframe "inherits" nothing because there is nothing to inherit; the secure posture is a local/tunneled operator port.
