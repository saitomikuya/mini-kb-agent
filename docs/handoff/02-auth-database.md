# Handoff: 02 Database and Auth

## Completed

- Centralized all environment-variable reads in `app/config.py` for
  `CHAT_PASSWORD`, `ADMIN_PASSWORD`, `DATA_DIR`, `SOURCE_DIR`, `TZ`, and
  `SESSION_MAX_AGE`.
- Derived the SQLite database path, signing-secret path, and future derived-data
  paths from `DATA_DIR`.
- Added SQLAlchemy models for `chat_sessions`, `messages`, and `chat_events`.
- Added Alembic revision `20260828_0001` with the same three tables and cascade
  foreign keys from messages to sessions and events to messages.
- Applied `journal_mode=WAL`, `foreign_keys=ON`, and `busy_timeout=5000` to every
  SQLite connection created through the application database boundary,
  including online Alembic migrations.
- Added the independent Chat and Admin password login endpoints, logout, and
  current-session endpoint.
- Added signed and timed `itsdangerous` role cookies with `HttpOnly`,
  `SameSite=Lax`, and the configured maximum age.
- Added `require_chat` and `require_admin`. Administrators may use chat APIs;
  Chat sessions receive HTTP 403 from the Admin dependency.
- Added automatic generation of `${DATA_DIR}/app.secret`. The file contains 32
  random bytes, is created without overwrite, and is tightened to mode `0600`
  where the filesystem permits. This stable root secret is reserved for later
  Fernet key derivation as required by the architecture.
- Updated the container entrypoint to apply `alembic upgrade head` before
  starting the Web process.

## HTTP contract

```text
POST /api/auth/chat/login
POST /api/auth/admin/login
POST /api/auth/logout
GET  /api/auth/me
```

Both login endpoints accept JSON in the form `{"password": "..."}`. Unset or
empty configured passwords cannot authenticate. `/api/auth/me` returns HTTP 401
for a missing, expired, or invalid signature. No Basic Auth is present.

The dependency probes used by the tests are registered only on the per-test
FastAPI application under `/_internal/tests/*`; they are not production routes
and are excluded from OpenAPI.

## Persistence details

- `chat_sessions.title`, all roles/content/event types, and timestamps are
  non-null.
- `messages.answer_json` is nullable because not every role produces a
  structured answer.
- `chat_events.event_json` is non-null.
- Deleting a chat session cascades to its messages; deleting a message cascades
  to its events. SQLite foreign-key enforcement is enabled on every application
  connection.
- No Provider, Model Profile, role-binding, source-file, conversion, queue, or
  knowledge-index tables were added.

## Verification

- Verification interpreter: Python 3.12.13.
- Full `pytest`: `12 passed`.
- Covered correct/wrong Chat password, correct Admin password, role separation,
  Admin access, logout, Cookie tampering, secret-file permission, exact settings
  variables, migration schema/foreign keys, and all required SQLite PRAGMAs.
- `alembic upgrade head`: succeeded and created only `alembic_version`,
  `chat_sessions`, `messages`, and `chat_events`.
- `alembic downgrade base`: succeeded.
- `alembic check`: `No new upgrade operations detected.`
- `compileall`: succeeded for `app/`, `migrations/`, and `tests/`.

The test environment emits one upstream Starlette deprecation warning from
FastAPI's current `TestClient` compatibility layer; it does not affect test
results or application behavior.

## Deliberately not implemented

API Providers, Model Profiles, model-role bindings, API keys/Fernet encryption,
file tables or upload, source management, Huey or a Worker, Markdown conversion,
incremental processing, hierarchical knowledge indexes, LLM calls, chat feature
APIs, SSE, and frontend pages.

## Read before continuing

1. `docs/ARCHITECTURE.md`
2. `docs/DECISIONS.md`
3. `docs/STATUS.md`
4. This handoff

Do not reinterpret the project as vector RAG. Keep Provider, Model Profile, and
Model Role Binding separate. Do not write to source files.

## Next allowed stage

Stage 03: Model Providers, only after the user explicitly requests it. Do not
begin source-file management or later pipeline work during the Provider stage.
