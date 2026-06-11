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
- Diff cap per `tools/nightshift/config.json` field `diff_cap_lines` (default 1500);
  per-task frontmatter `loc` (or legacy `diff_cap:` header default) overrides. Fixture paths exempt.
- Forbidden paths per `forbidden_paths` in config (read from `origin/main`, never PR head).
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
| `model` | string | config.json `model` | Anthropic model to use for this task |
| `draft` | bool | config.json `draft` | Open the PR as a draft |
| `automerge` | bool | config.json `automerge` | Enable automerge on the PR |
| `loc` | int | config.json `diff_cap_lines` | Max lines-of-code change for this PR |
| `evergreen` | bool | `false` (unless listed in config `evergreen_tasks`) | Treat this task as evergreen (reset from template instead of deleting) |

### Legacy headers

The following plain `key: value` headers (no frontmatter fences) are still supported:
  - `after: <NN>.<short-name>.md` — blocked until that task's file is gone from `main`
    (evergreen tasks never block dependents; their file always remains).
  - `diff_cap: <n>` — per-task override of the lane's diff cap (superseded by `loc` frontmatter).

## Empty evergreen tasks

Evergreen task files may be present but contain no actionable entries (only
template boilerplate with no items listed).
This is not an error — there is simply nothing to do.
The worker must:

- **Not** create a branch or PR.
- **Not** reset or delete the file.
- Skip the task and proceed to the next in the queue.

## Tests

- **Existing tests may be modified** when the spec changes the behavior they pin.
- Every modified test must be individually justified in the PR body:
  test name → spec line that licenses the change.
- Unexplained test edits are the gaming vector — machine review rejects them.

## Validation

- Run `just validate` (full suite) locally **before** opening the PR.
  CI should be confirmation, not discovery.
- After opening: poll `gh pr checks`, fix, repush, up to `max_fix_attempts`.
- If stuck after max attempts: convert to draft with a comment stating the blocker.

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
- Automerge enabled when checks pass (per config).

## Before push

- Run `just validate` before pushing.
- Stop cleanly at budget cap rather than pushing partial work.

## Parking

- A task whose PRs have been closed-unmerged `max_nights_before_parking` times is skipped.
- An issue is filed asking the human to revise or remove the spec.
- Prevents a bad spec from burning budget nightly.
