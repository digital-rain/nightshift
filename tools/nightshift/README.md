# Nightshift

Task-lane automation: reads `.tasks/*.md` specs, opens PRs, iterates until CI is green.

Prompts and charter live in this directory (`nightshift.md`, `NIGHTSHIFT.md`); tunables are in `config.json`.

## Kill switch

```bash
gh variable set NIGHTSHIFT_ENABLED -b false   # disable scheduled runs
gh variable set NIGHTSHIFT_ENABLED -b true    # re-enable
```

Scheduled runs no-op when `NIGHTSHIFT_ENABLED != 'true'`.
Manual `workflow_dispatch` with a task name still runs.

## GitHub App (required for automatic CI on nightshift PRs)

Nightshift runs inside GitHub Actions and opens PRs on your behalf.
If those PRs are pushed with the default `GITHUB_TOKEN`, GitHub queues downstream
`pull_request` workflows in an **approval-required** state.

The fix is a dedicated **GitHub App** whose short-lived installation token the worker uses
for `git push` and `gh pr create`.
That token is not subject to the recursive-workflow gate, so CI starts immediately.

### 1. Create the app

GitHub → **Settings** → **Developer settings** → **GitHub Apps** → **New GitHub App**

| Field | Value |
|---|---|
| Name | e.g. `myrepo-nightshift` |
| Homepage URL | target repo |
| Webhook | **Inactive** (not needed) |

**Repository permissions:**

| Permission | Access |
|---|---|
| Contents | Read and write |
| Pull requests | Read and write |
| Issues | Read and write |
| Workflows | Read (optional; helps `gh pr checks`) |

**Where can this GitHub App be installed?** → Only on this account/org.

Create the app, then **Generate a private key** (downloads a `.pem` file).

### 2. Install on the target repository

Install the app on the repo where nightshift will run.

Note the **App ID** on the app settings page.

### 3. Add Actions secrets

Repo → **Settings** → **Secrets and variables** → **Actions**:

| Secret | Value |
|---|---|
| `NIGHTSHIFT_APP_ID` | Numeric App ID |
| `NIGHTSHIFT_APP_PRIVATE_KEY` | Full contents of the `.pem` file |
| `ANTHROPIC_API_KEY` | Required by the workflow |

### 4. Verify

```bash
gh workflow run nightshift --field task=<one-task>
```

Open the resulting PR.
CI checks should start without "Approve workflows to run".
The PR author shows as `<app-name>[bot]`, not `github-actions[bot]`.

### PAT alternative

A fine-grained PAT stored as `NIGHTSHIFT_GITHUB_TOKEN` works for solo repos but is a
long-lived credential.
Prefer the App above; swap the workflow to pass that secret to `claude-code-action` only if
you cannot install an App.
