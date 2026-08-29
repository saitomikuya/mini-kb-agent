# Handoff: 12 Final Integration and Production Acceptance

## Outcome

The product is feature-complete and frozen. This final pass added no product
features. It integrated production packaging, added cross-layer acceptance
tests, fixed the final runtime-layout gaps, completed deployment/operations
documentation, and performed a real Docker lifecycle check.

## Final production changes

- Docker base is exactly `python:3.12-slim`.
- The image exposes/listens on port 8080 and declares only `/app/sources` and
  `/app/data` as volumes.
- Runtime database is `${DATA_DIR}/app.db`; queue storage remains
  `${DATA_DIR}/queue.db`.
- Entrypoint creates source/data/`md`/`index`/`tmp`/`logs`, loads and validates
  `app.secret` (creating it only if absent), runs `alembic upgrade head`, and
  `exec`s supervisord.
- No startup code deletes or recreates an existing application database.
- supervisord runs uvicorn and huey_consumer, with TERM, 30-second stop wait,
  and process-group stop/kill behavior for both programs.
- Docker `STOPSIGNAL SIGTERM` and a stdlib-only `GET /health` HEALTHCHECK are
  present.
- `.dockerignore` excludes local environments, runtime data, tests, docs, and
  build caches from the production context.
- README now covers the full user lifecycle, Linux/macOS and Windows mounts,
  layered model configuration, four roles, operations, backup/restore, all
  environment variables, FAQ, and limitations.
- The README architecture image and editable source are under `docs/assets/`.

## Repository structure

```text
mini-kb-agent/
  app/
    llm/                 Provider adapters, registry, stable role types
    models/              SQLAlchemy persistence models
    routers/             Auth, Admin, Chat, jobs, files, model config
    schemas/             Strict request/response/model-output contracts
    services/            Scan, conversion, index, navigation, answer, jobs
    static/              Vanilla JavaScript, CSS, built-in vision probe
    tasks/               SqliteHuey queue and consumer import target
    templates/           Chat and Admin Jinja2 pages
    config.py            The only environment-variable boundary
    db.py                SQLite engine/session and required PRAGMAs
    main.py              FastAPI construction, lifespan, `/health`
  migrations/            Alembic environment and revisions 0001–0005
  prompts/               Versioned prompt asset directory
  tests/
    test_final_integration.py
    test_production_packaging.py
    ...                  Feature and regression suites
  docs/
    assets/architecture.drawio
    assets/architecture.drawio.png
    ARCHITECTURE.md
    DECISIONS.md
    STATUS.md
    handoff/12-final.md
  Dockerfile
  entrypoint.sh
  supervisord.conf
  alembic.ini
  pyproject.toml
  README.md
```

## Required Docker commands

```bash
docker build -t mini-kb-agent:latest .

docker run -d \
  --name mini-kb-agent \
  -p 8080:8080 \
  -v /absolute/path/to/knowledge:/app/sources \
  -v /absolute/path/to/data:/app/data \
  -e CHAT_PASSWORD='change-me' \
  -e ADMIN_PASSWORD='change-me-too' \
  -e TZ='Asia/Shanghai' \
  --restart unless-stopped \
  mini-kb-agent:latest
```

Windows PowerShell uses the same container paths with host paths such as
`C:\Knowledge:/app/sources` and `C:\mini-kb-data:/app/data`; the complete
command is in the README.

## Test and acceptance results

- Full pytest suite: **95 passed, 2 upstream/optional warnings**.
- New final acceptance tests: 5.
- Scenarios A–E are exercised in one HTTP + Huey + filesystem + model-adapter
  integration flow.
- Scenario F uses the real converter, crashes after the second job item has
  been claimed, recovers the same job id, and confirms that the first artifact
  remains byte-identical.
- Scenario G rejects parent traversal, absolute paths, and symlink escape.
- The real entrypoint is executed twice by pytest; an inserted SQLite marker
  row and the exact `app.secret` bytes survive the second launch.
- `pip check` passes, compileall passes, Alembic upgrade/check passes.
- Installed top-level packages contain none of: LangChain, LlamaIndex,
  ChromaDB, FAISS, Qdrant, Milvus, Pinecone, Redis, or Celery.

## Four-role proof

The final integration transport exposes exactly:

```text
document_conversion -> conversion-model
index_generation    -> index-model
query_router        -> router-model
answer_generation   -> answer-model
```

Two initial image conversions and one changed-image conversion call only
`conversion-model`; two initial cards and one changed card call only
`index-model`; navigation calls only `router-model`; final structured answers
call only `answer-model`.

## Docker lifecycle acceptance

The built image was run with two empty host mounts and alternate host port
18080. Observed results:

- Alembic created all five revisions in `/app/data/app.db`.
- `/app/data` contained `app.db`, `queue.db`, `app.secret`, `md/`, `index/`,
  `tmp/`, and `logs/`.
- `app.secret` was 32 bytes with mode 0600.
- `docker top` showed PID 1 supervisord with separate uvicorn and
  huey_consumer children.
- Docker reported `healthy`; `/health` returned `{"status":"ok"}`.
- SIGTERM caused Huey to shut down and uvicorn to complete application
  shutdown; the container exited 0.
- Restart retained an inserted SQLite marker row and the exact secret hash.
- SIGINT was received by supervisord, both child processes shut down cleanly,
  and the container again exited 0.

The temporary acceptance container and its temporary host directories were
removed after verification. The `mini-kb-agent:latest` image remains locally.

## Environment variables

| Name | Default | Purpose |
| --- | --- | --- |
| `CHAT_PASSWORD` | empty | Independent Chat login; empty cannot authenticate |
| `ADMIN_PASSWORD` | empty | Independent Admin login; empty cannot authenticate |
| `DATA_DIR` | `/app/data` | Database, queue, secret, and artifact root |
| `SOURCE_DIR` | `/app/sources` | Source-of-truth file root |
| `SOURCE_DISPLAY_ROOT` | empty | Display metadata only; never used for I/O |
| `TZ` | `UTC` | Process/container timezone |
| `SESSION_MAX_AGE` | `604800` | Signed-cookie maximum age in seconds |
| `JOB_HEARTBEAT_TIMEOUT` | `60` | Stale RUNNING lease timeout in seconds |
| `NAVIGATION_MAX_SELECTED_DOCUMENTS` | `12` | Maximum accepted documents per navigation |
| `NAVIGATION_MAX_SELECTED_PARTS` | `16` | Maximum evidence parts per answer |
| `NAVIGATION_MAX_ROUNDS` | `2` | Logical navigation round cap |
| `NAVIGATION_DEFAULT_CONTEXT_WINDOW` | `32768` | Router fallback context window |
| `NAVIGATION_DEFAULT_MAX_OUTPUT_TOKENS` | `2048` | Router fallback output reserve |
| `QUERY_ROUTER_CONTEXT_TOKENS` | `131072` | Router runtime target, capped by Profile |
| `ANSWER_CONTEXT_TOKENS` | `131072` | Answer runtime target, capped by Profile |
| `ANSWER_MAX_OUTPUT_TOKENS` | `8192` | Answer output target, capped by Profile |
| `LEXICAL_CANDIDATE_PARTS` | `80` | FTS/BM25 candidate cap |
| `LEXICAL_MAX_PARTS_PER_DOCUMENT` | `12` | Per-document lexical candidate cap |

The Dockerfile additionally sets `PYTHONDONTWRITEBYTECODE=1` and
`PYTHONUNBUFFERED=1`. There are no hidden Provider/model/default-model
environment variables.

## First-use sequence

1. Create empty host knowledge and data directories.
2. Build and run the image with both absolute-path mounts and independent
   passwords.
3. Open `http://localhost:8080/admin` and log in with `ADMIN_PASSWORD`.
4. Create Provider records.
5. Create one or more Provider-scoped Model Profiles and test them.
6. Bind `document_conversion`, `index_generation`, `query_router`, and
   `answer_generation`; conversion requires the tested Vision capability.
7. Upload files or copy them into the mounted knowledge directory, then scan.
8. Convert NEW/CHANGED files and wait for the Worker to finish.
9. Generate and activate the index; confirm files are INDEXED.
10. Open `http://localhost:8080/chat`, log in with `CHAT_PASSWORD`, and ask a
    question or request an original source download.

## Known limitations

- One host/container only; no horizontal scaling.
- Container processes run as root for bind-mount compatibility; use dedicated
  source/data directories and do not use this as an untrusted multi-tenant
  security boundary.
- Worker concurrency is intentionally 1.
- Audio/video conversion is unsupported.
- Visual/scanned conversion quality depends on the configured Vision model.
- Indexing, navigation, and answers require reachable external model APIs.
- `/health` validates the Web HTTP process, not Provider reachability or an
  end-to-end Worker probe.
- Stop the container for the most consistent SQLite/artifact backup.
- MarkItDown imports emit pydub's optional ffmpeg warning because audio is not
  installed/supported; the verification host also emitted an onnxruntime CPU
  vendor warning under Docker Desktop emulation. Both Web and Worker remained
  healthy.

## Closure

There is no next feature stage. Preserve the architecture invariants in
`docs/ARCHITECTURE.md` and accepted decisions in `docs/DECISIONS.md` for any
future maintenance fix.
