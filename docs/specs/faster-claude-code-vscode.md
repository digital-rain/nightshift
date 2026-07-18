# Faster Claude Code in VS Code — Optimization Spec

**Subject:** A practical playbook for making **Claude Code in VS Code** as fast as — or, for agentic multi-file work, faster than — Cursor.
It separates the levers you fully control (approval latency, model/effort, context size, prompt-cache hits, tool/MCP overhead, output tokens) from the two things Cursor does that Claude Code cannot natively replicate (inline Tab completion, a warm embedding index), and gives a copy-paste config bundle plus tangible TODOs for each lever.
**Status:** Descriptive playbook — actionable now against Claude Code as of mid-2026 (`claude-code` ≥ v2.1.x; Opus 4.8 / Sonnet 4.6 / Haiku 4.5).
This is a developer-workflow spec, not a code change; where a TODO touches a config file the file governs.
**Why it lives here:** `claude-code` is nightshift's **default backend** and the latency baseline the in-house harness must beat (`docs/spec/agentic-backend.md`); the same levers that speed the interactive IDE also speed nightshift's headless runs (§8).
**Primary sources:** Claude Code docs — VS Code (`code.claude.com/docs/en/vscode`), settings (`/settings`), permission modes (`/permission-modes`), best practices (`/best-practices`); and nightshift's own `engine.build_claude_argv`.

---

## 0. The one idea

Claude Code re-processes its whole context on every turn, and **output tokens plus prefill dominate latency**.
So speed has one root cause and one cure: **keep the context small, the prompt prefix stable (cache-hot), the approvals automatic, and the model/effort matched to the task.**
Almost every TODO below is a variation on that sentence.

Cursor feels fast for two reasons Claude Code does not natively match — instant **Tab** completion and a **warm embedding index** — but for the *agentic edit loop* (multi-file changes, refactors, "do X across the repo"), a well-tuned Claude Code is competitive and often faster, because its `Edit` tool patches by string-replace (no full-file rewrite) and its prompt cache makes repeat turns cheap.

---

## 1. What you can and can't match vs Cursor

| Cursor capability | Claude Code equivalent | Verdict |
|---|---|---|
| Agentic multi-file edits | Agent loop + `Edit`/`MultiEdit` (string-replace, not full rewrite) | **Matchable / often faster** with the tuning below. |
| Instant inline **Tab** completion | None — Claude Code is an agent, not a completion model | **Not replicable.** Keep a dedicated completion provider (Copilot / Supermaven / Cursor Tab) if micro-completions are what "fast" means to you. |
| Warm **embedding index** retrieval | Agentic `Grep`/`Glob` + `CLAUDE.md` repo map + optional code-intelligence plugin/MCP | **Approximate.** Slower per first lookup, but no stale index; closeable with a good repo map (§3F). |
| Fast-apply / serving infra | Deterministic `Edit` tool already avoids full-file rewrites | **Sidestepped**, not matched. |
| Side-by-side diff review | Native VS Code diff via the extension's local MCP server | **At parity.** |

Honest expectation: tune for the **agentic loop** and you will beat Cursor on large changes and trail it on tiny local edits.
If your day is mostly tiny local edits, the biggest single win is pairing Claude Code (agent) with a Tab provider (completions) rather than trying to make the agent feel like Tab.

---

## 2. The latency model — where the time goes

Each turn's wall-clock ≈ **queue/network + prefill(input tokens) + thinking + output(tokens) + tool round-trips + your approval waits.**

- **Approval waits** are usually the biggest *interactive* cost — seconds of you clicking "Yes" per tool call. Free to remove (§3A).
- **Prefill** scales with everything in the window: `CLAUDE.md`, MCP tool listings, conversation history, files read. Shrinkable (§3C–E).
- **Output tokens** are the dominant *model* cost; terse output and targeted edits cut it (§3H).
- **Thinking** adds latency proportional to effort; dial it down for mechanical work (§3B).
- **Model choice** is a multiplier on all of the above; Sonnet ≫ Opus on speed, Haiku ≫ Sonnet (§3B).
- **Cache misses** make prefill expensive; a stable prefix keeps it ~10% (§3D).

---

## 3. The levers (with TODOs)

### A. Kill approval latency

Interactive "approve this tool?" prompts are pure dead time.

- [ ] Set the extension default to auto-accept edits: VS Code → Settings → Extensions → Claude Code → set **`claudeCode.initialPermissionMode`** to `acceptEdits` (options: `default`, `plan`, `acceptEdits`, `bypassPermissions`).
- [ ] Set the shared CLI/extension default in `~/.claude/settings.json`: `"permissions": { "defaultMode": "acceptEdits" }`.
- [ ] Pre-approve the commands you run constantly so `Bash` never prompts — add `permissions.allow` entries (e.g. `Bash(just *)`, `Bash(npm run test *)`, `Bash(git *)`); keep secrets in `permissions.deny` (`Read(./.env*)`, `Read(./secrets/**)`).
- [ ] For a fully trusted repo, consider `auto` mode (background safety checks; **must** be set in `~/.claude/settings.json`, ignored in project settings) or `bypassPermissions` for unattended runs.
- [ ] Use **Plan mode** only when you *want* the pause (architecture decisions), not for routine edits.

### B. Right-size the model and effort

- [ ] Make **Sonnet 4.6** your default (`/model sonnet` or set it in `/config`); reserve **Opus 4.8** for genuinely hard reasoning, switch with `/model opus` mid-session.
- [ ] Drop to **Haiku 4.5** (`/model haiku`) for syntax questions, simple edits, and file discovery.
- [ ] Lower thinking for mechanical work: **`/effort low`** (or `medium`) for renames/formatting/boilerplate; `/effort auto` to restore.
- [ ] Give investigation/verification to **subagents pinned to Haiku** (`model: haiku` in the subagent frontmatter) so search churn never touches your main, expensive context.

### C. Context hygiene (the highest-leverage habit)

- [ ] **`/clear` between unrelated tasks** — stale context is re-processed every turn; this is the single biggest speedup most people skip.
- [ ] **`/compact` at phase boundaries** (optionally `/compact focus on the API changes`) when one phase is done but state matters; don't wait for auto-compaction.
- [ ] Run **`/context`** when a session feels sluggish to see what's eating the window, then trim it.
- [ ] Use **`Esc`** to stop a wrong turn early (context preserved) and **`/rewind`** (or `Esc Esc`) to roll back conversation/code instead of letting a bad thread bloat the window.
- [ ] Keep **`CLAUDE.md` under ~200 lines**; move specialized guidance into Skills (loaded on demand, not always-on).
- [ ] Add a **`.claudeignore`** for build output, vendored deps, lockfiles, and large generated assets so they never get read into context.
- [ ] **Filter logs/test output** before pasting — don't dump full stack traces or whole files when a slice will do.

### D. Protect the prompt cache

Claude Code caches the stable prefix (system prompt + tools + `CLAUDE.md` + early history); cache reads are ~10% of input cost and skip prefill.

- [ ] **Don't edit `CLAUDE.md` mid-session** — every change invalidates the cache and forces a full re-prefill next turn. Batch `CLAUDE.md` edits, then start a fresh session.
- [ ] Keep the **MCP/tool set stable** within a session for the same reason (§E).
- [ ] Prefer **`/compact`/`/clear`** over manually deleting earlier messages, so the cache boundary moves predictably.

### E. Cut tool / MCP overhead

Every enabled MCP server injects its tool list into *every* request — permanent prefill tax and slower turns.

- [ ] Run **`/context`** (or `/mcp`) and **disable MCP servers you aren't actively using** this session.
- [ ] Prefer **CLI tools over MCP servers** where possible (`gh`, `aws`, `gcloud`, `sentry-cli`) — Claude just runs them as `Bash`, adding no per-tool listing.
- [ ] Scope the tool surface: only enable the connectors a given task needs; a lean tool list is a smaller prefix and fewer wrong-tool detours.
- [ ] Note: the VS Code extension's own local MCP server is cheap — it exposes only ~2 tools to the model (the rest are internal UI RPC), so it is not a meaningful tax.

### F. Approximate Cursor's retrieval

You can't get the embedding index, but you can stop Claude from grepping blind.

- [ ] Put a **repo map in `CLAUDE.md`**: the directory layout, where the important modules live, the build/test commands, and "to change X, look in Y." This converts several search round-trips into zero.
- [ ] Install a **code-intelligence plugin** for your language (precise symbol navigation + post-edit error detection) so Claude jumps to definitions instead of searching.
- [ ] **Point Claude at files directly** with `@`-mentions (the extension ties `@` to your current selection and line range) instead of letting it discover them.
- [ ] For big investigations, **delegate to a subagent** ("use a subagent to investigate how auth handles refresh") so the file-reading happens in a throwaway context and only the summary returns.

### G. VS Code extension setup for speed

- [ ] Install the official **Claude Code** extension (Anthropic); it gives native side-by-side diffs, `@`-mentions from selection, plan review, checkpoints, and automatic diagnostic sharing.
- [ ] Decide your surface: the **chat panel** (default) or check **"Use Terminal"** to run the CLI inside the integrated terminal — the CLI gets new features first and still drives the native diff viewer via the extension's local MCP server.
- [ ] Open **multiple conversation tabs** for independent tasks (parallel sessions) rather than serializing unrelated work in one window.
- [ ] Use **`/background`** to detach long-running tasks and **`/batch`** for parallel subagent fan-out; have Claude print long-running commands so you can watch them in the integrated terminal (extension background visibility is limited).
- [ ] Learn the fast keys: `Esc` (interrupt), `Esc Esc`/`/rewind` (roll back), and click the mode indicator to flip Plan ↔ acceptEdits without restarting.

### H. Reduce output tokens

- [ ] Tell Claude to **be terse and edit surgically** in `CLAUDE.md` ("make targeted `Edit`s; don't reprint whole files; keep prose short"). Output tokens are the dominant latency cost.
- [ ] Prefer **`Edit`/`MultiEdit`** (string-replace) over `Write` (full-file rewrite) for changes to existing files — fewer output tokens and a cleaner diff.
- [ ] Consider an **output style / concise mode** for routine work so explanations don't balloon the response.

### I. Infra and measurement

- [ ] Enable telemetry to *measure* instead of guess: in `~/.claude/settings.json` set `env.CLAUDE_CODE_ENABLE_TELEMETRY=1` (+ your `OTEL_*` exporter) and/or add a **status line** that shows model, context %, and timing.
- [ ] Track **time-to-first-token** and **tokens-per-task** before/after these changes; use `/context` and `/usage` to spot regressions.
- [ ] If on a cloud provider path (Bedrock/Vertex), pick the **region nearest you** and a stable network window for big refactors.

---

## 4. Copy-paste config bundle

**`~/.claude/settings.json`** (shared by CLI + extension):

```json
{
  "$schema": "https://json.schemastore.org/claude-code-settings.json",
  "permissions": {
    "defaultMode": "acceptEdits",
    "allow": [
      "Bash(just *)",
      "Bash(npm run test *)",
      "Bash(npm run lint *)",
      "Bash(git status)",
      "Bash(git diff *)",
      "Read(~/.zshrc)"
    ],
    "deny": [
      "Bash(curl *)",
      "Read(./.env)",
      "Read(./.env.*)",
      "Read(./secrets/**)"
    ]
  },
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1"
  }
}
```

**Project `.claude/settings.json`** (committed; repo-specific allow-list, no `auto`/bypass here):

```json
{
  "$schema": "https://json.schemastore.org/claude-code-settings.json",
  "permissions": {
    "allow": ["Bash(just validate)", "Bash(pytest *)", "Edit(src/**)", "Edit(tests/**)"],
    "deny": ["Edit(migrations/**)", "Read(./.env*)"]
  }
}
```

**`.claudeignore`** (keep big/irrelevant files out of context):

```
node_modules/
dist/
build/
.venv/
*.lock
*.min.js
**/__snapshots__/
**/*.generated.*
```

**`CLAUDE.md` skeleton** (lean, cache-stable, retrieval-replacing):

```markdown
# <Project> — agent guide

## Map
- src/<pkg>/ — <what lives here>
- tests/ — <how tests are organized>
- To change <feature>, edit <file>; tests in <path>.

## Commands
- Validate: `just validate`   (lint + type + tests)
- Test one: `pytest tests/<x>::<name> -v`

## Conventions
- Make targeted Edits; do not reprint whole files; keep prose short.
- <language/style rules that actually matter>
```

**VS Code `settings.json`** (extension behavior):

```json
{
  "claudeCode.initialPermissionMode": "acceptEdits"
}
```

---

## 5. Measure it (definition of "faster")

Don't trust feel. Pick 3–5 representative tasks (a one-file fix, a multi-file refactor, a "find and change across repo") and record, before and after:

- [ ] **Time-to-first-token** and **wall-clock to merge-ready** per task.
- [ ] **Input/output tokens per task** (telemetry or `/usage`) — the proxy for both speed and cost.
- [ ] **Approval-wait count** (should be ~0 after §3A).
- [ ] A side-by-side run of the *same* task in Cursor for the agentic cases, so "as fast as Cursor" is a measured claim, not a vibe.

---

## 6. Honest limits

- **No inline Tab.** If your speed pain is micro-completions, keep a completion provider; Claude Code won't be that.
- **First-lookup retrieval** is slower than a warm index; a good `CLAUDE.md` map and code-intelligence plugin narrow but don't erase the gap.
- **Serving latency** is Anthropic's, not yours — you optimize tokens and round-trips, not their infra.
- **`bypassPermissions`/`auto`** trade safety for speed; scope them to trusted repos and pair with a tight `deny` list.

---

## 7. Quickstart (do these five first)

1. [ ] `~/.claude/settings.json` → `permissions.defaultMode: "acceptEdits"` + an `allow` list for your test/lint/build commands.
2. [ ] VS Code → `claudeCode.initialPermissionMode: "acceptEdits"`.
3. [ ] Default to **Sonnet**; `/effort low` for mechanical tasks; **Haiku** for search/subagents.
4. [ ] `/clear` between tasks, `/compact` at phase boundaries; keep `CLAUDE.md` < 200 lines and don't edit it mid-session.
5. [ ] `/context` → disable unused MCP servers; add a `.claudeignore`.

---

## 8. Relationship to nightshift

Nightshift's headless `claude-code` backend is **already tuned** along these lines: `engine.build_claude_argv` runs `--allowedTools Bash,Edit,MultiEdit,Write,Read,Glob,Grep,LS --dangerously-skip-permissions --output-format stream-json`, i.e. zero approval latency and a fixed, lean tool set.
Two carry-overs:

- The interactive wins above (acceptEdits, model/effort, lean `CLAUDE.md`, MCP diet) apply verbatim to a developer driving nightshift tasks locally.
- The model/effort and token-discipline levers are the same ones the in-house agentic backend formalizes as owned config (`docs/spec/agentic-backend.md` §1.3, §5.4) — this playbook is the manual version of that backend's automatic token budget.
