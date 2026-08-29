# Architecture Decision Records

Accepted decisions are binding unless an explicit user-approved ADR supersedes
them.

## ADR-001: No vector database

- **Status:** Accepted
- **Decision:** Retrieval uses hierarchical root/folder JSON indexes and LLM
  selection. Embeddings, vector databases, and vector-RAG frameworks are
  prohibited.
- **Reason:** The product goal is transparent, file-oriented index navigation
  without a vector infrastructure layer.

## ADR-002: Provider separated from Model Profile

- **Status:** Accepted
- **Decision:** API Provider, Model Profile, and Model Role Binding are separate
  concepts in that order. A Provider must not own one global model field.
- **Reason:** Connection details and model selection have different lifecycles,
  and one endpoint can expose many models or deployments.

## ADR-003: Four independent model roles

- **Status:** Accepted
- **Decision:** `document_conversion`, `index_generation`, `query_router`, and
  `answer_generation` are independently bound to Model Profiles.
- **Reason:** Every workload may require a different provider, model, cost,
  context window, or capability set.

## ADR-004: Source files immutable during conversion

- **Status:** Accepted
- **Decision:** `/app/sources` is the content source of truth. Conversion,
  indexing, routing, and answer generation must never change files there.
  Derived output belongs under `/app/data/md` and `/app/data/index`.
- **Reason:** Generated artifacts must be reproducible without risking the
  user's original knowledge files.

## ADR-005: JSON is canonical index; Markdown is preview only

- **Status:** Accepted
- **Decision:** JSON is the retrieval authority for indexes. A Markdown view of
  an index is only a preview. Converted document Markdown remains a separate
  derived content artifact and is loaded only after index selection.
- **Reason:** One structured representation prevents drift between machine and
  human index formats.

## ADR-006: One container, separate Web and Worker processes

- **Status:** Accepted
- **Decision:** Web and future Huey Worker run as independent supervisord-managed
  processes inside one Docker container. The queue backend is SQLiteHuey.
- **Reason:** Conversion must not block HTTP requests while deployment remains a
  single-container unit.

## ADR-007: Server-rendered UI without a Node build

- **Status:** Accepted
- **Decision:** Jinja2 and Vanilla JavaScript provide the UI. React, Vue, and a
  Node-based build pipeline are prohibited.
- **Reason:** The project favors a small, inspectable deployment and toolchain.

## ADR-008: No business schema in scaffold stage

- **Status:** Accepted
- **Decision:** Stage 01 defines SQLAlchemy and Alembic initialization only. It
  declares no business tables and creates no feature migration revision.
- **Reason:** Persistence design belongs to stage 02 and must follow the stable
  architecture rather than being guessed during scaffolding.

## ADR-009: Role registry is the only business model lookup

- **Status:** Accepted
- **Decision:** Future business code must obtain a model with
  `model_registry.get_for_role(ModelRole.<ROLE>)`. It must not select by
  `provider_id`, decrypt a Provider credential, directly construct an adapter,
  or use a global `DEFAULT_MODEL`. Direct adapter construction is reserved for
  Profile connection testing and the registry implementation.
- **Reason:** Provider is connection/authentication configuration, Model Profile
  is a remote model or deployment, and Role Binding is workload selection.
  Bypassing any layer would collapse their independent lifecycles.

## ADR-010: Provider credentials use derived Fernet encryption

- **Status:** Accepted
- **Decision:** API keys are encrypted with Fernet using a purpose-specific key
  derived from the stable `${DATA_DIR}/app.secret`. Admin APIs expose only a
  masked value and never expose the plaintext or ciphertext. Credential-field
  validation errors and provider errors are sanitized.
- **Reason:** The application needs reversible credentials for outbound calls,
  while database access, API responses, logs, and remote error bodies must not
  reveal usable keys.

## ADR-011: Protocol capability is established by generation requests

- **Status:** Accepted
- **Decision:** OpenAI-compatible `auto` probes Responses with an actual
  generation request, then tries Chat Completions only after failure. It must
  not infer generation support from `GET /v1/models`. JSON generation first
  uses native Structured Outputs and then a strict JSON prompt/parse fallback.
- **Reason:** Model-list endpoints do not prove that a particular request
  dialect or structured-output feature works through a gateway.

## ADR-012: Azure deployment name belongs to Model Profile

- **Status:** Accepted
- **Decision:** For Azure OpenAI, `remote_model_name` stores the deployment name
  on each Model Profile. Azure v1 uses the normalized `/openai/v1/` endpoint and
  sends that deployment as `model`; legacy routing remains an independent
  adapter branch. A Provider must not own one deployment field.
- **Reason:** One Azure resource can expose multiple deployments with different
  models and capabilities.

## ADR-013: Source identity and content-change detection

- **Status:** Accepted
- **Decision:** A source record is uniquely identified by its normalized
  `relative_path`. Scans compare path, size, and nanosecond mtime first and
  calculate SHA-256 only for new, reappearing, or metadata-changed files. A
  matching hash preserves conversion and index states even when metadata has
  changed. Missing files retain their database rows.
- **Reason:** This avoids unnecessary full-file reads and prevents
  timestamp-only changes from triggering false conversion work.

## ADR-014: One source path boundary and atomic managed writes

- **Status:** Accepted
- **Decision:** Every source filesystem operation passes through
  `safe_source_path()`, which rejects absolute paths, parent traversal, resolved
  root escape, and escaping symlinks. Upload and replace use a same-directory
  temporary file, `fsync`, and `os.replace`. Download resolves only a database
  id. `SOURCE_DISPLAY_ROOT` is presentation metadata and must never be used for
  file I/O.
- **Reason:** Source management needs controlled mutation without exposing an
  arbitrary server-path interface or partially written source files.

## ADR-015: Persisted idempotent jobs over queue-owned progress

- **Status:** Accepted
- **Decision:** `jobs` and `job_items` in the main SQLite database are the
  durable authority for background state and progress. Huey uses
  `SqliteHuey` at `/app/data/queue.db` and receives only a stable job id.
  Recovery and retry re-submit the same id, while task claiming skips fresh
  RUNNING jobs, terminal jobs, and completed items. Stale RUNNING jobs return
  to QUEUED on application startup. Worker concurrency defaults to 1, but the
  persisted claim and idempotency rules do not depend on that default.
- **Reason:** Huey delivery can be repeated or interrupted. Persisted state
  makes progress inspectable, supports Worker restart, and prevents recovery
  from manufacturing duplicate Job records or repeating completed work.

## ADR-016: Deterministic conversion with manifest-last atomic artifacts

- **Status:** Accepted
- **Decision:** Source conversion first uses local deterministic adapters.
  MarkItDown/markitdown-ocr handle general documents, while spreadsheet table
  bodies are read with openpyxl or xlrd and split by physical row ranges. A
  model may enrich genuinely visual content only after resolution through
  `ModelRole.DOCUMENT_CONVERSION`; it is never the sole extraction path for
  spreadsheet values. A complete artifact is staged at
  `${DATA_DIR}/tmp/<job_id>/<document_id>/` and directory-swapped into
  `${DATA_DIR}/md/<document_id>/` with `os.replace`. The last successful
  artifact is restored if replacement fails.
- **Reason:** Deterministic extraction preserves source numbers and makes
  conversion reproducible. Staging and replacement prevent partial Markdown
  from becoming the active artifact, while the dedicated role prevents vision
  work from leaking to an answer model.

## ADR-017: Immutable validated index generations

- **Status:** Accepted
- **Decision:** Document cards are generated only from validated READY Markdown
  artifacts through `ModelRole.INDEX_GENERATION`. Canonical root/folder indexes
  are built as JSON in an isolated generation, validated completely, and then
  activated by atomically replacing `${DATA_DIR}/index/current.json`. Unchanged
  cards and folder indexes are reused without another model call. Markdown
  index files are administrator previews only.
- **Reason:** A failed or interrupted build must not partially alter the live
  index, and incremental source changes must not incur unrelated model work.

## ADR-018: Answer models consume evidence, not storage

- **Status:** Accepted
- **Decision:** Final answers resolve only `ModelRole.ANSWER_GENERATION`. The
  model input is limited to the user question, already selected Markdown parts,
  source metadata, and system answer rules. Model-produced citations,
  conflicts, and download ids are untrusted: the backend validates them against
  the current evidence and source records. A model download intent carries only
  `document_id`; filenames, relative directories, and source paths are never
  model-generated.
- **Reason:** Retrieval and answer generation need independent model bindings,
  and neither model output nor a user question may become an arbitrary
  filesystem addressing mechanism. Deterministic provenance checks keep
  hallucinated ids and anchors out of the final structured answer.

## ADR-019: Editable prompts follow supported runtime tasks

- **Status:** Accepted
- **Decision:** Administrators may edit prompts only for the seven task call
  sites implemented by the four model roles. Prompt settings are persisted in
  `model_role_prompt_settings`, independently of the optional model binding,
  and are attached to clients by `ModelRegistry`. Unsupported task keys and
  blank prompts are rejected. Compatibility-oriented Profile request limits
  remain backend defaults instead of ordinary UI fields.
- **Reason:** The configuration page should expose meaningful product choices,
  not transport-level model tuning or controls that do not affect runtime
  behavior. Independent persistence also keeps prompt edits stable when models
  are rebound.

## ADR-020: Local lexical recall is derived and non-authoritative

- **Status:** Accepted
- **Decision:** Every new immutable index generation contains a local FTS5/BM25
  `lexical.sqlite3` built from source paths, card metadata, part summaries, and
  converted Markdown. It recalls a bounded part candidate set before model
  routing. CJK bigrams/trigrams and normalized alphanumeric terms support exact
  names, dates, identifiers, and numeric values without embeddings. The query
  router reranks only the recalled candidates when they exist; an empty or
  low-confidence model result may be supplemented with a bounded local fallback.
  Every lexical folder/document/part id must still pass the canonical JSON/card
  whitelist. A missing lexical artifact from a pre-upgrade generation falls back
  to hierarchical navigation; a malformed present artifact fails closed.
- **Reason:** A lossy root summary cannot reliably expose long-tail facts, while
  sending every Markdown part to a model on every question is unnecessary.
  Deterministic local recall improves exact-term coverage and reduces model
  context without introducing a vector database or a second source of truth.

## ADR-021: Chat history belongs to the browser

- **Status:** Accepted
- **Decision:** The chat stream is stateless and must not create or update
  server-side chat sessions, messages, or events. The Chat UI stores all history
  in browser IndexedDB, identifies the active local conversation in the
  `/chat/<local-id>` URL, and performs per-conversation deletion locally. After
  the first completed exchange, the title endpoint reuses
  `ModelRole.QUERY_ROUTER` to summarize browser-supplied conversation text and
  returns the title without persisting the request or result. Legacy chat tables
  remain migration-compatible but are not part of the runtime chat path.
- **Reason:** Conversation history should remain private to the current browser,
  while direct URLs must still survive refresh and the existing fast router
  model is sufficient for short title generation.
