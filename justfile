set dotenv-load := true

GREEN := `printf "\033[32m"`
ENDCOLOR := `printf "\033[0m"`

venv    := ".venv"
py      := venv + "/bin/python"
root    := justfile_directory()
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

# ----- run -----

# Launch the manager: operator UI + worker/operator API (default :8800).
manager port="":
    #!/usr/bin/env bash
    set -euo pipefail
    args=(--root "{{root}}")
    if [ -n "{{port}}" ]; then args+=(--port "{{port}}"); fi
    {{py}} -m nightshift.manager "${args[@]}"

# Launch a worker: polls the manager, runs/validates/submits (worker UI :8810).
worker port="":
    #!/usr/bin/env bash
    set -euo pipefail
    args=(--root "{{root}}")
    if [ -n "{{port}}" ]; then args+=(--ui-port "{{port}}"); fi
    {{py}} -m nightshift.worker "${args[@]}"

# Launch a worker with no UI (poll loop only).
worker-headless:
    {{py}} -m nightshift.worker --root "{{root}}" --no-ui

# Launch the legacy single-box UI server (viewer + player; default :8799).
server port="":
    #!/usr/bin/env bash
    set -euo pipefail
    args=(--root "{{root}}")
    if [ -n "{{port}}" ]; then args+=(--port "{{port}}"); fi
    {{py}} -m nightshift.server "${args[@]}"

# Launch the Slack Socket Mode capture daemon (needs the `slack` extra + tokens).
slackd:
    {{py}} -m nightshift.slack.slackd --root "{{root}}"

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
