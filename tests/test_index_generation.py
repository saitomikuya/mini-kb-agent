"""Hierarchical canonical JSON indexing and atomic activation tests."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base, build_engine, build_session_factory
from app.indexes import IndexGenerationStatus
from app.llm.types import ModelRole
from app.models.index_generation import IndexGeneration
from app.models.model_config import APIProvider, ModelProfile, ModelRoleBinding
from app.models.source_file import SourceFile
from app.llm.registry import ModelRegistry
from app.services.index_generation import IndexGenerationError, IndexGenerationService
from app.services.lexical_index import LEXICAL_INDEX_FILENAME, search_lexical_index
from app.services.secrets import APIKeyCipher
from app.source_files import ConversionStatus, IndexStatus, SourceStatus
import app.models  # noqa: F401  # Register all metadata.


class RecordingIndexClient:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate_json(self, prompt: str, **_kwargs):
        self.prompts.append(prompt)
        source_line = next(
            line for line in prompt.splitlines() if line.startswith("source_path: ")
        )
        source_path = json.loads(source_line.removeprefix("source_path: "))
        part_ids = [
            json.loads(value)
            for value in re.findall(r"<markdown-part id=(.+)>", prompt)
        ]
        return SimpleNamespace(
            value={
                "title": Path(source_path).stem,
                "document_type": Path(source_path).suffix.lstrip(".") or "document",
                "summary": f"Summary for {source_path}",
                "topics": ["knowledge", Path(source_path).parent.as_posix()],
                "entities": [Path(source_path).stem],
                "parts": [
                    {
                        "part_id": part_id,
                        "label": f"Part {number}",
                        "summary": f"Summary of {part_id}",
                    }
                    for number, part_id in enumerate(part_ids, start=1)
                ],
            }
        )

    async def generate_text(self, *_args, **_kwargs):  # pragma: no cover
        pytest.fail("index generation must use generate_json")

    async def generate_multimodal(self, *_args, **_kwargs):  # pragma: no cover
        pytest.fail("index generation must not use a vision/conversion call")


@dataclass
class IndexInfra:
    settings: Settings
    session: Session
    client: RecordingIndexClient
    roles: list[ModelRole]

    def build(self, *, activation_hook=None):
        def resolver(role: ModelRole):
            self.roles.append(role)
            assert role is ModelRole.INDEX_GENERATION
            return self.client

        return IndexGenerationService(
            self.settings,
            self.session,
            model_resolver=resolver,
            activation_hook=activation_hook,
        ).build_and_activate()


@pytest.fixture
def index_infra(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        source_dir=tmp_path / "sources",
    )
    settings.data_dir.mkdir()
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    session = build_session_factory(engine)()
    infra = IndexInfra(
        settings=settings,
        session=session,
        client=RecordingIndexClient(),
        roles=[],
    )
    try:
        yield infra
    finally:
        session.close()
        engine.dispose()


def _add_ready_document(
    infra: IndexInfra,
    relative_path: str,
    body: str,
) -> SourceFile:
    source_hash = hashlib.sha256(body.encode()).hexdigest()
    record = SourceFile(
        relative_path=relative_path,
        filename=Path(relative_path).name,
        extension=Path(relative_path).suffix,
        size=len(body.encode()),
        mtime_ns=1,
        sha256=source_hash,
        source_status=SourceStatus.PRESENT,
        conversion_status=ConversionStatus.READY,
        index_status=IndexStatus.NOT_INDEXED,
    )
    infra.session.add(record)
    infra.session.commit()
    _write_artifact(infra.settings, record, body)
    return record


def _write_artifact(settings: Settings, record: SourceFile, body: str) -> None:
    _write_artifact_parts(settings, record, [body])


def _write_artifact_parts(
    settings: Settings,
    record: SourceFile,
    bodies: list[str],
) -> None:
    artifact_dir = settings.markdown_dir / str(record.id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    manifest_parts = []
    for number, body in enumerate(bodies, start=1):
        part_id = f"part-{number:03d}"
        filename = f"{part_id}.md"
        part = (
            "---\n"
            f"document_id: {record.id}\n"
            f"part_id: {part_id}\n"
            "---\n\n"
            f"{body}\n"
        )
        part_bytes = part.encode()
        (artifact_dir / filename).write_bytes(part_bytes)
        manifest_parts.append(
            {
                "part_id": part_id,
                "path": filename,
                "anchors": {"section": f"section-{number}"},
                "sha256": hashlib.sha256(part_bytes).hexdigest(),
            }
        )
    manifest = {
        "document_id": record.id,
        "source_path": record.relative_path,
        "source_sha256": record.sha256,
        "converted_at": "2026-08-28T00:00:00+00:00",
        "converter_version": "test",
        "status": "READY",
        "parts": manifest_parts,
    }
    (artifact_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )


def _current_root(infra: IndexInfra) -> dict:
    pointer = json.loads(
        (infra.settings.index_dir / "current.json").read_text(encoding="utf-8")
    )
    return json.loads(
        (infra.settings.index_dir / pointer["root_index_path"]).read_text(
            encoding="utf-8"
        )
    )


def _indexed_document_ids(infra: IndexInfra) -> set[int]:
    root = _current_root(infra)
    ids: set[int] = set()
    pointer = json.loads((infra.settings.index_dir / "current.json").read_text())
    generation_dir = (
        infra.settings.index_dir / pointer["root_index_path"]
    ).parent
    for entry in root["folders"]:
        folder = json.loads((generation_dir / entry["index_path"]).read_text())
        ids.update(int(document["document_id"]) for document in folder["documents"])
    return ids


def test_first_build_creates_cards_compact_indexes_and_current_pointer(
    index_infra: IndexInfra,
) -> None:
    first = _add_ready_document(index_infra, "sales/guide.md", "SECRET FULL BODY A")
    second = _add_ready_document(index_infra, "training/course.txt", "FULL BODY B")

    result = index_infra.build()

    assert result.generation_number == 1
    assert result.document_count == 2
    assert index_infra.roles == [ModelRole.INDEX_GENERATION]
    assert len(index_infra.client.prompts) == 2
    pointer = json.loads((index_infra.settings.index_dir / "current.json").read_text())
    assert pointer["generation_number"] == 1
    root_path = index_infra.settings.index_dir / pointer["root_index_path"]
    root = json.loads(root_path.read_text())
    assert set(root) == {"folders"}
    assert all(
        set(entry)
        == {
            "folder_id",
            "source_directory",
            "summary",
            "document_count",
            "document_types",
            "topics",
            "entities",
            "representative_titles",
            "index_path",
        }
        for entry in root["folders"]
    )
    assert all(entry["document_types"] for entry in root["folders"])
    assert all(entry["topics"] for entry in root["folders"])
    assert all(entry["representative_titles"] for entry in root["folders"])
    assert _indexed_document_ids(index_infra) == {first.id, second.id}
    folder_json = "\n".join(
        (root_path.parent / entry["index_path"]).read_text()
        for entry in root["folders"]
    )
    assert "SECRET FULL BODY A" not in folder_json
    lexical_path = root_path.parent / LEXICAL_INDEX_FILENAME
    assert lexical_path.is_file()
    lexical_matches = search_lexical_index(
        lexical_path,
        "SECRET FULL BODY A",
        limit=5,
    )
    assert lexical_matches
    assert lexical_matches[0].document_id == str(first.id)
    assert lexical_matches[0].part_id == "part-001"
    card = json.loads(
        (index_infra.settings.markdown_dir / str(first.id) / "card.json").read_text()
    )
    assert set(card) == {
        "document_id",
        "source_path",
        "title",
        "document_type",
        "summary",
        "topics",
        "entities",
        "updated_at",
        "parts",
    }
    assert card["parts"][0]["md_path"] == f"md/{first.id}/part-001.md"
    assert card["parts"][0]["source_anchors"] == [{"section": "section-1"}]
    assert first.index_status == IndexStatus.INDEXED
    assert second.index_status == IndexStatus.INDEXED


def test_card_schema_requires_manifest_part_count_and_retries_invalid_response(
    index_infra: IndexInfra,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _add_ready_document(
        index_infra,
        "contracts/two-parts.docx",
        "first section\nsecond section",
    )
    _write_artifact_parts(
        index_infra.settings,
        document,
        ["first section", "second section"],
    )

    class InitiallyIncompleteClient:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def generate_json(self, _prompt: str, **kwargs):
            self.calls.append(kwargs)
            part_count = 1 if len(self.calls) == 1 else 2
            return SimpleNamespace(
                value={
                    "title": "Two-part contract",
                    "document_type": "docx",
                    "summary": "Two bounded sections.",
                    "topics": ["contract"],
                    "entities": [],
                    "parts": [
                        {
                            "part_id": f"part-{number:03d}",
                            "label": f"Section {number}",
                            "summary": f"Summary {number}",
                        }
                        for number in range(1, part_count + 1)
                    ],
                }
            )

    async def no_delay(_seconds: float) -> None:
        return None

    monkeypatch.setattr("app.services.index_generation.asyncio.sleep", no_delay)
    client = InitiallyIncompleteClient()
    result = IndexGenerationService(
        index_infra.settings,
        index_infra.session,
        model_resolver=lambda _role: client,
    ).build_and_activate()

    assert result.document_count == 1
    assert len(client.calls) == 2
    schema = client.calls[0]["json_schema"]
    assert schema["properties"]["parts"]["minItems"] == 2
    assert schema["properties"]["parts"]["maxItems"] == 2
    assert schema["properties"]["parts"]["items"]["properties"]["part_id"][
        "enum"
    ] == ["part-001", "part-002"]
    card = json.loads(
        (index_infra.settings.markdown_dir / str(document.id) / "card.json").read_text()
    )
    assert [part["part_id"] for part in card["parts"]] == [
        "part-001",
        "part-002",
    ]


def test_adding_one_file_calls_model_only_for_new_card(index_infra: IndexInfra) -> None:
    existing = _add_ready_document(index_infra, "same/existing.md", "existing")
    index_infra.build()
    existing_card = (
        index_infra.settings.markdown_dir / str(existing.id) / "card.json"
    ).read_bytes()
    initial_calls = len(index_infra.client.prompts)
    added = _add_ready_document(index_infra, "same/added.md", "added")

    result = index_infra.build()

    assert result.generation_number == 2
    assert len(index_infra.client.prompts) == initial_calls + 1
    assert (
        index_infra.settings.markdown_dir / str(existing.id) / "card.json"
    ).read_bytes() == existing_card
    assert _indexed_document_ids(index_infra) == {existing.id, added.id}


def test_modifying_one_file_regenerates_only_its_card_and_folder(
    index_infra: IndexInfra,
) -> None:
    changed = _add_ready_document(index_infra, "alpha/a.md", "version one")
    untouched = _add_ready_document(index_infra, "beta/b.md", "untouched")
    index_infra.build()
    untouched_card = (
        index_infra.settings.markdown_dir / str(untouched.id) / "card.json"
    ).read_bytes()
    old_pointer = json.loads((index_infra.settings.index_dir / "current.json").read_text())
    old_beta = next(
        entry
        for entry in _current_root(index_infra)["folders"]
        if entry["source_directory"] == "beta"
    )
    old_beta_bytes = (
        (index_infra.settings.index_dir / old_pointer["root_index_path"]).parent
        / old_beta["index_path"]
    ).read_bytes()
    initial_calls = len(index_infra.client.prompts)

    changed.sha256 = hashlib.sha256(b"version two").hexdigest()
    changed.size = len(b"version two")
    changed.mtime_ns = 2
    changed.conversion_status = ConversionStatus.READY
    changed.index_status = IndexStatus.STALE
    index_infra.session.commit()
    _write_artifact(index_infra.settings, changed, "version two")

    index_infra.build()

    assert len(index_infra.client.prompts) == initial_calls + 1
    assert (
        index_infra.settings.markdown_dir / str(untouched.id) / "card.json"
    ).read_bytes() == untouched_card
    new_pointer = json.loads((index_infra.settings.index_dir / "current.json").read_text())
    new_beta = next(
        entry
        for entry in _current_root(index_infra)["folders"]
        if entry["source_directory"] == "beta"
    )
    new_beta_bytes = (
        (index_infra.settings.index_dir / new_pointer["root_index_path"]).parent
        / new_beta["index_path"]
    ).read_bytes()
    assert new_beta_bytes == old_beta_bytes
    assert changed.index_status == IndexStatus.INDEXED


def test_deleted_failed_and_unconverted_stale_files_are_removed(
    index_infra: IndexInfra,
) -> None:
    deleted = _add_ready_document(index_infra, "docs/deleted.md", "deleted")
    failed = _add_ready_document(index_infra, "docs/failed.md", "failed")
    stale = _add_ready_document(index_infra, "docs/stale.md", "stale")
    kept = _add_ready_document(index_infra, "docs/kept.md", "kept")
    index_infra.build()
    initial_calls = len(index_infra.client.prompts)

    deleted.source_status = SourceStatus.MISSING
    deleted.index_status = IndexStatus.STALE
    failed.conversion_status = ConversionStatus.FAILED
    stale.conversion_status = ConversionStatus.CHANGED
    stale.index_status = IndexStatus.STALE
    index_infra.session.commit()

    index_infra.build()

    assert len(index_infra.client.prompts) == initial_calls
    assert _indexed_document_ids(index_infra) == {kept.id}
    assert failed.index_status == IndexStatus.STALE


def test_failure_before_activation_keeps_previous_generation_current(
    index_infra: IndexInfra,
) -> None:
    _add_ready_document(index_infra, "docs/original.md", "original")
    index_infra.build()
    old_pointer = (index_infra.settings.index_dir / "current.json").read_bytes()
    _add_ready_document(index_infra, "docs/new.md", "new")

    def fail_before_activation(_generation_number: int) -> None:
        raise RuntimeError("injected build failure")

    with pytest.raises(IndexGenerationError, match="before activation"):
        index_infra.build(activation_hook=fail_before_activation)

    assert (index_infra.settings.index_dir / "current.json").read_bytes() == old_pointer
    generations = index_infra.session.scalars(
        select(IndexGeneration).order_by(IndexGeneration.generation_number)
    ).all()
    assert [generation.status for generation in generations] == [
        IndexGenerationStatus.ACTIVE,
        IndexGenerationStatus.FAILED,
    ]
    assert _indexed_document_ids(index_infra) == {1}


def test_model_failure_mid_build_does_not_change_current_generation(
    index_infra: IndexInfra,
) -> None:
    original = _add_ready_document(index_infra, "docs/original.md", "original")
    index_infra.build()
    old_pointer = (index_infra.settings.index_dir / "current.json").read_bytes()
    _add_ready_document(index_infra, "new/first.md", "first")
    _add_ready_document(index_infra, "new/second.md", "second")
    real_generate = index_infra.client.generate_json
    async def fail_second_new_card(prompt: str, **kwargs):
        if '"new/second.md"' in prompt:
            raise RuntimeError("injected model interruption")
        return await real_generate(prompt, **kwargs)

    index_infra.client.generate_json = fail_second_new_card  # type: ignore[method-assign]

    with pytest.raises(IndexGenerationError, match="model failed"):
        index_infra.build()

    assert (index_infra.settings.index_dir / "current.json").read_bytes() == old_pointer
    assert _indexed_document_ids(index_infra) == {original.id}
    failed = index_infra.session.scalar(
        select(IndexGeneration).where(
            IndexGeneration.generation_number == 2,
            IndexGeneration.status == IndexGenerationStatus.FAILED,
        )
    )
    assert failed is not None


def test_large_document_uses_bounded_hierarchical_batches_and_resume_cache(
    index_infra: IndexInfra,
) -> None:
    document = _add_ready_document(
        index_infra,
        "large/manual.md",
        "bounded evidence " * 5_000,
    )

    class HierarchicalClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []
            self.fail_metadata = True

        async def generate_json(self, prompt: str, **kwargs):
            self.calls.append((prompt, kwargs))
            if prompt.startswith("Summarize this bounded batch"):
                part_ids = [
                    json.loads(value)
                    for value in re.findall(r"<markdown-part id=(.+)>", prompt)
                ]
                return SimpleNamespace(
                    value={
                        "batch_summary": "Bounded batch summary",
                        "parts": [
                            {
                                "part_id": part_id,
                                "label": f"Part {number}",
                                "summary": "Concise part evidence " + ("x" * 700),
                            }
                            for number, part_id in enumerate(part_ids, start=1)
                        ],
                    }
                )
            if self.fail_metadata:
                raise RuntimeError("persistent metadata failure")
            return SimpleNamespace(
                value={
                    "title": "Large manual",
                    "document_type": "markdown",
                    "summary": "Document-level summary",
                    "topics": ["manual"],
                    "entities": ["Large manual"],
                }
            )

    client = HierarchicalClient()
    progress_updates: list[dict[str, object]] = []

    def build():
        return IndexGenerationService(
            index_infra.settings,
            index_infra.session,
            model_resolver=lambda role: (
                client
                if role is ModelRole.INDEX_GENERATION
                else pytest.fail(f"unexpected role: {role}")
            ),
        ).build_and_activate(
            progress=lambda progress: progress_updates.append(dict(progress))
        )

    with pytest.raises(IndexGenerationError, match="model failed"):
        build()
    assert any(
        prompt.startswith("Summarize this bounded batch")
        for prompt, _kwargs in client.calls
    )
    batch_calls = [
        kwargs
        for prompt, kwargs in client.calls
        if prompt.startswith("Summarize this bounded batch")
    ]
    assert all(kwargs["reasoning_effort"] == "low" for kwargs in batch_calls)
    assert all(kwargs["max_output_tokens"] <= 8_192 for kwargs in batch_calls)
    assert list(
        (index_infra.settings.markdown_dir / str(document.id) / ".index-cache").glob(
            "*.json"
        )
    )
    cached_batch = json.loads(
        next(
            (index_infra.settings.markdown_dir / str(document.id) / ".index-cache").glob(
                "*.json"
            )
        ).read_text()
    )
    assert len(cached_batch["parts"][0]["summary"]) == 600

    client.fail_metadata = False
    client.calls.clear()
    result = build()

    assert result.document_count == 1
    assert not any(
        prompt.startswith("Summarize this bounded batch")
        for prompt, _kwargs in client.calls
    )
    assert len(client.calls) == 1
    assert progress_updates[-1]["phase"] == "completed"
    assert int(progress_updates[-1]["model_cache_hits"]) >= 1


@respx.mock
def test_only_index_generation_role_provider_is_called(index_infra: IndexInfra) -> None:
    document = _add_ready_document(index_infra, "roles/card.md", "role isolation")
    cipher = APIKeyCipher(index_infra.settings.secret_path)
    routes = {}
    for role in ModelRole:
        provider = APIProvider(
            name=f"{role.value} provider",
            provider_type="openai_compatible",
            base_url=f"https://{role.value}.example.test/v1",
            encrypted_api_key=cipher.encrypt(f"sk-{role.value}"),
            protocol_preference="responses",
            extra_headers_json={},
            azure_mode="v1",
            enabled=True,
        )
        profile = ModelProfile(
            provider=provider,
            name=f"{role.value} profile",
            remote_model_name=f"{role.value}-model",
            supports_text=True,
            supports_vision=role is ModelRole.DOCUMENT_CONVERSION,
            supports_structured_output=True,
            tested_protocol="responses",
            last_test_status="passed",
            enabled=True,
            extra_request_json={},
        )
        index_infra.session.add(profile)
        index_infra.session.flush()
        index_infra.session.add(
            ModelRoleBinding(role=role.value, model_profile_id=profile.id)
        )
        routes[role] = respx.post(
            f"https://{role.value}.example.test/v1/responses"
        ).mock(return_value=httpx.Response(500))
    index_infra.session.commit()

    response = {
        "title": "card",
        "document_type": "markdown",
        "summary": "A role-isolation document.",
        "topics": ["roles"],
        "entities": [],
        "parts": [
            {"part_id": "part-001", "label": "Document", "summary": "Content."}
        ],
    }
    routes[ModelRole.INDEX_GENERATION].mock(
        return_value=httpx.Response(200, json={"output_text": json.dumps(response)})
    )
    registry = ModelRegistry(index_infra.session, cipher)
    requested_roles: list[ModelRole] = []

    def resolve(role: ModelRole):
        requested_roles.append(role)
        return registry.get_for_role(role)

    IndexGenerationService(
        index_infra.settings,
        index_infra.session,
        model_resolver=resolve,
    ).build_and_activate()

    assert requested_roles == [ModelRole.INDEX_GENERATION]
    assert routes[ModelRole.INDEX_GENERATION].call_count == 1
    assert all(
        route.call_count == 0
        for role, route in routes.items()
        if role is not ModelRole.INDEX_GENERATION
    )
    assert document.index_status == IndexStatus.INDEXED
