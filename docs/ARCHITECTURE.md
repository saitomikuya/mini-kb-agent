# mini-kb-agent Architecture Contract

## 1. Document authority

This document records the stable architecture of `mini-kb-agent`. Later Codex
sessions must read this file, `docs/STATUS.md`, `docs/DECISIONS.md`, and the
latest `docs/handoff/*.md` before changing code.

The rules marked **MUST**, **MUST NOT**, and **DO NOT IMPLEMENT EARLY** are
binding. They must not be changed casually during feature work. A necessary
change requires an explicit user decision and a new or superseding ADR in
`docs/DECISIONS.md`.

Stage 01 establishes boundaries only. Descriptions of future behavior are
contracts, not evidence that those features already exist.

## 2. Product boundary

The final system will contain:

- a chat page and an administration page;
- local knowledge-source file management;
- asynchronous and incremental source-to-Markdown conversion;
- hierarchical JSON indexes;
- LLM-guided selection of relevant Markdown through those indexes;
- answers grounded in the selected Markdown;
- source citations, conflict display, and source-file download;
- OpenAI-compatible, Azure OpenAI, and Sub2API connectivity;
- independent configuration for four model roles;
- deployment as one Docker container.

The system MUST NOT use embeddings, a vector database, LangChain, LlamaIndex,
Elasticsearch, Redis, Celery, PostgreSQL, React, Vue, or a Node-based frontend
build.

## 3. Fixed technology stack

- Python 3.12
- FastAPI and Jinja2
- Vanilla JavaScript
- SQLite
- SQLAlchemy 2.x and Alembic
- Huey `SqliteHuey`
- OpenAI Python SDK and httpx
- MarkItDown, markitdown-ocr, and openpyxl
- itsdangerous and cryptography/Fernet
- pytest and respx
- supervisord

New infrastructure or framework dependencies require explicit user approval.
Small runtime adapters required by the listed stack, such as FastAPI's ASGI
server extra, are not alternative application frameworks.

## 4. Runtime and repository layout

The Docker working directory is `/app`. Runtime paths are:

```text
/app/sources       managed source-of-truth files
/app/data/md       derived Markdown
/app/data/index    derived canonical JSON indexes
```

Repository package boundaries are:

```text
app/main.py        application construction and system endpoints
app/config.py      process configuration boundary
app/db.py          SQLAlchemy initialization boundary
app/models/        persistence models
app/schemas/       input/output schemas
app/routers/       HTTP feature routers
app/services/      application orchestration
app/llm/           provider/profile/role abstractions and LLM calls
app/tasks/         Huey task entry points
app/templates/     server-rendered Jinja2 templates
app/static/        handwritten JavaScript, CSS, and static assets
prompts/           versioned prompt templates
migrations/        Alembic environment and revisions
```

Dependencies should point inward through explicit service boundaries. HTTP
routers must not contain conversion, indexing, or provider logic. Huey tasks
must call services rather than duplicate service logic.

## 5. Source-of-truth and mutation contract

Files under `/app/sources` are the only source of truth for knowledge content.
The application, LLMs, conversion code, indexing code, Web process, and Worker
process MUST NOT modify source files during conversion or retrieval.

Authenticated source-management APIs may add, atomically replace, or delete
files under the configured source root. No other feature may mutate them.

Markdown and indexes are reproducible derived artifacts. They must be written
only under `/app/data/md` and `/app/data/index`. A failed conversion or indexing
operation must not make a partially written artifact appear complete. The exact
atomic-write strategy belongs to the relevant later stage.

Source download must return the source file, never a generated substitute.

## 6. Retrieval contract: hierarchical index, not vector RAG

The retrieval path is fixed:

```text
User question
  -> root JSON index
  -> choose one or more folders
  -> folder JSON index
  -> choose document and/or document part
  -> load selected generated Markdown
  -> generate the final grounded answer
```

The query router must make its selection from the hierarchical indexes. It must
not silently introduce similarity search, embeddings, or an external search
engine. The answer generator receives only the selected context and the
metadata needed for citations and conflict reporting.

JSON is the canonical machine-readable index format. Any Markdown rendering of
an index is a human preview only and must never become the retrieval authority.
This does not demote converted document Markdown: document Markdown remains a
derived retrieval artifact loaded after index selection.

## 7. Provider, model profile, and role binding contract

Provider and model configuration MUST be separate concepts. A provider record
must not contain a single `model=gpt-...` field that implicitly controls all
uses.

The required direction of dependency is:

```text
API Provider
  -> Model Profile
  -> Model Role Binding
```

Conceptual responsibilities:

- **API Provider**: endpoint family and connection/authentication behavior for
  OpenAI-compatible, Azure OpenAI, or Sub2API APIs.
- **Model Profile**: a provider-scoped model/deployment name plus model-level
  request capabilities and hard context/output ceilings.
- **Model Role Binding**: chooses one Model Profile for one application role.
- **Knowledge tuning**: persisted runtime targets for retrieval, evidence,
  chunking, index granularity, and answer generation. A request uses the lower
  of its tuning target and the selected Profile ceiling.

The four required role identifiers are exactly:

```text
document_conversion
index_generation
query_router
answer_generation
```

Each role is independently configurable. Roles may share a provider and use
different models, use different providers, share the same model profile, or use
four unrelated model profiles. No service may assume that two roles share a
provider, credentials, model name, request dialect, or capability set.

Stage 03 implements the model separation as the tables `api_providers`,
`model_profiles`, and `model_role_bindings`; later runtime tuning is stored in
the singleton `knowledge_tuning_settings` table. The following equivalences are
explicitly false:

```text
Provider != Model
Model != Role
```

An Azure Provider represents one Azure resource. Each Azure Model Profile
stores its own deployment name in `remote_model_name`, so one resource can own
multiple deployments. Deployment names MUST NOT move onto the Provider.

Business services MUST NOT select a provider, decrypt credentials, construct a
provider client, or call a model by `provider_id`. They MUST request a client
through:

```python
model_registry.get_for_role(ModelRole.QUERY_ROUTER)
```

Direct client construction is limited to the model-configuration connection
test and the registry itself. There is no global `DEFAULT_MODEL` escape hatch.

Role capability validation is enforced both when a role is bound and again
when the registry resolves it:

- `document_conversion`: tested text and vision support;
- `index_generation`: tested text support; native Structured Outputs are
  preferred, but adapter JSON prompt/parse fallback is allowed;
- `query_router`: tested text plus a successful reliable-JSON probe; native
  Structured Outputs are optional because the adapter supplies fallback;
- `answer_generation`: tested text support.

The application may start with no role bindings. Missing roles fail only when
a later model-dependent operation requests them through the registry.

Editable prompt settings are persisted independently from role bindings, so an
administrator may prepare prompts before choosing a model. The backend exposes
only prompt tasks that have a real runtime call site: visual evidence conversion,
three index-generation tasks, two query-routing phases, and grounded final-answer
generation. Defaults and task metadata are centralized in
`app/llm/prompts.py`. Model resolution attaches the effective role prompts to the
role-scoped client, keeping Web and Worker behavior consistent. Changing an
index or visual-conversion prompt must invalidate the corresponding derived cache.

## 8. Conversion and incremental-processing contract

Conversion is asynchronous. The Web process accepts or initiates work and
records/enqueues it; it must not perform document conversion inside an HTTP
request. A separate Worker process consumes Huey `SqliteHuey` tasks from
`/app/data/queue.db`.

Durable business progress belongs in the main SQLite database's `jobs` and
`job_items` tables, not in Huey result storage or process memory. Huey task
payloads carry a stable job id. Re-delivery and recovery submit that same id;
they never create a replacement Job row. Task execution must claim persisted
state, skip terminal/already-owned work, and never repeat completed items.

The first Worker deployment uses concurrency 1 to limit SQLite and filesystem
contention. Claiming and idempotency must still be correct independently of
that default so concurrency can be revisited later.

Incremental processing must identify unchanged source inputs and avoid needless
reconversion/reindexing. The later design must define stable source identity,
change detection, artifact versioning, status, retry behavior, and recovery
before implementing the pipeline.

Web and Worker are separate OS processes managed by supervisord but deployed in
the same Docker container. They may coordinate only through explicit persisted
state/artifacts and the SQLite-backed queue, not in-process memory. Redis,
Celery, and a second service container are prohibited.

A RUNNING job whose heartbeat has timed out is recovered on Web startup by
atomically returning the same job and any RUNNING item to QUEUED, then
re-submitting the same job id.

## 9. Answer evidence contract

Answers retain enough provenance to identify and download their source files.
The answer model receives only the user question, selected Markdown parts,
trusted source metadata, and system answer rules, and it is resolved only
through `ModelRole.ANSWER_GENERATION`. Citation document/part/anchor tuples are
validated against the selected evidence after generation. If selected sources
contain inconsistent claims, the answer path preserves and exposes every valid
conflicting value rather than averaging or choosing one. Model download output
contains only `document_id`; the backend validates that id and supplies trusted
filename and relative-directory metadata.

## 10. Security and configuration contract

Configuration enters through the settings boundary in `app/config.py` and may
be extended in later stages. Secrets must not be committed or emitted to logs.
Provider API keys are encrypted at rest with Fernet using a purpose-specific
key derived from `${DATA_DIR}/app.secret`. Plaintext credentials exist only at
the schema/service/adapter boundary required to encrypt or issue a request.
Provider API responses expose only `api_key_masked`; they never expose the
plaintext or `encrypted_api_key`. Validation errors for credential fields are
redacted, remote response bodies are excluded from adapter errors, and model
requests do not log headers or payloads.

Source paths and generated paths must be validated before filesystem access.
Stage 04 enforces the source boundary through `safe_source_path()`. Generated
paths must never escape their configured roots when those later stages arrive.

## 11. Web and UI contract

Pages will be rendered with Jinja2 and enhanced with handwritten Vanilla
JavaScript. There is no React, Vue, Node runtime, package manager, bundler, or
frontend compilation step.

## 12. Stage-01 implementation boundary

Stage 01 may contain only:

- the package and directory skeleton;
- a minimal FastAPI application;
- `GET /health`;
- configuration loading primitives;
- SQLAlchemy initialization primitives with no business tables;
- an initialized Alembic environment with no business revisions;
- minimal smoke tests;
- architecture, status, decision, and handoff documentation;
- a non-production Docker/supervisord scaffold with no Worker.

Stage 01 MUST NOT implement Provider records or clients, API key handling,
login/authentication, file scanning/upload, Huey configuration or tasks,
conversion, incremental detection, indexes, chat, SSE, HTML pages, or complete
production container startup logic.

## 13. Stage-03 implementation contract

Stage 03 adds only model configuration and connectivity:

- Provider/Profile/Role Binding persistence and Alembic migration;
- Admin-only Provider and Model Profile CRUD APIs;
- Admin-only role read/update APIs for exactly the four roles in section 7;
- Fernet API-key encryption and masked response presentation;
- a `ModelClient` protocol with `generate_text`, `generate_json`, and
  `generate_multimodal`;
- OpenAI-compatible, Azure OpenAI, and Sub2API adapters;
- Responses/Chat Completions selection based on real POST probes, never
  `GET /v1/models` alone;
- Profile-level text, JSON, and built-in-image vision connection testing;
- `ModelRegistry.get_for_role(...)` for all future business model access.

OpenAI-compatible `auto` tries Responses first and then Chat Completions after
an actual request failure. Azure v1 normalizes the resource endpoint to
`https://RESOURCE.openai.azure.com/openai/v1/` and sends the Profile's
`remote_model_name` deployment as `model`. Azure legacy uses its independent
deployment-path adapter branch. Sub2API retains a distinct Provider/client type
while reusing the OpenAI-compatible protocol core.

The Profile connection test is sequential: plain text, exact `{"ok": true}`
JSON, then the built-in `KB-VISION-42` image. It persists discovered
capabilities, the tested protocol, total latency, status, and timestamp. A
vision failure does not erase successful text capability and does not prevent
binding the Profile to a text-only role.

Stage 03 MUST NOT implement source scanning or upload, Huey, MarkItDown,
conversion, indexes, retrieval, chat, UI pages, or any knowledge-base behavior.

## 14. Stage-04 implementation contract

Stage 04 adds only source-file inventory and source management:

- a `source_files` table keyed by unique logical `relative_path`;
- scanning of `SOURCE_DIR` with size and nanosecond mtime as the fast path;
- streaming SHA-256 only for new, reappearing, or metadata-changed files;
- preservation of processing states when metadata changes but content hash does
  not;
- `MISSING` records for absent files, without database-row deletion;
- Admin list, scan, upload, replace, and delete APIs;
- authenticated source download by database id;
- one containment-checking `safe_source_path()` boundary for filesystem access;
- optional `SOURCE_DISPLAY_ROOT` output that is never an I/O root;
- same-directory temporary-file, `fsync`, and `os.replace` writes.

Stage 04 MUST NOT add Huey, conversion, Markdown generation, model business
calls, hierarchical index generation, retrieval, chat behavior, or HTML pages.

## 15. Stage-05 implementation contract

Stage 05 connects persisted jobs to incremental source-to-Markdown conversion:

- `POST /api/admin/jobs/convert-changed` selects only PRESENT files in NEW or
  CHANGED state, plus FAILED only when retry is explicitly requested;
- `POST /api/admin/files/{id}/convert` creates an explicit one-file conversion
  job, while READY files remain excluded from automatic incremental work;
- supported formats are PDF, DOCX, PPTX, XLSX, XLS, CSV, TSV, TXT, MD, HTML,
  JSON, XML, PNG, JPG, JPEG, and WEBP; other extensions become UNSUPPORTED;
- local deterministic extraction precedes optional multimodal enrichment;
- spreadsheet cell/table bodies are read by code and large sheets are split by
  physical row ranges; a screenshot/LLM path must never recreate cell values;
- visual calls resolve only through
  `ModelRegistry.get_for_role(ModelRole.DOCUMENT_CONVERSION)` and a missing or
  unusable conversion binding fails the file clearly;
- each Markdown part carries document/source/hash/part/time/converter metadata
  and a page, slide, sheet/rows, or section source anchor;
- complete artifacts are staged below `${DATA_DIR}/tmp/<job>/<document>/` and
  directory-replaced below `${DATA_DIR}/md/<document>/`; a failure retains the
  previous successful artifact;
- one failed item marks only its source FAILED and does not prevent later job
  items from running.

Stage 05 MUST NOT create document cards, root/folder indexes, query routing,
answers, retrieval behavior, or UI.
