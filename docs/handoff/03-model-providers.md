# Handoff: 03 Model Providers

## Completed

- Added Alembic revision `20260828_0002` and matching SQLAlchemy models for
  `api_providers`, `model_profiles`, and `model_role_bindings`.
- Enforced Provider type, protocol preference, Azure mode, tested protocol,
  test status, and the four exact model roles with API enums and database check
  constraints.
- Kept Azure deployment names in `model_profiles.remote_model_name`; one Azure
  resource Provider can own any number of deployment Profiles.
- Added Admin-only Provider and Model Profile CRUD APIs. Deleting a Provider
  that owns Profiles or a Profile referenced by a role returns HTTP 409.
- Added `GET /api/admin/model-roles` and
  `PUT /api/admin/model-roles/{role}`. All four roles may share a Profile or use
  unrelated Profiles.
- Added role capability validation at bind time and registry-resolution time:
  conversion requires tested text+vision, index and answer require tested text,
  and query routing requires tested text plus the successful JSON probe.
- Added Fernet encryption using a purpose-specific key derived from the stable
  `app.secret`. Provider responses expose `api_key_masked` only. Plaintext and
  ciphertext are absent from response schemas; credential validation errors and
  remote provider errors are sanitized.
- Added the `ModelClient` protocol with `generate_text`, `generate_json`, and
  `generate_multimodal`, plus `OpenAICompatibleClient`, `AzureOpenAIClient`, and
  `Sub2APIClient`.
- Added real protocol behavior: OpenAI-compatible `auto` POSTs to Responses
  first and POSTs to Chat Completions only after failure. No model-list probe is
  used. Sub2API keeps a distinct type while reusing the compatible core.
- Added Azure v1 endpoint normalization to `/openai/v1/` and sends each
  Profile's deployment name as `model`. Azure legacy has a separate
  deployment-path/API-version branch.
- Added native Structured Output requests with strict JSON prompt and parsing
  fallback when the native feature is unavailable.
- Added `POST /api/admin/model-profiles/{id}/test`. It sequentially tests plain
  text, exact `{"ok": true}` JSON, and the built-in `KB-VISION-42` PNG. It
  persists text/vision/Structured Output capabilities, tested protocol, total
  latency, status, and timestamp.
- Added `ModelRegistry.get_for_role(ModelRole.<ROLE>)`. An unbound role fails
  only when a later model-dependent operation requests it; application startup
  does not require model configuration.

## HTTP contract

All endpoints require an Admin authentication session:

```text
GET    /api/admin/providers
POST   /api/admin/providers
GET    /api/admin/providers/{id}
PUT    /api/admin/providers/{id}
DELETE /api/admin/providers/{id}

GET    /api/admin/model-profiles?provider_id={provider_id}
POST   /api/admin/model-profiles
GET    /api/admin/model-profiles/{id}
PUT    /api/admin/model-profiles/{id}
DELETE /api/admin/model-profiles/{id}
POST   /api/admin/model-profiles/{id}/test

GET    /api/admin/model-roles
PUT    /api/admin/model-roles/{role}
```

Provider create/update accepts `api_key`; reads return only
`api_key_masked` such as `sk-****abcd`. Omitting `api_key` during update keeps
the existing encrypted credential. Model capability/test fields are read-only
and are populated by the Profile connection test.

`GET /api/admin/model-roles` always returns the four fixed roles, using a null
`model_profile_id` for an unbound role. Sending null to a role `PUT` removes its
binding.

## Non-negotiable lookup rule

```text
Provider != Model
Model != Role
```

Provider stores endpoint/authentication behavior. Model Profile stores one
remote model or Azure deployment and its tested capabilities. Model Role
Binding selects one Profile for one workload.

Future business code is prohibited from calling a model by `provider_id` or
constructing a provider adapter directly. It must use:

```python
model_registry.get_for_role(ModelRole.QUERY_ROUTER)
```

There is no global `DEFAULT_MODEL`. Direct adapter construction is limited to
the Admin Profile connection test and `ModelRegistry` itself.

## Test status semantics

- `passed`: text, required JSON, and vision probes all passed;
- `partial`: text and required JSON passed, but vision did not;
- `failed`: text or required JSON did not pass.

`supports_structured_output` records a successful native Structured Output
probe. A JSON fallback success leaves this flag false while still making the
Profile eligible for `query_router`. Vision failure leaves `supports_text`
intact, so the Profile remains eligible for text-only roles.

Changing Provider connection fields or Profile request/model fields clears old
test results. Disabling a bound Provider/Profile or invalidating its capability
causes registry resolution to fail rather than silently using stale settings.

## Verification

- Python 3.12.13.
- Full `.venv/bin/pytest`: `29 passed`.
- HTTP is mocked for every adapter and model-test request.
- `alembic upgrade head`: succeeded through revisions 0001 and 0002.
- `alembic check`: `No new upgrade operations detected.`
- `alembic downgrade base`: succeeded through both revisions.
- `compileall`: succeeded for `app/`, `migrations/`, and `tests/`.
- `pip check`: `No broken requirements found.`

The only known test warning remains FastAPI/Starlette's upstream `TestClient`
compatibility deprecation under the installed dependency versions.

## Deliberately not implemented

File scanning, file upload, source management, Huey or a Worker, MarkItDown,
conversion, incremental processing, hierarchical indexes, retrieval, chat/SSE,
citations, HTML pages, or any UI.

## Read before continuing

1. `docs/ARCHITECTURE.md`
2. `docs/DECISIONS.md`
3. `docs/STATUS.md`
4. This handoff

Do not collapse Provider, Model, and Role. Do not bypass the model role
registry. Do not write to source files and do not reinterpret the system as
vector RAG.

## Next allowed stage

Stage 04: Source File Management, only after the user explicitly requests it.
Do not begin conversion, indexing, retrieval, chat, or UI while implementing
source management.
