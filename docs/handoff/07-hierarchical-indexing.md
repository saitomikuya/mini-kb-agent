# Handoff: 07 Hierarchical JSON Indexing

## Scope completed

This completes Stage 06. It implements only document-card and hierarchical
index generation. There is no query router, retrieval, answer generation,
citations, chat/SSE behavior, or UI.

- Added `${DATA_DIR}/md/<document_id>/card.json`, generated from the complete
  validated Markdown artifact through `ModelRole.INDEX_GENERATION` and
  `generate_json()` with a strict schema.
- Added compact canonical root/folder JSON indexes and administrator-only
  Markdown previews below immutable numbered generation directories.
- Root entries contain only folder id, source directory, summary, document
  count, and folder-index path. Folder indexes contain compact document cards
  and never converted Markdown bodies.
- Added `index_generations` with BUILDING, VALIDATED, ACTIVE, SUPERSEDED, and
  FAILED lifecycle states, root path, document count, and timestamps.
- A build uses an isolated staging directory, validates every reference and
  configured size bound, publishes the numbered generation, and atomically
  replaces `current.json`. Failure before activation leaves the prior pointer
  unchanged.
- Incremental builds call the model only for READY cards in NOT_INDEXED/STALE
  state. Unchanged cards are read and validated, unchanged folder files are
  copied into the next generation, affected folders are rebuilt, and root is
  always rebuilt.
- MISSING sources and non-READY conversions, including FAILED or unconverted
  STALE sources, are excluded from newly activated indexes.
- Added the Admin-triggered `POST /api/admin/jobs/generate-index` Worker path as
  one atomic durable job item.

## Model-role isolation

Indexing resolves exactly:

```python
ModelRegistry.get_for_role(ModelRole.INDEX_GENERATION)
```

It never resolves or calls `document_conversion`, `query_router`, or
`answer_generation`. A four-Provider regression test verifies that only the
index Provider endpoint receives a request.

## Verification

- Conversion stability was checked before indexing changes: all 7 conversion
  tests passed.
- Full Python 3.12.13 suite: `67 passed`.
- Index tests cover first build, one-file add, one-file modification, deletion,
  conversion failure, unconverted STALE exclusion, mid-build failure,
  pre-activation failure, retention of the old current generation, incremental
  model-call counts, compact indexes, background dispatch, and role isolation.
- `alembic upgrade head`, `alembic check`, `alembic downgrade base`, Python
  compilation, and `pip check` all succeeded.

## Explicitly not implemented

- query routing or retrieval;
- answer generation, citations, or conflict handling;
- chat or SSE;
- index, conversion, or knowledge-base UI.

## Continue only when requested

The next allowed stage is Stage 07 Query Routing and Answer Generation, only
after an explicit request. Do not add any of those features while adjusting
Stage 06.
