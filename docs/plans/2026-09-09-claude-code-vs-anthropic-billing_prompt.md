# Prompt: split claude-code (subscription) from anthropic (API) billing in Nightshift

*Scoping prompt for a fresh Claude session (Fable 5.1) in `~/workspaces/nightshift`.
Written 2026-09-09 by a prior session that verified everything below on this
machine but had too much accumulated context to do the build cleanly. Deliverable:
a sessionized plan in `docs/plans/` (date-prefixed, e.g.
`2026-09-09-claude-code-billing-split-plan.md`), then the fixes themselves. This
is a worker fix, and it is important to get right.*

---

## The problem

Nightshift runs are billing the **Anthropic API** — the operator's console shows
~$200 on 2026-09-08 and ~$1000 September-to-date — even though the `claude` CLI
is authenticated with a **claude.ai Max subscription** and the queue's models are
`claude-code/`-prefixed.

Goals, in the operator's words:

1. Differentiate anthropic-API billing from claude-code (subscription) billing.
2. Route `claude-code/` prefixes to the Claude Code CLI using its existing
   authentication.
3. Leave `anthropic/` prefixes on the Anthropic Messages API.
4. Follow **declared** (settings-page / config) fallbacks rather than hard-coded
   fallback literals.
5. Fix stats counting so spend numbers can be trusted.

## The spend is already attributed (done 2026-09-09 — do not redo, do re-verify)

Run records live in `~/workspaces/.nightshift-worker/runs.jsonl`
(`worker/local_store.py`: `LOCAL_DIR=".nightshift-worker"` under the workspace,
`RUNS_FILE="runs.jsonl"`; 406 records). September, summed `cost_usd` by model:

```
claude-code/claude-opus-5    166 runs   $970.30
claude-code/claude-fable-5     6 runs   $132.85
daily: 09-02 $334.70 · 09-04 $260.44 · 09-08 $279.96 · 09-03 $108.25 · rest < $70
```

Every dollar is `backend: "claude-code"` — **CLI runs, not the API backends**.
The totals line up with the operator's console figures, so the CLI on the worker
path is billing the **API key**, not the subscription. (Caveat to carry into the
plan: the CLI emits `total_cost_usd` in *both* billing modes — under
subscription it is a notional list-price figure. runs.jsonl alone can't
distinguish; it is the match with the operator's console statement that closes
the loop. After the fix, these same records must stop being read as dollars —
that's goal #5.)

The competing hypothesis was checked and is OFF: `config/worker.py:41` defines
an operator toggle `nightshift.enabled` ("Use in-house agentic harness") that
routes runs through `NightshiftAgentBackend` → the Anthropic API directly
(`backends.py:856+`, `agent/loop.py`/`agent/transport.py`). Deployed
`~/workspaces/nightshift/.nightshift/settings.json` has only
`{"worker_backend": "claude-code"}` — harness disabled, default false. Keep it
in view for goal #4 (it's a declared routing surface), but it is not the bill.

Longitude's LLM gateway is a separate, additional consumer of the same API key —
sibling effort at
`~/workspaces/longitude/docs/plans/2026-09-09-claude-code-provider-plan.md`.
Nightshift's ~$1100 of run records ≈ the whole "~$1000" console figure, so
nightshift is the dominant term; don't try to explain longitude's share from
nightshift data or vice versa.

## Root-cause mechanism (verified file:line — re-verify, then kill it)

Nightshift's *routing* is correct: `model_id.py` splits `provider/model` on the
first `/`; `backends.py` registers `claude-code` (agentic CLI, `DEFAULT_BACKEND`),
`anthropic` (Messages API, one-shot), `cursor`, `antigravity`, `ollama`,
`ollama-cloud`, `nightshift` (harness). The `claude-code/` models DID run through
the CLI. The bug is **auth leakage into the CLI subprocess**:

- `preflight.py:334-337` — `check_preconditions` **hard-exits unless
  `ANTHROPIC_API_KEY` is set** ("Add it to .env or export it in your shell").
  The operator obeyed: the key is live in `~/workspaces/nightshift/.env` line 9.
- `prompts.py:262` (`worker_env`) — child env is `os.environ.copy()` + PATH /
  PYTHONPATH tweaks; nothing is scrubbed. (`run_local.py:302` loads `.env` into
  the process, so the key is in `os.environ`.)
- `backends.py` `ClaudeCodeBackend.run` / `complete_text` pass that env straight
  to the `claude` subprocess. With the key present, the CLI bills it.

Precedence subtlety, measured on this box 2026-09-09 (CLI v2.1.183): in an
*interactive user shell*, `claude auth status` with the real key in env reports
`authMethod: "claude.ai"` **plus** `apiKeySource: "ANTHROPIC_API_KEY"`;
`~/.claude.json` has `customApiKeyResponses: null`; a print-mode run with a
*bogus* key succeeded (so OAuth served it there). Yet the worker-path runs
billed the API. Precedence evidently differs between contexts and CLI versions —
**do not build on precedence**. Build on explicit env: when the operator wants
subscription billing, the key must simply not be in the CLI subprocess env.

## Shape of the fix (validate and adjust; don't treat as gospel)

- **Per-backend env discipline, not a global scrub.** The `anthropic` API
  backend legitimately *requires* the key (`backends.py:629-631`), and so does
  the harness's anthropic vendor. `ClaudeCodeBackend` must build its subprocess
  env by **removing** `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
  `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`
  when subscription billing is selected — in `run()`, `complete_text()`
  (manager-side brief enhancement, `enhance.py`), and every other spawn path
  (`resolve_runner.py`, workflow doc steps). Add a declared setting (suggested:
  `claude_billing: auto | subscription | api`, default `auto` = subscription
  when `claude auth status` shows `loggedIn` + `authMethod: "claude.ai"` for
  the spawning user, else API key with a loud log line). `auto` keeps headless
  boxes with only a key working.
- **Preflight** (`preflight.py:334-337`): stop demanding `ANTHROPIC_API_KEY`
  unconditionally. Require what the *selected* backends/billing modes need:
  subscription/auto claude-code → `claude auth status` login check (cached);
  the key only when an API-billed path is actually configured. End state the
  plan should recommend to the operator: remove the key from `.env` entirely
  unless the API backends are in use — the belt-and-braces that makes
  regression impossible.
- **Never pass `--bare`** to the CLI (documented: "Anthropic auth is strictly
  ANTHROPIC_API_KEY … OAuth and keychain are never read"). Not used today;
  keep it that way.
- **Stats (goal #5).** Record billing mode with every run: extend
  `WorkerResult` / the `AgentStreamParser` result consumption
  (`backends.py:126, 241-246`) and the persisted payloads
  (`worker/execute.py:303,583`, `local_store.py`) with
  `billing: "api" | "subscription"`, derived from the spawn-time auth decision
  (never guessed after the fact). `cost_usd` stays, but rollups/stat panels
  (`local_store.stats`, the worker UI stat panel — see recent commit "task:
  change stat panel") must sum **actual** (api) and **notional**
  (subscription) separately. `total_cost_usd` from the CLI wins today with the
  owned `price.py` sheet as fallback — keep that, it's the right precedence in
  both modes. Do not rewrite historical records; September was genuinely
  API-billed.
- **Declared fallbacks (goal #4).** Audit for call-site literals that shadow
  declared config: `DEFAULT_BACKEND = "claude-code"` (`backends.py:987`) and
  `get_backend`'s silent fallback; bare-model legacy qualification
  (`config/worker.py:273-277, _qualify`); `auto_model`/`max_model` resolution;
  `enhance.py`'s model choice (`enhance_brief_model` in deployed manager
  config); `spawn_daily.py`; harness vendor/model defaults. Settings-schema
  defaults that render in the UI (like `NightshiftBackendConfig` fields) are
  legitimate declarations; literals buried in call sites that ignore what the
  settings surface declares are the bug. Missing declaration → error naming
  the setting, never a silent literal. Check per-repo `.nightshift/worker.json`
  / `manager.json` (e.g. `~/workspaces/longitude/.nightshift/`) too — that's
  where models lists, `auto_model`, `max_model`, `enhance_brief_model` deploy.

## Useful CLI facts (verified v2.1.183, this machine)

- `claude auth status` → JSON: `loggedIn`, `authMethod`, `apiProvider`,
  `apiKeySource` (present only when a key is visible in env), `subscriptionType`.
  Machine-readable differentiator for goal #1 and the `auto` decision.
- Print mode: `--output-format json` → `result`, Anthropic-shaped `usage`,
  `stop_reason`, `total_cost_usd` (notional under subscription).
  `--output-format stream-json --verbose` (the agentic path today) emits the
  same fields on the final `result` event. `--json-schema '<schema>'` gives
  enforced `structured_output` if `complete_text` ever wants it.

## Working agreements (operator's standing preferences — follow them)

- Never edit main directly: worktree → validate → squash-merge.
- Worktree `.venv` symlinks to main and its editable install points at main's
  `src/` — validate with `PYTHONPATH=$PWD/src:$PWD/tests` (see `worker_env`'s
  own docstring for the same trap).
- `just validate` skips `tests/ui/*.mjs` — regression coverage goes in pytest.
- The VM clock can drift hours behind after suspend — check the clock before
  trusting run-record timestamps.
- Test CLI spawns against a **fake `claude` script on PATH** emitting canned
  json / stream-json (success, error, auth-status variants, hang) — never burn
  subscription or API calls in tests. One operator-run live smoke script is
  fine: a claude-code run with the fix, then the operator confirms the
  Anthropic console shows no new API usage (the console is the only ground
  truth for "not billed") while `runs.jsonl` shows `billing: "subscription"`.
- When done, push the branch and open a PR with `gh`; don't stop at a local
  commit.

## Deliverables, in order

1. `docs/plans/2026-09-09-claude-code-billing-split-plan.md` — re-verified
   findings with file:line cites (corrections to this prompt stated loudly),
   then independently-landable sessions with verify gates. Flag anything only
   the operator can verify (the console, other machines' auth state) rather
   than assuming it.
2. The fixes, session by session, each validated before merge.
3. An operator runbook section: the `claude_billing` setting, removing
   `ANTHROPIC_API_KEY` from `.env` (or scoping it to API backends), confirming
   subscription billing took effect, and how to read the new actual-vs-notional
   stat split.
