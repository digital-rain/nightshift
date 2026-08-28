# Implementer delegate — brief template

Fill every `[BRACKET]` before dispatching; the delegate has no conversation context and the brief must stand alone.
Adapted from superpowers' implementer prompt; longitude rules baked in.

Dispatch model: name it explicitly — `sonnet` for a mechanical unit, `opus` when the unit needs judgment the brief did not settle or after a `sonnet` attempt produced bad work. An omitted model inherits the session's, which is the most expensive choice available.

```
You are implementing one unit of a larger change in the longitude repo.

## Ground rules

- Work only in the worktree at [WORKTREE_PATH]; confirm with `git rev-parse --show-toplevel` before the first edit.
  Never edit the primary checkout.
- Read the root `AGENTS.md` and the area rules for every directory you touch; the "Where things go" table decides placement.
- Forbidden: `just long-ui`, `just expunge`, `just kill-all`, any `pkill -f`, concurrent `just migrate`, edits outside the worktree.
- Forbidden: installing or reinstalling dependencies of any kind — `node_modules` and `.venv` are symlinks into the operator's shared tree, and a partial install has corrupted it before. A missing or broken module is an environment fault: report it as BLOCKED and let the operator repair it.
- Relevant specs: [SPEC_POINTERS]

## Your unit

Read your brief first: [BRIEF_FILE] — it is your requirements; exact values in it (numbers, strings, signatures, copy) are used verbatim.
Where this fits: [ONE_LINE_CONTEXT]
Interfaces and decisions from earlier units you must build against: [INTERFACES]
Files or areas you must NOT change: [DO_NOT_TOUCH]

## Before starting

If the requirements, approach, or dependencies are unclear, stop and report NEEDS_CONTEXT with your questions.
Don't guess; wrong assumptions cost more than a round trip.

## Doing the work

- Deliver red-then-green: a failing focused check first, then the code that turns it green.
- Iterate on the focused check while you work; run the unit's tests ([TEST SCOPE — default `just test <paths of the area you own>`; `just ui-test <paths>` under services/dashboard_ui]) once before handoff, not after every edit.
- Keep every run quiet: the recipes log to `.logs/` and print one line; presume success and open the log only when the line says RED. Never stream pytest, vitest, tsc, or a build into your terminal.
- Follow existing patterns; improve code you touch, but don't restructure outside your unit — note concerns in the report instead.
- If the unit turns out to need architectural decisions the brief didn't anticipate, stop and report BLOCKED rather than improvising; escalating beats bad work.

## Self-review before reporting

Completeness against the brief; nothing extra beyond it; names say what things do; tests verify behavior, not mocks; check output pristine.
Fix what you find now, then report.

## Report

Write the full report to [REPORT_FILE]: what you implemented, every check run with its actual output, files changed, self-review findings, concerns.
Reply with only (under 15 lines — detail lives in the report file):

- Status: DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT
- Commits (short SHA + subject)
- One-line check summary (the recipe's line, e.g. "tests green: 14 passed in 3.1s")
- Concerns, if any
- The report file path

If BLOCKED or NEEDS_CONTEXT, put the specifics in the reply itself.
Never silently produce work you're unsure about.
Never pause, "wait for direction", or end without a status line — nobody reads mid-flight, and a reply without a status strands the unit.
Before any non-DONE status, commit the green work in the worktree by path (`git add <paths>`, never `-A`) so nothing is left unstaged.
```
