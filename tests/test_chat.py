"""Authenticated stateless chat, local-history page, and title tests."""

import json
from pathlib import Path
from typing import Any, Iterator

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db import Base
from app.llm.clients import JSONGeneration
from app.llm.types import TestedProtocol as LLMProtocol
from app.main import create_app
from app.models.chat import ChatEvent, ChatSession, Message
from app.routers.chat import _friendly_reasoning_summary
from app.schemas.answers import AnswerResult
from app.schemas.navigation import (
    NavigatedDocument,
    NavigatedFolder,
    NavigatedPart,
    NavigationIntent,
    NavigationResult,
    NavigationTokenBudget,
)
from app.services.chat import human_source_location, public_answer_payload
import app.models  # noqa: F401  # Register complete SQLAlchemy metadata.


CHAT_PASSWORD = "chat-test-password"


def test_provider_jargon_is_reduced_to_short_chinese_progress() -> None:
    summary = _friendly_reasoning_summary(
        "Defining structured JSON response schema. "
        "Cataloging evidence conflicts and citation metadata."
    )

    assert summary == (
        "- 正在规划回答结构\n"
        "- 正在核对资料中的关键信息\n"
        "- 正在比较不同来源的数据差异\n"
        "- 正在核对出处与引用位置"
    )
    assert "JSON" not in summary


def test_public_answer_restores_internal_markers_to_original_file_links(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        source_dir=tmp_path / "sources",
        source_display_root=Path("/逻辑知识库"),
    )
    answer_data = _answer().model_dump(mode="json")
    answer_data["answer_markdown"] = (
        "## 规格\n\n规格值为 10。[产品A规格书第3页] "
        "具体位置见 [part-003]、[Page 3] 和 [section-1]。"
    )
    answer_data["citations"].append(
        {
            "document_id": "7",
            "part_id": "part-001",
            "anchor": '{"section":"section-1"}',
            "label": "section-1",
        }
    )
    answer = AnswerResult.model_validate(answer_data)

    payload = public_answer_payload(answer, settings, _navigation())

    expected_link = "[产品A规格书.pdf（第 3 页）](/api/files/7/download)"
    assert payload["answer_markdown"].count(expected_link) == 3
    assert "[产品A规格书.pdf（第 1 部分）](/api/files/7/download)" in payload[
        "answer_markdown"
    ]
    assert "[part-003]" not in payload["answer_markdown"]
    assert "[section-1]" not in payload["answer_markdown"]
    citation = payload["citations"][0]
    assert citation["source_filename"] == "产品A规格书.pdf"
    assert citation["source_path"] == "产品资料/产品A规格书.pdf"
    assert citation["display_path"] == "/逻辑知识库/产品资料/产品A规格书.pdf"
    assert citation["source_location"] == "第 3 页"
    assert citation["download_url"] == "/api/files/7/download"
    assert human_source_location('{"section":"section-2"}') == "第 2 部分"


def _navigation() -> NavigationResult:
    return NavigationResult(
        intent=NavigationIntent.DOWNLOAD,
        folders=[
            NavigatedFolder(
                folder_id="products",
                source_directory="产品资料",
                summary="产品资料索引",
                display_reason="与问题相关",
            )
        ],
        documents=[
            NavigatedDocument(
                folder_id="products",
                document_id="7",
                source_path="产品资料/产品A规格书.pdf",
                title="产品A规格书",
                document_type="pdf",
                selected_part_ids=["part-003"],
                display_reason="包含所需规格",
            )
        ],
        parts=[
            NavigatedPart(
                folder_id="products",
                document_id="7",
                part_id="part-003",
                label="产品A规格书第3页",
                summary="产品规格",
                md_path="md/7/part-003.md",
                source_anchors=[{"page": 3}],
                content="规格值为 10。",
                estimated_tokens=8,
                within_evidence_budget=True,
            )
        ],
        display_steps=[],
        confidence=0.91,
        token_budget=NavigationTokenBudget(
            context_window=32_768,
            root_budget=4_000,
            folder_budget=8_000,
            evidence_budget=16_000,
            output_reserve=2_048,
        ),
    )


def _answer() -> AnswerResult:
    return AnswerResult.model_validate(
        {
            "answer_markdown": "产品A的规格值存在两个来源值，请核对。",
            "citations": [
                {
                    "document_id": "7",
                    "part_id": "part-003",
                    "anchor": '{"page":3}',
                    "label": "产品A规格书第3页",
                }
            ],
            "conflicts": [
                {
                    "subject": "产品A规格值",
                    "values": [
                        {
                            "value": "10",
                            "document_id": "7",
                            "anchor": '{"page":3}',
                        },
                        {
                            "value": "12",
                            "document_id": "8",
                            "anchor": '{"page":5}',
                        },
                    ],
                    "analysis": "两个来源数据不同。",
                }
            ],
            "downloads": [
                {
                    "document_id": "7",
                    "filename": "产品A规格书.pdf",
                    "relative_directory": "产品资料",
                }
            ],
            "research_handoff": None,
        }
    )


class FakeAnswering:
    async def navigate(self, _question: str) -> NavigationResult:
        return _navigation()

    async def generate_answer(
        self,
        _question: str,
        _navigation_result: NavigationResult,
    ) -> AnswerResult:
        return _answer()


class ProgressAnswering(FakeAnswering):
    async def generate_answer_with_progress(
        self,
        _question: str,
        _navigation_result: NavigationResult,
        *,
        on_progress: Any,
    ) -> AnswerResult:
        await on_progress(
            "reasoning_summary",
            {"summary": "先核对两个来源，再组织有引用的答案。"},
        )
        await on_progress("output_progress", {"generated_characters": 360})
        return _answer()


class FailingAnswering(FakeAnswering):
    async def generate_answer(
        self,
        _question: str,
        _navigation_result: NavigationResult,
    ) -> AnswerResult:
        raise RuntimeError("provider secret must not be exposed")


class FakeTitleModel:
    role_prompts: dict[str, str] = {}
    context_window = 32_768
    max_output_tokens = 4_096

    async def generate_json(self, prompt: str, **options: Any) -> JSONGeneration:
        assert "Conversation JSON" in prompt
        assert options["max_output_tokens"] == 80
        return JSONGeneration(
            value={"title": "产品A规格与来源。"},
            protocol=LLMProtocol.RESPONSES,
            latency_ms=4,
            native_structured_output=True,
        )


def _client(
    tmp_path: Path,
    answering: Any,
) -> Iterator[TestClient]:
    settings = Settings(
        chat_password=CHAT_PASSWORD,
        admin_password="admin-test-password",
        data_dir=tmp_path / "data",
        source_dir=tmp_path / "sources",
        source_display_root=Path("/逻辑知识库"),
    )
    settings.data_dir.mkdir()
    settings.source_dir.mkdir()
    application = create_app(
        settings,
        chat_answering_service_factory=lambda _session: answering,
        chat_title_model_factory=lambda _session: FakeTitleModel(),
    )
    Base.metadata.create_all(application.state.database_engine)
    with TestClient(application) as test_client:
        assert test_client.post(
            "/api/auth/chat/login",
            json={"password": CHAT_PASSWORD},
        ).status_code == 200
        yield test_client


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    yield from _client(tmp_path, FakeAnswering())


def _sse_events(response_text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for block in response_text.strip().split("\n\n"):
        lines = block.splitlines()
        event_type = next(
            line.removeprefix("event: ")
            for line in lines
            if line.startswith("event: ")
        )
        data = json.loads(
            next(
                line.removeprefix("data: ")
                for line in lines
                if line.startswith("data: ")
            )
        )
        events.append((event_type, data))
    return events


def test_root_directly_renders_public_chat_login_and_local_history_ui(
    client: TestClient,
) -> None:
    client.post("/api/auth/logout")
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 200
    assert "location" not in root.headers
    assert 'id="chat-app"' in root.text

    page = client.get("/chat")
    assert page.status_code == 200
    assert 'id="login-form"' in page.text
    assert 'id="chat-password"' in page.text
    assert 'id="sessions"' in page.text
    assert 'id="messages"' in page.text
    assert 'id="question"' in page.text
    assert 'id="source-library-button"' in page.text
    assert 'id="source-library-dialog"' in page.text
    assert 'id="source-library-search"' in page.text
    assert 'aria-controls="source-library-dialog">知识库</button>' in page.text
    assert "源文件" not in page.text
    assert '<h2 id="source-library-title">知识库文件</h2>' in page.text
    assert '<p class="eyebrow">知识问答</p>' not in page.text
    assert "浏览知识库文件，需要时可直接下载。" in page.text
    assert "索引" not in page.text
    assert 'id="delete-session"' not in page.text
    assert "聊天记录仅保存在本浏览器，不会上传云端" in page.text
    assert "输入问题，开始知识问答。" in page.text
    assert "React" not in page.text

    direct_session_page = client.get("/chat/browser-session-id")
    assert direct_session_page.status_code == 200
    assert 'id="chat-app"' in direct_session_page.text

    chat_script = client.get("/static/chat.js")
    assert chat_script.status_code == 200
    assert "复制提示词" in chat_script.text
    assert "发送到第三方大模型前" in chat_script.text
    assert 'api("/api/files")' in chat_script.text
    assert "source-library-download-icon" in chat_script.text
    assert "source-library-open-folder" not in chat_script.text
    assert 'file.index_status === "INDEXED"' in chat_script.text
    assert "formatFileSize(file.size)" in chat_script.text
    assert "stats.subfolderCount" in chat_script.text
    assert "stats.fileCount" in chat_script.text
    assert "索引转换时间" not in chat_script.text
    assert 'const card = document.createElement("div");' in chat_script.text
    assert 'document.createElement(source.downloadUrl ? "a" : "div")' not in chat_script.text
    assert 'download.className = "source-action"' in chat_script.text
    assert "source-action-label" in chat_script.text
    assert "source-action-icon" in chat_script.text
    assert "文件目录：" in chat_script.text
    assert "list.start = start" in chat_script.text
    assert "renderAnswerProgressively" in chat_script.text
    assert "window.requestAnimationFrame" in chat_script.text
    assert 'classList.remove("is-streaming")' in chat_script.text
    assert "file.view_url" not in chat_script.text
    assert "只读查看" not in chat_script.text
    assert "源文件" not in chat_script.text

    chat_styles = client.get("/static/chat.css")
    assert chat_styles.status_code == 200
    assert ".source-action-label { display: none; }" in chat_styles.text
    assert ".source-library-download-icon" in chat_styles.text

    assert client.post(
        "/api/chat/stream",
        json={"question": "未登录"},
    ).status_code == 401


def test_sse_order_is_stateless_and_display_only_download_path(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/chat/stream",
        json={"question": "产品A的规格是什么？"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(response.text)
    assert [event_type for event_type, _data in events] == [
        "request_received",
        "navigation_started",
        "intent_detected",
        "folders_selected",
        "documents_selected",
        "answer_generating",
        "conflict_detected",
        "download_ready",
        "completed",
    ]
    assert events[3][1]["message"] == "已定位资料范围：1 个资料目录"
    assert events[4][1]["documents"][0]["title"] == "产品A规格书"
    assert events[4][1]["documents"][0]["source_path"] == "产品资料/产品A规格书.pdf"
    assert events[5][1]["message"] == "正在根据相关资料整理回答"
    assert "Markdown" not in response.text
    assert "分片" not in response.text

    completed = events[-1][1]
    download = completed["answer"]["downloads"][0]
    assert download["filename"] == "产品A规格书.pdf"
    assert download["relative_directory"] == "产品资料"
    assert download["display_path"] == "/逻辑知识库/产品资料/产品A规格书.pdf"
    assert download["download_url"] == "/api/files/7/download"
    assert download["display_path"] not in download["download_url"]
    citation = completed["answer"]["citations"][0]
    assert citation["source_filename"] == "产品A规格书.pdf"
    assert citation["source_location"] == "第 3 页"
    assert citation["download_url"] == "/api/files/7/download"

    assert "session_id" not in events[0][1]
    assert "user_message_id" not in completed
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count(ChatSession.id))) == 0
        assert session.scalar(select(func.count(Message.id))) == 0
        assert session.scalar(select(func.count(ChatEvent.id))) == 0


def test_sse_forwards_safe_model_progress_without_raw_reasoning(tmp_path: Path) -> None:
    client_iterator = _client(tmp_path, ProgressAnswering())
    client = next(client_iterator)
    try:
        response = client.post(
            "/api/chat/stream",
            json={"question": "产品A的规格是什么？"},
        )
        events = _sse_events(response.text)
        event_types = [event_type for event_type, _data in events]

        summary_index = event_types.index("answer_reasoning_summary")
        output_index = event_types.index("answer_output_progress")
        assert summary_index > event_types.index("answer_generating")
        assert output_index > summary_index
        assert events[summary_index][1] == {
            "type": "answer_reasoning_summary",
            "message": "模型正在整理回答要点",
            "summary": "先核对两个来源，再组织有引用的答案。",
            "source": "model",
        }
        assert events[output_index][1]["generated_characters"] == 360
        assert "raw_reasoning" not in response.text
        assert event_types[-1] == "completed"
    finally:
        client_iterator.close()


def test_title_uses_router_role_model_contract_without_persistence(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/chat/title",
        json={
            "messages": [
                {"role": "user", "content": "产品A的规格是什么？"},
                {"role": "assistant", "content": "规格值为 10。"},
            ]
        },
    )

    assert response.status_code == 200
    assert response.json() == {"title": "产品A规格与来源"}
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count(ChatSession.id))) == 0


def test_sse_error_does_not_persist_chat_content(tmp_path: Path) -> None:
    client_iterator = _client(tmp_path, FailingAnswering())
    client = next(client_iterator)
    try:
        response = client.post(
            "/api/chat/stream",
            json={"question": "触发回答失败"},
        )
        events = _sse_events(response.text)
        assert events[-1][0] == "error"
        assert events[-2][0] == "answer_generating"
        assert events[-1][1]["error_type"] == "RuntimeError"
        assert "provider secret" not in response.text

        with client.app.state.session_factory() as session:
            assert session.scalar(select(func.count(ChatSession.id))) == 0
            assert session.scalar(select(func.count(Message.id))) == 0
            assert session.scalar(select(func.count(ChatEvent.id))) == 0
    finally:
        client_iterator.close()


def test_stream_rejects_server_session_identifiers(client: TestClient) -> None:
    response = client.post(
        "/api/chat/stream",
        json={"session_id": 999_999, "question": "问题"},
    )

    assert response.status_code == 422
