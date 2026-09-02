# Auto-import: a repo's `.tasks/` inbox as a queue Nightshift services

## Why

Nightshift already drains a target repo's publishing inboxes, but only when an
operator opens the picker and ticks briefs.
A repo whose tooling publishes work continuously into `.tasks/<queue>/` needs
that drain to happen on its own.

Auto-import is the existing import machinery on a clock: each pass drains everything fresh the host queue publishes, exactly as a manual import would, and the pass recurs on a cadence — so work published throughout the day keeps flowing in, and the host queue empties rather than backing up.

## Surface

The controls are all on the Repos page (gear → Repos); the state they
produce is shown back on the queue itself.

**Known repos.**
Each row gains an `Auto-import .tasks` YES/NO segment.
The switch is repo-level because the inbox is a property of the repo, not of
any one queue bound to it, and because turning it on is what makes the repo's
host queues discoverable at all — a switched-off repo is never read.

**Queue bindings.**
A queue whose bound repo has the switch on gains a `Host task queue` dropdown
beside `Default repo`, listing the `.tasks/<name>/` subdirs the repo publishes
on `main`.

* Unset defaults to the subdir named after the queue — Nightshift queue
  `longitude` drains `.tasks/longitude`, which is the binding the operator
  almost always wants and needs no configuration.
* An explicit `— none —` opts this queue out while the repo's switch stays on
  for the queues beside it.
* A binding whose subdir is absent is preserved and shown tagged, the same way
  a queue's `repo` binding survives the repo being absent.

**Badge.**
A queue that is draining an inbox carries a green `A` badge — on its Playlists
row, and beside the queue name in the queue chrome — titled with the inbox it
drains. The controls sit two screens away from the queues they govern, so
without it the switch has no visible effect at all: a queue with auto-import
off is indistinguishable from one that has simply run dry, and turning it off
is silent.

The badge tracks configuration, not backlog: the repo's switch is on and the
queue is not opted out. It deliberately does *not* consult what the repo is
publishing right now, because a drained inbox is an empty directory and git
carries no empty directories in a tree — keying the badge on availability
would blink it out at exactly the moment auto-import had caught up. See
`configured_host_queue` vs `resolve_host_queue`.

**Cadences.**
`cadences.auto_import_seconds` (default 60) is how often a bound queue checks
its inbox.
It is the latency of *noticing* new published work: each open check drains every fresh brief the inbox publishes, and the next check picks up whatever was published since.

## Storage

| Setting | Where | Shape |
|---------|-------|-------|
| Repo switch | `<tasks_root>/config.json` (the store-level layer) | `auto_import_repos: ["longitude", …]` |
| Host binding | `<tasks_root>/<queue>/config.json` | `host_queue: "longitude"` (`""` = explicit none) |
| Provenance | the imported brief's own frontmatter | `imported_from: <repo>/<source-path>` |

Both settings live in the content store, so they commit and travel with the
rest of the queue configuration.

## Mechanism

The reconciler gains a duty, throttled per destination queue, that runs before
hold set/clear so a brief pulled this tick is reconciled as a candidate
immediately.
A workspace with no repo switched on makes no git call for the feature at all.

Per bound queue, per pass:

1. Skip unless the queue's repo has the switch on and is present.
2. Discover the repo's host queues, resolve this queue's binding, and scan
   exactly that one inbox tree of the repo's `main`.
3. Take every fresh brief — skipping published quarantines (below) and any
   source this queue has already imported: normalise each one's frontmatter,
   stamp its provenance, write it into the queue appended to the end of the
   execution order, then commit the content store once for the batch.
4. Remove the batch's sources from the repo's `main` as one repo-executor job.

Ordering matters and matches the manual import: the briefs are durable in the
content store *before* the removal runs.
A failed removal is a warning, never unwound. The still-published sources are
not pulled a second time: step 3 skips any source already stamped into this
queue (`imported_from`), which is the check that has to do the work, because an
import is rewritten on the way in and so never matches its source text
verbatim.

### FIFO drain

Imports append to the *end* of the destination queue's execution order, in the host queue's publish order — via the manual import's own copy step (`copy_repo_tasks`), so the two paths cannot drift.
The queue's own tasks keep their places, so manually added work still runs exactly where the operator put it, and an operator who wants a pulled brief sooner re-prioritises it by hand — the same lever every task has.
A queue holding `[n1, n2]` that drains `[h1, h2]` becomes `[n1, n2, h1, h2]`; an empty queue just starts the line.

### Holds

A brief published `quarantined: true` is not imported at all.
Quarantine is the publisher saying "do not run this", and Nightshift honours it at the door: the brief stays in the host queue, re-skipped on every pass and never removed, until the publisher clears the flag or deletes the file.
(Nightshift's own quarantine stays reserved for a task *this* manager stopped — importing one would put a verdict in the queue that this History has no run behind.)
The manual picker still offers such a brief, tagged `quarantined` and unticked by default, so importing one is a deliberate override rather than a side effect of drain-all.

A brief published `disabled: true` *is* imported, flag intact: it queues here held, and waits for this operator's eye before it dispatches.

### Frontmatter

Briefs in a repo's inbox are written by whoever publishes them, so their
frontmatter is untrusted: it may be absent, incomplete, or carry values
Nightshift cannot use.
Four fields are resolved against a default — `title` (falls back to the file
stem), `model`, `priority`, and `repo`.

A value is replaced when Nightshift could not act on it: a bare `model: sonnet`
would pin the task to something no worker advertises, and `priority: urgent`
means nothing — both would block work that nobody downstream can unblock, so
each takes the configured default instead.
Agnostic model keywords (`auto`, `max`) and provider-qualified ids are kept.

Keys outside that set — a published `disabled` hold included — pass through untouched.
They are inert to dispatch, and dropping them would lose author intent for
fields Nightshift may learn later.

## Consequences

An imported brief is an ordinary task from the moment it is written: same
dispatch path, same attempt row, same History entry, same statistics.
Nothing downstream knows it came from a host queue except the `imported_from`
stamp.
