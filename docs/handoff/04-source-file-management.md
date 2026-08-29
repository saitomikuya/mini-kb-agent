# Handoff: 04 Source File Management

## Completed

- Added Alembic revision `20260828_0003` and matching SQLAlchemy
  `source_files` model with the exact requested fields.
- Added uppercase source, conversion, and index enums plus database check
  constraints. New files start as `PRESENT`, `NEW`, and `NOT_INDEXED`.
- Added recursive `SOURCE_DIR` scanning. The fast path compares unique logical
  relative path, size, and nanosecond mtime. SHA-256 is streamed in 1 MiB chunks
  only for new, reappearing, or metadata-changed files.
- A recomputed identical hash updates metadata and restores `PRESENT` without
  changing conversion or index states. A different hash sets conversion to
  `CHANGED`, clears the prior error, and changes `INDEXED` to `STALE`.
- Files absent from a scan are marked `MISSING`; their database rows and last
  known fingerprint remain. Admin deletion removes the physical source and
  applies the same missing/stale state without deleting the row.
- Added Admin-only list, scan, upload, atomic replace, and delete APIs, plus
  Chat/Admin source download by database id.
- Upload accepts multipart `file` and optional logical `relative_path`; replace
  accepts multipart `file` and always preserves the record's path. Both stream
  to a same-directory temporary file, call `fsync`, and publish with
  `os.replace`.
- Added `safe_source_path()` as the shared boundary for scan, upload, replace,
  delete, and download. It rejects empty/absolute paths, `..`, resolved root
  escape, and symlink escape. Browser APIs expose no arbitrary server-path
  download.
- Added optional `SOURCE_DISPLAY_ROOT`. API records expose `display_path`
  derived from it, while all real file operations continue to use only
  `SOURCE_DIR`.

## HTTP contract

Admin session required:

```text
GET    /api/admin/files
POST   /api/admin/files/scan
POST   /api/admin/files/upload
PUT    /api/admin/files/{id}/replace
DELETE /api/admin/files/{id}
```

Chat or Admin session required:

```text
GET /api/files/{id}/download
```

The scan response reports `scanned`, `new`, `changed`, `unchanged`, `missing`,
and `unsafe_skipped`. `missing` is the current number of known database records
not observed in the source directory.

## State semantics

- First observation or upload: `PRESENT / NEW / NOT_INDEXED`.
- Different content hash: `PRESENT / CHANGED`; an `INDEXED` record becomes
  `STALE`, while `NOT_INDEXED` remains `NOT_INDEXED`.
- Same content hash after metadata change or identical replacement: processing
  states are preserved.
- Missing or deleted source: `source_status=MISSING`; the row is retained and
  an `INDEXED` state becomes `STALE` for later cleanup.
- No conversion, conversion queue, Markdown artifact, or index cleanup runs in
  this stage.

## Verification

- Python 3.12.13.
- Full `.venv/bin/pytest`: `44 passed`.
- Tests cover new, unchanged, content-changed, timestamp-only/hash-identical,
  missing, uploaded, replaced, downloaded, and deleted files.
- Tests cover Unicode filenames, Chinese directories, `../`, POSIX/Windows
  absolute paths, symlink escape, Admin/download authentication, and bounded
  streaming SHA-256 for a multi-megabyte input.
- `alembic upgrade head`: succeeded through revision `0003`.
- `alembic check`: `No new upgrade operations detected.`
- `alembic downgrade base`: succeeded through revisions `0003`, `0002`, and
  `0001`.
- Python compilation succeeded for `app/`, `migrations/`, and `tests/`.
- OpenAPI route paths and the exact `source_files` column contract were
  verified directly; `pip check` reported no broken requirements.

The only test warning remains FastAPI/Starlette's upstream `TestClient`
compatibility deprecation under the installed dependency versions.

## Deliberately not implemented

Huey configuration or Worker, document conversion, MarkItDown, generated
Markdown, LLM business calls, hierarchical index creation or cleanup,
retrieval, chat/SSE, citations, HTML pages, or any UI.

## Read before continuing

1. `docs/ARCHITECTURE.md`
2. `docs/DECISIONS.md`
3. `docs/STATUS.md`
4. This handoff

Keep source management separate from conversion. Do not use
`SOURCE_DISPLAY_ROOT` for I/O, bypass `safe_source_path()`, delete missing rows,
or mark timestamp-only changes as content changes.

## Next allowed stage

Stage 05: Async and Incremental Conversion, only after the user explicitly
requests it. Do not begin indexing, retrieval, chat, or UI work while adding the
conversion pipeline.
