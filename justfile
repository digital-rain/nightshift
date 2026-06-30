set dotenv-load := true

GREEN := `printf "\033[32m"`
ENDCOLOR := `printf "\033[0m"`

venv    := ".venv"
py      := venv + "/bin/python"
root    := justfile_directory()
workspace := env_var_or_default("NIGHTSHIFT_WORKSPACE", justfile_directory())
mig_dir := justfile_directory() / "src/nightshift/assets/migrations"

# List available recipes.
default:
    @just --list

# ----- setup -----

# Install dependencies into .venv via uv (creates .venv if absent).
install:
    uv sync

# Recreate the virtualenv from scratch.
venv:
    uv venv
    uv sync

# Scaffold workspace config from shipped templates (idempotent, never clobbers).
init:
    {{py}} -m nightshift init --workspace "{{workspace}}"

# ----- run -----

# Launch the manager: operator UI + worker/operator API (default :8800).
manager port="":
    #!/usr/bin/env bash
    set -euo pipefail
    args=(--workspace "{{workspace}}")
    if [ -n "{{port}}" ]; then args+=(--port "{{port}}"); fi
    echo "launching manager on {{workspace}}:{{port}}"
    {{py}} -m nightshift.manager "${args[@]}"

# Launch a worker: polls the manager, runs/validates/submits (worker UI :8810).
worker port="":
    #!/usr/bin/env bash
    set -euo pipefail
    args=(--workspace "{{workspace}}")
    if [ -n "{{port}}" ]; then args+=(--ui-port "{{port}}"); fi
    echo "launching worker on {{workspace}}:{{port}}"
    {{py}} -m nightshift.worker "${args[@]}"

# Launch a worker with no UI (poll loop only).
worker-headless:
    echo "launching worker-headless on {{workspace}}"
    {{py}} -m nightshift.worker --workspace "{{workspace}}" --no-ui

# Launch the legacy single-box UI server (viewer + player; default :8799).
server port="":
    #!/usr/bin/env bash
    set -euo pipefail
    args=(--workspace "{{workspace}}")
    if [ -n "{{port}}" ]; then args+=(--port "{{port}}"); fi
    echo "launching server on {{workspace}}:{{port}}"
    {{py}} -m nightshift.server "${args[@]}"

# ----- react UI (assets/ui-react) -----

react_dir := justfile_directory() / "src/nightshift/assets/ui-react"

# Install the React UI's npm deps (idempotent; run once before the react recipes).
react-install:
    cd "{{react_dir}}" && npm install

# Install npm deps only if node_modules is absent (e.g. fresh checkout, or after
# a worktree was removed). The run/build recipes depend on this so they never
# fail with `vite: not found`.
_react-deps:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -d "{{react_dir}}/node_modules" ]; then
        echo "react: node_modules missing — installing deps…"
        cd "{{react_dir}}" && npm install
    fi

# Build both React bundles → dist-manager / dist-worker (side-by-side; leaves the
# legacy assets/ui + assets/ui-worker untouched).
react-build: _react-deps
    cd "{{react_dir}}" && npm run build

# Dev: Vite dev server for the operator UI with HMR, proxying /api to a running
# manager (start `just manager` in another terminal first). Opens on :5173.
manager-react port="": _react-deps
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{react_dir}}"
    echo "vite dev (manager UI) on :5173 → proxying /api to the manager (:8800)"
    echo "  ↳ make sure 'just manager' is running in another terminal"
    npm run dev

# Dev: Vite dev server for the worker UI with HMR, proxying /api to a running
# worker UI backend (start `just worker` first). Opens on :5273.
worker-react port="": _react-deps
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{react_dir}}"
    echo "vite dev (worker UI) on :5273 → proxying /api to the worker UI (:8810)"
    echo "  ↳ make sure 'just worker' is running in another terminal"
    npm run dev:worker

# Production: build the React operator UI, then launch the real manager backend
# serving that bundle (via NIGHTSHIFT_UI_DIR) instead of the legacy UI. Same
# backend, same API — the React build is just the static surface. Default :8800.
manager-react-prod port="": _react-deps
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{react_dir}}" && npm run build:manager
    cd "{{root}}"
    args=(--workspace "{{workspace}}")
    if [ -n "{{port}}" ]; then args+=(--port "{{port}}"); fi
    export NIGHTSHIFT_UI_DIR="{{react_dir}}/dist-manager"
    echo "launching manager (serving React build at $NIGHTSHIFT_UI_DIR) on :{{port}}"
    {{py}} -m nightshift.manager "${args[@]}"

# Production: build the React worker UI, then launch the real worker backend
# serving that bundle (via NIGHTSHIFT_WORKER_UI_DIR). /shared still resolves from
# the legacy operator assets (NIGHTSHIFT_UI_DIR is left unset). Worker UI :8810.
worker-react-prod port="": _react-deps
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{react_dir}}" && npm run build:worker
    cd "{{root}}"
    args=(--workspace "{{workspace}}")
    if [ -n "{{port}}" ]; then args+=(--ui-port "{{port}}"); fi
    export NIGHTSHIFT_WORKER_UI_DIR="{{react_dir}}/dist-worker"
    echo "launching worker (serving React build at $NIGHTSHIFT_WORKER_UI_DIR) on :{{port}}"
    {{py}} -m nightshift.worker "${args[@]}"

# Launch the Slack Socket Mode capture daemon (needs the `slack` extra + tokens).
slackd:
    echo "launching slackd on {{workspace}}"
    {{py}} -m nightshift.slack.slackd --workspace "{{workspace}}"

# Stop whatever server is listening on `port` (default 8800).
stop port="8800":
    #!/usr/bin/env bash
    set -euo pipefail
    pid=$(lsof -nP -iTCP:{{port}} -sTCP:LISTEN -t 2>/dev/null || true)
    if [ -n "$pid" ]; then
        echo "stopping nightshift (pid $pid) on :{{port}}"
        kill $pid
    else
        echo "nothing listening on :{{port}}"
    fi

# Restart the manager: stop whatever holds `port` (default 8800), then relaunch it.
restart port="":
    #!/usr/bin/env bash
    set -euo pipefail
    p="{{port}}"
    p="${p:-8800}"
    pid=$(lsof -nP -iTCP:"$p" -sTCP:LISTEN -t 2>/dev/null || true)
    if [ -n "$pid" ]; then
        echo "stopping nightshift (pid $pid) on :$p"
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 20); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.25
        done
        if kill -0 "$pid" 2>/dev/null; then
            echo "still alive after SIGTERM; sending SIGKILL"
            kill -9 "$pid" 2>/dev/null || true
            sleep 0.5
        fi
    else
        echo "nothing listening on :$p"
    fi
    just manager "{{port}}"

# ----- database -----

# Apply Nightshift's migrations to NIGHTSHIFT_PG_DSN (idempotent).
migrate:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "${NIGHTSHIFT_PG_DSN:-}" ]; then
        echo "NIGHTSHIFT_PG_DSN is not set" >&2
        exit 1
    fi
    psql "$NIGHTSHIFT_PG_DSN" -v ON_ERROR_STOP=1 -q <<'SQL'
    CREATE SCHEMA IF NOT EXISTS _meta;
    CREATE TABLE IF NOT EXISTS _meta.schema_migrations (
        filename   TEXT PRIMARY KEY,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    SQL
    shopt -s nullglob
    for f in "{{mig_dir}}"/*.sql; do
        name=$(basename "$f")
        applied=$(psql "$NIGHTSHIFT_PG_DSN" -At -c "SELECT 1 FROM _meta.schema_migrations WHERE filename='$name'")
        if [ "$applied" = "1" ]; then
            printf "  skipping %s (already applied)\n" "$name"
            continue
        fi
        printf "{{GREEN}}applying %s{{ENDCOLOR}}\n" "$name"
        { echo 'BEGIN;'; \
          awk '/-- migrate:down/{exit} {print}' "$f"; \
          echo "INSERT INTO _meta.schema_migrations(filename) VALUES ('$name');"; \
          echo 'COMMIT;'; } \
        | psql "$NIGHTSHIFT_PG_DSN" -v ON_ERROR_STOP=1 -q
    done

# Roll back Nightshift's migrations newest-first (drops the `nightshift` schema).
rollback:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "${NIGHTSHIFT_PG_DSN:-}" ]; then
        echo "NIGHTSHIFT_PG_DSN is not set" >&2
        exit 1
    fi
    shopt -s nullglob
    files=( "{{mig_dir}}"/*.sql )
    for (( i=${#files[@]}-1; i>=0; i-- )); do
        f="${files[$i]}"
        name=$(basename "$f")
        applied=$(psql "$NIGHTSHIFT_PG_DSN" -At -c "SELECT 1 FROM _meta.schema_migrations WHERE filename='$name'" || echo "")
        if [ "$applied" != "1" ]; then
            printf "  skipping %s (not applied)\n" "$name"
            continue
        fi
        printf "{{GREEN}}rolling back %s{{ENDCOLOR}}\n" "$name"
        { echo 'BEGIN;'; \
          awk 'f{print} /-- migrate:down/{f=1}' "$f"; \
          echo "DELETE FROM _meta.schema_migrations WHERE filename='$name';"; \
          echo 'COMMIT;'; } \
        | psql "$NIGHTSHIFT_PG_DSN" -v ON_ERROR_STOP=1 -q
    done

# ----- quality -----

# Run the test suite.
test *args:
    {{py}} -m pytest tests {{args}}

# Lint + test.
validate:
    {{py}} -m ruff check src tests
    {{py}} -m pytest tests
