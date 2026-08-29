# Handoff: 05 Background Job Infrastructure

## Scope completed

This is intentionally only the background-infrastructure slice of Stage 05.
It does not implement source-file conversion or incremental artifacts.

- Added the exact requested `jobs` and `job_items` fields in Alembic revision
  `20260828_0004` and matching SQLAlchemy models.
- Added the shared uppercase states `PENDING`, `QUEUED`, `RUNNING`,
  `COMPLETED`, and `FAILED`, with database constraints and progress checks.
- Added Huey `SqliteHuey` at `${DATA_DIR}/queue.db`, which resolves to
  `/app/data/queue.db` in the container. Redis is not used.
- Added separate supervisord Web and Worker programs. Worker concurrency is
  explicitly 1 for the first release.
- Added Admin-only list, current, detail, and fake-work APIs.
- Added persisted heartbeat/progress updates, item attempts, per-item failure
  isolation, automatic Huey retry, and terminal job aggregation.
- Added startup recovery for stale RUNNING jobs. Recovery atomically returns
  the same job and RUNNING item to QUEUED and submits the same job id; it never
  creates another Job row.
- Made the task idempotent: terminal/freshly owned jobs are no-ops, completed
  items are skipped, and claiming is a conditional database update rather than
  an in-process lock.

## HTTP contract

All endpoints require an Admin session:

```text
GET  /api/admin/jobs
GET  /api/admin/jobs/current
GET  /api/admin/jobs/{id}
POST /api/admin/jobs/test-background
```

`POST /api/admin/jobs/test-background` creates five fake items, persists them
as QUEUED, submits the existing job id to Huey, and returns HTTP 202. The fake
Worker sleeps briefly while emitting heartbeats and progress. It never opens a
source file and produces no artifact.

`GET /api/admin/jobs/current` returns the newest PENDING, QUEUED, or RUNNING
job, or JSON `null` when there is no active job. Detail responses include the
job's items; list/current responses contain job summaries.

## Runtime and recovery

```text
Web process
  -> main SQLite: create jobs/job_items
  -> /app/data/queue.db: enqueue stable job_id

Worker process (default concurrency 1)
  -> conditional persisted claim
  -> fake item work
  -> main SQLite: attempts/heartbeat/progress/terminal state
```

The default heartbeat timeout is 60 seconds and can be configured with
`JOB_HEARTBEAT_TIMEOUT`. On Web startup, only RUNNING jobs with a null or
expired heartbeat are recovered. A second recovery pass sees QUEUED and does
nothing. If a Huey delivery itself is duplicated, persisted claiming and
terminal checks make the duplicate a no-op.

Expected `ItemFailure` exceptions mark only that item FAILED and processing
continues. Unexpected exceptions return the current item and job to QUEUED and
are retried by Huey twice with a one-second delay. Once retries are exhausted,
the item and job become FAILED. A hard Worker interruption leaves RUNNING state
for heartbeat recovery.

## Verification

- Python 3.12.13.
- Full `.venv/bin/pytest`: `51 passed`.
- Tests cover job creation, API inspection, Web response before work, partial
  progress, completion, item failure isolation, retry, stale recovery,
  duplicate recovery, duplicate task delivery, application-startup recovery,
  and Worker-restart simulation.
- A real independent `huey_consumer.py` process consumed a queued test job with
  one thread and persisted `COMPLETED / 5 of 5` after the enqueuing process had
  exited.
- `alembic upgrade head`, `alembic check`, and `alembic downgrade base`
  succeeded. `alembic check` reported no schema drift.
- Python compilation succeeded for `app/`, `migrations/`, and `tests/`.

The only test warning remains the upstream FastAPI/Starlette `TestClient`
deprecation under the installed dependencies.

## Explicitly not implemented

- MarkItDown or any real source-file conversion;
- any LLM call;
- generated Markdown or other derived artifacts;
- hierarchical index creation or cleanup;
- a real conversion button or conversion endpoint;
- HTML pages or other UI.

## Continue only when requested

The next Stage-05 slice may connect eligible `source_files` to this job
infrastructure and implement incremental conversion. Read
`docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `docs/STATUS.md`, and this handoff
before doing so. Do not begin conversion, indexing, retrieval, chat, or UI
without an explicit request.
