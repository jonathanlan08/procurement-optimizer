# Contributing

## Local setup

Backend (Python 3.12, [uv](https://docs.astral.sh/uv/)):

```
cd backend
uv sync
cp ../.env.example ../.env   # adjust if needed; never commit .env
```

You need a Postgres instance. Two supported paths:

- **No Docker (used by the test suite by default):** nothing to start — `pgserver`
  (a dev dependency) spins up a real, user-space PostgreSQL automatically the
  first time tests run. This is the default when `PO_TEST_DATABASE_URL` is unset.
- **Docker:** `docker compose up -d` from the repo root starts `postgres` and
  `minio` (see `docker-compose.yml`). Point the app/tests at it by setting
  `PO_DATABASE_URL` / `PO_TEST_DATABASE_URL` to
  `postgresql+psycopg://postgres:postgres@localhost:5432/procurement`.

The backend itself always runs on the host (`uv run uvicorn ...` from `backend/`),
never inside compose — see `docs/planning/01-architecture.md` §11.

Frontend setup will be documented here once the frontend package is scaffolded
(see `docs/SPEC.md` §Technical architecture for the intended stack).

## Test commands

Run from `backend/`:

```
uv run pytest -m "not integration"   # unit + component + contract, no DB required, < 60s
uv run pytest                        # full suite incl. integration (needs Postgres, see above)
```

## Code style

```
uv run ruff check .
uv run ruff format .
uv run mypy
```

`ruff` and `mypy` (strict on `app/domain`) run in CI and should run clean before
you open a PR. Floats are banned in domain/money code — use `Decimal`.

## File ownership and review

Per `docs/planning/09-task-decomposition.md` §10 (ratified in
`docs/planning/00-decisions.md` §6), some paths carry extra review requirements:

- **Principal review required (R):** changes to `app/schemas/*` (decimal-as-string
  and missing-field conventions), `app/models/*` (org FK, constraints, indexes),
  `app/exports/**` (formula-escaping), `app/seed/**`, and any migration —
  need a principal diff review before merge, even though the underlying work is
  delegable.
- **Principal-owned, not delegable:** `app/core/**`, `api/deps.py`,
  `api/permissions.py`, `repositories/base.py`, `models/base.py`/`mixins.py`,
  `schemas/base.py`/`common.py`/`errors.py`/`pagination.py`,
  `domain/**/contracts.py`, the solver correctness modules
  (`domain/optimization/model_builder.py`, `scaling.py`, `determinism.py`),
  `providers/__init__.py`/`*/base.py`, `services/audit.py`, `jobs/runner.py`,
  and any migration touching tenancy or composite FKs. Don't modify these
  directly — propose the change and let the principal make or review it.
- A PR that needs a change to a principal-owned file should describe the
  requested change rather than editing the file itself.

**Migrations are append-only once merged.** Never edit a migration that has
already been merged, even to fix a bug in it — write a new migration instead.

## Before opening a PR

Use the checklist in `.github/PULL_REQUEST_TEMPLATE.md`: no secrets committed,
migrations reviewed, organization isolation respected, decimal-as-string
respected.
