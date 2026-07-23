---
name: nightshift
description: Create a task in a running Nightshift queue via the manager API. Use when the user types "/nightshift <queue> <task description>", or asks to add/create/queue a nightshift task in a specific queue (e.g. "nightshift the longitude queue to fix X"). Targets a live manager over HTTP — no hand-editing of task files.
---

# nightshift

Create a **Nightshift** task brief in a named queue by calling the manager's HTTP
API — the same endpoint the operator UI uses (`POST /api/tasks`). This is the
robust path: the manager slugifies the title, writes the brief from the template,
appends it to the queue's execution order, and commits it. Never hand-edit the
content store to add a task.

Invocation: `/nightshift <queue> <task description>`
The first whitespace-delimited token is the **queue**; everything after it is the
**task description** (free prose). The worker that later runs this task has **no
conversation context**, so the brief you write must stand on its own.

## Procedure

1. **Resolve the manager URL.** Use `$NIGHTSHIFT_MANAGER_URL` if set, else
   `http://localhost:8800`. Call it `$URL` below.

2. **List queues to validate the target.** `GET $URL/api/playlists` returns
   `[{name, task_count, disabled}, ...]`.
   - If the request fails to connect, stop and tell the user the manager isn't
     reachable at `$URL` — they can start it with `just manager` or set
     `NIGHTSHIFT_MANAGER_URL`. Do **not** create anything.
   - Match the user's `<queue>` against the `name` fields: exact match first, then
     case-insensitive, then compare `slugify(<queue>)` (lowercase, each run of
     non-`[a-z0-9]` → `-`, trimmed) against each name.
   - The default/library queue is called **main**: if the user writes `main` or
     `library`, target the main queue by sending `queue=main`.
   - If nothing matches, **stop** and list the available queue names — do not
     guess and do not create a new queue. (`POST /api/tasks` will silently
     create a queue directory for an unknown name, which is almost never what
     the user wants — this validation step is what prevents that.)
   - If the matched queue is `disabled: true`, note it in your report (the task
     is created but the queue won't be scheduled until re-enabled).

3. **Write the title and brief.**
   - **Title** — one concise line; becomes the PR title and the task's filename
     slug. Prefer the user's own wording.
   - **Brief** — self-contained prose (see "Writing the brief"). Use the user's
     description as the spec.

4. **Build the request payload as a file** (avoids shell-quoting pitfalls). Write
   the JSON with the Write tool to a temp path, e.g. `/tmp/nightshift-task.json`:
   ```json
   {"title": "<title>", "text": "<brief>"}
   ```
   Only `title` and `text` are required. (The queue's model/draft/automerge
   defaults are applied by the manager; you can preview them at
   `GET $URL/api/task-defaults?queue=<queue>`.)

5. **Create the task:**
   ```bash
   curl -sS -X POST "$URL/api/tasks?queue=<queue>" \
     -H 'Content-Type: application/json' \
     -d @/tmp/nightshift-task.json
   ```
   - Success is `{"task": "<slug>", "title": "<title>"}`.
   - A `409` with `{"error": "task already exists: <slug>"}` means the slug is
     taken — adjust the title (append a distinguishing word) and retry.
   - Any other non-2xx returns `{"error": "..."}`; surface it to the user.

6. **Report** — the created task slug, the queue it landed in, and the UI link
   `$URL` (the operator can drag it to reorder). Mention the queue is disabled if
   it was.

## Writing the brief

Make the body stand alone — the worker sees only this text:

- State what to build or change, and why, in clear prose.
- Use the user's explicit wording **verbatim** where they gave it (requirements,
  copy, UX behavior). Do not paraphrase their intent away.
- Spell out concrete acceptance / done criteria so the worker knows when it's
  finished.
- Name the relevant files or areas of the codebase if known. Keep it to one
  coherent change.
- The worker runs the queue's validate command before submitting — don't restate
  generic CI rules.

Keep briefs lightweight prose, not a heavyweight spec.

## Notes

- This targets a **running manager**. It does not create branches, commit to the
  target repo, or run the task — it only enqueues the brief for a worker to pick
  up.
- To override the task's model or other frontmatter, tell the user they can edit
  the task in the UI at `$URL`, or ask and this skill can add supported fields
  (`repo`, `workflow`, `planner_model`) to the payload.
