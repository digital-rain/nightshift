# Nightshift — Slack Integration Specification

**Subject:** Slack integration — an outbound **activity feed** that announces task lifecycle to a channel, and an inbound **capture inbox** that turns ordinary Slack messages into queued tasks. Both are driven by a single long-lived control-plane daemon (`nightshift slackd`) using Slack **Socket Mode**, so no public HTTPS endpoint is required.
**Status:** Proposed — design for a feature **not yet implemented**. This document governs intent and shape; once built, the code governs and this spec should be revised to descriptive form (as `playlists.md` is).
**Primary sources (today):** `tools/nightshift/events.py` (event vocabulary, `fan_out`, `RunStore`), `tools/nightshift/run_local.py` (`main`, listener wiring), `tools/nightshift/server/player.py` (`fan_out` listener composition), `tools/nightshift/render_task.py` + `tools/nightshift/templates/task.md` (task materialisation), `.github/workflows/nightshift.yml` (remote plane), `services/dispatcher/receivers.py` (`SlackReceiver`, Block Kit reference).
**New code (to add):** `tools/nightshift/slack/{__init__,notify,intake,slackd,threads}.py`, a `slack` block in `tools/nightshift/config.json`, and `just nightshift-slackd`.

---

## 0. The one idea

Nightshift already speaks two dialects of the same lifecycle: the **local plane** emits a
typed `Event` stream (`tools/nightshift/events.py`) through composable listeners
(`fan_out`), and the **remote plane** (`.github/workflows/nightshift.yml`) expresses the
same lifecycle as **pull requests** (opened → CI → merged/closed). Slack integration is a
thin adapter over both:

- **Outbound:** translate lifecycle into **one threaded card per task** in an activity
  channel — updated in place rather than spammed as one message per event.
- **Inbound:** treat a dedicated channel as a **capture inbox** — any message becomes a
  candidate task, normalised into the existing `.tasks/<slug>.md` format with **no required
  fields**, behind a confirmation gate.

A single **Socket Mode daemon** owns both directions plus a GitHub poll that folds
remote-plane PR state into the same task threads.

---

## 1. Goals & non-goals

**Goals**
- Announce task activity (run start/finish, task start, phase changes, completion, failure,
  skip/abort, and — via the remote plane — proposed/merged) to an **activity channel**.
- Let a person add a task from **any device, including mobile**, by posting to an **intake
  channel**, with zero syntax required and optional power-user directives.
- Keep the channel **scannable**: grouped per task, low message volume.
- Reuse existing machinery: the event stream, `render_task.py`, the playlist queue, and the
  Block Kit / `chat.postMessage` patterns already in `services/dispatcher/receivers.py`.
- Degrade safely: when unconfigured, every hook is a no-op; Slack failures never break a run.

**Non-goals (this spec)**
- Replacing the UI (`server/`) — Slack is an adjunct, not a control surface for runs.
- A full bidirectional chatops console (pause/skip from Slack) beyond the buttons in §8.
- Multi-workspace / multi-tenant Slack. One workspace, one bot.

---

## 2. Architecture

Two planes, one daemon:

```
                    ┌──────────────────────────────────────────┐
                    │            nightshift slackd               │
                    │        (slack-bolt, Socket Mode)           │
   Slack  ◀────────▶│  outbound: notify.py   inbound: intake.py  │
                    │  thread store: threads.py (slug→thread_ts) │
                    │  github poll: remote PR state reconcile    │
                    └───────▲───────────────────────▲────────────┘
                            │                       │
        local plane ────────┘                       └──────── remote plane
   run_local.py / player.py                    .github/workflows/nightshift.yml
   emit Event stream  ──►  notify listener      PRs labelled `nightshift`
                                                + optional notify steps
```

- **Outbound, local:** `notify.py` exposes a `Listener` (`Callable[[Event], None]`)
  registered alongside `writer.emit` via `fan_out` in `run_local.py` and `player.py`. It
  posts/edits cards directly (in-process); the daemon is **not required** for local
  outbound.
- **Outbound, remote:** the workflow posts a "proposed" card when a PR opens; the daemon's
  GitHub poll reconciles merged/closed/parked into the same thread. Both key off the
  `[task:<slug>]` title marker the workflow already uses
  (`.github/workflows/nightshift.yml`).
- **Inbound:** only the daemon does inbound. Socket Mode means no inbound networking, so it
  runs fine on a laptop behind NAT, next to the UI.
- **Thread store** (`threads.py`): a small JSON map `slug → {thread_ts, channel}` persisted
  at `.tasks/slack-threads.json` (and per-playlist `.tasks/<name>/slack-threads.json`) so a
  task captured in Slack, run locally, then merged remotely shares **one** thread across
  restarts and planes.

---

## 3. Configuration & secrets

A new `slack` block in the layered config (resolved by `spawn_daily.resolve_config`, so
`.tasks/config.json` and a playlist may override it):

```jsonc
// tools/nightshift/config.json (shipped defaults)
"slack": {
  "enabled": false,
  "activity_channel": "#nightshift-activity",
  "intake_channel": "#nightshift-intake",
  "allowed_users": [],          // Slack user IDs permitted to enqueue; [] = nobody
  "require_confirmation": true, // Enqueue button gate before a task lands
  "announce_task_log": false,   // keep TASK_LOG out of Slack by default (noisy)
  "thread_per_task": true,
  "default_enqueue": "commit"   // "commit" to main | "pr" (remote-first)
}
```

Secrets (never in config): `SLACK_BOT_TOKEN` (xoxb-…) and `SLACK_APP_TOKEN` (xapp-…, Socket
Mode). Loaded from `.env` locally (`run_local.load_dotenv`) and GitHub Actions secrets
remotely. When `enabled` is false or tokens are missing, all hooks no-op.

**Slack app manifest:** Socket Mode on; bot scopes `chat:write`, `channels:history`,
`reactions:read`, `reactions:write`, `commands`, `app_mentions:read`; event subscriptions
`message.channels`; interactivity enabled (for buttons in §8).

---

## 4. Outbound — threaded activity cards

### 4.1 Event → channel mapping

The event vocabulary lives in `tools/nightshift/events.py`. The notifier maps it to a
**single parent card per task** plus terse threaded replies:

| Event (`events.py`) | Channel action |
|---|---|
| `RUN_STARTED` | Post a **run summary** card: `Run started · N tasks · queue <name>`. Hold its `ts`. |
| `TASK_STARTED` | Create the task's **parent card** (title + status `▶ running`), record `thread_ts` in the store. |
| `TASK_STATUS` (phase) | Edit the parent card's status line: `worker → 🧪 validate → 💾 commit → 🩹 resolve`. |
| `TASK_RESULT` | Finalise the parent status (`✅ landed` + short SHA / `❌ failed` / `⏭ skipped` / `🛑 stopped` / `⚠ aborted`) and post a **threaded reply** with `result_line`, `error`, `failure_kind`. |
| `RUN_FINISHED` | Edit the run summary card: `Run finished · k landed · m failed · s skipped`. |
| `TASK_LOG` | Ignored unless `announce_task_log` (then last-line tail to thread, throttled). |

Status emoji/text map 1:1 to the terminal statuses defined in `events.py`
(`completed`/`error`/`skipped`/`stopped`/`ABORTED`) and the `phase` values set by the
engine. Failure cards surface `failure_kind` (`merge_conflict`, `validation_error`,
`worker_error`, …) so the cause is visible without opening a log.

### 4.2 Card shape

Block Kit, built the same way as `services/dispatcher/receivers.py:build_block_kit`:

- **Parent card (per task):** a `section` with `*<title>*` + status line + queue/run
  context; a `context` block with elapsed time and model. Buttons added in §8.
- **Run summary card:** counts + queue name + launched-by.
- Replies: plain `mrkdwn` sections kept under a few lines (errors truncated, link to UI/PR
  for detail).

### 4.3 Thread identity & idempotency

`threads.py` persists `slug → {channel, thread_ts}`. Rules:

1. On `TASK_STARTED`, if the slug already has a `thread_ts` (e.g. captured via intake or a
   prior remote "proposed" card), **reuse it** — update that parent instead of posting new.
2. Posting is **best-effort**: a Slack error is logged and dropped; the run continues. A
   missing `thread_ts` on an update falls back to a fresh post.
3. The notifier is safe to attach to both `run_local.py` and `player.py` concurrently for
   different runs; writes to the store are serialised.

### 4.4 Wiring (local)

- `run_local.py:main` — extend the `listeners=[...]` list passed to `run_queue` with the
  notifier listener (currently `make_stdout_listener()` + `writer.emit`).
- `server/player.py` — add the notifier to both `fan_out([...])` compositions (the resolve
  path and the main run loop) so UI-launched runs announce too.

---

## 5. Inbound — the capture inbox

A dedicated **intake channel** where **any top-level message is a candidate task**. The
daemon (`intake.py`, driven by `slackd.py`) runs this flow:

1. **Receive** a `message.channels` event in `intake_channel` (ignore threaded replies,
   edits, bot messages, and messages from users not in `allowed_users`).
2. **Normalise** the text into a task via the existing worker backend (Claude): derive a
   concise `title`, clean the body, and extract any directives (§6) into frontmatter.
   Free-form prose in → structured `.tasks/<slug>.md` out. A pasted `---` frontmatter block
   bypasses the LLM and is honoured verbatim.
3. **Confirm** (when `require_confirmation`): the bot replies **in-thread** with the
   rendered task (title + frontmatter + body preview) and **Enqueue / Edit / Cancel**
   buttons. *Edit* lets the author tweak directives in a follow-up; *Cancel* drops it.
4. **Enqueue** on confirmation: materialise the file with the existing
   `render_task.py`/`templates/task.md` path under the target queue
   (`.tasks/` or `.tasks/<playlist>/`), then land it per `default_enqueue`:
   - `commit` — commit to local `main` and push, so **both planes** see it immediately.
   - `pr` — open a small PR (remote-first teams; reviewable).
5. **Acknowledge:** react ✅ on the original message and reply with the queued task name and
   a link (to the UI queue and/or the file/PR).

Errors at any step react ⚠ and reply with the reason; nothing lands silently.

---

## 6. Intake message format (mobile-first, all-optional)

The entire required format is: **first line = title, the rest = description.** Everything
else is inferred or defaulted. Optional directives are parsed out of the body,
order-independent, and map onto the existing frontmatter understood by the engine and
`templates/task.md` (`title`, `model`, `draft`, `automerge`, `split`, `loc`, `turns`,
`after`):

| Directive | Frontmatter / behaviour | Default |
|---|---|---|
| `#draft` / `#automerge` | `draft` / `automerge` | inherited from config |
| `#opus` / `#sonnet` / `model: <id>` | `model` | inherited |
| `queue: <name>` / `#q-<name>` | target playlist (`.tasks/<name>/`) | main `.tasks/` |
| `#now` | prepend to the queue's `config.json` `order` | appended |
| `loc: N` / `turns: N` | `loc` / `turns` | unset |
| `after: <slug>` | `after:` dependency | none |
| `#split` | `split` | false |

Example intake message:

```
Fix flaky timezone test in capital-flow

The capital-flow test fails intermittently around DST. Suspect a naive
datetime in the rollup — make it tz-aware.

#sonnet #automerge queue: experiments after: tz-helpers
```

…normalises to (shown in the confirmation reply, then written via `render_task.py`):

```yaml
---
title: Fix flaky timezone test in capital-flow
model: claude-sonnet-4-6
draft: false
automerge: true
split: false
after: tz-helpers
---
The capital-flow test fails intermittently around DST. Suspect a naive
datetime in the rollup — make it tz-aware.
```

`queue: experiments` routes the file under `.tasks/experiments/` (creating the playlist if
needed, per `playlists.py` rules) rather than the main queue.

---

## 7. Remote plane lifecycle

The remote plane has no Python event stream, so lifecycle is read from GitHub:

- **Proposed:** add an optional step to `.github/workflows/nightshift.yml` that posts a
  "task proposed" card when a worker PR opens (keyed by the `[task:<slug>]` title marker the
  workflow already writes). If the slug already has a `thread_ts`, the daemon reuses it.
- **Merged / closed / parked:** the daemon polls `gh pr list --label nightshift` (and the
  parked-issue convention in the workflow) on an interval, diffs against last-seen state,
  and posts threaded updates: `🔀 merged`, `🚫 closed unmerged`, `🅿 parked (n closes)`.
- The GitHub poll is **optional in v1**; without it, the workflow's "proposed" step still
  gives a useful remote signal and merges show up via the normal GitHub→Slack app if one is
  installed. Full thread reuse requires the poller.

---

## 8. Interactivity (buttons & slash command)

Parent cards carry actions (handled by `slackd.py` interactivity handlers, allowlisted):

- **View PR** / **View log** — deep links (PR URL; UI run/log URL).
- **Re-run** — re-enqueue the task (same materialised spec).
- **Disable** — set `disabled: true` in the task's frontmatter (honoured by both planes).

Slash command `/nightshift status` returns the active queue + current run snapshot from
`RunStore.list_runs()` / the player state. All write actions are gated by `allowed_users`.

---

## 9. Security model

Enqueuing starts an **autonomous code-writing agent**, so inbound is the sensitive surface:

1. **Allowlist:** only `slack.allowed_users` may enqueue or use write buttons. Everyone else
   is ignored (silent for messages; ephemeral "not permitted" for button clicks).
2. **Confirmation gate:** `require_confirmation` forces an explicit **Enqueue** click before
   anything lands; the parsed spec is shown first.
3. **Channel scoping:** only `intake_channel` is monitored for capture; other channels are
   ignored even if the bot is present.
4. **Provenance:** materialised tasks record the Slack author and message permalink (in the
   commit message / PR body) for auditability.
5. **No secret echo:** the notifier never posts tokens, env, or full logs (only tails when
   explicitly enabled), and truncates errors.

---

## 10. Phased delivery (maps to `.tasks/nightshift/` tasks)

| Phase | Task file | Deliverable |
|---|---|---|
| 0 | `slack-phase-0-app-and-config` | Slack app/manifest, secrets loading, `slack` config block + a `slack/` package skeleton with a no-op notifier and `threads.py`. |
| 1 | `slack-phase-1-outbound-notifier` | `notify.py` event→card mapping + thread store, wired into `run_local.py` and `player.py`. (Highest value, lowest risk.) |
| 2 | `slack-phase-2-inbound-capture` | `slackd.py` Socket Mode app + `intake.py` normalisation, confirmation buttons, enqueue via `render_task.py`; `just nightshift-slackd`. |
| 3 | `slack-phase-3-remote-lifecycle` | Workflow "proposed" step + daemon GitHub poll reconciling merged/closed/parked into task threads. |
| 4 | `slack-phase-4-interactivity` | Parent-card buttons (View PR/log, Re-run, Disable), `/nightshift status`, restart backfill from `RunStore`. |

Each phase is independently shippable and leaves the system working when unconfigured.

---

## 11. Invariants

1. **Unconfigured = invisible.** With `slack.enabled` false or tokens absent, every hook is
   a no-op and no run behaviour changes.
2. **One thread per task.** A slug maps to a single `thread_ts` reused across intake → local
   → remote and across restarts (`threads.py`).
3. **Best-effort, never fatal.** A Slack API failure is logged and dropped; it must never
   fail or stall a task run.
4. **No task lands without consent.** Inbound requires an allowlisted author and (by
   default) an explicit Enqueue click.
5. **Reuse, don't fork.** Tasks materialise through the existing
   `render_task.py`/`templates/task.md` path and the existing queue/playlist rules; Slack
   adds no second task format.
6. **Socket Mode only.** No public HTTPS endpoint is introduced for inbound.

---

## 12. Out of scope / open questions

- **Daemon hosting:** standalone `just nightshift-slackd` (keeps Nightshift self-contained)
  vs. folding into `services/dispatcher` (already has `slack-bolt`). Spec assumes
  standalone.
- **Enqueue landing default:** `commit`-to-main (assumed) vs. always-PR.
- **Remote thread fidelity:** GitHub poller is optional in v1; without it remote updates are
  less tightly grouped.
- No threading of `TASK_LOG` by default (volume); opt-in only.
- No multi-workspace support; one bot, one workspace.
