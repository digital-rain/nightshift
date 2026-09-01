# Auto-import: a repo's `.tasks/` inbox as a queue Nightshift services

## Why

Nightshift already drains a target repo's publishing inboxes, but only when an
operator opens the picker and ticks briefs.
A repo whose tooling publishes work continuously into `.tasks/<queue>/` needs
that drain to happen on its own.

Auto-import is the existing import machinery on a clock, taking one brief at a
time so the host queue and the Nightshift queue take turns rather than one
flooding the other.

## Surface

Everything is on the Repos page (gear → Repos).

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

**Cadences.**
`cadences.auto_import_seconds` (default 60) is how often a bound queue checks
its inbox.
It is the latency of *noticing* new published work, not a dispatch rate: each
check pulls at most one brief, and only once the previous one has run.

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
2. Skip if the queue still holds an un-run imported brief — the round-robin
   gate (below).
3. Discover the repo's host queues, resolve this queue's binding, and scan
   exactly that one inbox tree of the repo's `main`.
4. Take the first brief: normalise its frontmatter, stamp its provenance,
   write it into the queue, commit the content store.
5. Remove the source from the repo's `main` as a repo-executor job.

Ordering matters and matches the manual import: the brief is durable in the
content store *before* the removal runs.
A failed removal is a warning, never unwound — the round-robin gate stops the
still-published source being pulled a second time while the first copy is
waiting to run.

### Round-robin

Two rules together produce strict alternation:

* **One in flight.** A queue holding an un-run imported brief pulls nothing
  new. A landed task's brief is dropped from the store, so "un-run" is just
  "the file is still there".
* **Behind the head.** A new import is inserted one slot after whatever the
  queue would dispatch next, not at the front.

So a queue holding `[n1, n2, n3]` becomes `[n1, h1, n2, n3]`; `n1` runs, `h1`
runs, the gate opens, and `[n2, n3]` becomes `[n2, h2, n3]`.
Inserting at the front instead would displace a task already about to run;
appending would let a busy queue starve its inbox indefinitely.
An empty queue puts the import at the front — there is nothing to alternate
with.

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

Keys outside that set pass through untouched.
They are inert to dispatch, and dropping them would lose author intent for
fields Nightshift may learn later.

## Consequences

An imported brief is an ordinary task from the moment it is written: same
dispatch path, same attempt row, same History entry, same statistics.
Nothing downstream knows it came from a host queue except the `imported_from`
stamp.
