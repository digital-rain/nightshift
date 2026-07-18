# Arena synthesis note — task documents (`docs:`)

Companion to `2026-07-18-task-documents.md`. Records how the recommendation was produced.
Ran **twice**: a first round text-only, then a full **greenfield re-run** once binary (images/PDF) became a hard requirement. The re-run — not the first round — produced the shipped spec. Both rounds are recorded because the re-run's value is partly *how the requirement skewed the field*.

## Rubric (weighted, /37.5)
1. Recoverability & cross-machine safety (×2) · 2. Binary/mixed-media handling (×1.5) · 3. Cap discipline (×1) · 4. Authoring ergonomics & mental model (×1) · 5. Fidelity to existing machinery (×1) · 6. Edge-case & lifecycle coverage (×1).
Round-1 rubric was the same minus criterion 2 (binary), with recoverability still ×2. Adding a dedicated binary criterion at ×1.5 and folding the cap into criterion 3 is exactly what re-scoped the problem.
Hard requirements (re-run): text **and** small binary first-class from day one · a hard per-doc byte cap that is a configurable setting under a code-constant ceiling · recoverable-by-construction & cross-machine-safe.

## Candidates (5, parallel, distinct stances & model families)
| # | Stance | Model | Judge | My call |
|---|---|---|---|---|
| 1 | Pure attachments (+ `.docstore/` content-addressed dedup) | (re-run) | 31 | 31 — sharpest binary argument in the field (cross-machine blob-reachability critique of paths); duplicates canonical repo docs |
| 2 | Pure repo-paths, sha-pinned, `cat-file` delivery | (re-run) | 26 | 26 — cleanest no-embed story, but **no home for a pasted image** → disqualifying under binary-first |
| 3 | Unified `docs:` typed refs (`attach:`/`repo:`) + orthogonal media | (re-run) | 35 | 35 — cleanest *conceptual* model (source×media 2×2), best ergonomics; hides the one decision that matters |
| 4 | Repo-paths default + attachment escape, **always by-reference** | (re-run) | **36.5** | **36.5 — base** |
| 5 | Content-addressed media-object store (+ refcount/GC + provenance) | (re-run) | 32.5 | 32.5 — best dedup + structural recoverability; net-new machinery, lowest fidelity |

Cross-judge: read-only **Gemini 3.1 Pro**. Judge and I agreed on base and the full ordering.

## How binary skewed the field (the point of the re-run)
The requirement moved scores but **not the winner**, which is itself the finding. Pure-attachments (C1) and the content-addressed store (C5) rose sharply from their text-only analogues — binary rewards custodying real bytes with exact dedup. Pure-paths (C2) **fell**: with binary in scope it has no home for a pasted screenshot (the marquee case). C4 still wins, and for a *stronger* reason than round 1: binary makes its "always by-reference, never embed" spine **load-bearing** (you can't base64 a PNG; you can't `range:`-slice one; duplicating large media is what the non-LFS tasks repo can't absorb), not stylistic.

## Base
**Candidate 4 (repo-paths default + attachment escape hatch).** Top on the ×2 recoverability criterion and the ×1.5 binary criterion simultaneously: uniform sha-pinned, always-by-reference delivery for text *and* binary, custody asymmetry (canonical paths, narrow attach escape) matching a corpus that mixes committed diagrams with pasted screenshots. Best fidelity to the existing materialize/header/executor rail. Its one real weakness (two operator fields leak custody into the mental model) is the deliberate cost of the "prefer paths" nudge.

## Grafts (with sources)
- **Candidate 3 → the single media-aware materializer + invisible-kind header** (§3). C3's orthogonal *source* (attach/repo) × *media* (text/binary) framing is why one `materialize_docs` cleanly covers all four cells and the agent sees a uniform read-only file list — its "build the delivery layer once" argument is the strongest case against a split implementation, and it survives even though C3's *single-field* surface was rejected. Also its `docs_embed`-threshold thinking informed keeping everything by-reference rather than a text-embed special case.
- **Candidate 5 → sha-verify-on-delivery + the media-derived-extension framing + the drift badge** (§3.2, §6). The worker re-hashes materialized bytes against the pin (`document_unavailable` → RETRY_ELSEWHERE); the scratch extension derives from `media`; a "source drifted — pinned to older version" badge makes staleness an auditable operator choice, not a silent fact.
- **Candidate 1 → the cross-machine blob-reachability critique**, answered head-on in §4 rather than papered over: path-doc blobs ride the `prepare_worktree_base` fetch the code step already performs (the pinned commit is an ancestor of `origin/main`), so no new fetch machinery; the residual force-push risk is outside Nightshift's controlled land path. C1's `.docstore/` dedup instinct is acknowledged as the escalation if attachment sharing ever bites.
- **Candidate 2 → the pure no-embed discipline** as the invariant the whole design rests on (nothing, text or binary, crosses the wire), plus its framing that a path-doc's recoverability is the pinned blob sha, never embedded content.

## Rejections (highest-signal part of the record)
- **Candidate 3's single unified `docs:` field (`attach:`/`repo:` scheme):** the closest runner-up and the main live fork. Cleanest mental model, but one field hides the one decision that actually matters (canonical-in-repo vs task-local bytes), letting operators drift toward attaching by ergonomic accident. Kept the *mechanism* unification (one materializer) and rejected the *surface* unification — two asymmetric fields make "prefer paths" a physical nudge. Recorded as a non-goal with the fork called out in the spec (§1, §10) so it can be swapped if the softer nudge is preferred.
- **Candidate 5's content-addressed store + refcount + GC:** elegant dedup and structural recoverability, but net-new machinery and the lowest fidelity to existing patterns; path docs already give free sharing (the canonical blob) and git delta-compresses identical attachment blobs. Deferred as a clean additive follow-up (an attachment *becomes* a store object) if sharing proves a real cost.
- **Embedding document content in the work order (the artifact-rail precedent):** binary detonates it — a PNG has no `text`, base64-in-JSON inflates ~1.33× and re-ships bytes on every retry/step. Deliberate departure from artifacts (which embed text): artifacts have no other home, documents do. Everything rides by-reference.
- **Routing image-docs to vision-capable workers:** couples doc-media to scheduling, which the requirements forbid; the model decides what it can use, and a text-only backend gets a truthful header annotation, never a failure.
- **Live path resolution without a pin / auto-refresh:** breaks determinism across a workflow's steps and cross-machine safety; re-pin is always an explicit operator act.
- **A hard-coded cap literal or a text-line cap:** a literal fails the "configurable under a code ceiling" requirement; a *line* cap can't bound a PNG. The cap is a *byte* cap referenced against `DOCUMENT_CAP_CEILING_BYTES`, with `range:` as the text-only fit-under-cap lever and binary bounded by the cap alone.
- **LFS / large-binary tier & engine-side paging into an over-cap doc:** out of scope by construction — the tasks repo is non-LFS and the engine never parses content.

## Verification
- Grounded against the workflows spec's real conventions before scoring: the `<task>.artifacts/` custody pattern, the `materialize_artifacts`/`_artifact_header` rail, the tasks-repo executor lane, `_EDITABLE_META_KEYS`/`ENGINE_META_KEYS`, and `_DOCUMENT_MAX_BYTES = 256 * 1024` in `manager/api_worker.py` (the ceiling constant is sited beside it; the existing artifact-submit cap is a separate concern).
- Recoverability invariant checked per candidate against "reconstruct every doc — text *and* binary — from tasks-repo + target-repo @ `base_ref` on any box": C4/C3/C5 satisfy it via a content-addressed sha; C2 satisfies it for paths but has no attachment story; C1 satisfies it trivially (bytes in the tasks repo) at the cost of duplication.
- The base's cross-machine claim (path-doc blob reachable on a stale clone) verified against the existing `prepare_worktree_base` fetch semantics — the pin rides the fetch worktree setup already does; no new transport.
- `range:`-on-binary confirmed as a parse/resolution error in every surviving candidate (a truncated PNG is not a PNG); the cap is the sole size lever for images.
- Dropouts: **Candidate 3 hit `resource_exhausted` on first launch**, relaunched with a length-focus instruction and completed (356 lines, all 8 grounding sections + `## Rationale`). Final: 5/5 completed.
- The shipped spec was rewritten **clean from the C4 base**, not patched from the round-1 (text-only) synthesis — greenfielding from the corrected requirements, per the explicit instruction that starting from the right requirements yields a different answer than massaging the old one.

## Open fork flagged in the spec, not resolved here
Two operator fields (`docs:` + `attachments:`) vs C3's single `attach:`/`repo:` field. The recommendation is two fields (nudge made physical); the one-field alternative is crisper but softer. §1/§10 record it so the surface can be swapped without touching the delivery mechanism underneath.
