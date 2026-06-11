# Nightshift

Nightly task automation for GitHub repos. Drop markdown specs into `.tasks/`, and a scheduled GitHub Actions workflow runs Claude Code against each one: implement the spec, open a PR, iterate on CI until green (or leave a draft with blockers).

## Install into a repo

Copy these paths into the target repository root:

```
.github/workflows/nightshift.yml
tools/nightshift/
.tasks/                    # create; seed from tools/nightshift/templates/ if needed
```

Then configure the target repo:

1. **Actions secrets** (Settings → Secrets and variables → Actions):

   | Secret | Purpose |
   |---|---|
   | `ANTHROPIC_API_KEY` | Claude API access for the worker |
   | `NIGHTSHIFT_APP_ID` | GitHub App ID (see below) |
   | `NIGHTSHIFT_APP_PRIVATE_KEY` | GitHub App private key (`.pem` contents) |

   A dedicated GitHub App is required so nightshift PRs trigger CI without the "approve workflows" gate. See [tools/nightshift/README.md](tools/nightshift/README.md) for app permissions and setup.

2. **Repository variable** (Settings → Secrets and variables → Actions → Variables):

   | Variable | Value |
   |---|---|
   | `NIGHTSHIFT_ENABLED` | `true` to run on schedule; omit or set anything else to disable |

3. **Label:** create a `nightshift` label (used for PR tracking and parking issues).

4. **Validation command:** the worker prompt expects `just validate` before push. Add that recipe to your `justfile`, or edit `tools/nightshift/nightshift.md` and `NIGHTSHIFT.md` to match your project's test/CI command.

5. **Config:** tune limits and guardrails in `tools/nightshift/config.json` (`max_per_day`, `forbidden_paths`, `evergreen_tasks`, etc.).

## Usage

- Add tasks as `.tasks/<NN>.<name>.md` (see `tools/nightshift/templates/task.md`).
- Scheduled runs: weekdays at 14:00 UTC (configurable in the workflow).
- Manual run: `gh workflow run nightshift --field task=10.my-task`
- Kill switch: `gh variable set NIGHTSHIFT_ENABLED -b false`

Charter, worker prompt, and detailed GitHub App setup: [tools/nightshift/](tools/nightshift/).
