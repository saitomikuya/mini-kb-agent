# Handoff: 06 Source-to-Markdown Document Conversion

## Scope completed

This completes Stage 05. It implements only source-file to Markdown artifact
conversion and connects that work to the existing durable Job/Worker path.
There is no indexing, retrieval, answer generation, chat behavior, or UI.

- Added deterministic conversion for PDF, DOCX, PPTX, XLSX, XLS, CSV, TSV,
  TXT, MD, HTML, JSON, XML, PNG, JPG, JPEG, and WEBP. Other extensions become
  `UNSUPPORTED` without blocking later job items.
- Enabled MarkItDown and the markitdown-ocr plugin for general local
  PDF/Office/HTML extraction. The plugin's OpenAI-shaped OCR hook is a local
  facade with no Provider, credential, Profile, or remote model name; it
  delegates embedded image calls into the role-resolved application client.
- Kept spreadsheet data on a code-only path: openpyxl reads XLSX, xlrd reads
  XLS, and Python's CSV reader handles CSV/TSV. Markdown tables use generated
  column letters plus physical source row numbers and split every 200 rows.
- Added optional multimodal enrichment for standalone images, embedded Office
  images, and scanned PDF pages. The only resolver call is
  `ModelRegistry.get_for_role(ModelRole.DOCUMENT_CONVERSION)`.
- Added required YAML metadata to every numbered Markdown part and retained
  `page`, `slide`, `sheet` + `rows`, or `section` anchors. `manifest.json`
  records source identity, converter version, parts, anchors, and part hashes.
- Staged complete artifacts at
  `${DATA_DIR}/tmp/<job_id>/<document_id>/`, fsynced their files and manifest,
  and published to `${DATA_DIR}/md/<document_id>/` with `os.replace` directory
  swaps. Replacement failure restores the previous successful artifact.
- Conversion verifies the inventoried source SHA-256 before extraction and
  again immediately before publishing. Conversion never writes a source file.

## HTTP and incremental contract

Both endpoints require an Admin session and return a persisted Job detail with
HTTP 202:

```text
POST /api/admin/jobs/convert-changed
POST /api/admin/files/{id}/convert
```

`convert-changed` selects only PRESENT records in `NEW` or `CHANGED` state.
`FAILED` records are selected only when retry is explicit through JSON
`{"retry": true}`, `?retry=true`, or `?retry_failed=true`. It does not select
unchanged `READY`, `QUEUED`, `CONVERTING`, `UNSUPPORTED`, or MISSING records.
An empty incremental selection creates an immediately COMPLETED zero-item Job
and submits nothing to Huey.

The one-file endpoint is itself an explicit request, so it may reconvert READY,
FAILED, or UNSUPPORTED PRESENT records. It rejects a MISSING record or a record
whose conversion is already QUEUED/CONVERTING.

When queued, selected source records become `QUEUED`; the Worker changes the
active record to `CONVERTING`, then to `READY`, `FAILED`, or `UNSUPPORTED`.
Expected per-file failures become failed Job items and later files continue.
Stale Worker recovery also returns a still-CONVERTING source record to QUEUED.

## Model-role isolation

Text-bearing files do not require a model binding. Visual extraction fails the
individual file with a clear error when `document_conversion` is unbound or
not vision-capable. It never falls back to `answer_generation` or constructs a
Provider adapter directly.

The regression test creates:

```text
document_conversion -> profile A -> conversion endpoint A
answer_generation    -> profile B -> answer endpoint B
```

Image conversion calls endpoint A exactly once and endpoint B zero times.

## Verification

- Python 3.12.13.
- Full `.venv/bin/pytest`: `58 passed`.
- New tests cover XLSX value fidelity and row splitting, source immutability,
  incremental selection, explicit FAILED retry, UNSUPPORTED handling, per-file
  failure isolation, previous-artifact restoration after injected replacement
  failure, missing conversion-model errors, and Profile A/B role isolation.
- `pip check`: no broken requirements.
- Python compilation succeeded for `app/`, `migrations/`, and `tests/`.
- The existing Starlette TestClient deprecation warning remains. MarkItDown
  also triggers pydub's optional ffmpeg warning; supported conversion formats
  do not include audio and are unaffected.

## Explicitly not implemented

- `card.json`;
- document, folder, or root indexes;
- query routing or retrieval;
- answer generation, citations, or chat behavior;
- conversion or knowledge-base UI.

## Continue only when requested

The next allowed stage is Stage 06 Hierarchical JSON Indexing, only after an
explicit request. Read `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`,
`docs/STATUS.md`, and all handoffs before continuing. Do not reinterpret the
system as vector RAG and do not make Markdown indexes canonical.
