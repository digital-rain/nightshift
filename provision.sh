#!/usr/bin/env bash
# provision.sh — Provision a host to run Nightshift (manager and/or worker).
#
# Installs the shared toolchain (uv, Python 3.12, just, lsof) and builds the
# project's .venv. Optional extras:
#   --with-db       Install a local PostgreSQL server and create the
#                   `nightshift` role + database for durable manager state.
#                   Opens UFW port 5432 to the local subnet.
#   --with-claude   Install Node + the Claude Code CLI (the default worker
#                   backend). Other backends (cursor/gemini/ollama) are BYO.
#   --with-react    Install Node + the React UI's npm deps and pre-build the
#                   bundles (assets/ui-react). Needed only to serve the React
#                   operator/worker UI (just manager-react-prod / worker-react-prod);
#                   the legacy vanilla UI needs nothing extra.
#
# Usage:
#   ./provision.sh [--with-db] [--with-claude] [--with-react] [--repo=PATH] [--pg-version=NN]
#
# Safe to re-run: every step is guarded by an "is it already there?" check.
# Opens UFW ports 8800 (manager), 8810 (worker UI), 8799 (legacy UI server).
#
# After it finishes (run from the repo):
#   just install        # uv sync (also done here)
#   just migrate        # apply the schema (Postgres only)
#   just manager        # operator UI + API on :8800

set -uo pipefail

GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; BOLD=$'\033[1m'; NC=$'\033[0m'

print_help() {
  awk 'NR>1 && /^#/{sub(/^# ?/,""); print; next} NR>1{exit}' "${BASH_SOURCE[0]:-$0}"
}

REPO="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PG_VERSION="17"
DO_DB=0
DO_CLAUDE=0
DO_REACT=0

for arg in "$@"; do
  case "$arg" in
    --with-db)       DO_DB=1 ;;
    --with-claude)   DO_CLAUDE=1 ;;
    --with-react)    DO_REACT=1 ;;
    --repo=*)        REPO="${arg#*=}" ;;
    --pg-version=*)  PG_VERSION="${arg#*=}" ;;
    -h|--help)       print_help; exit 0 ;;
    *)               echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

step()  { printf '\n%s==> %s%s\n' "$GREEN" "$1" "$NC"; }
note()  { printf '%s  - %s%s\n' "$YELLOW" "$1" "$NC"; }
fail()  { printf '%s!! %s%s\n' "$RED" "$1" "$NC" >&2; exit 1; }
have()  { command -v "$1" >/dev/null 2>&1; }

# Ensure Node + npm are present (shared by --with-claude and --with-react).
# Returns non-zero if Node could not be installed, so callers can degrade.
ensure_node() {
  if have node; then return 0; fi
  if have apt-get; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | $SUDO -E bash -
    apt_install nodejs
    have node && note "installed node $(node --version)"
  else
    note "cannot auto-install Node (no apt); install Node >=18 manually."
  fi
  have node
}

SUDO=""
if [[ "$(id -u)" -ne 0 ]]; then SUDO="sudo"; fi

APT_UPDATED=0
apt_update_once() { [[ "$APT_UPDATED" -eq 1 ]] && return 0; $SUDO apt-get update -qq; APT_UPDATED=1; }
apt_install() { apt_update_once; $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"; }

# ── baseline ─────────────────────────────────────────────────────────────
step "Checking baseline tools"
MISSING=()
for cmd in git curl gcc; do have "$cmd" || MISSING+=("$cmd"); done
[[ ${#MISSING[@]} -gt 0 ]] && fail "missing baseline tools: ${MISSING[*]} (install build-essential, curl, git first)."
note "baseline OK (git, curl, gcc)."

# ── uv ───────────────────────────────────────────────────────────────────
step "uv (Python venv + lockfile manager)"
if have uv; then
  note "uv present: $(uv --version)"
else
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  have uv || fail "uv install failed (not on PATH)."
  note "installed $(uv --version)"
fi

# ── Python 3.12 ──────────────────────────────────────────────────────────
step "Python 3.12 toolchain"
uv python install 3.12
note "Python 3.12 available to uv."

# ── just ──────────────────────────────────────────────────────────────────
step "just (command runner)"
if have just; then
  note "just present: $(just --version)"
else
  curl --proto '=https' --tlsv1.2 -sSf https://just.systems/install.sh | bash -s -- --to "$HOME/.local/bin"
  export PATH="$HOME/.local/bin:$PATH"
  have just || fail "just install failed (not on PATH)."
  note "installed $(just --version)"
fi

# ── lsof (used by `just stop`) ─────────────────────────────────────────────
step "lsof"
have lsof || apt_install lsof || note "could not install lsof (non-fatal)."
note "lsof: $(command -v lsof || echo MISSING)"

# ── optional: PostgreSQL for durable manager state ─────────────────────────
if [[ "$DO_DB" -eq 1 ]]; then
  step "── PostgreSQL (durable manager state) ──────────────────"
  if have apt-get; then
    if [[ ! -f /etc/apt/sources.list.d/pgdg.list ]]; then
      CODENAME="$(lsb_release -cs)"
      curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | $SUDO gpg --batch --yes --dearmor -o /usr/share/keyrings/pgdg.gpg
      echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt ${CODENAME}-pgdg main" \
        | $SUDO tee /etc/apt/sources.list.d/pgdg.list >/dev/null
      APT_UPDATED=0
    fi
    apt_install "postgresql-${PG_VERSION}" "postgresql-client-${PG_VERSION}"
    # Bind PostgreSQL to this host's IP (plus localhost) so non-loopback DSNs
    # work. We use the explicit machine address rather than '*' because
    # localhost is ambiguous across co-located Parallels VMs and the host.
    PG_CONF="/etc/postgresql/${PG_VERSION}/main/postgresql.conf"
    PG_HBA="/etc/postgresql/${PG_VERSION}/main/pg_hba.conf"
    if [[ -f "$PG_CONF" ]]; then
      HOST_IP=$(hostname -I | awk '{print $1}')
      if [[ -n "$HOST_IP" ]]; then
        $SUDO sed -i "s|^#\?listen_addresses.*|listen_addresses = '${HOST_IP},localhost'|" "$PG_CONF"
        note "postgresql.conf: listen_addresses = '${HOST_IP},localhost'"
        SUBNET="${HOST_IP%.*}.0/24"
        if ! $SUDO grep -q "$SUBNET" "$PG_HBA" 2>/dev/null; then
          $SUDO sed -i "/^host.*all.*all.*127.0.0.1\/32/a host    all             all             ${SUBNET}            scram-sha-256   # nightshift subnet" "$PG_HBA"
          note "pg_hba.conf: allowed connections from ${SUBNET}"
        fi
      else
        note "could not determine host IP; left listen_addresses unchanged."
      fi
    fi

    if have systemctl; then $SUDO systemctl restart postgresql 2>/dev/null || true; else
      $SUDO pg_ctlcluster "${PG_VERSION}" main restart 2>/dev/null || note "start Postgres manually."
    fi

    step "Create role 'nightshift' + database 'nightshift'"
    $SUDO -u postgres psql -v ON_ERROR_STOP=1 <<'SQL' || note "role/db may already exist (non-fatal)."
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='nightshift') THEN
    CREATE ROLE nightshift WITH LOGIN PASSWORD 'nightshift' CREATEDB;
  END IF;
END $$;
SELECT 'CREATE DATABASE nightshift OWNER nightshift'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='nightshift')\gexec
SQL
    note "DB ready. Set NIGHTSHIFT_PG_DSN in .env, then run: just migrate"
  else
    note "cannot install Postgres (no apt). Install it manually or use the in-memory store."
  fi

  step "UFW rule for Postgres (5432, local subnet)"
  $SUDO ufw allow from 10.0.0.0/8 to any port 5432 proto tcp comment "nightshift postgres" 2>/dev/null \
    || note "ufw: could not open 5432 (non-fatal)."
fi

# ── optional: Claude Code CLI (default worker backend) ──────────────────────
if [[ "$DO_CLAUDE" -eq 1 ]]; then
  step "── Claude Code CLI (worker backend) ────────────────────"
  ensure_node || true
  if have claude; then
    note "claude present: $(claude --version 2>/dev/null || echo installed)"
  elif have npm; then
    npm install -g @anthropic-ai/claude-code && note "installed claude CLI" \
      || note "claude install failed; run: npm install -g @anthropic-ai/claude-code"
  else
    note "npm not found — install Node first, then: npm install -g @anthropic-ai/claude-code"
  fi
fi

# ── optional: React UI (assets/ui-react) ────────────────────────────────────
if [[ "$DO_REACT" -eq 1 ]]; then
  step "── React UI (assets/ui-react) ──────────────────────────"
  REACT_DIR="$REPO/src/nightshift/assets/ui-react"
  if [[ ! -d "$REACT_DIR" ]]; then
    note "react UI dir not found at $REACT_DIR (skipping)."
  elif ensure_node && have npm; then
    note "node $(node --version), npm $(npm --version)"
    ( cd "$REACT_DIR" && npm install ) \
      && note "installed React UI npm deps" \
      || note "npm install failed (run it manually in $REACT_DIR)."
    # Pre-build both bundles so `just manager-react-prod` / `worker-react-prod`
    # start without a first-run build. Non-fatal: dev (`just manager-react`)
    # and the prod recipes both rebuild as needed.
    ( cd "$REACT_DIR" && npm run build ) \
      && note "pre-built React bundles (dist-manager, dist-worker)" \
      || note "React build failed (non-fatal; the just recipes will build on demand)."
  else
    note "Node/npm unavailable — cannot set up the React UI. Install Node >=18,"
    note "then: cd $REACT_DIR && npm install && npm run build"
  fi
fi

# ── UFW rules for the Nightshift services ──────────────────────────────────
step "UFW rules for Nightshift (8800 manager, 8810 worker UI, 8799 server)"
for port in 8800 8810 8799; do
  $SUDO ufw allow "$port"/tcp comment "nightshift" 2>/dev/null || note "ufw: could not open $port (non-fatal)."
done

# ── build the venv ─────────────────────────────────────────────────────────
step "Build the Python venv (uv sync)"
cd "$REPO"
export PATH="$HOME/.local/bin:$PATH"
uv sync
note ".venv ready."

# ── scaffold workspace config ─────────────────────────────────────────────
step "Scaffold workspace config (.nightshift/)"
WORKSPACE="${NIGHTSHIFT_WORKSPACE:-$HOME/workspaces}"
WORKSPACE="${WORKSPACE/#\~/$HOME}"
"$REPO/.venv/bin/python" -m nightshift init --workspace "$WORKSPACE"
note "workspace config at ${WORKSPACE}/.nightshift/"

# ── done ────────────────────────────────────────────────────────────────────
step "Done"
printf '\n%sProvisioning complete.%s\n' "$GREEN" "$NC"
cat <<EOF

Make sure uv/just are on your PATH in new shells:
  export PATH="\$HOME/.local/bin:\$PATH"

Next steps (run from ${REPO}):
  ${BOLD}1.${NC} Edit .env (NIGHTSHIFT_PG_DSN, backend credentials).
  ${BOLD}2.${NC} just migrate        # apply the schema (Postgres only)
  ${BOLD}3.${NC} just manager        # operator UI + API on :8800
  ${BOLD}4.${NC} just worker         # in another shell / on another box
$( [[ "$DO_REACT" -eq 1 ]] && printf '  %s5.%s just manager-react-prod  # serve the React UI on :8800 instead' "$BOLD" "$NC" )
EOF
