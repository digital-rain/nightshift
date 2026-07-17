# Nightshift — Authoring Workflows

How to create and edit workflow definitions and their prompt charters — in the visual editor or by hand.
For the engine semantics (steps, routing, budgets, artifacts), see the workflows spec (`docs/spec/2026-07-16-workflows.md`); for the editor's design, see `docs/spec/2026-07-17-workflow-editor.md`.

## The files

Definitions and prompts are plain files; the editor is a view over them, not a replacement custody model.

| What | Shipped (read-only package assets) | Yours (operator files) |
|---|---|---|
| Workflow definitions | `assets/workflows/<name>.json` | `<workspace>/.nightshift/workflows/<name>.json` |
| Prompt charters (doc steps) | `assets/prompts/<name>.md` | `<workspace>/.nightshift/prompts/<name>.md` |

An operator file with the same name as a shipped one **shadows** it — your version wins everywhere the name is used.
Deleting the operator file restores the shipped original.
The UI marks shadows explicitly and warns before you create one.

Everything under `.nightshift/` is your tree: keep it in git and you get versioning, diff, and rollback for free.
The editor writes canonical JSON (two-space indent, trailing newline) precisely so those diffs read well next to hand edits.

## The visual editor

Open it from the gear menu (**Workflows**) or from the **Edit…** button beside the create panel's workflow picker.

- **Step cards (left).** One card per step, drag-to-reorder.
  List order matters: it defines the default `next` chaining and which artifacts are offered as a later step's inputs.
- **Graph preview (right).** A render-only picture of the definition: solid arrows for `next`, dashed labeled arrows for signal edges, a distinct `$end` terminal.
  Back-edges are highlighted and badged with the target's `max_visits` budget.
  Clicking a node focuses its card; edges are never drawn by hand — every destination is picked from the closed set of step ids + `$end`.
- **Live validation.** Every edit round-trips through the manager's own `parse_workflow` (plus the prompt-reference check), so the editor can never save a definition the loader would reject.
  The current error (or a green check) shows in the strip above Save; the offending card is outlined.
- **Raw JSON toggle.** A two-way JSON view of the same definition — edit either representation.
  Parse errors in the raw view block switching back.
- **Cycles.** A step reachable from itself must declare `max_visits`; the editor highlights the field as required the moment the graph detects a cycle.
- **Shipped definitions** open read-only with a **Duplicate** action; save the copy under a new name (or under the shipped name to shadow it — the editor says so).

Saves hot-reload the manager's definition set: the next dispatch sees the new graph, no restart.
If a *different* operator file on disk is broken (hand-edited badly), the save reports that error and the manager keeps serving the previous, working set until the file is fixed.

## Custom prompts

A doc step's `prompt` names a markdown charter.
Write your own in the prompt editor (opened from a step card's **Edit prompt…** or the Workflows screen), or drop a file into `.nightshift/prompts/`.
Prompt bodies are resolved on the **manager** and ride the work order, so remote workers never need a copy of your `.nightshift/`.

**The charter discipline (spec §8.2):** keep the body byte-stable across runs — no task-varying content.
The engine injects the task file path, artifact paths, and `$OUTPUT_FILE` in a header above the body, so implicit prompt-caching keeps hitting across runs of the same step.
A charter should tell the agent what document to produce and to write it to `$OUTPUT_FILE`; the run is read-only.

Deleting an operator-only prompt is refused while a loaded definition still references it; deleting a shadow just restores the shipped original.

## Hand-editing

Hand-editing stays fully supported.
A file edited on disk is validated at manager startup (fail-loud, including the prompt-reference check) and on the next editor save's reload.
To validate a hand edit without restarting, open it in the editor — the validation strip is the same `parse_workflow` verdict — or `POST /api/workflows/validate` with the candidate JSON.

## Editing while tasks run

Edits are safe, not clever (editor spec §6):

- In-flight attempts finish under the definition they were dispatched with.
- A task mid-workflow picks up the edited graph on its next cursor move.
- If an edit removes or renames the step a task's cursor sits on, the task blocks with `workflow '<name>' has no step '<id>'`; restore the step (or delete/re-create the task) to release it.
  The editor warns when a delete would strand queued tasks.
- Renaming a step resets its visit counter — a renamed step is a new step.
