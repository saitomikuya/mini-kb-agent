"""Grounded final-answer generation from already selected Markdown evidence."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import json
from pathlib import PurePosixPath
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.llm.clients import ModelClient, ModelProgressCallback
from app.llm.prompts import default_role_prompts, prompt_for_client
from app.llm.types import ModelRole
from app.models.source_file import SourceFile
from app.schemas.answers import (
    AnswerModelOutput,
    AnswerResult,
    Citation,
    Conflict,
    ConflictValue,
    Download,
)
from app.schemas.navigation import NavigatedPart, NavigationResult
from app.services.navigation import NavigationService
from app.source_files import SourceStatus


ModelResolver = Callable[[ModelRole], ModelClient]

_ANSWER_MODEL_SCHEMA = AnswerModelOutput.model_json_schema()
ANSWER_SYSTEM_PROMPT = default_role_prompts(ModelRole.ANSWER_GENERATION)[
    "grounded_answer"
]


class AnswerGenerationError(RuntimeError):
    """Base safe failure for grounded answer generation."""


class AnswerModelOutputError(AnswerGenerationError):
    """The answer model returned JSON outside its strict contract."""


class AnswerEvidenceError(AnswerGenerationError):
    """Selected evidence metadata cannot be represented safely."""


class AnswerGenerationService:
    """Call only ANSWER_GENERATION and validate its output against evidence."""

    def __init__(
        self,
        session: Session,
        *,
        model_resolver: ModelResolver,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.model_resolver = model_resolver
        self.settings = settings or Settings()

    async def generate(
        self,
        question: str,
        navigation: NavigationResult,
    ) -> AnswerResult:
        return await self._generate(question, navigation, on_progress=None)

    async def generate_with_progress(
        self,
        question: str,
        navigation: NavigationResult,
        *,
        on_progress: ModelProgressCallback,
    ) -> AnswerResult:
        return await self._generate(
            question,
            navigation,
            on_progress=on_progress,
        )

    async def _generate(
        self,
        question: str,
        navigation: NavigationResult,
        *,
        on_progress: ModelProgressCallback | None,
    ) -> AnswerResult:
        normalized_question = question.strip()
        if not normalized_question:
            raise AnswerGenerationError("The user question must not be blank")

        records = self._source_records(navigation)
        evidence_parts = [
            part
            for part in navigation.parts
            if part.within_evidence_budget and part.content is not None
        ]
        prompt = _answer_prompt(
            normalized_question,
            navigation,
            evidence_parts,
            records,
        )
        client = self.model_resolver(ModelRole.ANSWER_GENERATION)
        profile_output_limit = getattr(client, "max_output_tokens", None)
        output_limits = [self.settings.answer_max_output_tokens]
        if (
            isinstance(profile_output_limit, int)
            and not isinstance(profile_output_limit, bool)
            and profile_output_limit > 0
        ):
            output_limits.append(profile_output_limit)
        budget_output_limit = navigation.token_budget.answer_output_reserve
        if (
            isinstance(budget_output_limit, int)
            and not isinstance(budget_output_limit, bool)
            and budget_output_limit > 0
        ):
            output_limits.append(budget_output_limit)
        answer_output_limit = min(output_limits)
        generation_options = {
            "system_prompt": prompt_for_client(
                client,
                ModelRole.ANSWER_GENERATION,
                "grounded_answer",
            ),
            "json_schema": _ANSWER_MODEL_SCHEMA,
            "max_output_tokens": answer_output_limit,
            "verbosity": self.settings.answer_verbosity,
        }
        streaming_generation = getattr(client, "generate_json_stream", None)
        if on_progress is not None and callable(streaming_generation):
            generated = await streaming_generation(
                prompt,
                on_progress=on_progress,
                **generation_options,
            )
        else:
            generated = await client.generate_json(prompt, **generation_options)
        try:
            output = AnswerModelOutput.model_validate(generated.value)
        except ValidationError as exc:
            raise AnswerModelOutputError(
                "answer_generation returned invalid structured output"
            ) from exc
        return _validated_result(output, navigation, evidence_parts, records)

    def _source_records(
        self,
        navigation: NavigationResult,
    ) -> dict[str, SourceFile]:
        numeric_ids = {
            numeric_id
            for document in navigation.documents
            if (numeric_id := _database_id(document.document_id)) is not None
        }
        if not numeric_ids:
            return {}
        records = self.session.scalars(
            select(SourceFile).where(SourceFile.id.in_(numeric_ids))
        ).all()
        return {str(record.id): record for record in records}


class QuestionAnsweringService:
    """Complete question -> navigation -> grounded final-answer service flow."""

    def __init__(
        self,
        settings: Settings,
        session: Session,
        *,
        model_resolver: ModelResolver,
    ) -> None:
        clients: dict[ModelRole, ModelClient] = {}

        def cached_model_resolver(role: ModelRole) -> ModelClient:
            client = clients.get(role)
            if client is None:
                client = model_resolver(role)
                clients[role] = client
            return client

        self.navigation = NavigationService(
            settings,
            model_resolver=cached_model_resolver,
            evidence_model_resolver=cached_model_resolver,
        )
        self.answer_generation = AnswerGenerationService(
            session,
            model_resolver=cached_model_resolver,
            settings=settings,
        )

    async def answer(self, question: str) -> AnswerResult:
        _navigation, answer = await self.answer_with_navigation(question)
        return answer

    async def navigate(self, question: str) -> NavigationResult:
        return await self.navigation.navigate(question)

    async def generate_answer(
        self,
        question: str,
        navigation: NavigationResult,
    ) -> AnswerResult:
        return await self.answer_generation.generate(question, navigation)

    async def generate_answer_with_progress(
        self,
        question: str,
        navigation: NavigationResult,
        *,
        on_progress: ModelProgressCallback,
    ) -> AnswerResult:
        return await self.answer_generation.generate_with_progress(
            question,
            navigation,
            on_progress=on_progress,
        )

    async def answer_with_navigation(
        self,
        question: str,
    ) -> tuple[NavigationResult, AnswerResult]:
        """Return both audited navigation facts and the grounded answer.

        Chat streaming uses the navigation result to report only operations that
        actually completed.  Keeping this composition here also guarantees that
        no second navigation pass is needed merely to build the public trace.
        """
        navigation = await self.navigate(question)
        answer = await self.generate_answer(question, navigation)
        return navigation, answer


def canonical_anchor(anchor: Mapping[str, Any]) -> str:
    """Return the exact stable string answer models must copy for an anchor."""
    try:
        return json.dumps(
            dict(anchor),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise AnswerEvidenceError("An artifact anchor is not valid JSON metadata") from exc


def _answer_prompt(
    question: str,
    navigation: NavigationResult,
    evidence_parts: Sequence[NavigatedPart],
    records: Mapping[str, SourceFile],
) -> str:
    selected_markdown_parts = [
        {
            "document_id": part.document_id,
            "part_id": part.part_id,
            "markdown": part.content,
        }
        for part in evidence_parts
    ]
    parts_by_document: dict[str, list[NavigatedPart]] = {}
    for part in evidence_parts:
        parts_by_document.setdefault(part.document_id, []).append(part)

    source_metadata: list[dict[str, Any]] = []
    for document in navigation.documents:
        record = records.get(document.document_id)
        source_metadata.append(
            {
                "document_id": document.document_id,
                "title": document.title,
                "filename": record.filename if record is not None else document.title,
                "source_modified_at": (
                    _source_modified_at(record) if record is not None else None
                ),
                "parts": [
                    {
                        "part_id": part.part_id,
                        "citation_label": part.label,
                        "anchors": [
                            canonical_anchor(anchor)
                            for anchor in part.source_anchors
                        ],
                    }
                    for part in parts_by_document.get(document.document_id, [])
                ],
            }
        )

    return json.dumps(
        {
            "user_question": question,
            "selected_markdown_parts": selected_markdown_parts,
            "source_metadata": source_metadata,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _validated_result(
    output: AnswerModelOutput,
    navigation: NavigationResult,
    evidence_parts: Sequence[NavigatedPart],
    records: Mapping[str, SourceFile],
) -> AnswerResult:
    citation_whitelist: dict[tuple[str, str, str], str] = {}
    anchors_by_document: dict[str, set[str]] = {}
    for part in evidence_parts:
        for raw_anchor in part.source_anchors:
            anchor = canonical_anchor(raw_anchor)
            citation_whitelist[(part.document_id, part.part_id, anchor)] = part.label
            anchors_by_document.setdefault(part.document_id, set()).add(anchor)

    citations: list[Citation] = []
    seen_citations: set[tuple[str, str, str]] = set()
    for citation in output.citations:
        key = (citation.document_id, citation.part_id, citation.anchor)
        trusted_label = citation_whitelist.get(key)
        if trusted_label is None or citation.label != trusted_label:
            continue
        if key not in seen_citations:
            citations.append(citation)
            seen_citations.add(key)

    conflicts: list[Conflict] = []
    for conflict in output.conflicts:
        valid_values = [
            value
            for value in conflict.values
            if value.anchor in anchors_by_document.get(value.document_id, set())
        ]
        if len(valid_values) >= 2:
            conflicts.append(
                Conflict(
                    subject=conflict.subject,
                    values=[
                        ConflictValue(
                            value=value.value,
                            document_id=value.document_id,
                            anchor=value.anchor,
                        )
                        for value in valid_values
                    ],
                    analysis=conflict.analysis,
                )
            )

    selected_document_ids = {
        document.document_id for document in navigation.documents
    }
    downloads: list[Download] = []
    seen_downloads: set[str] = set()
    for intent in output.downloads:
        document_id = intent.document_id
        record = records.get(document_id)
        if (
            document_id not in selected_document_ids
            or record is None
            or record.source_status != SourceStatus.PRESENT.value
            or document_id in seen_downloads
        ):
            continue
        parent = PurePosixPath(record.relative_path).parent.as_posix()
        downloads.append(
            Download(
                document_id=document_id,
                filename=record.filename,
                relative_directory="" if parent == "." else parent,
            )
        )
        seen_downloads.add(document_id)

    return AnswerResult(
        answer_markdown=output.answer_markdown,
        citations=citations,
        conflicts=conflicts,
        downloads=downloads,
        research_handoff=output.research_handoff,
    )


def _source_modified_at(record: SourceFile) -> str:
    return datetime.fromtimestamp(
        record.mtime_ns / 1_000_000_000,
        tz=timezone.utc,
    ).isoformat()


def _database_id(value: str) -> int | None:
    if not value.isascii() or not value.isdigit():
        return None
    numeric_id = int(value)
    if str(numeric_id) != value or not 0 < numeric_id <= 9_223_372_036_854_775_807:
        return None
    return numeric_id
