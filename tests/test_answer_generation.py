"""Grounded answer generation, validation, and role-isolation tests."""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from app.config import Settings
from app.db import Base, build_engine, build_session_factory
from app.llm.registry import ModelRegistry
from app.llm.types import ModelRole
from app.models.model_config import APIProvider, ModelProfile, ModelRoleBinding
from app.models.source_file import SourceFile
from app.schemas.navigation import (
    NavigatedDocument,
    NavigatedFolder,
    NavigatedPart,
    NavigationIntent,
    NavigationResult,
    NavigationTokenBudget,
)
from app.schemas.answers import AnswerResult
from app.services.answer_generation import (
    ANSWER_SYSTEM_PROMPT,
    AnswerGenerationService,
    AnswerModelOutputError,
    QuestionAnsweringService,
    canonical_anchor,
)
from app.services.secrets import APIKeyCipher
import app.models  # noqa: F401  # Register all SQLAlchemy metadata.


class RecordingAnswerClient:
    context_window = 32_768
    max_output_tokens = 1_024

    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def generate_json(self, prompt: str, **kwargs: Any):
        self.calls.append((prompt, kwargs))
        return SimpleNamespace(value=self.value)

    async def generate_text(self, *_args, **_kwargs):  # pragma: no cover
        pytest.fail("answer generation must use generate_json")

    async def generate_multimodal(self, *_args, **_kwargs):  # pragma: no cover
        pytest.fail("answer generation must not use multimodal generation")


class StreamingAnswerClient(RecordingAnswerClient):
    async def generate_json_stream(
        self,
        prompt: str,
        *,
        on_progress: Any,
        **kwargs: Any,
    ):
        self.calls.append((prompt, kwargs))
        raw_json = json.dumps(self.value, ensure_ascii=True)
        for character in raw_json:
            await on_progress("output_delta", {"delta": character})
        return SimpleNamespace(value=self.value)


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(
        data_dir=tmp_path / "data",
        source_dir=tmp_path / "sources",
    )
    settings.data_dir.mkdir()
    settings.source_dir.mkdir()
    return settings


def _source_record(
    *,
    relative_path: str,
    mtime_ns: int,
) -> SourceFile:
    filename = Path(relative_path).name
    return SourceFile(
        relative_path=relative_path,
        filename=filename,
        extension=Path(filename).suffix.lower(),
        size=10,
        mtime_ns=mtime_ns,
        sha256=hashlib.sha256(relative_path.encode()).hexdigest(),
        source_status="PRESENT",
        conversion_status="READY",
        index_status="INDEXED",
    )


def _navigation(document_ids: list[str]) -> NavigationResult:
    documents = [
        NavigatedDocument(
            folder_id="docs",
            document_id=document_id,
            source_path=f"资料/文档-{document_id}.md",
            title=f"文档 {document_id}",
            document_type="markdown",
            selected_part_ids=["part-001"],
            display_reason="Selected by router.",
        )
        for document_id in document_ids
    ]
    parts = [
        NavigatedPart(
            folder_id="docs",
            document_id=document_id,
            part_id="part-001",
            label=f"Evidence {document_id}",
            summary="Router-only summary.",
            md_path=f"md/{document_id}/part-001.md",
            source_anchors=[{"page": int(document_id)}],
            content=f"# Evidence {document_id}\n\nThe stated value is {document_id}0.",
            estimated_tokens=20,
            within_evidence_budget=True,
        )
        for document_id in document_ids
    ]
    return NavigationResult(
        intent=NavigationIntent.ANSWER,
        folders=[
            NavigatedFolder(
                folder_id="docs",
                source_directory="资料",
                summary="Router folder summary.",
                display_reason="Selected by router.",
            )
        ],
        documents=documents,
        parts=parts,
        display_steps=["Private navigation details must not reach answer model."],
        confidence=0.9,
        token_budget=NavigationTokenBudget(
            context_window=32_768,
            root_budget=4_000,
            folder_budget=8_000,
            evidence_budget=18_720,
            output_reserve=2_048,
            answer_output_reserve=768,
        ),
    )


def test_answer_input_is_isolated_and_output_is_deterministically_validated(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    session = build_session_factory(engine)()
    first = _source_record(
        relative_path="中文目录/报告一.md",
        mtime_ns=1_700_000_000_000_000_000,
    )
    second = _source_record(
        relative_path="报告二.md",
        mtime_ns=1_800_000_000_000_000_000,
    )
    session.add_all([first, second])
    session.commit()
    assert [first.id, second.id] == [1, 2]

    page_one = canonical_anchor({"page": 1})
    page_two = canonical_anchor({"page": 2})
    client = RecordingAnswerClient(
        {
            "answer_markdown": "证据给出 10 和 20，二者冲突。",
            "citations": [
                {
                    "document_id": "1",
                    "part_id": "part-001",
                    "anchor": page_one,
                    "label": "Evidence 1",
                },
                {
                    "document_id": "999",
                    "part_id": "part-001",
                    "anchor": page_one,
                    "label": "Evidence 1",
                },
                {
                    "document_id": "2",
                    "part_id": "part-001",
                    "anchor": '{"page":999}',
                    "label": "Evidence 2",
                },
            ],
            "conflicts": [
                {
                    "subject": "stated value",
                    "values": [
                        {"value": "10", "document_id": "1", "anchor": page_one},
                        {"value": "20", "document_id": "2", "anchor": page_two},
                        {"value": "21", "document_id": "2", "anchor": page_two},
                    ],
                    "analysis": "The selected sources disagree; no value is chosen.",
                },
                {
                    "subject": "invented conflict",
                    "values": [
                        {"value": "x", "document_id": "999", "anchor": page_one},
                        {"value": "y", "document_id": "2", "anchor": page_two},
                    ],
                    "analysis": "Contains an invented source.",
                },
            ],
            "downloads": [
                {"document_id": "1"},
                {"document_id": "999"},
                {"document_id": "1"},
            ],
            "research_handoff": None,
        }
    )
    roles: list[ModelRole] = []

    def resolve(role: ModelRole):
        roles.append(role)
        assert role is ModelRole.ANSWER_GENERATION
        return client

    try:
        result = asyncio.run(
            AnswerGenerationService(session, model_resolver=resolve).generate(
                "证据中的数值是什么？",
                _navigation(["1", "2"]),
            )
        )
    finally:
        session.close()
        engine.dispose()

    assert roles == [ModelRole.ANSWER_GENERATION]
    assert [citation.document_id for citation in result.citations] == ["1"]
    assert len(result.conflicts) == 1
    assert [value.value for value in result.conflicts[0].values] == ["10", "20", "21"]
    assert [download.model_dump() for download in result.downloads] == [
        {
            "document_id": "1",
            "filename": "报告一.md",
            "relative_directory": "中文目录",
        }
    ]

    prompt, kwargs = client.calls[0]
    payload = json.loads(prompt)
    assert set(payload) == {
        "user_question",
        "selected_markdown_parts",
        "source_metadata",
    }
    assert kwargs["system_prompt"] == ANSWER_SYSTEM_PROMPT
    assert "only from the supplied" in kwargs["system_prompt"]
    assert "do not average" in kwargs["system_prompt"]
    assert "only an inference" in kwargs["system_prompt"]
    assert "filesystem path" in kwargs["system_prompt"]
    assert "plain-language Chinese" in kwargs["system_prompt"]
    assert "research_handoff" in kwargs["system_prompt"]
    assert "ChatGPT or Doubao" in kwargs["system_prompt"]
    assert "Never claim that this application searched the web" in kwargs[
        "system_prompt"
    ]
    assert kwargs["max_output_tokens"] == 768
    assert kwargs["verbosity"] == "medium"
    assert payload["selected_markdown_parts"][0]["markdown"].startswith("# Evidence")
    assert payload["source_metadata"][0]["parts"][0]["anchors"] == [page_one]
    assert "source_modified_at" in payload["source_metadata"][0]
    assert "source_path" not in prompt
    assert "md_path" not in prompt
    assert "display_reason" not in prompt
    assert "Router-only" not in prompt
    download_ref = kwargs["json_schema"]["properties"]["downloads"]["items"][
        "$ref"
    ]
    download_schema = kwargs["json_schema"]["$defs"][download_ref.rsplit("/", 1)[1]]
    assert set(download_schema["properties"]) == {"document_id"}


def test_public_answer_schema_has_the_required_exact_shape() -> None:
    schema = AnswerResult.model_json_schema()

    assert set(schema["properties"]) == {
        "answer_markdown",
        "citations",
        "conflicts",
        "downloads",
        "research_handoff",
    }
    assert set(schema["$defs"]["Citation"]["properties"]) == {
        "document_id",
        "part_id",
        "anchor",
        "label",
    }
    assert set(schema["$defs"]["Conflict"]["properties"]) == {
        "subject",
        "values",
        "analysis",
    }
    assert set(schema["$defs"]["ConflictValue"]["properties"]) == {
        "value",
        "document_id",
        "anchor",
    }
    assert set(schema["$defs"]["Download"]["properties"]) == {
        "document_id",
        "filename",
        "relative_directory",
    }
    assert set(schema["$defs"]["ResearchHandoff"]["properties"]) == {
        "reason",
        "known_information",
        "missing_information",
        "prompt",
    }


def test_streaming_answer_publishes_only_decoded_markdown_deltas(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    session = build_session_factory(engine)()
    expected_markdown = '第一行\n引用“原文”与表情 😀'
    client = StreamingAnswerClient(
        {
            "answer_markdown": expected_markdown,
            "citations": [],
            "conflicts": [],
            "downloads": [],
            "research_handoff": None,
        }
    )
    progress: list[tuple[str, dict[str, Any]]] = []

    async def on_progress(progress_type: str, data: dict[str, Any]) -> None:
        progress.append((progress_type, data))

    try:
        result = asyncio.run(
            AnswerGenerationService(
                session,
                model_resolver=lambda _role: client,
                settings=settings,
            ).generate_with_progress(
                "请整理证据",
                _navigation([]),
                on_progress=on_progress,
            )
        )
    finally:
        session.close()
        engine.dispose()

    answer_deltas = [
        data["delta"]
        for progress_type, data in progress
        if progress_type == "answer_text_delta"
    ]
    assert "".join(answer_deltas) == expected_markdown
    assert result.answer_markdown == expected_markdown
    assert all(progress_type != "output_delta" for progress_type, _data in progress)
    assert '"citations"' not in "".join(answer_deltas)


def test_answer_prompt_includes_bounded_follow_up_history(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    session = build_session_factory(engine)()
    client = RecordingAnswerClient(
        {
            "answer_markdown": "来源见已选证据。",
            "citations": [],
            "conflicts": [],
            "downloads": [],
            "research_handoff": None,
        }
    )
    history = [
        {"role": "user", "content": "产品A的规格是什么？"},
        {"role": "assistant", "content": "规格值为 10。"},
    ]
    try:
        asyncio.run(
            AnswerGenerationService(
                session,
                model_resolver=lambda _role: client,
                settings=settings,
            ).generate(
                "那它的来源呢？",
                _navigation([]),
                conversation_history=history,
            )
        )
    finally:
        session.close()
        engine.dispose()

    payload = json.loads(client.calls[0][0])
    assert payload["user_question"] == "那它的来源呢？"
    assert payload["conversation_history"] == history


def test_external_research_handoff_is_preserved_for_user_review(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    session = build_session_factory(engine)()
    source = _source_record(
        relative_path="设备/服务器配置.md",
        mtime_ns=1_800_000_000_000_000_000,
    )
    session.add(source)
    session.commit()
    client = RecordingAnswerClient(
        {
            "answer_markdown": (
                "知识库能够确认项目使用 8 张示例 GPU，但没有提供单卡算力，"
                "因此无法计算项目总算力。"
            ),
            "citations": [],
            "conflicts": [],
            "downloads": [],
            "research_handoff": {
                "reason": "需要查询该型号在指定精度下的官方单卡理论算力。",
                "known_information": ["GPU 型号：示例 GPU", "GPU 数量：8 张"],
                "missing_information": [
                    "算力口径（例如 FP32、FP16、BF16 或 INT8）",
                    "该口径下的官方单卡理论算力",
                ],
                "prompt": (
                    "请联网查询示例 GPU 的官方算力规格，并在确认精度口径后，"
                    "计算 8 张卡的理论总算力。请提供官方来源链接和规格发布日期，"
                    "展示公式与假设；无法核实时请明确说明。"
                ),
            },
        }
    )

    try:
        result = asyncio.run(
            AnswerGenerationService(
                session,
                model_resolver=lambda _role: client,
            ).generate("这个项目有多少算力？", _navigation([str(source.id)]))
        )
    finally:
        session.close()
        engine.dispose()

    assert result.research_handoff is not None
    assert result.research_handoff.known_information == [
        "GPU 型号：示例 GPU",
        "GPU 数量：8 张",
    ]
    assert "官方来源链接" in result.research_handoff.prompt


def test_invalid_answer_schema_stops_generation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    session = build_session_factory(engine)()
    client = RecordingAnswerClient(
        {
            "answer_markdown": "Not found.",
            "citations": "not-an-array",
            "conflicts": [],
            "downloads": [],
            "research_handoff": None,
        }
    )

    try:
        with pytest.raises(AnswerModelOutputError, match="invalid structured"):
            asyncio.run(
                AnswerGenerationService(
                    session,
                    model_resolver=lambda _role: client,
                ).generate("Question", _navigation([]))
            )
    finally:
        session.close()
        engine.dispose()


def _write_index(settings: Settings, document_id: str) -> None:
    artifact_dir = settings.markdown_dir / document_id
    artifact_dir.mkdir(parents=True)
    part_path = artifact_dir / "part-001.md"
    part_path.write_text("# Policy\n\nThe policy value is 42.", encoding="utf-8")
    card = {
        "document_id": document_id,
        "source_path": "政策/规则.md",
        "title": "规则",
        "document_type": "markdown",
        "summary": "Policy rules.",
        "topics": ["policy"],
        "entities": [],
        "updated_at": "2026-08-28T00:00:00+00:00",
        "parts": [
            {
                "part_id": "part-001",
                "label": "Policy",
                "summary": "The policy value.",
                "md_path": f"md/{document_id}/part-001.md",
                "source_anchors": [{"section": "policy"}],
            }
        ],
    }
    (artifact_dir / "card.json").write_text(json.dumps(card), encoding="utf-8")

    generation = settings.index_dir / "generations" / "1"
    folders = generation / "folders"
    folders.mkdir(parents=True)
    folder = {
        "folder_id": "policy",
        "source_directory": "政策",
        "summary": "Policy files.",
        "document_count": 1,
        "documents": [
            {
                "document_id": document_id,
                "source_path": "政策/规则.md",
                "title": "规则",
                "document_type": "markdown",
                "summary": "Policy rules.",
                "topics": ["policy"],
                "entities": [],
                "updated_at": "2026-08-28T00:00:00+00:00",
                "card_path": f"md/{document_id}/card.json",
            }
        ],
    }
    (folders / "policy.json").write_text(json.dumps(folder), encoding="utf-8")
    root = {
        "folders": [
            {
                "folder_id": "policy",
                "source_directory": "政策",
                "summary": "Policy files.",
                "document_count": 1,
                "index_path": "folders/policy.json",
            }
        ]
    }
    (generation / "root.json").write_text(json.dumps(root), encoding="utf-8")
    settings.index_dir.mkdir(parents=True, exist_ok=True)
    (settings.index_dir / "current.json").write_text(
        json.dumps(
            {
                "generation_number": 1,
                "root_index_path": "generations/1/root.json",
            }
        ),
        encoding="utf-8",
    )


@respx.mock
def test_complete_service_uses_router_profile_a_and_answer_profile_b(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    session = build_session_factory(engine)()
    source = _source_record(
        relative_path="政策/规则.md",
        mtime_ns=1_800_000_000_000_000_000,
    )
    session.add(source)
    session.flush()
    document_id = str(source.id)
    _write_index(settings, document_id)

    cipher = APIKeyCipher(settings.secret_path)
    provider_a = APIProvider(
        name="router provider A",
        provider_type="openai_compatible",
        base_url="https://profile-a.example.test/v1",
        encrypted_api_key=cipher.encrypt("sk-router-a"),
        protocol_preference="responses",
        extra_headers_json={},
        azure_mode="v1",
        enabled=True,
    )
    provider_b = APIProvider(
        name="answer provider B",
        provider_type="openai_compatible",
        base_url="https://profile-b.example.test/v1",
        encrypted_api_key=cipher.encrypt("sk-answer-b"),
        protocol_preference="responses",
        extra_headers_json={},
        azure_mode="v1",
        enabled=True,
    )
    profile_a = ModelProfile(
        provider=provider_a,
        name="profile A",
        remote_model_name="router-a",
        context_window=32_768,
        max_output_tokens=1_024,
        supports_text=True,
        supports_vision=False,
        supports_structured_output=True,
        tested_protocol="responses",
        last_test_status="passed",
        enabled=True,
        extra_request_json={},
    )
    profile_b = ModelProfile(
        provider=provider_b,
        name="profile B",
        remote_model_name="answer-b",
        context_window=32_768,
        max_output_tokens=1_024,
        supports_text=True,
        supports_vision=False,
        supports_structured_output=True,
        tested_protocol="responses",
        last_test_status="passed",
        enabled=True,
        extra_request_json={},
    )
    session.add_all([profile_a, profile_b])
    session.flush()
    session.add_all(
        [
            ModelRoleBinding(
                role=ModelRole.QUERY_ROUTER.value,
                model_profile_id=profile_a.id,
            ),
            ModelRoleBinding(
                role=ModelRole.ANSWER_GENERATION.value,
                model_profile_id=profile_b.id,
            ),
        ]
    )
    session.commit()

    router_models: list[str] = []

    def router_response(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        router_models.append(payload["model"])
        prompt = payload["input"]
        if "phase 1" in prompt:
            value = {
                "intent": "download",
                "selected_folders": ["policy"],
                "display_reason": "Search policy files.",
                "need_more_information": False,
            }
        else:
            value = {
                "selected_documents": [
                    {
                        "document_id": document_id,
                        "part_ids": ["part-001"],
                        "display_reason": "Use the selected policy.",
                    }
                ],
                "confidence": 0.95,
            }
        return httpx.Response(200, json={"output_text": json.dumps(value)})

    answer_payloads: list[dict[str, Any]] = []

    def answer_response(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        answer_payloads.append(payload)
        value = {
            "answer_markdown": "The selected evidence states 42.",
            "citations": [
                {
                    "document_id": document_id,
                    "part_id": "part-001",
                    "anchor": canonical_anchor({"section": "policy"}),
                    "label": "Policy",
                }
            ],
            "conflicts": [],
            "downloads": [{"document_id": document_id}],
            "research_handoff": None,
        }
        return httpx.Response(200, json={"output_text": json.dumps(value)})

    route_a = respx.post("https://profile-a.example.test/v1/responses").mock(
        side_effect=router_response
    )
    route_b = respx.post("https://profile-b.example.test/v1/responses").mock(
        side_effect=answer_response
    )
    registry = ModelRegistry(session, cipher)
    requested_roles: list[ModelRole] = []

    def resolve(role: ModelRole):
        requested_roles.append(role)
        return registry.get_for_role(role)

    try:
        result = asyncio.run(
            QuestionAnsweringService(
                settings,
                session,
                model_resolver=resolve,
            ).answer("What is the policy value? Download the source.")
        )
    finally:
        session.close()
        engine.dispose()

    assert requested_roles == [
        ModelRole.QUERY_ROUTER,
        ModelRole.ANSWER_GENERATION,
    ]
    assert route_a.call_count == 2
    assert route_b.call_count == 1
    assert router_models == ["router-a", "router-a"]
    assert answer_payloads[0]["model"] == "answer-b"
    assert answer_payloads[0]["instructions"] == ANSWER_SYSTEM_PROMPT
    assert "current_root_json" not in answer_payloads[0]["input"]
    assert len(result.citations) == 1
    assert result.downloads[0].document_id == document_id
    assert result.downloads[0].filename == "规则.md"
