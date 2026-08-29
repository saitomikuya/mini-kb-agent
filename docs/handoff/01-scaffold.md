# Handoff: 01 Scaffold

## Completed

- Created the requested Python package and project directory skeleton.
- Added a minimal FastAPI application with `GET /health`.
- Added immutable environment-backed settings.
- Added SQLAlchemy 2.x Engine, Session factory, and empty declarative Base.
- Initialized Alembic without a business migration revision.
- Added minimal smoke tests, including an assertion that no business tables are
  declared.
- Added the Docker, entrypoint, and supervisord scaffold. Only Web is configured;
  a Worker is intentionally absent.
- Recorded the stable architecture and accepted ADRs.

## Verification

- Verification interpreter: Python 3.12.14.
- `pytest`: `2 passed in 0.24s`.
- The health smoke test issued an ASGI request and verified HTTP 200 with
  `{"status": "ok"}`.
- `alembic upgrade head`: succeeded against SQLite using both the configurable
  URL and the repository default; the initialized database contained only
  `alembic_version`.
- `compileall`: succeeded for `app/`, `migrations/`, and `tests/`.
- Runtime inspection: `/health` is registered and `Base.metadata` contains zero
  business tables.

The Dockerfile could not be built during this run because Docker Hub repeatedly
timed out during the TLS handshake for `python:3.12-slim`. The failure occurred
before project files or dependencies were processed; it does not alter the
successful Python 3.12 application checks above.

## Deliberately not implemented

Provider/model storage or clients, API keys, login/authentication, scanning,
upload, Huey tasks or Worker, conversion, incremental detection, indexing,
retrieval, chat, SSE, HTML pages, citations, conflict UI, and downloads.

## Read before continuing

1. `docs/ARCHITECTURE.md`
2. `docs/DECISIONS.md`
3. `docs/STATUS.md`
4. This handoff

Do not reinterpret the project as vector RAG. Do not combine Provider and Model
Profile. Do not write to source files.

## Next allowed stage

Stage 02: Database and Auth, only after the user explicitly requests it. Define
the persistence/auth scope and migration before writing business models. Do not
implement model providers during stage 02.
