# End-to-End Smoke Test

`just smoke` runs a self-contained end-to-end validation of the manager and
worker together: real subprocesses, real HTTP protocol, real git landing — in
an isolated temp workspace that cannot touch a live deployment.

The harness lives in `tools/smoke.py`. A full run takes ~15 seconds.

---

## Running it

```bash
just smoke           # run, clean up the temp workspace on pass
just smoke --keep    # keep the temp workspace even on pass
```

On failure the harness prints the failing assertion, dumps the tail of the
manager and worker logs, and **keeps the temp workspace** for inspection. The
workspace path is printed at the start of every run:

```text
[smoke] workspace: /tmp/nightshift-smoke-XXXXXXXX/workspace
```

### Safe alongside a live instance

The smoke test can run while a live manager/worker/UI is up on the same host.
Isolation rests on three legs:

1. **Fresh temp workspace** — its own clone of this repo as the target, its
   own `nightshift-tasks` content store, its own `.nightshift/` config. The
   clone's `origin` remote is removed so nothing can reach back into the
   source checkout.
2. **Ephemeral ports** — the manager binds a free port on `127.0.0.1`; the
   worker is configured to talk only to that port.
3. **Environment scrubbing** — every `NIGHTSHIFT_*` variable is dropped
   before anything starts. This matters because `just` dotenv-loads the repo
   `.env`, which may carry `NIGHTSHIFT_PG_DSN` or `NIGHTSHIFT_MANAGER_URL`
   for the operator's real setup. With the DSN scrubbed, the manager under
   test always uses the in-memory store, never a shared Postgres.

---

## What is real, what is substituted

Everything is production code except one seam: the **agent backend**. The
worker subprocess registers a deterministic `SmokeBackend` in place of an
agent CLI (claude-code, cursor, …), so no LLM is invoked and every run is
reproducible.

| Layer | Real or substituted |
|---|---|
| Manager (`python -m nightshift.manager`) | Real subprocess |
| Worker loop (checkin / poll / heartbeat / submit) | Real (`WorkerLoop`) |
| Scheduler, leases, failure policy, quarantine | Real |
| Worktrees, validate command, squash-landing to main | Real |
| Store | Real `MemoryStore` (no Postgres) |
| Agent backend | `SmokeBackend` (deterministic) |

### The `SMOKE:` directive

The backend reads the materialized brief and obeys a single directive line in
the task body:

```text
SMOKE: [sleep=N] commit|fail
```

- `commit` — write `smoke-artifact.txt` in the worktree and commit it on the
  task branch (the landable path).
- `fail` — exit non-zero (the worker-error path).
- `sleep=N` — sleep N seconds first, giving the orchestrator time to pause
  or stop the queue mid-run.

The registration mechanism is `backends._BACKENDS`: the worker role appends
`SmokeBackend()` to the tuple before starting the loop, which makes the
`smoke/…` model provider resolvable without touching production code.

---

## The scenario

The orchestrator builds the workspace, launches manager then worker, and
drives two tasks over the operator API — a good task
(`SMOKE: sleep=4 commit`) and a bad task (`SMOKE: fail`):

1. **Launch manager** on an ephemeral port; wait for `/api/info`.
2. **Repos visible** — the clone appears in `/api/repos` as available.
3. **Create tasks** via `POST /api/tasks`; pin both to the smoke model.
4. **Pause, then start the worker** — the worker checks in, but several poll
   cycles must grant no lease while the queue is paused.
5. **Play** — the good task starts; `/api/state` reports `playing` with
   `now_playing` and a `run_id`.
6. **Pause mid-run** — state flips to `paused`, but the in-flight lease
   survives (pause never cancels work).
7. **Stop** — the lease is cancelled. The backend is still mid-sleep; when
   it finishes and submits, the manager must fence the stale submit (409):
   nothing lands, the stopped run must not complete.
8. **Start again** — the good task re-runs under a new lease, validation
   (`test -f smoke-artifact.txt`) passes, and the run squash-lands on the
   clone's `main`. The commit subject and artifact are verified, and the
   brief is dropped from the queue (404).
9. **Error path** — the bad task fails, is marked `failed`, is retried by
   the Phase B policy, fails again, and ends **quarantined** with the queue
   paused on `retry_failed`. The run history shows ≥2 `worker_error` runs
   and the task appears in `/api/blocked`.

A passing run prints one line per step and finishes with:

```text
[smoke] PASS (13.3s)
```

---

## Harness anatomy

`tools/smoke.py` plays two roles, selected by `--role`:

- **Orchestrator** (default, what `just smoke` runs) — builds the workspace,
  spawns the manager and the worker, drives the scenario through an
  `httpx` client, and cleans up.
- **Worker** (`--role worker --workspace …`) — spawned *by* the orchestrator;
  registers the smoke backend and runs the stock `WorkerLoop` headless.

Key configuration choices in the generated workspace:

| Setting | Value | Why |
|---|---|---|
| `cadences.poll_seconds` | `0.5` | Fast dispatch so the run stays short |
| `landing_mode` | `"none"` | Land locally; no remote push/PR |
| `default_model` | `smoke/deterministic` | Routes every task to the smoke backend |
| queue `validate` | `test -f smoke-artifact.txt` | Cheap proof the commit is on the branch |
| queue `preflight` | `""` | Opt out (absent would inherit `uv sync --frozen`) |

Subprocess logs are written to `<workspace>/logs/manager.log` and
`worker.log`; the last 40 lines of each are dumped on failure.

---

## Debugging a failure

1. Read the `[smoke] FAIL: …` line — assertions carry the observed payload.
2. Check the dumped log tails; for more, open the full logs in the kept
   workspace under `logs/`.
3. Inspect the kept workspace directly: `nightshift-tasks/main/` holds the
   brief files and `config.json`; the clone's git log shows what landed.
4. Re-run with `--keep` to preserve a passing workspace for comparison.

## Extending the scenario

- New directives belong in `SmokeBackend._directive` / `run` — keep them
  deterministic (no network, no wall-clock dependence beyond `sleep=`).
- New lifecycle steps go in `run_scenario`, using the `check`/`wait_for`
  helpers so failures time out with a description instead of hanging.
- Timings: the good task's sleep (`SLOW_SLEEP`) must stay long enough to
  pause/stop it mid-run; `wait_for` polls at 0.2s with a 30s ceiling.
