---
name: task
description: Capture a prompt as a nightshift task brief — writes `.tasks/<slug>.md` and queues it in `.tasks/config.json`. Use when the user says "task", "/task", "new task", "add task", or asks to capture or queue work for nightshift.
---

# task

Capture the user's request as a **nightshift** task brief: a markdown file in `.tasks/`
that a nightshift worker later implements autonomously. The worker has **no
conversation context**, so the brief must stand on its own.

## Procedure

1. **Find the nightshift root** — the nearest ancestor of the cwd containing both
   `.tasks/` and `tools/nightshift/templates/task.md`. If there is none, stop and tell
   the user this isn't a nightshift repo.
2. **Title** — one concise line (becomes the PR title). Prefer the user's own wording.
3. **Slug** — derive from the title: lowercase, replace each run of `[^a-z0-9]+` with
   `-`, collapse repeated `-`, strip leading/trailing `-`, truncate to 48 chars (then
   strip any trailing `-`). This is both the filename and the queue id.
   If `.tasks/<slug>.md` already exists, append `-2`, `-3`, … or choose a distinct title.
4. **Write `.tasks/<slug>.md`** using the format below, with the brief as the body.
5. **Queue it** — append `<slug>` to the `order` array in `.tasks/config.json`
   (create `{"order": ["<slug>"]}` if the file is missing; preserve any other keys).
   It lands at the end of the queue; the operator can drag it to reorder in the UI.
6. **Report** — the path written and that it's queued.

## File format

```markdown
---
title: <concise title>
automerge: true
split: false
---

<brief>
```

- Omit `model` — the runner falls back to the lane default in
  `tools/nightshift/config.json` → `model`. Only add a `model:` line when the user
  explicitly asks to override it for this task (and only with a value listed in
  `scheduled_models`).
- `split: true` (with a short body) when the work should be decomposed into subtasks by
  the runner instead of implemented directly.

## Writing the brief

Make the body self-contained:

- State what to build or change, and why, in clear prose.
- Use the user's explicit wording **verbatim** where they gave it (requirements, copy,
  UX behavior). Do not paraphrase their intent away.
- Spell out concrete acceptance / done criteria so the worker knows when it's finished.
- Name the relevant files or areas of the codebase if known. Keep it to one coherent
  change.
- The worker runs `just validate` before opening a PR — don't restate generic CI rules.

Keep briefs lightweight prose (match the tone of existing `.tasks/*.md`), not a
heavyweight spec.
