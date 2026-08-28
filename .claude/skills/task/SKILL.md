---
name: task
description: Capture a prompt as a nightshift task brief — writes a standalone brief into a queue under .tasks/ and appends it to that queue's config.json order. Use when the user says "task", "/task", "new task", "add task", or asks to capture or queue work for nightshift.
---

# task

Capture the user's request as a nightshift task brief: a markdown file a worker later implements autonomously.
The worker has **no conversation context**, so the brief must stand on its own.

## Procedure

1. **Find the queue** — queues live under `.tasks/<queue>/`, each with its own `config.json` (`validate` command, `order` array, optional `sort`).
   Default to the queue matching the work's repo (for longitude work, `.tasks/longitude/`); use another queue only when the operator names it.
   No `.tasks/` in any ancestor → stop and tell the user this repo has no task queue.
2. **Title** — one concise line (becomes the commit title), preferring the user's own wording.
3. **Slug** — from the title: lowercase, `[^a-z0-9]+` → `-`, collapse repeats, trim `-`, max 48 chars.
   On collision with an existing file, append `-2`, `-3`, … or pick a distinct title.
4. **Write `.tasks/<queue>/<slug>.md`** in the format below.
5. **Queue it** — append `<slug>` to the `order` array in that queue's `config.json`, preserving all other keys.
   It lands at the end; the operator reorders in the UI.
6. **Report** — the path written and its queue position.

## File format

```markdown
---
title: <concise title>
automerge: false
split: false
---

<brief>
```

- Match the frontmatter conventions of the queue's existing briefs; `draft`, `disabled`, `priority`, and `evergreen` are operator knobs — leave them out unless told.
  Exception: a goal loop captured via the `until` skill sets `loop: true` — the brief carries the done-check, budget, and state-note path, and re-queues until the worker marks it complete.
- Omit `model` — the runner falls back to its configured default; add one only when the user explicitly asks.
- `split: true` (with a short body) when the runner should decompose the work into subtasks instead of implementing directly — use it when the right decomposition will only emerge during implementation.

## Decomposing a request

One brief = one coherent, independently landable change (one worker run, one landing).
The queue discipline in the `sniffer` skill applies:

- An already-atomic request stays one brief; don't manufacture decomposition.
- Several independently landable changes → several briefs; repeat the procedure per brief.
- `order` is the only sequencing mechanism: place a brief after anything it literally cannot land without; "logically follows" is not a dependency.
- Briefs that would touch the same files: merge into one, or order them explicitly.

## Writing the brief

- State what to build or change, and why, in clear prose.
- Use the user's explicit wording **verbatim** where they gave it (requirements, copy, UX behavior); do not paraphrase their intent away.
- Spell out concrete acceptance / done criteria so the worker knows when it's finished.
- Name the relevant files or areas if known — and, when adjacent work is in flight, what the worker must *not* touch.
- The worker runs the queue's `validate` command before landing — don't restate generic CI rules.
- Lightweight prose matching the tone of existing briefs, not a heavyweight spec.
