---
name: thermo-review
description: Deep branch audit with two parallel lenses — correctness and security (bugs, breakages, devex regressions, feature-gate leaks) and code quality (maintainability, abstraction quality, spaghetti growth). Use for thermo, thermonuclear, or thermos review requests, deep PR or branch audits, or especially strict pre-merge reviews.
disable-model-invocation: true
---

# Thermo Review

One deep audit of a branch's changes, two lenses run as parallel subagents. Run a single lens when asked for only correctness or only quality.

## Scope (both lenses)

- Only code ADDED or MODIFIED by this branch. Skip anything the standing gates (compiler, lints, tests, CI) catch mechanically — this audit exists for what they can't see.
- Trace every finding end-to-end before reporting. Never present unfinished research ("if the backend handles it, this is fine") when you can check the other side yourself.
- Calibrate severity honestly. A few high-conviction findings beat a list of nits; inflated priorities burn the author's trust.
- Intended, well-scoped breakage (flag removal, safeguard deletion) is not a finding — unless the author seems unaware of the implications or the change looks malicious.
- Form your own findings first. Only then check the PR discussion (gh/glab); evaluate BugBot and reviewer comments on merit, and attribute what you incorporate.

## Lens 1: Correctness and security

- Cross-module side effects: trace the dependents of every changed interface, type, and behavior. Subtle interactions are where breakage hides.
- Devex breakage: secrets read differently, renamed or added env vars, remapped ports, new required setup steps. New *alternative* workflows and ordinary dependencies don't count.
- Feature-gate leaks: check every new code path against its gate.
- Security: injection, authz gaps, secret exposure, unsafe deserialization, SSRF — in the changed code and in what it newly enables.

## Lens 2: Code quality

The core move is **code judo**: restructurings that keep behavior while whole branches, modes, or layers disappear. Don't rubber-stamp "it works"; don't settle for a cleaner version of the same messy idea.

| Rule | Flag | Remedy |
|---|---|---|
| No file crosses 1k lines via this PR without strong reason | Diff pushes a file past 1000 lines | Decompose first: extract helpers, subcomponents, modules |
| No spaghetti growth | Ad-hoc conditionals, one-off booleans/nullable modes, special cases bolted into unrelated flows | Reframe the state model so branches disappear; dedicated abstraction, not another `if` |
| Direct, boring code over magic | Generic mechanisms hiding simple data shapes; thin wrappers; identity abstractions | Delete the indirection; keep the direct flow |
| Clean type boundaries | Unnecessary `any`/`unknown`/casts/optionality; silent fallbacks papering over unclear invariants | Explicit typed model; make the boundary explicit |
| Logic in the canonical layer | Feature logic leaking into shared paths; near-duplicates of existing helpers; code in the wrong package | Move to the layer that owns the concept; reuse the canonical helper |
| Sane orchestration | Independent work serialized for no reason; related updates that can leave state half-applied | Parallelize independent work; restructure toward atomic updates |

Table rows are presumptive blockers unless the author justifies them. Be direct, not rude: name the messier codebase or the missed simplification plainly.

## Orchestration

1. Scope from the request, PR, or current branch; gather the diff plus enough context that reviewers don't guess.
2. One background subagent per lens, launched together, each given the Scope section plus its lens, the same diff, and instructions to return prioritized findings with file references and evidence.
3. Synthesize findings-first, deduplicated; overlapping findings weigh heavier; resolve disagreements with your own judgment. Order: correctness/security, then structural regressions and missed simplifications, then boundary/type problems, then size and legibility.
