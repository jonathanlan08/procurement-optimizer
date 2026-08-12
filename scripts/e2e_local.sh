#!/usr/bin/env bash
# Procurement Optimizer — local Playwright E2E runner.
#
# One command boots an EPHEMERAL PostgreSQL (deliberately separate from the
# shared ~/.local/share/procurement-optimizer/pgdata the README's Path B
# quickstart uses for everyday dev — see backend/scripts/dev_db.py), runs
# migrations, seeds the synthetic Meridian demo dataset
# (backend/scripts/seed_demo.py), starts the real backend on :8010 and the
# real Vite dev server on :5173, points Playwright (frontend/playwright.config.ts)
# at both, and tears everything down on exit — success or failure — so the
# suite is safe to run twice in a row against an identical, pristine seed
# (proving no order-dependence) instead of accumulating state (materialized
# quotes, reviewed briefs, generated reports) across runs.
#
# No paid providers anywhere: PO_EXTRACTION_PROVIDER/PO_NARRATIVE_PROVIDER
# default to mock/template (backend/src/app/core/config.py) and are never
# overridden here — every "AI" surface this suite exercises is the mock
# extraction provider and the deterministic template narrative, exactly as
# the seeded demo dataset already labels them ("Simulated").
#
# Usage:
#   bash scripts/e2e_local.sh                 # run the whole suite once
#   bash scripts/e2e_local.sh --headed         # extra args forwarded to `playwright test`
#   bash scripts/e2e_local.sh e2e/auth.spec.ts # run a single spec file

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
FRONTEND_DIR="$REPO_ROOT/frontend"

export PATH="$HOME/Library/Python/3.9/bin:$PATH"
export PATH="$HOME/.local/node22/bin:$PATH"

BACKEND_PORT=8010
FRONTEND_PORT=5173
BACKEND_URL="http://localhost:${BACKEND_PORT}"
FRONTEND_URL="http://localhost:${FRONTEND_PORT}"

PGDATA_DIR=""
BACKEND_PID=""
FRONTEND_PID=""
BACKEND_LOG="$REPO_ROOT/.e2e-backend.log"
FRONTEND_LOG="$REPO_ROOT/.e2e-frontend.log"

log() { printf '\n\033[1;36m[e2e]\033[0m %s\n' "$1"; }
err() { printf '\n\033[1;31m[e2e]\033[0m %s\n' "$1" >&2; }

cleanup() {
  local status=$?
  log "cleaning up..."

  if [[ -n "$FRONTEND_PID" ]] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
    kill "$FRONTEND_PID" 2>/dev/null || true
    wait "$FRONTEND_PID" 2>/dev/null || true
  fi
  if [[ -n "$BACKEND_PID" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi

  if [[ -n "$PGDATA_DIR" && -d "$PGDATA_DIR" ]]; then
    (cd "$BACKEND_DIR" && uv run python - "$PGDATA_DIR" <<'PY'
# Stops the ephemeral postgres instance this run booted and deletes its data
# directory (pgserver's own documented pattern for a temporary server — see
# its get_server() docstring: "use mkdtemp() ... and set cleanup_mode to
# 'delete'"). Best-effort: a failure here must not mask the suite's own
# pass/fail result, which has already been decided by this point.
import sys
try:
    import pgserver
    server = pgserver.get_server(sys.argv[1], cleanup_mode="delete")
    server.cleanup()
except Exception as exc:  # noqa: BLE001
    print(f"pgserver cleanup warning: {exc}")
PY
    ) || true
    rm -rf "$PGDATA_DIR" 2>/dev/null || true
  fi

  rm -f "$BACKEND_LOG" "$FRONTEND_LOG" 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT INT TERM

# -- free up :8010/:5173 from any leftover process (e.g. a previous crashed
# run) so this run starts from a known-clean slate -------------------------
for port in "$BACKEND_PORT" "$FRONTEND_PORT"; do
  stale_pid="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$stale_pid" ]]; then
    log "port $port is busy (pid $stale_pid) — killing the leftover process"
    kill -9 $stale_pid 2>/dev/null || true
    sleep 1
  fi
done

# -- ephemeral PostgreSQL: boot, migrate -----------------------------------
log "booting an ephemeral PostgreSQL (pgserver) and migrating to head..."
PGDATA_PARENT="$(mktemp -d "${TMPDIR:-/tmp}/po-e2e-pgdata.XXXXXX")"
PGDATA_DIR="$PGDATA_PARENT/data"

DB_URL="$(cd "$BACKEND_DIR" && uv run python - "$PGDATA_DIR" <<'PY'
import os
import sys

import pgserver
from alembic import command
from alembic.config import Config

data_dir = sys.argv[1]
server = pgserver.get_server(data_dir, cleanup_mode=None)
url = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
os.environ["PO_DATABASE_URL"] = url
command.upgrade(Config("alembic.ini"), "head")
print(url)
PY
)"
export PO_DATABASE_URL="$DB_URL"
log "database ready at $PGDATA_DIR"

# -- seed the synthetic demo dataset (idempotent, but this DB is always
# fresh here anyway) --------------------------------------------------------
log "seeding the synthetic Meridian demo dataset..."
(cd "$BACKEND_DIR" && uv run python scripts/seed_demo.py)

# -- backend on :8010 --------------------------------------------------------
log "starting the backend on :$BACKEND_PORT..."
export PO_ALLOWED_ORIGINS="[\"$FRONTEND_URL\"]"
# The general in-memory rate limiter (120/min/IP by default) and the login-
# specific one (10/min/IP) are tuned for a single interactive dev session,
# not a scripted browser driving dozens of TanStack Query fetches per page
# in a tight loop — raised here (config only, no code change) so this
# suite exercises the app's real behavior instead of its own rate limiter.
export PO_RATE_LIMIT_PER_MINUTE=6000
export PO_RATE_LIMIT_AUTH_PER_MINUTE=300

cd "$BACKEND_DIR"
uv run uvicorn app.main:create_app --factory --port "$BACKEND_PORT" \
  > "$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!
cd "$REPO_ROOT"

log "waiting for the backend to become healthy..."
backend_ready=""
for _ in $(seq 1 60); do
  if curl -fsS "$BACKEND_URL/api/health" >/dev/null 2>&1; then
    backend_ready=1
    break
  fi
  sleep 1
done
if [[ -z "$backend_ready" ]]; then
  err "backend did not become healthy in time — last log lines:"
  tail -n 60 "$BACKEND_LOG" >&2 || true
  exit 1
fi

# -- frontend dependencies + Chromium (idempotent, cached across runs) -----
if [[ ! -x "$FRONTEND_DIR/node_modules/.bin/playwright" ]]; then
  log "installing frontend dependencies (npm install)..."
  (cd "$FRONTEND_DIR" && npm install)
fi
log "ensuring Playwright's browsers are installed (chromium, firefox, webkit — no-op if already cached)..."
# 2026-08 audit remediation, item 5: playwright.config.ts now defines
# firefox/webkit projects (a chromium-full + firefox/webkit-smoke matrix —
# see that file's own header) alongside chromium, so a local run of "every
# project" needs all three engines present, not just chromium.
(cd "$FRONTEND_DIR" && npx playwright install chromium firefox webkit)

# -- frontend dev server on :5173 -------------------------------------------
log "starting the frontend dev server on :$FRONTEND_PORT..."
cd "$FRONTEND_DIR"
BACKEND_PORT="$BACKEND_PORT" npm run dev -- --port "$FRONTEND_PORT" --strictPort \
  > "$FRONTEND_LOG" 2>&1 &
FRONTEND_PID=$!
cd "$REPO_ROOT"

log "waiting for the frontend to become ready..."
frontend_ready=""
for _ in $(seq 1 60); do
  if curl -fsS "$FRONTEND_URL/" >/dev/null 2>&1; then
    frontend_ready=1
    break
  fi
  sleep 1
done
if [[ -z "$frontend_ready" ]]; then
  err "frontend did not become ready in time — last log lines:"
  tail -n 60 "$FRONTEND_LOG" >&2 || true
  exit 1
fi

# -- the suite itself ---------------------------------------------------
log "running the Playwright E2E suite..."
cd "$FRONTEND_DIR"
E2E_BASE_URL="$FRONTEND_URL" npx playwright test "$@"
