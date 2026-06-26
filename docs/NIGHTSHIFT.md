# Nightshift charter

Hard constraints for the nightshift task lane.
The nightshift prompt includes this document by reference.

## Relationship to janitor charter

The nightshift inherits *structural* patterns from the janitor lane (worktree isolation,
one-concern-per-PR, forbidden paths, pre-push validation, budget caps) but operates under
fundamentally different rules: **behavior change is licensed by a task spec.**

## Branching and isolation

- Branch fresh from `origin/main` each run, in an isolated worktree.
- Never reuse a branch.
- Branch name: `task/<NN>.<short-name>` (matching the task file).

## Scope

- **One task per PR.** The PR implements exactly one `.tasks/<NN>.<short-name>.md` spec.
- **Regular tasks:** the PR **includes the deletion** of its own task file — merge atomically
  removes the task from the queue. Close-unmerged returns it automatically.
- **Evergreen tasks** (listed in `evergreen_tasks` in `tools/nightshift/config.json`):
  the task file is **not deleted**. Reset it from
  `tools/nightshift/templates/<task>.md` so the queue is fresh for the next day.
- **Daily queue files** (`00._questions`, `00._todo`): when items are
  present, `tools/nightshift/spawn_daily.py` creates one spawned task per item,
  resets the daily file from its template, opens a **`nightshift-dispatch` PR**
  (never pushes directly to `main`), auto-merges when checks pass, then launches one
  worker per spawned task. Optional frontmatter on the daily file sets the model
  (and other fields) inherited by each spawned task; absent fields use
  `tools/nightshift/config.json` defaults.
- Diff cap per `tools/nightshift/config.json` field `diff_cap_lines` (default 1500);
  per-task frontmatter `loc` (or legacy `diff_cap:` header default) overrides.
  Exempt from the cap: paths in `diff_cap_exempt_paths` (fixtures, `.tasks/` queue files,
  docs, prompts, and other prose — see config). Code changes still count.
  A task that cannot fit under its cap falls back to a **decomposition run**
  (see "Split tasks") instead of shipping an oversized PR or dead-ending in a draft.
- Forbidden paths per `forbidden_paths` in config (read from `origin/main`, never PR head).
  Template files under `tools/nightshift/templates/` are read-only: copy into `.tasks/`
  and edit the copy; do not commit template changes.
  A task spec requesting changes to forbidden paths is **declined** — file an issue explaining why.

## Task queue — `.tasks/`

- Files: `.tasks/<NN>.<short-name>.md`. Lexicographic order determines pickup order.
- Zero-padded numbers sort ahead; number with gaps (10, 20, 30).
- Optional YAML frontmatter (delimited by `---`) may appear at the top of the file.
  All fields are optional; the entire frontmatter block may be absent.
  See `tools/nightshift/templates/task.md` for the canonical example.

### Frontmatter fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `title` | string | filename sans numbered prefix and extension | PR title (replaces `[task:$TASK]`) |
| `model` | string | `auto` (config `default_model`) | Worker-interpreted model. `auto` lets the worker pick the most cost-effective capable model; `max` uses its most capable configuration; an explicit id uses the `provider/model` format (e.g. `cursor/gpt-5`, `claude-code/claude-opus-4-8`) and routes to **any worker that advertises that exact qualified id** (capability-based routing). A worker can remap an explicit id via its `model_aliases`. Leave unset unless the task needs a specific capability. In the GitHub Actions path an explicit model outside `scheduled_models_allow` makes the task **dispatch-only** (nightly skips it; runs only via `workflow_dispatch` with `task=<name>` as a cost guard). The UI model dropdown is populated from live worker registrations, not this config list. |
| `mcp` | string | none | Comma-separated MCP connectors this task requires (e.g. `slack, github`). The manager routes the task only to a worker whose advertised connectors cover this set; with none declared the task routes anywhere. Pairs with `evergreen` for standing automations against an external system. |
| `draft` | bool | config.json `draft` | Open the PR as a draft |
| `automerge` | bool | config.json `automerge` | Enable automerge on the PR |
| `loc` | int | config.json `diff_cap_lines` | Max lines-of-code change for this PR |
| `turns` | int | unlimited (config.json `max_turns` if present) | Optional hard turn cap for this task. When absent the session runs until done; the workflow job timeout is the runaway guard, and daily cost is regulated by `max_per_day`. |
| `split` | bool | `false` | Decompose into subtasks instead of implementing (see "Split tasks") |
| `evergreen` | bool | `false` (unless listed in config `evergreen_tasks`) | Treat this task as evergreen (reset from template instead of deleting) |

### Legacy headers

The following plain `key: value` headers (no frontmatter fences) are still supported:
  - `after: <NN>.<short-name>.md` — blocked until that task's file is gone from `main`
    (evergreen tasks never block dependents; their file always remains).
  - `diff_cap: <n>` — per-task override of the lane's diff cap (superseded by `loc` frontmatter).

## Split tasks

- A task with frontmatter `split: true` is a **decomposition run**: the worker does
  not implement the spec.
- The worker decomposes the work into subtask files `.tasks/<NN>.<n>.<short-name>.md`
  (parent number preserved), each:
  - sized to fit the default `loc` budget within one worker session;
  - independently green (`just validate` passes when the subtask alone is implemented);
  - self-sufficient (the subtask spec stands alone — workers never see the parent spec);
  - chained with `after:` headers only where ordering matters, so independent
    subtasks can run in parallel within `max_per_day`.
- The decomposition PR contains **only** the subtask files plus deletion of the
  parent task file. It is the reviewable plan — the human can edit slice
  boundaries before implementation runs.
  Subtask markdown under `.tasks/` is exempt from the diff cap (see
  `diff_cap_exempt_paths`); the cap applies to code changes in implementation PRs.
- A subtask may itself carry `split: true` if it proves too large.
- A non-split task that exceeds its diff cap during implementation must abort
  and fall back to a decomposition run rather than shipping partial work.

## Empty evergreen tasks

Evergreen task files may be present but contain no actionable entries (only
template boilerplate with no items listed under the section heading).
The workflow skips them before any worker runs — there is nothing to dispatch.

## Daily queue dispatch

See `tools/nightshift/spawn_daily.py`.
`00._todo` items use numbered/bullet lines under `## TO DO:`.
`00._questions` items are non-empty lines under `## Questions:`.
Each item becomes `.tasks/<NN>.<n>.<slug>.md` with frontmatter inherited from the
daily file (or config defaults).

## Tests

- **Existing tests may be modified** when the spec changes the behavior they pin.
- Every modified test must be individually justified in the PR body:
  test name → spec line that licenses the change.
- Unexplained test edits are the gaming vector — human review rejects them.

## Validation

- Reach green **before** opening the PR — CI is confirmation, not discovery.
  Commits pushed to the branch with no PR open are free; opening the PR is what
  triggers every CI workflow and the Copilot review (and each later fix push
  re-triggers them). Run the checks the PR will face — `ruff check --fix` first
  (cheapest), then `pyright`, then the `pytest` scope CI runs (or `just validate`
  for all three) — and iterate on the branch until they pass.
- After opening: stay in the CI fix loop until checks are green.
  Each commit pushed after the PR opened counts as one fix attempt
  (pre-PR commits don't count); at `max_fix_attempts` with checks still red,
  convert to draft with a blocker comment instead of leaving a red ready PR.
- If stuck after max attempts: convert to draft with a comment stating the blocker.

## Copilot review gate

- Copilot code review is a **merge gate**: the repository requires conversation
  resolution before merging, and Copilot's threads count.
- Every Copilot review comment must be handled by: fixing the code, posting a
  public inline reply describing the fix, then resolving the thread.
- Resolving without a fix and reply is forbidden. Closing the PR to avoid a
  comment is forbidden.
- Sole exception: a Copilot suggestion that directly contradicts the task spec —
  the spec is authority; reply citing the spec line, then resolve.
- Copilot may post its review after CI is green; the worker must wait for it
  and re-check review threads before declaring the PR ready.
- If a comment cannot be fixed to green within the fix-attempt budget: convert
  to draft with a blocker comment (honest failure).

## Honest failure

- If the spec is ambiguous beyond reasonable interpretation, contradicts the codebase,
  or the implementation cannot reach green: leave a **draft PR** with a comment stating
  exactly what is blocking. File nothing into `main`. Move to the next task.
- A draft PR with a precise question is a good morning artifact.

## PR format

- **Label (required):** every PR opened by nightshift must have the `nightshift` label.
  Pass `--label nightshift` to `gh pr create`; if missing, run `gh pr edit --add-label nightshift`.
  Draft and honest-failure PRs are not exempt.
- Title: resolved frontmatter `title` (defaults to short-name from filename).
- Body includes:
  - Spec interpretation (the worker's reading of the spec).
  - What was built and which spec sections governed it.
  - Test-modification justifications (test name → spec line).
  - Checklist of acceptance criteria from the spec (if present).
- Automerge is **off by default**; a task opts in via frontmatter `automerge: true`
  (otherwise a human reviews and merges).

## Before push

- Run `just validate` before pushing.
- Stop cleanly at budget cap rather than pushing partial work.

## Parking

- A task whose PRs have been closed-unmerged `max_nights_before_parking` times is skipped.
- An issue is filed asking the human to revise or remove the spec.
- Prevents a bad spec from burning budget nightly.
