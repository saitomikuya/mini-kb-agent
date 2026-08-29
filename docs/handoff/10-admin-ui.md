# Handoff: 10 Administration UI

## Scope completed

- Added the Jinja2 and Vanilla JavaScript management workspace at `/admin`.
  The page renders an Admin-password login gate; every data read and mutation
  remains protected by the existing signed Admin cookie and `require_admin`.
- Added the requested File, Job, Model, and Index tabs.
- File management lists source/conversion/index states and supports upload,
  atomic replace, browser-confirmed source deletion, explicit reconversion,
  generated Markdown preview, source download, source scanning, and changed-
  file conversion.
- Job management restores the persisted current job on page load and polls at
  two-second intervals only while a PENDING, QUEUED, or RUNNING job exists.
  Closing the page does not affect Worker execution.
- Model management preserves the three architectural layers: Provider,
  Provider-scoped Model Profiles, and four independently persisted Role
  Bindings. There is no operational default-model setting. The optional
  apply-all shortcut calls the four existing role-binding APIs separately.
- Role help text includes the required capability/cost guidance and illustrative
  Luna/Terra/Sol examples without creating or hard-coding defaults.
- Index management shows the current generation, document count, folder count,
  activation time, root JSON/Markdown previews, and the existing asynchronous
  rebuild action.

## Minimal backend additions

Only read capabilities required by the UI were added:

```text
GET /api/admin/files/{source_file_id}/markdown
GET /api/admin/index
GET /api/admin/index/root.json
GET /api/admin/index/root.md
```

Generated artifact reads validate containment, reject unsafe symlinked paths,
and never expose arbitrary filesystem addressing. No database migration was
needed.

## Verification

- Full Python 3.12.13 suite: `90 passed`.
- `alembic check` reports no schema drift.
- Python compilation and JavaScript syntax checks pass.
- The local `/admin` route and all new Admin read APIs return successfully with
  the expected Admin authentication behavior.

## Explicitly unchanged

- converter;
- indexer;
- query router;
- answer generator;
- model registry and role semantics;
- Docker and supervisord configuration.

No unrelated backend bug was found or fixed during this stage.
