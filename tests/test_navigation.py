"""Two-phase folder/document/Markdown-part navigation tests."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from app.config import Settings
from app.llm.types import ModelRole
from app.schemas.navigation import NavigationIntent
from app.services.navigation import (
    NavigationModelOutputError,
    NavigationService,
    calculate_navigation_budget,
    estimate_tokens,
)
from app.services.lexical_index import (
    LEXICAL_INDEX_FILENAME,
    LexicalPartRecord,
    build_lexical_index,
)


class RecordingRouterClient:
    def __init__(
        self,
        responder: Callable[[str, int], dict[str, Any]],
        *,
        context_window: int = 32_768,
        max_output_tokens: int = 1_024,
    ) -> None:
        self.responder = responder
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.prompts: list[str] = []
        self.options: list[dict[str, Any]] = []

    async def generate_json(self, prompt: str, **kwargs):
        self.prompts.append(prompt)
        self.options.append(kwargs)
        return SimpleNamespace(value=self.responder(prompt, len(self.prompts)))

    async def generate_text(self, *_args, **_kwargs):  # pragma: no cover
        pytest.fail("navigation must use generate_json")

    async def generate_multimodal(self, *_args, **_kwargs):  # pragma: no cover
        pytest.fail("navigation must not use multimodal generation")


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values = {
        "data_dir": tmp_path / "data",
        "source_dir": tmp_path / "sources",
    }
    values.update(overrides)
    settings = Settings(**values)
    settings.data_dir.mkdir(parents=True)
    return settings


def _write_navigation_index(
    settings: Settings,
    folders: dict[str, list[dict[str, Any]]],
    *,
    include_lexical: bool = False,
) -> None:
    generation_dir = settings.index_dir / "generations" / "1"
    folder_dir = generation_dir / "folders"
    folder_dir.mkdir(parents=True)
    root_entries: list[dict[str, Any]] = []
    lexical_records: list[LexicalPartRecord] = []
    for folder_id, documents in folders.items():
        source_directory = f"directory-{folder_id}"
        compact_documents: list[dict[str, Any]] = []
        for source in documents:
            document_id = str(source["document_id"])
            parts = source.get(
                "parts",
                [
                    {
                        "part_id": "part-001",
                        "label": "Overview",
                        "summary": source.get("part_summary", "Relevant facts."),
                        "content": source.get("content", "# Evidence\n\nRelevant facts."),
                    }
                ],
            )
            card_parts: list[dict[str, Any]] = []
            artifact_dir = settings.markdown_dir / document_id
            artifact_dir.mkdir(parents=True)
            for part in parts:
                md_path = f"md/{document_id}/{part['part_id']}.md"
                (settings.data_dir / md_path).write_text(
                    part["content"],
                    encoding="utf-8",
                )
                card_parts.append(
                    {
                        "part_id": part["part_id"],
                        "label": part["label"],
                        "summary": part["summary"],
                        "md_path": md_path,
                        "source_anchors": [{"section": part["part_id"]}],
                    }
                )
            title = source.get("title", f"Document {document_id}")
            summary = source.get("summary", f"Summary for {title}")
            card = {
                "document_id": document_id,
                "source_path": f"{source_directory}/{title}.md",
                "title": title,
                "document_type": "markdown",
                "summary": summary,
                "topics": source.get("topics", [folder_id]),
                "entities": [],
                "updated_at": "2026-08-28T00:00:00+00:00",
                "parts": card_parts,
            }
            lexical_records.extend(
                LexicalPartRecord(
                    folder_id=folder_id,
                    document_id=document_id,
                    part_id=part["part_id"],
                    source_path=card["source_path"],
                    title=title,
                    document_type="markdown",
                    topics=tuple(str(value) for value in card["topics"]),
                    entities=(),
                    label=part["label"],
                    summary=part["summary"],
                    body=part["content"],
                )
                for part in parts
            )
            (artifact_dir / "card.json").write_text(
                json.dumps(card, ensure_ascii=False),
                encoding="utf-8",
            )
            compact_documents.append(
                {
                    "document_id": document_id,
                    "source_path": card["source_path"],
                    "title": title,
                    "document_type": "markdown",
                    "summary": summary,
                    "topics": card["topics"],
                    "entities": [],
                    "updated_at": card["updated_at"],
                    "card_path": f"md/{document_id}/card.json",
                }
            )
        folder = {
            "folder_id": folder_id,
            "source_directory": source_directory,
            "summary": f"Knowledge in {source_directory}",
            "document_count": len(compact_documents),
            "documents": compact_documents,
        }
        index_path = f"folders/{folder_id}.json"
        (generation_dir / index_path).write_text(
            json.dumps(folder, ensure_ascii=False),
            encoding="utf-8",
        )
        root_entries.append(
            {
                "folder_id": folder_id,
                "source_directory": source_directory,
                "summary": folder["summary"],
                "document_count": len(compact_documents),
                "index_path": index_path,
            }
        )
    (generation_dir / "root.json").write_text(
        json.dumps({"folders": root_entries}, ensure_ascii=False),
        encoding="utf-8",
    )
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
    if include_lexical:
        build_lexical_index(
            generation_dir / LEXICAL_INDEX_FILENAME,
            lexical_records,
        )


def _run_navigation(
    settings: Settings,
    client: RecordingRouterClient,
    question: str = "Where are the relevant facts?",
    conversation_history: list[dict[str, str]] | None = None,
):
    roles: list[ModelRole] = []

    def resolver(role: ModelRole):
        roles.append(role)
        assert role is ModelRole.QUERY_ROUTER
        return client

    result = asyncio.run(
        NavigationService(settings, model_resolver=resolver).navigate(
            question,
            conversation_history=conversation_history or (),
        )
    )
    return result, roles


def test_question_selects_correct_folder_document_and_markdown_part(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_navigation_index(
        settings,
        {
            "sales": [
                {
                    "document_id": "1",
                    "title": "Sales Guide",
                    "content": "# Sales\n\nThe grounded sales fact.",
                }
            ],
            "training": [{"document_id": "2", "title": "Training Guide"}],
        },
    )

    def respond(prompt: str, _call: int) -> dict[str, Any]:
        if "phase 1" in prompt:
            return {
                "intent": "answer",
                "selected_folders": ["sales"],
                "display_reason": "Look in sales material.",
                "need_more_information": False,
            }
        return {
            "selected_documents": [
                {
                    "document_id": "1",
                    "part_ids": ["part-001"],
                    "display_reason": "Open the sales overview.",
                }
            ],
            "confidence": 0.91,
        }

    result, roles = _run_navigation(settings, RecordingRouterClient(respond))

    assert roles == [ModelRole.QUERY_ROUTER]
    assert result.intent is NavigationIntent.ANSWER
    assert [folder.folder_id for folder in result.folders] == ["sales"]
    assert [document.document_id for document in result.documents] == ["1"]
    assert [part.part_id for part in result.parts] == ["part-001"]
    assert result.parts[0].content == "# Sales\n\nThe grounded sales fact."
    assert result.confidence == 0.91
    assert "grounded sales fact" not in " ".join(result.display_steps).lower()


def test_follow_up_history_is_included_in_every_router_prompt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_navigation_index(
        settings,
        {"products": [{"document_id": "1", "title": "产品A规格"}]},
    )

    def respond(prompt: str, _call: int) -> dict[str, Any]:
        if "phase 1" in prompt:
            return {
                "intent": "answer",
                "selected_folders": ["products"],
                "display_reason": "定位产品资料。",
                "need_more_information": False,
            }
        return {
            "selected_documents": [
                {
                    "document_id": "1",
                    "part_ids": ["part-001"],
                    "display_reason": "读取产品规格。",
                }
            ],
            "confidence": 0.9,
        }

    client = RecordingRouterClient(respond)
    _run_navigation(
        settings,
        client,
        question="那它的来源呢？",
        conversation_history=[
            {"role": "user", "content": "产品A的规格是什么？"},
            {"role": "assistant", "content": "规格值为 10。"},
        ],
    )

    assert len(client.prompts) == 2
    assert all('"conversation_history"' in prompt for prompt in client.prompts)
    assert all('"current_user_question":"那它的来源呢？"' in prompt for prompt in client.prompts)
    assert all("产品A的规格是什么" in prompt for prompt in client.prompts)


def test_lexical_recall_bypasses_wrong_root_choice_and_falls_back_to_exact_part(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        navigation_low_confidence_percent=45,
        lexical_fallback_parts=3,
    )
    _write_navigation_index(
        settings,
        {
            "contracts": [
                {
                    "document_id": "1",
                    "title": "Confidentiality Appendix",
                    "summary": "Generic appendix summary.",
                    "part_summary": "Signing evidence.",
                    "content": "ZXQ-9472 的保密期限为永久。",
                },
                {
                    "document_id": "2",
                    "title": "Unrelated Contract",
                    "summary": "Unrelated material.",
                    "content": "No matching identifier.",
                },
            ],
            "other": [
                {
                    "document_id": "3",
                    "title": "Other Guide",
                    "content": "General guidance.",
                }
            ],
        },
        include_lexical=True,
    )

    def respond(prompt: str, _call: int) -> dict[str, Any]:
        if "phase 1" in prompt:
            return {
                "intent": "answer",
                "selected_folders": ["other"],
                "display_reason": "The semantic root summary suggested another folder.",
                "need_more_information": False,
            }
        return {"selected_documents": [], "confidence": 0.2}

    client = RecordingRouterClient(respond)
    result, _roles = _run_navigation(
        settings,
        client,
        question="ZXQ-9472 的保密期限是多久？",
    )

    assert [part.document_id for part in result.parts] == ["1"]
    assert [part.part_id for part in result.parts] == ["part-001"]
    assert result.parts[0].content == "ZXQ-9472 的保密期限为永久。"
    contract_prompts = [
        prompt
        for prompt in client.prompts
        if '"folder_id":"contracts"' in prompt and "phase 2" in prompt
    ]
    assert len(contract_prompts) == 1
    assert '"document_id":"1"' in contract_prompts[0]
    assert '"document_id":"2"' not in contract_prompts[0]
    assert any("locally recalled" in step for step in result.display_steps)


def test_multiple_folders_are_routed_in_phase_two(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_navigation_index(
        settings,
        {
            "alpha": [{"document_id": "1"}],
            "beta": [{"document_id": "2"}],
        },
    )

    def respond(prompt: str, _call: int) -> dict[str, Any]:
        if "phase 1" in prompt:
            return {
                "intent": "answer",
                "selected_folders": ["alpha", "beta"],
                "display_reason": "Check both collections.",
                "need_more_information": False,
            }
        document_id = "1" if '"folder_id":"alpha"' in prompt else "2"
        return {
            "selected_documents": [
                {
                    "document_id": document_id,
                    "part_ids": ["part-001"],
                    "display_reason": "Use the matching overview.",
                }
            ],
            "confidence": 0.8,
        }

    client = RecordingRouterClient(respond)
    result, _roles = _run_navigation(settings, client)

    assert [folder.folder_id for folder in result.folders] == ["alpha", "beta"]
    assert [document.document_id for document in result.documents] == ["1", "2"]
    assert len(client.prompts) == 3


def test_multiple_documents_can_be_selected(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_navigation_index(
        settings,
        {"docs": [{"document_id": "1"}, {"document_id": "2"}]},
    )

    def respond(prompt: str, _call: int) -> dict[str, Any]:
        if "phase 1" in prompt:
            return {
                "intent": "list_files",
                "selected_folders": ["docs"],
                "display_reason": "List the relevant files.",
                "need_more_information": False,
            }
        return {
            "selected_documents": [
                {"document_id": value, "part_ids": [], "display_reason": "Relevant file."}
                for value in ("1", "2")
            ],
            "confidence": 0.77,
        }

    result, _roles = _run_navigation(settings, RecordingRouterClient(respond))

    assert result.intent is NavigationIntent.LIST_FILES
    assert [document.document_id for document in result.documents] == ["1", "2"]
    assert result.parts == []


def test_hallucinated_document_and_part_ids_are_rejected_by_whitelist(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_navigation_index(settings, {"docs": [{"document_id": "1"}]})

    def respond(prompt: str, _call: int) -> dict[str, Any]:
        if "phase 1" in prompt:
            return {
                "intent": "answer",
                "selected_folders": ["docs", "invented-folder"],
                "display_reason": "Check documents.",
                "need_more_information": False,
            }
        return {
            "selected_documents": [
                {
                    "document_id": "999",
                    "part_ids": ["part-never-existed"],
                    "display_reason": "Invented selection.",
                },
                {
                    "document_id": "1",
                    "part_ids": ["part-never-existed"],
                    "display_reason": "Known document.",
                },
            ],
            "confidence": 0.2,
        }

    result, _roles = _run_navigation(settings, RecordingRouterClient(respond))

    assert [folder.folder_id for folder in result.folders] == ["docs"]
    assert [document.document_id for document in result.documents] == ["1"]
    assert result.documents[0].selected_part_ids == []
    assert result.parts == []
    assert any("not present" in step for step in result.display_steps)


def test_selection_is_capped_at_configured_eight_documents(tmp_path: Path) -> None:
    settings = _settings(tmp_path, navigation_max_selected_documents=8)
    documents = [{"document_id": str(number)} for number in range(1, 11)]
    _write_navigation_index(settings, {"docs": documents})

    def respond(prompt: str, _call: int) -> dict[str, Any]:
        if "phase 1" in prompt:
            return {
                "intent": "list_files",
                "selected_folders": ["docs"],
                "display_reason": "List documents.",
                "need_more_information": False,
            }
        return {
            "selected_documents": [
                {"document_id": str(number), "part_ids": [], "display_reason": "Match."}
                for number in range(1, 11)
            ],
            "confidence": 0.9,
        }

    result, _roles = _run_navigation(settings, RecordingRouterClient(respond))

    assert len(result.documents) == 8
    assert [document.document_id for document in result.documents] == [
        str(number) for number in range(1, 9)
    ]
    assert any("maximum of 8" in step for step in result.display_steps)


def test_selected_folders_are_routed_in_parallel(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_navigation_index(
        settings,
        {
            "first": [{"document_id": "1"}],
            "second": [{"document_id": "2"}],
            "third": [{"document_id": "3"}],
        },
    )

    class ParallelRouterClient(RecordingRouterClient):
        def __init__(self) -> None:
            super().__init__(lambda _prompt, _call: {})
            self.in_flight = 0
            self.max_in_flight = 0

        async def generate_json(self, prompt: str, **kwargs):
            self.prompts.append(prompt)
            self.options.append(kwargs)
            if "phase 1" in prompt:
                return SimpleNamespace(
                    value={
                        "intent": "answer",
                        "selected_folders": ["first", "second", "third"],
                        "display_reason": "Search all matching folders.",
                        "need_more_information": False,
                    }
                )
            folder = json.loads(prompt.split("folder_index:\n", 1)[1])
            document_id = folder["documents"][0]["document_id"]
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                await asyncio.sleep(0.02)
            finally:
                self.in_flight -= 1
            return SimpleNamespace(
                value={
                    "selected_documents": [
                        {
                            "document_id": document_id,
                            "part_ids": ["part-001"],
                            "display_reason": "Relevant evidence.",
                        }
                    ],
                    "confidence": 0.9,
                }
            )

    client = ParallelRouterClient()
    result, _roles = _run_navigation(settings, client)

    assert client.max_in_flight == 3
    assert [document.document_id for document in result.documents] == ["1", "2", "3"]
    assert all(options["verbosity"] == "low" for options in client.options)


def test_navigation_output_reserve_is_clamped_for_large_model_profiles(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    client = RecordingRouterClient(
        lambda _prompt, _call: {},
        context_window=1_050_000,
        max_output_tokens=128_000,
    )

    budget = calculate_navigation_budget(client, settings)

    assert budget.output_reserve == settings.navigation_default_max_output_tokens


def test_large_folder_is_batched_as_valid_json_without_string_truncation(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    documents = [
        {
            "document_id": str(number),
            "summary": f"Document {number} " + ("long-summary " * 35),
            "part_summary": "part-summary " * 30,
        }
        for number in range(1, 31)
    ]
    _write_navigation_index(settings, {"large": documents})

    def respond(prompt: str, _call: int) -> dict[str, Any]:
        if "phase 1" in prompt:
            return {
                "intent": "answer",
                "selected_folders": ["large"],
                "display_reason": "Search the large folder.",
                "need_more_information": False,
            }
        assert json.loads(prompt.split("folder_index:\n", 1)[1])
        return {"selected_documents": [], "confidence": 0.6}

    client = RecordingRouterClient(
        respond,
        context_window=8_000,
        max_output_tokens=1_000,
    )
    result, _roles = _run_navigation(settings, client)
    folder_prompts = [prompt for prompt in client.prompts if "phase 2" in prompt]

    assert len(folder_prompts) > 1
    assert all(
        estimate_tokens(prompt) <= result.token_budget.folder_budget
        for prompt in folder_prompts
    )
    assert result.documents == []


def test_profile_limits_allocate_all_budgets_and_omit_oversized_evidence(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    oversized_content = "evidence-token " * 2_000
    _write_navigation_index(
        settings,
        {
            "docs": [
                {
                    "document_id": "1",
                    "content": oversized_content,
                }
            ]
        },
    )

    def respond(prompt: str, _call: int) -> dict[str, Any]:
        if "phase 1" in prompt:
            return {
                "intent": "answer",
                "selected_folders": ["docs"],
                "display_reason": "Open the evidence folder.",
                "need_more_information": False,
            }
        return {
            "selected_documents": [
                {
                    "document_id": "1",
                    "part_ids": ["part-001"],
                    "display_reason": "Use the evidence part.",
                }
            ],
            "confidence": 0.7,
        }

    client = RecordingRouterClient(
        respond,
        context_window=4_096,
        max_output_tokens=512,
    )
    result, _roles = _run_navigation(settings, client)

    assert result.token_budget.context_window == 4_096
    assert result.token_budget.output_reserve == 512
    assert result.token_budget.answer_context_window == 4_096
    assert result.token_budget.answer_output_reserve == 512
    assert (
        result.token_budget.root_budget
        + result.token_budget.router_safety_reserve
        + result.token_budget.output_reserve
        == result.token_budget.context_window
    )
    assert (
        result.token_budget.evidence_budget
        + result.token_budget.answer_safety_reserve
        + result.token_budget.answer_output_reserve
        == result.token_budget.answer_context_window
    )
    assert len(result.parts) == 1
    assert result.parts[0].content is None
    assert result.parts[0].within_evidence_budget is False
    assert result.parts[0].estimated_tokens > result.token_budget.evidence_budget
    assert any("without silently truncating" in step for step in result.display_steps)


def test_answer_profile_independently_controls_evidence_budget(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    router = RecordingRouterClient(
        lambda _prompt, _call: {},
        context_window=32_768,
        max_output_tokens=2_048,
    )
    answer = RecordingRouterClient(
        lambda _prompt, _call: {},
        context_window=131_072,
        max_output_tokens=8_192,
    )

    budget = calculate_navigation_budget(
        router,
        settings,
        answer_client=answer,
    )

    assert budget.root_budget == 29_184
    assert budget.folder_budget == 29_184
    assert budget.evidence_budget == 116_736
    assert budget.answer_context_window == 131_072
    assert budget.answer_output_reserve == 8_192


def test_missing_answer_profile_output_uses_answer_tuning_reserve(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    router = RecordingRouterClient(
        lambda _prompt, _call: {},
        context_window=32_768,
        max_output_tokens=2_048,
    )
    answer = RecordingRouterClient(
        lambda _prompt, _call: {},
        context_window=131_072,
        max_output_tokens=None,  # type: ignore[arg-type]
    )

    budget = calculate_navigation_budget(router, settings, answer_client=answer)

    assert budget.answer_output_reserve == settings.answer_max_output_tokens
    assert budget.evidence_budget == 116_736


def test_part_cap_is_distributed_across_selected_documents(tmp_path: Path) -> None:
    settings = _settings(tmp_path, navigation_max_selected_parts=4)
    parts = [
        {
            "part_id": f"part-{number:03d}",
            "label": f"Part {number}",
            "summary": f"Evidence {number}",
            "content": f"Evidence body {number}",
        }
        for number in range(1, 5)
    ]
    _write_navigation_index(
        settings,
        {
            "docs": [
                {"document_id": "1", "parts": parts},
                {"document_id": "2", "parts": parts},
            ]
        },
    )

    def respond(prompt: str, _call: int) -> dict[str, Any]:
        if "phase 1" in prompt:
            return {
                "intent": "answer",
                "selected_folders": ["docs"],
                "display_reason": "Search both documents.",
                "need_more_information": False,
            }
        return {
            "selected_documents": [
                {
                    "document_id": document_id,
                    "part_ids": [part["part_id"] for part in parts],
                    "display_reason": "Relevant evidence.",
                }
                for document_id in ("1", "2")
            ],
            "confidence": 0.9,
        }

    result, _roles = _run_navigation(settings, RecordingRouterClient(respond))

    assert [document.selected_part_ids for document in result.documents] == [
        ["part-001", "part-002"],
        ["part-001", "part-002"],
    ]
    assert any("distributed set" in step for step in result.display_steps)


def test_invalid_pydantic_model_output_stops_navigation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_navigation_index(settings, {"docs": [{"document_id": "1"}]})
    client = RecordingRouterClient(
        lambda _prompt, _call: {
            "intent": "answer",
            "selected_folders": "not-an-array",
            "display_reason": "Invalid.",
            "need_more_information": False,
        }
    )

    with pytest.raises(NavigationModelOutputError, match="phase-one"):
        _run_navigation(settings, client)


def test_only_query_router_role_is_resolved_when_answer_model_differs(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _write_navigation_index(settings, {"docs": [{"document_id": "1"}]})
    router = RecordingRouterClient(
        lambda prompt, _call: (
            {
                "intent": "download",
                "selected_folders": [],
                "display_reason": "No matching download.",
                "need_more_information": True,
            }
            if "phase 1" in prompt
            else pytest.fail("phase 2 should not run")
        )
    )
    answer = RecordingRouterClient(lambda _prompt, _call: pytest.fail("answer called"))
    requested_roles: list[ModelRole] = []

    def resolver(role: ModelRole):
        requested_roles.append(role)
        return {
            ModelRole.QUERY_ROUTER: router,
            ModelRole.ANSWER_GENERATION: answer,
        }[role]

    result = asyncio.run(
        NavigationService(settings, model_resolver=resolver).navigate("Download it")
    )

    assert result.intent is NavigationIntent.DOWNLOAD
    assert requested_roles == [ModelRole.QUERY_ROUTER]
    assert len(router.prompts) == 1
    assert answer.prompts == []
