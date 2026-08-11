# Deployment and operations

Two supported ways to run the stack locally, the full environment-variable reference, and
what CI actually does.

Related: [DATABASE.md](DATABASE.md) · [SECURITY.md](SECURITY.md) ·
[ARCHITECTURE.md](ARCHITECTURE.md)

---

## 1. Topology

Application processes run **on the host**; Docker Compose provides only the backing
services. The compose file says so at the top, and `CONTRIBUTING.md` repeats it.

```
host: uvicorn (FastAPI)  :8000        host: vite dev server :5173
        │                                      │
        └──────────► PostgreSQL :5432 ◄────────┘ (via /api proxy)
                     MinIO      :9000 / :9001 (only when PO_STORAGE_PROVIDER=s3)
```

## 2. Prerequisites

- **Python 3.12+** and [uv](https://docs.astral.sh/uv/)
- **Node 22** (CI pins `node-version: 22`)
- **PostgreSQL 16** — from Docker Compose, or user-space via `pgserver` (bundled as a dev
  dependency; no installation needed)

## 3. Path A — Docker Compose (the standard path)

`docker-compose.yml` at the repository root defines exactly two services:

| Service | Image | Ports | Notes |
|---|---|---|---|
| `postgres` | `postgres:16-alpine` | 5432 | `POSTGRES_DB=procurement`, user/password `postgres`; volume `postgres-data`; `pg_isready` healthcheck |
| `minio` | `minio/minio` | 9000 (S3 API), 9001 (console) | `minioadmin` / `minioadmin`; volume `minio-data`; healthcheck uses `mc ready local` (the image no longer ships `curl`) |

MinIO is only needed when you set `PO_STORAGE_PROVIDER=s3`; the default filesystem provider
needs nothing.

```bash
# from the repository root
cp .env.example .env                 # adjust as needed; never commit .env
docker compose up -d                 # postgres + minio
docker compose down                  # stop, keep data
docker compose down -v               # stop and wipe volumes (fresh DB/bucket)

# backend
cd backend
uv sync
export PO_DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5432/procurement"
uv run alembic upgrade head
uv run python scripts/seed_demo.py
uv run uvicorn app.main:create_app --factory --reload --port 8000

# frontend (second terminal)
cd frontend
npm ci
npm run dev                          # http://localhost:5173
```

## 4. Path B — no Docker (the path this project was actually built on)

The development machine for this build had neither Docker nor Homebrew available, so the
no-Docker path is fully supported rather than an afterthought: `pgserver` boots a real,
user-space PostgreSQL, and Node lives under `~/.local/node22`.

```bash
export PATH="$HOME/Library/Python/3.9/bin:$PATH"   # puts uv on PATH

cd "<repo>/backend"
uv sync

# 1. start (or reuse) the local database and migrate it to head
uv run python scripts/dev_db.py
#    → prints:  export PO_DATABASE_URL="postgresql+psycopg://.../postgres"
#    the server keeps running after the script exits; re-running is idempotent
#    `uv run python scripts/dev_db.py --stop` stops it

export PO_DATABASE_URL="<the URL the script printed>"

# 2. load the synthetic demo dataset (idempotent — safe to re-run)
uv run python scripts/seed_demo.py

# 3. run the API on 8001 (8000 was occupied on this machine)
uv run uvicorn app.main:create_app --factory --reload --port 8001
```

```bash
# frontend, second terminal
export PATH="$HOME/.local/node22/bin:$PATH"
cd "<repo>/frontend"
npm ci
BACKEND_PORT=8001 npm run dev        # Vite proxies /api → localhost:8001
```

`backend/scripts/dev_db.py` puts its data directory at
`~/.local/share/procurement-optimizer/pgdata`, deliberately **outside the repository**:
`pgserver` passes the socket path unquoted to `pg_ctl`, so a path containing spaces (this
repository's own path does) or exceeding the 104-character socket limit would break.

`BACKEND_PORT` is read by `frontend/vite.config.ts` and defaults to `8000`.

## 5. Environment variables

All settings are read by `backend/src/app/core/config.py::Settings` with the `PO_` prefix,
from the environment or a `.env` file in the process working directory. `.env.example`
documents every one of them with safe placeholder values.

### Core

| Variable | Default | Meaning |
|---|---|---|
| `PO_ENVIRONMENT` | `dev` | `dev` \| `test` \| `prod`. In `prod`: API docs are hidden (unless `PO_DEMO_MODE=true`), HSTS is sent, and `cookie_secure` is forced on. |
| `PO_DATABASE_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/procurement` | SQLAlchemy URL |
| `PO_SESSION_TTL_HOURS` | `12` | Absolute session cookie lifetime (a 2 h idle timeout applies independently) |
| `PO_SECRET_KEY` | `dev-only-secret-change-me` | The app **refuses to start** in `prod` with this value |
| `PO_COOKIE_SECURE` | `false` | Forced `true` in `prod` regardless |
| `PO_ALLOWED_ORIGINS` | `["http://localhost:5173"]` | JSON array (pydantic-settings parses list fields as JSON). Used by CORS **and** the Origin allowlist |

### Uploads and rate limiting

| Variable | Default | Meaning |
|---|---|---|
| `PO_MAX_UPLOAD_BYTES` | `20971520` (20 MiB) | Per-document upload cap |
| `PO_RATE_LIMIT_PER_MINUTE` | `120` | General per-IP limit |
| `PO_RATE_LIMIT_AUTH_PER_MINUTE` | `10` | Limit on `/api/v1/auth/login` |

### Providers

| Variable | Default | Values |
|---|---|---|
| `PO_EXTRACTION_PROVIDER` | `mock` | `mock` \| `anthropic` (**adapter not implemented**; selecting it raises `ProviderUnavailableError`) |
| `PO_OCR_PROVIDER` | `mock` | `mock` (only supported value) |
| `PO_NARRATIVE_PROVIDER` | `template` | `template` \| `anthropic` (**adapter not implemented**) |
| `PO_STORAGE_PROVIDER` | `filesystem` | `filesystem` \| `s3` |
| `PO_JOB_RUNNER` | `thread` | `inline` \| `thread` — **read by nothing in this build**; every job runs inline. See [ARCHITECTURE.md](ARCHITECTURE.md) §7 |
| `PO_STORAGE_ROOT` | `.local-storage` | Filesystem provider root (gitignored) |
| `PO_DEMO_MODE` | `true` | Seeds/labels the demo organization; also keeps API docs exposed in `prod` |

### Optional external services

| Variable | Required when |
|---|---|
| `PO_ANTHROPIC_API_KEY` | `PO_EXTRACTION_PROVIDER=anthropic` or `PO_NARRATIVE_PROVIDER=anthropic` — absent ⇒ the app refuses to start (never silently falls back to mock) |
| `PO_S3_ENDPOINT_URL`, `PO_S3_BUCKET`, `PO_S3_ACCESS_KEY`, `PO_S3_SECRET_KEY` | `PO_STORAGE_PROVIDER=s3` — any missing one is named in the startup error |

Secrets are typed `SecretStr`, so they do not leak through `repr()` or serialization.

### Test-only

`PO_TEST_DATABASE_URL` — when set, the test suite uses that database; when unset, it boots
`pgserver` automatically. CI sets it to the Postgres service container.

## 6. Common operations

```bash
cd backend

uv run alembic upgrade head            # migrate
uv run alembic downgrade base          # tear the schema down
uv run alembic current                 # where am I
uv run python scripts/seed_demo.py     # idempotent demo dataset
uv run python scripts/generate_fixtures.py   # regenerate synthetic quote documents + goldens
                                             # (byte-deterministic: git status should stay clean)

uv run pytest -m "not integration"     # unit + contract, no database, fast
uv run pytest                          # full suite (needs Postgres; pgserver by default)
uv run ruff check .                    # lint
uv run ruff format .                   # format
uv run mypy                            # strict typecheck
```

```bash
cd frontend
npm test          # vitest run
npm run typecheck # tsc --noEmit
npm run build     # tsc --noEmit && vite build
```

Alembic reads the database URL from the environment via
`backend/migrations/env.py` (which calls `load_settings()` and overrides
`sqlalchemy.url`), **never** from the placeholder value in `alembic.ini`.

**Migrations are append-only once merged.** Never edit a merged migration, even to fix a bug
in it — write a new one (`CONTRIBUTING.md`).

## 7. Continuous integration

`.github/workflows/ci.yml`, triggered on pushes to `main` and on every pull request. Three
jobs:

**`backend`** — Ubuntu, with a `postgres:16-alpine` service container
(`procurement_test`, `pg_isready` healthcheck), `astral-sh/setup-uv@v5` pinned to Python
3.12:

1. `uv sync --locked` (the lockfile is authoritative)
2. `uv run ruff check .`
3. `uv run mypy` — strict, over the whole `app` package
4. **Migration cycle on an empty database**: `alembic upgrade head` → `downgrade base` →
   `upgrade head`, so every migration's `downgrade()` is exercised
5. `uv run pytest --cov=src/app --cov-report=term-missing`

**`frontend`** — Node 22 with npm cache keyed on `frontend/package-lock.json`:
`npm ci` → `npm run typecheck` → `npm test` → `npm run build`.

**`secrets-scan`** — `gitleaks/gitleaks-action@v2` with `fetch-depth: 0` (full history).

Not yet in CI, and listed on the [roadmap](ROADMAP.md): `pip-audit`, a licence gate that
blocks AGPL/GPL dependencies, and an OpenAPI-drift job (which needs `docs/openapi.json` to be
committed first).

## 8. Production notes

This build is a portfolio demonstration, not a hardened production deployment. Before running
it anywhere real:

- set `PO_ENVIRONMENT=prod`, a real `PO_SECRET_KEY`, and a tight `PO_ALLOWED_ORIGINS`;
- terminate TLS in front of the app (the `Secure` cookie flag and HSTS assume HTTPS);
- cap request bodies at the proxy (`client_max_body_size 21m;` in nginx, matching
  `PO_MAX_UPLOAD_BYTES` + slack): the app rejects oversized **declared** bodies
  pre-routing, but a chunked upload without `Content-Length` is only bounded by the proxy;
- if the app runs behind that proxy, start uvicorn with `--proxy-headers
  --forwarded-allow-ips=<proxy address>` — otherwise every client shares the proxy's IP
  for rate limiting (one shared login bucket) and `audit_events.ip_address` records the
  proxy, not the client; never use a permissive `--forwarded-allow-ips=*`, which would
  make the per-IP rate limit header-spoofable;
- replace the in-memory rate limiter with a shared store — it is per-process;
- `REVOKE UPDATE, DELETE ON audit_events` from the application role, as belt-and-braces
  alongside the append-only triggers (development runs as the table owner);
- give `PO_STORAGE_PROVIDER=s3` a bucket with no public access and lifecycle rules;
- expect long scenario solves to hold an HTTP request open — there is no queue yet.
