"""Index-guided folder, document, and Markdown-part navigation only."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
import json
import math
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError

from app.config import Settings
from app.llm.clients import ModelClient
from app.llm.prompts import default_role_prompts, prompt_for_client
from app.llm.types import ModelRole
from app.schemas.navigation import (
    DocumentSelectionResult,
    FolderSelection,
    NavigatedDocument,
    NavigatedFolder,
    NavigatedPart,
    NavigationIntent,
    NavigationResult,
    NavigationTokenBudget,
)
from app.services.lexical_index import (
    LEXICAL_INDEX_FILENAME,
    LexicalCandidate,
    LexicalIndexError,
    search_lexical_index,
)


ModelResolver = Callable[[ModelRole], ModelClient]

_ROOT_SCHEMA = FolderSelection.model_json_schema()
_FOLDER_SCHEMA = DocumentSelectionResult.model_json_schema()
_REQUEST_SCHEMA_OVERHEAD = 128


class NavigationError(RuntimeError):
    """Base safe failure for index navigation."""


class NavigationIndexError(NavigationError):
    """The current index or one of its whitelisted artifacts is invalid."""


class NavigationModelOutputError(NavigationError):
    """The query router returned JSON that violates its Pydantic contract."""


class NavigationBudgetError(NavigationError):
    """A complete structured routing request cannot fit its phase budget."""


class NavigationRoundLimitError(NavigationError):
    """The configured round limit cannot complete the required navigation."""


class NavigationService:
    """Navigate canonical indexes without producing a user-question answer."""

    def __init__(
        self,
        settings: Settings,
        *,
        model_resolver: ModelResolver,
        evidence_model_resolver: ModelResolver | None = None,
    ) -> None:
        self.settings = settings
        self.model_resolver = model_resolver
        self.evidence_model_resolver = evidence_model_resolver
        self._role_prompts = default_role_prompts(ModelRole.QUERY_ROUTER)

    async def navigate(
        self,
        question: str,
        *,
        conversation_history: Sequence[Mapping[str, str]] = (),
    ) -> NavigationResult:
        normalized_question = question.strip()
        if not normalized_question:
            raise NavigationError("The user question must not be blank")
        model_question = _conversation_aware_question(
            normalized_question,
            conversation_history,
        )
        lexical_question = _conversation_aware_lexical_query(
            normalized_question,
            conversation_history,
        )

        root, generation_dir = self._load_current_root()
        client = self.model_resolver(ModelRole.QUERY_ROUTER)
        answer_client = (
            self.evidence_model_resolver(ModelRole.ANSWER_GENERATION)
            if self.evidence_model_resolver is not None
            else None
        )
        self._role_prompts = {
            task: prompt_for_client(client, ModelRole.QUERY_ROUTER, task)
            for task in self._role_prompts
        }
        budget = calculate_navigation_budget(
            client,
            self.settings,
            answer_client=answer_client,
        )
        root_responses = await self._select_folders(
            model_question,
            root,
            client,
            budget,
        )
        intent = _consensus_intent(root_responses)
        if intent is NavigationIntent.SMALL_TALK:
            return NavigationResult(
                intent=intent,
                folders=[],
                documents=[],
                parts=[],
                display_steps=["Classified the request as conversation-only."],
                confidence=1.0,
                need_more_information=False,
                token_budget=budget,
            )
        try:
            lexical_candidates = search_lexical_index(
                generation_dir / LEXICAL_INDEX_FILENAME,
                lexical_question,
                limit=self.settings.lexical_candidate_parts,
                per_document_limit=self.settings.lexical_max_parts_per_document,
            )
        except LexicalIndexError as exc:
            raise NavigationIndexError("The local lexical index is invalid") from exc
        root_entries = {entry["folder_id"]: entry for entry in root["folders"]}
        valid_root_candidates = [
            candidate
            for candidate in lexical_candidates
            if candidate.folder_id in root_entries
        ]
        invalid_lexical_count = len(lexical_candidates) - len(valid_root_candidates)
        selected_folder_ids: list[str] = []
        folder_reasons: dict[str, str] = {}
        for candidate in valid_root_candidates:
            if candidate.folder_id not in folder_reasons:
                selected_folder_ids.append(candidate.folder_id)
                folder_reasons[candidate.folder_id] = (
                    "Local lexical recall found matching indexed terms."
                )
        invalid_folder_count = 0
        for response in root_responses:
            for folder_id in response.selected_folders:
                if folder_id not in root_entries:
                    invalid_folder_count += 1
                    continue
                if folder_id not in folder_reasons:
                    selected_folder_ids.append(folder_id)
                    folder_reasons[folder_id] = response.display_reason

        need_more_information = (
            not selected_folder_ids
            and any(response.need_more_information for response in root_responses)
        )
        display_steps = _public_root_steps(
            root_responses,
            selected_folder_ids,
            invalid_folder_count,
        )
        if valid_root_candidates:
            display_steps.insert(
                0,
                f"Local lexical recall ranked {len(valid_root_candidates)} candidate "
                "Markdown part(s).",
            )
        if invalid_lexical_count:
            display_steps.append(
                f"Ignored {invalid_lexical_count} lexical candidate(s) not present "
                "in the current root index."
            )
        folders = [
            NavigatedFolder(
                folder_id=folder_id,
                source_directory=root_entries[folder_id]["source_directory"],
                summary=root_entries[folder_id]["summary"],
                display_reason=folder_reasons[folder_id],
            )
            for folder_id in selected_folder_ids
        ]

        if not selected_folder_ids or need_more_information:
            return NavigationResult(
                intent=intent,
                folders=folders,
                documents=[],
                parts=[],
                display_steps=display_steps,
                confidence=0.0,
                need_more_information=need_more_information,
                token_budget=budget,
            )
        if self.settings.navigation_max_rounds < 2:
            raise NavigationRoundLimitError(
                "The configured navigation round limit is below the two required phases"
            )

        documents: list[NavigatedDocument] = []
        selected_cards: dict[tuple[str, str], Mapping[str, Any]] = {}
        confidence_values: list[float] = []
        invalid_document_count = 0
        invalid_part_count = 0
        compressed_folder_count = 0
        limit_reached = False
        selected_part_count = 0
        folder_payloads: dict[str, Mapping[str, Any]] = {}
        valid_lexical_candidates: list[LexicalCandidate] = []

        routed_folder_ids = selected_folder_ids[
            : self.settings.navigation_max_selected_documents
        ]
        if len(routed_folder_ids) < len(selected_folder_ids):
            limit_reached = True
        routing_jobs: list[
            tuple[
                str,
                Mapping[str, Any],
                dict[str, Mapping[str, Any]],
                Sequence[Mapping[str, Any]],
            ]
        ] = []
        for folder_id in routed_folder_ids:
            entry = root_entries[folder_id]
            folder = self._load_folder(generation_dir, entry)
            folder_payloads[folder_id] = folder
            folder_candidates = [
                candidate
                for candidate in valid_root_candidates
                if candidate.folder_id == folder_id
            ]
            routing_documents, cards, compressed = self._routing_documents(
                folder,
                budget,
                model_question,
                intent,
                folder_candidates,
            )
            compressed_folder_count += int(compressed)
            selected_cards.update(
                ((folder_id, document_id), card)
                for document_id, card in cards.items()
            )
            for candidate in folder_candidates:
                card = cards.get(candidate.document_id)
                if card is None or candidate.part_id not in {
                    part["part_id"] for part in card["parts"]
                }:
                    invalid_lexical_count += 1
                    continue
                valid_lexical_candidates.append(candidate)
            routing_jobs.append(
                (folder_id, folder, cards, routing_documents)
            )

        routing_results = await asyncio.gather(
            *(
                self._select_documents(
                    model_question,
                    intent,
                    folder,
                    routing_documents,
                    client,
                    budget,
                    self.settings.navigation_max_selected_documents,
                )
                for _folder_id, folder, _cards, routing_documents in routing_jobs
            )
        )

        for (folder_id, folder, cards, _routing_documents), responses in zip(
            routing_jobs,
            routing_results,
            strict=True,
        ):
            if len(documents) >= self.settings.navigation_max_selected_documents:
                limit_reached = True
                break
            document_entries = {
                document["document_id"]: document for document in folder["documents"]
            }
            accepted: dict[str, tuple[list[str], str]] = {}
            for response in responses:
                confidence_values.append(response.confidence)
                for selection in response.selected_documents:
                    document_id = selection.document_id
                    if document_id not in document_entries or document_id not in cards:
                        invalid_document_count += 1
                        continue
                    card = cards[document_id]
                    part_whitelist = {
                        part["part_id"] for part in card["parts"]
                    }
                    valid_parts: list[str] = []
                    for part_id in selection.part_ids:
                        if part_id not in part_whitelist:
                            invalid_part_count += 1
                        elif part_id not in valid_parts:
                            valid_parts.append(part_id)
                    if document_id in accepted:
                        previous_parts, previous_reason = accepted[document_id]
                        for part_id in valid_parts:
                            if part_id not in previous_parts:
                                previous_parts.append(part_id)
                        if not previous_reason and selection.display_reason:
                            accepted[document_id] = (
                                previous_parts,
                                selection.display_reason,
                            )
                    else:
                        accepted[document_id] = (
                            valid_parts,
                            selection.display_reason,
                        )

            for document_id, (part_ids, display_reason) in accepted.items():
                if len(documents) >= self.settings.navigation_max_selected_documents:
                    limit_reached = True
                    break
                document = document_entries[document_id]
                documents.append(
                    NavigatedDocument(
                        folder_id=folder_id,
                        document_id=document_id,
                        source_path=document["source_path"],
                        title=document["title"],
                        document_type=document["document_type"],
                        selected_part_ids=part_ids,
                        display_reason=display_reason,
                    )
                )

        selected_part_count, omitted_selected_parts = _cap_selected_parts_round_robin(
            documents,
            self.settings.navigation_max_selected_parts,
        )
        confidence = min(confidence_values) if confidence_values else 0.0
        lexical_fallback_count = 0
        if (
            intent is NavigationIntent.ANSWER
            and valid_lexical_candidates
            and (
                selected_part_count == 0
                or confidence
                < min(100, self.settings.navigation_low_confidence_percent) / 100
            )
        ):
            lexical_fallback_count = self._apply_lexical_fallback(
                documents,
                selected_cards,
                folder_payloads,
                valid_lexical_candidates,
            )
            selected_part_count += lexical_fallback_count

        if compressed_folder_count:
            display_steps.append(
                f"Split or structurally compressed {compressed_folder_count} large "
                "folder index(es) to stay within the configured folder budget."
            )
        if invalid_document_count:
            display_steps.append(
                f"Ignored {invalid_document_count} document selection(s) not present "
                "in the selected folder indexes."
            )
        if invalid_part_count:
            display_steps.append(
                f"Ignored {invalid_part_count} part selection(s) not present in the "
                "selected document cards."
            )
        if limit_reached:
            display_steps.append(
                "Stopped document selection at the configured maximum of "
                f"{self.settings.navigation_max_selected_documents}."
            )
        if omitted_selected_parts:
            display_steps.append(
                f"Kept the best distributed set of "
                f"{self.settings.navigation_max_selected_parts} Markdown part(s) and "
                f"omitted {omitted_selected_parts} lower-priority selection(s)."
            )
        if lexical_fallback_count:
            display_steps.append(
                f"Added {lexical_fallback_count} locally recalled part(s) because "
                "model routing was empty or low-confidence."
            )
        display_steps.append(
            f"Selected {len(documents)} whitelisted document(s) across "
            f"{len(folders)} folder(s)."
        )

        parts, evidence_omissions = self._load_parts(
            documents,
            selected_cards,
            budget.evidence_budget,
        )
        if parts:
            display_steps.append(
                f"Resolved {len(parts)} whitelisted Markdown part(s)."
            )
        if evidence_omissions:
            display_steps.append(
                f"Kept {evidence_omissions} oversized Markdown part(s) as metadata "
                "without silently truncating their content."
            )

        return NavigationResult(
            intent=intent,
            folders=folders,
            documents=documents,
            parts=parts,
            display_steps=display_steps,
            confidence=confidence,
            need_more_information=False,
            token_budget=budget,
        )

    async def _select_folders(
        self,
        question: str,
        root: Mapping[str, Any],
        client: ModelClient,
        budget: NavigationTokenBudget,
    ) -> list[FolderSelection]:
        entries = list(root["folders"])

        def build_prompt(batch: Sequence[Mapping[str, Any]]) -> str:
            payload = {"folders": list(batch)}
            return _root_prompt(
                question,
                payload,
                instruction=self._role_prompts["folder_selection"],
            )

        prompts = _pack_prompts(entries, build_prompt, budget.root_budget, _ROOT_SCHEMA)
        responses: list[FolderSelection] = []
        generated_responses = await asyncio.gather(
            *(
                client.generate_json(
                    prompt,
                    json_schema=_ROOT_SCHEMA,
                    max_output_tokens=budget.output_reserve,
                    verbosity="low",
                )
                for prompt in prompts
            )
        )
        for generated in generated_responses:
            try:
                responses.append(FolderSelection.model_validate(generated.value))
            except ValidationError as exc:
                raise NavigationModelOutputError(
                    "query_router returned invalid phase-one structured output"
                ) from exc
        return responses

    async def _select_documents(
        self,
        question: str,
        intent: NavigationIntent,
        folder: Mapping[str, Any],
        routing_documents: Sequence[Mapping[str, Any]],
        client: ModelClient,
        budget: NavigationTokenBudget,
        remaining_document_limit: int,
    ) -> list[DocumentSelectionResult]:
        def build_prompt(batch: Sequence[Mapping[str, Any]]) -> str:
            routing_folder = {
                "folder_id": folder["folder_id"],
                "source_directory": folder["source_directory"],
                "summary": folder["summary"],
                "document_count": folder["document_count"],
                "documents": list(batch),
            }
            return _folder_prompt(
                question,
                intent,
                routing_folder,
                remaining_document_limit,
                instruction=self._role_prompts["document_selection"],
            )

        prompts = _pack_prompts(
            routing_documents,
            build_prompt,
            budget.folder_budget,
            _FOLDER_SCHEMA,
        )
        responses: list[DocumentSelectionResult] = []
        generated_responses = await asyncio.gather(
            *(
                client.generate_json(
                    prompt,
                    json_schema=_FOLDER_SCHEMA,
                    max_output_tokens=budget.output_reserve,
                    verbosity="low",
                )
                for prompt in prompts
            )
        )
        for generated in generated_responses:
            try:
                responses.append(
                    DocumentSelectionResult.model_validate(generated.value)
                )
            except ValidationError as exc:
                raise NavigationModelOutputError(
                    "query_router returned invalid phase-two structured output"
                ) from exc
        return responses

    def _load_current_root(self) -> tuple[dict[str, Any], Path]:
        pointer_path = self.settings.index_dir / "current.json"
        pointer = _read_json(pointer_path, "current index pointer")
        if not isinstance(pointer.get("root_index_path"), str):
            raise NavigationIndexError("Current index pointer has no root_index_path")
        root_path = _contained_relative_file(
            self.settings.index_dir,
            pointer["root_index_path"],
            "root index",
        )
        root = _read_json(root_path, "current root index")
        if set(root) != {"folders"} or not isinstance(root["folders"], list):
            raise NavigationIndexError("Current root index has an invalid shape")
        seen: set[str] = set()
        required = {
            "folder_id",
            "source_directory",
            "summary",
            "document_count",
            "index_path",
        }
        enriched = required | {
            "document_types",
            "topics",
            "entities",
            "representative_titles",
        }
        for entry in root["folders"]:
            if not isinstance(entry, Mapping) or set(entry) not in {
                frozenset(required),
                frozenset(enriched),
            }:
                raise NavigationIndexError("Current root folder entry is invalid")
            folder_id = _required_text(entry["folder_id"], "folder id")
            if folder_id in seen:
                raise NavigationIndexError("Current root folder ids are not unique")
            seen.add(folder_id)
            _required_text(entry["source_directory"], "source directory")
            _required_text(entry["summary"], "folder summary")
            if not isinstance(entry["document_count"], int) or entry["document_count"] < 0:
                raise NavigationIndexError("Folder document_count is invalid")
            if set(entry) == enriched:
                for field in (
                    "document_types",
                    "topics",
                    "entities",
                    "representative_titles",
                ):
                    if not isinstance(entry[field], list) or any(
                        not isinstance(value, str) or not value.strip()
                        for value in entry[field]
                    ):
                        raise NavigationIndexError(
                            f"Current root folder {field} is invalid"
                        )
            _safe_relative(entry["index_path"], "folder index path")
        return root, root_path.parent

    def _load_folder(
        self,
        generation_dir: Path,
        root_entry: Mapping[str, Any],
    ) -> dict[str, Any]:
        folder_path = _contained_relative_file(
            generation_dir,
            root_entry["index_path"],
            "folder index",
        )
        folder = _read_json(folder_path, "folder index")
        required = {
            "folder_id",
            "source_directory",
            "summary",
            "document_count",
            "documents",
        }
        if set(folder) != required or not isinstance(folder["documents"], list):
            raise NavigationIndexError("Folder index has an invalid shape")
        if any(
            folder[key] != root_entry[key]
            for key in ("folder_id", "source_directory", "summary", "document_count")
        ):
            raise NavigationIndexError("Root and folder index metadata differ")
        if folder["document_count"] != len(folder["documents"]):
            raise NavigationIndexError("Folder document_count does not match documents")
        expected_document_keys = {
            "document_id",
            "source_path",
            "title",
            "document_type",
            "summary",
            "topics",
            "entities",
            "updated_at",
            "card_path",
        }
        seen: set[str] = set()
        for document in folder["documents"]:
            if not isinstance(document, Mapping) or set(document) != expected_document_keys:
                raise NavigationIndexError("Folder document entry is invalid")
            document_id = _required_text(document["document_id"], "document id")
            if document_id in seen:
                raise NavigationIndexError("Folder document ids are not unique")
            seen.add(document_id)
            for field in ("source_path", "title", "document_type", "summary", "updated_at"):
                _required_text(document[field], field)
            if not isinstance(document["topics"], list) or not isinstance(document["entities"], list):
                raise NavigationIndexError("Folder topics/entities must be arrays")
            expected_card_path = f"md/{document_id}/card.json"
            if document["card_path"] != expected_card_path:
                raise NavigationIndexError("Folder card path does not match its document id")
        return folder

    def _routing_documents(
        self,
        folder: Mapping[str, Any],
        budget: NavigationTokenBudget,
        question: str,
        intent: NavigationIntent,
        lexical_candidates: Sequence[LexicalCandidate],
    ) -> tuple[list[dict[str, Any]], dict[str, Mapping[str, Any]], bool]:
        cards: dict[str, Mapping[str, Any]] = {}
        routing_documents: list[dict[str, Any]] = []
        compressed = False
        documents = list(folder["documents"])
        ranks_by_document: dict[str, int] = {}
        ranks_by_part: dict[tuple[str, str], int] = {}
        for candidate in lexical_candidates:
            ranks_by_document[candidate.document_id] = min(
                candidate.rank,
                ranks_by_document.get(candidate.document_id, candidate.rank),
            )
            ranks_by_part[(candidate.document_id, candidate.part_id)] = candidate.rank
        if ranks_by_document:
            documents = sorted(
                (
                    document
                    for document in documents
                    if document["document_id"] in ranks_by_document
                ),
                key=lambda document: ranks_by_document[document["document_id"]],
            )

        for document in documents:
            document_id = document["document_id"]
            card_path = _contained_relative_file(
                self.settings.data_dir,
                document["card_path"],
                "document card",
            )
            card = _read_json(card_path, "document card")
            _validate_card(card, document_id)
            cards[document_id] = card
            card_parts = list(card["parts"])
            if ranks_by_part:
                direct_indexes = {
                    index
                    for index, part in enumerate(card_parts)
                    if (document_id, part["part_id"]) in ranks_by_part
                }
                context_indexes = {
                    nearby
                    for index in direct_indexes
                    for nearby in (index - 1, index, index + 1)
                    if 0 <= nearby < len(card_parts)
                }
                card_parts = [
                    part
                    for index, part in enumerate(card_parts)
                    if index in context_indexes
                ]
            routing_document = {
                "document_id": document_id,
                "source_path": document["source_path"],
                "title": document["title"],
                "document_type": document["document_type"],
                "summary": document["summary"],
                "topics": document["topics"],
                "entities": document["entities"],
                "parts": [
                    {
                        "part_id": part["part_id"],
                        "label": part["label"],
                        "summary": part["summary"],
                        **(
                            {
                                "lexical_rank": ranks_by_part.get(
                                    (document_id, part["part_id"])
                                ),
                            }
                            if ranks_by_part
                            else {}
                        ),
                    }
                    for part in card_parts
                ],
                **(
                    {"lexical_best_rank": ranks_by_document[document_id]}
                    if document_id in ranks_by_document
                    else {}
                ),
            }
            if self._folder_document_fits(
                folder,
                routing_document,
                budget,
                question,
                intent,
            ):
                routing_documents.append(routing_document)
                continue
            split_documents = self._split_large_routing_document(
                folder,
                routing_document,
                budget,
                question,
                intent,
            )
            routing_documents.extend(split_documents)
            compressed = True
        return routing_documents, cards, compressed

    def _folder_document_fits(
        self,
        folder: Mapping[str, Any],
        document: Mapping[str, Any],
        budget: NavigationTokenBudget,
        question: str,
        intent: NavigationIntent,
    ) -> bool:
        prompt = _folder_prompt(
            question,
            intent,
            {
                "folder_id": folder["folder_id"],
                "source_directory": folder["source_directory"],
                "summary": folder["summary"],
                "document_count": folder["document_count"],
                "documents": [document],
            },
            self.settings.navigation_max_selected_documents,
            instruction=self._role_prompts["document_selection"],
        )
        return _request_tokens(prompt, _FOLDER_SCHEMA) <= budget.folder_budget

    def _split_large_routing_document(
        self,
        folder: Mapping[str, Any],
        document: Mapping[str, Any],
        budget: NavigationTokenBudget,
        question: str,
        intent: NavigationIntent,
    ) -> list[dict[str, Any]]:
        compact = dict(document)
        compact["summary"] = "[summary omitted to fit the folder routing budget]"
        compact["topics"] = []
        compact["entities"] = []
        parts = list(compact["parts"])
        compact["parts"] = []
        result: list[dict[str, Any]] = []
        current: list[dict[str, Any]] = []
        for original_part in parts:
            part = dict(original_part)
            candidate = {**compact, "parts": [*current, part]}
            if self._folder_document_fits(
                folder, candidate, budget, question, intent
            ):
                current.append(part)
                continue
            if current:
                result.append({**compact, "parts": current})
                current = []
            candidate = {**compact, "parts": [part]}
            if not self._folder_document_fits(
                folder, candidate, budget, question, intent
            ):
                part["summary"] = (
                    "[part summary omitted to fit the folder routing budget]"
                )
                candidate = {**compact, "parts": [part]}
            if not self._folder_document_fits(
                folder, candidate, budget, question, intent
            ):
                raise NavigationBudgetError(
                    "One document/part routing record cannot fit the folder budget"
                )
            current = [part]
        if current or not parts:
            candidate = {**compact, "parts": current}
            if not self._folder_document_fits(
                folder, candidate, budget, question, intent
            ):
                raise NavigationBudgetError(
                    "One document routing record cannot fit the folder budget"
                )
            result.append(candidate)
        return result

    def _apply_lexical_fallback(
        self,
        documents: list[NavigatedDocument],
        cards: Mapping[tuple[str, str], Mapping[str, Any]],
        folders: Mapping[str, Mapping[str, Any]],
        candidates: Sequence[LexicalCandidate],
    ) -> int:
        selected_parts = {
            (document.folder_id, document.document_id, part_id)
            for document in documents
            for part_id in document.selected_part_ids
        }
        documents_by_key = {
            (document.folder_id, document.document_id): document
            for document in documents
        }
        remaining_part_slots = max(
            0,
            self.settings.navigation_max_selected_parts - len(selected_parts),
        )
        fallback_limit = min(
            remaining_part_slots,
            self.settings.lexical_fallback_parts,
        )
        added = 0
        for candidate in sorted(candidates, key=lambda value: value.rank):
            if added >= fallback_limit:
                break
            part_key = (
                candidate.folder_id,
                candidate.document_id,
                candidate.part_id,
            )
            if part_key in selected_parts:
                continue
            document_key = (candidate.folder_id, candidate.document_id)
            card = cards.get(document_key)
            if card is None or candidate.part_id not in {
                part["part_id"] for part in card["parts"]
            }:
                continue
            navigated = documents_by_key.get(document_key)
            if navigated is None:
                if len(documents) >= self.settings.navigation_max_selected_documents:
                    continue
                folder = folders.get(candidate.folder_id)
                if folder is None:
                    continue
                source = next(
                    (
                        document
                        for document in folder["documents"]
                        if document["document_id"] == candidate.document_id
                    ),
                    None,
                )
                if source is None:
                    continue
                navigated = NavigatedDocument(
                    folder_id=candidate.folder_id,
                    document_id=candidate.document_id,
                    source_path=source["source_path"],
                    title=source["title"],
                    document_type=source["document_type"],
                    selected_part_ids=[],
                    display_reason="Local lexical fallback selected an exact-term match.",
                )
                documents.append(navigated)
                documents_by_key[document_key] = navigated
            navigated.selected_part_ids.append(candidate.part_id)
            selected_parts.add(part_key)
            added += 1
        return added

    def _load_parts(
        self,
        documents: Sequence[NavigatedDocument],
        cards: Mapping[tuple[str, str], Mapping[str, Any]],
        evidence_budget: int,
    ) -> tuple[list[NavigatedPart], int]:
        result: list[NavigatedPart] = []
        used_tokens = 0
        omissions = 0
        for document in documents:
            card = cards[(document.folder_id, document.document_id)]
            parts = {part["part_id"]: part for part in card["parts"]}
            for part_id in document.selected_part_ids:
                part = parts[part_id]
                md_path = _contained_relative_file(
                    self.settings.data_dir,
                    part["md_path"],
                    "Markdown part",
                )
                expected_parent = (
                    self.settings.markdown_dir / document.document_id
                ).resolve(strict=False)
                if md_path.parent.resolve(strict=False) != expected_parent:
                    raise NavigationIndexError(
                        "Markdown part path does not match its document id"
                    )
                try:
                    content = md_path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise NavigationIndexError(
                        "Markdown part is not readable UTF-8 text"
                    ) from exc
                # The answer request embeds Markdown inside JSON, so estimate
                # the serialized representation rather than the raw file.
                part_tokens = estimate_tokens(
                    json.dumps(content, ensure_ascii=False, separators=(",", ":"))
                )
                within_budget = used_tokens + part_tokens <= evidence_budget
                if within_budget:
                    used_tokens += part_tokens
                    included_content: str | None = content
                else:
                    omissions += 1
                    included_content = None
                result.append(
                    NavigatedPart(
                        folder_id=document.folder_id,
                        document_id=document.document_id,
                        part_id=part_id,
                        label=part["label"],
                        summary=part["summary"],
                        md_path=part["md_path"],
                        source_anchors=part["source_anchors"],
                        content=included_content,
                        estimated_tokens=part_tokens,
                        within_evidence_budget=within_budget,
                    )
                )
        return result, omissions


def _cap_selected_parts_round_robin(
    documents: Sequence[NavigatedDocument],
    limit: int,
) -> tuple[int, int]:
    """Cap parts without allowing the first routed batch to monopolize evidence."""
    original_counts = [len(document.selected_part_ids) for document in documents]
    kept: list[list[str]] = [[] for _document in documents]
    selected = 0
    depth = 0
    while selected < limit:
        added_at_depth = False
        for index, document in enumerate(documents):
            if depth >= len(document.selected_part_ids):
                continue
            kept[index].append(document.selected_part_ids[depth])
            selected += 1
            added_at_depth = True
            if selected >= limit:
                break
        if not added_at_depth:
            break
        depth += 1

    for document, selected_ids in zip(documents, kept, strict=True):
        document.selected_part_ids = selected_ids
    omitted = max(0, sum(original_counts) - selected)
    return selected, omitted


def calculate_navigation_budget(
    client: ModelClient,
    settings: Settings,
    *,
    answer_client: ModelClient | None = None,
) -> NavigationTokenBudget:
    """Allocate independent router-call and answer-evidence token budgets."""
    profile_context_window = _positive_model_limit(
        getattr(client, "context_window", None),
        settings.query_router_context_tokens,
    )
    context_window = min(
        profile_context_window,
        settings.query_router_context_tokens,
    )
    profile_max_output_tokens = _positive_model_limit(
        getattr(client, "max_output_tokens", None),
        settings.navigation_default_max_output_tokens,
    )
    output_reserve = min(
        profile_max_output_tokens,
        settings.navigation_default_max_output_tokens,
        max(1, context_window // 4),
    )
    router_available = context_window - output_reserve
    if router_available < 12:
        raise NavigationBudgetError("The query-router context window is too small")
    router_safety_reserve = _context_safety_reserve(
        router_available,
        settings.navigation_context_safety_percent,
    )
    router_input_budget = router_available - router_safety_reserve
    if router_input_budget < 1:
        raise NavigationBudgetError("The query-router input budget is too small")
    root_budget = min(
        router_input_budget,
        settings.navigation_root_input_token_cap,
    )
    folder_budget = min(
        router_input_budget,
        settings.navigation_folder_input_token_cap,
    )

    evidence_client = answer_client or client
    answer_profile_context_window = _positive_model_limit(
        getattr(evidence_client, "context_window", None),
        settings.answer_context_tokens,
    )
    answer_context_window = min(
        answer_profile_context_window,
        settings.answer_context_tokens,
    )
    answer_profile_output = _positive_model_limit(
        getattr(evidence_client, "max_output_tokens", None),
        settings.answer_max_output_tokens,
    )
    answer_output_reserve = min(
        answer_profile_output,
        settings.answer_max_output_tokens,
        max(1, answer_context_window // 4),
    )
    answer_available = answer_context_window - answer_output_reserve
    if answer_available < 12:
        raise NavigationBudgetError("The answer-model context window is too small")
    answer_safety_reserve = _context_safety_reserve(
        answer_available,
        settings.navigation_context_safety_percent,
    )
    evidence_budget = answer_available - answer_safety_reserve
    if evidence_budget < 1:
        raise NavigationBudgetError("The answer evidence budget is too small")
    return NavigationTokenBudget(
        context_window=context_window,
        root_budget=root_budget,
        folder_budget=folder_budget,
        evidence_budget=evidence_budget,
        output_reserve=output_reserve,
        answer_context_window=answer_context_window,
        answer_output_reserve=answer_output_reserve,
        router_safety_reserve=router_safety_reserve,
        answer_safety_reserve=answer_safety_reserve,
    )


def estimate_tokens(value: str) -> int:
    """Conservative tokenizer-independent estimate suitable for batching."""
    if not value:
        return 0
    return max(1, math.ceil(len(value.encode("utf-8")) / 3))


def _conversation_aware_question(
    question: str,
    history: Sequence[Mapping[str, str]],
) -> str:
    if not history:
        return question
    return json.dumps(
        {
            "conversation_history": [dict(message) for message in history],
            "current_user_question": question,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _conversation_aware_lexical_query(
    question: str,
    history: Sequence[Mapping[str, str]],
) -> str:
    prior_user_questions = [
        content.strip()
        for message in history
        if message.get("role") == "user"
        and isinstance((content := message.get("content")), str)
        and content.strip()
    ]
    return "\n".join([*prior_user_questions[-3:], question])


def _positive_model_limit(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default


def _context_safety_reserve(available: int, percent: int) -> int:
    """Reserve a bounded margin for protocol/tokenizer estimation drift."""
    bounded_percent = min(max(percent, 1), 25)
    return min(
        max(1_024, available * bounded_percent // 100),
        max(1, available // 4),
    )


def _root_prompt(
    question: str,
    root: Mapping[str, Any],
    *,
    instruction: str | None = None,
) -> str:
    return (
        f"{instruction or default_role_prompts(ModelRole.QUERY_ROUTER)['folder_selection']}\n\n"
        "Mandatory intent contract: return one of answer, download, list_files, or "
        "small_talk. Use small_talk only for a purely social/casual request that "
        "needs no knowledge-base fact; then select no folders and set "
        "need_more_information to false.\n\n"
        f"user_question:\n{question}\n\n"
        "current_root_json:\n"
        f"{json.dumps(root, ensure_ascii=False, separators=(',', ':'))}"
    )


def _folder_prompt(
    question: str,
    intent: NavigationIntent,
    folder: Mapping[str, Any],
    remaining_document_limit: int,
    *,
    instruction: str | None = None,
) -> str:
    return (
        f"{instruction or default_role_prompts(ModelRole.QUERY_ROUTER)['document_selection']}\n"
        f"本次最多选择 {remaining_document_limit} 个文档。\n\n"
        "If lexical_rank is present, a smaller number is a stronger local exact-term "
        "match; a null lexical_rank is adjacent context rather than a direct hit. "
        "Rerank the supplied candidates against the whole question.\n\n"
        f"intent: {intent.value}\n"
        f"user_question:\n{question}\n\n"
        "folder_index:\n"
        f"{json.dumps(folder, ensure_ascii=False, separators=(',', ':'))}"
    )


def _pack_prompts(
    items: Sequence[Mapping[str, Any]],
    prompt_builder: Callable[[Sequence[Mapping[str, Any]]], str],
    input_budget: int,
    schema: Mapping[str, Any],
) -> list[str]:
    if not items:
        prompt = prompt_builder([])
        if _request_tokens(prompt, schema) > input_budget:
            raise NavigationBudgetError("The routing prompt cannot fit its phase budget")
        return [prompt]

    batches: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    for item in items:
        candidate = [*current, item]
        if _request_tokens(prompt_builder(candidate), schema) <= input_budget:
            current = candidate
            continue
        if current:
            batches.append(current)
            current = []
        if _request_tokens(prompt_builder([item]), schema) > input_budget:
            raise NavigationBudgetError(
                "One structured index entry cannot fit its phase budget"
            )
        current = [item]
    if current:
        batches.append(current)
    return [prompt_builder(batch) for batch in batches]


def _request_tokens(prompt: str, schema: Mapping[str, Any]) -> int:
    schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    return estimate_tokens(prompt) + estimate_tokens(schema_text) + _REQUEST_SCHEMA_OVERHEAD


def _consensus_intent(responses: Sequence[FolderSelection]) -> NavigationIntent:
    counts = Counter(response.intent for response in responses)
    return max(counts, key=lambda intent: counts[intent])


def _public_root_steps(
    responses: Sequence[FolderSelection],
    selected_folder_ids: Sequence[str],
    invalid_folder_count: int,
) -> list[str]:
    steps: list[str] = []
    for response in responses:
        reason = response.display_reason.strip()
        if reason and reason not in steps:
            steps.append(reason)
    if invalid_folder_count:
        steps.append(
            f"Ignored {invalid_folder_count} folder selection(s) not present in the "
            "current root index."
        )
    steps.append(
        f"Selected {len(selected_folder_ids)} whitelisted folder(s) from the current index."
    )
    return steps


def _validate_card(card: Mapping[str, Any], document_id: str) -> None:
    expected = {
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
    if set(card) != expected or card.get("document_id") != document_id:
        raise NavigationIndexError("Document card has an invalid shape or id")
    if not isinstance(card["parts"], list):
        raise NavigationIndexError("Document card parts must be an array")
    seen: set[str] = set()
    expected_part_keys = {
        "part_id",
        "label",
        "summary",
        "md_path",
        "source_anchors",
    }
    for part in card["parts"]:
        if not isinstance(part, Mapping) or set(part) != expected_part_keys:
            raise NavigationIndexError("Document card part has an invalid shape")
        part_id = _required_text(part["part_id"], "part id")
        if part_id in seen:
            raise NavigationIndexError("Document card part ids are not unique")
        seen.add(part_id)
        _required_text(part["label"], "part label")
        _required_text(part["summary"], "part summary")
        expected_prefix = PurePosixPath("md") / document_id
        relative = _safe_relative(part["md_path"], "Markdown part path")
        if relative.parent != expected_prefix or relative.suffix != ".md":
            raise NavigationIndexError("Markdown part path does not match its document id")
        if not isinstance(part["source_anchors"], list) or not all(
            isinstance(anchor, Mapping) for anchor in part["source_anchors"]
        ):
            raise NavigationIndexError("Markdown source_anchors must be objects")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise NavigationIndexError(f"{label} is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NavigationIndexError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise NavigationIndexError(f"{label} must be a JSON object")
    return value


def _contained_relative_file(root: Path, value: Any, label: str) -> Path:
    relative = _safe_relative(value, label)
    root_resolved = root.resolve(strict=False)
    path = root / Path(*relative.parts)
    path_resolved = path.resolve(strict=False)
    if path_resolved == root_resolved or root_resolved not in path_resolved.parents:
        raise NavigationIndexError(f"{label} escapes its configured root")
    if path.is_symlink() or not path.is_file():
        raise NavigationIndexError(f"{label} is missing or unsafe")
    return path


def _safe_relative(value: Any, label: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise NavigationIndexError(f"{label} must be text")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise NavigationIndexError(f"{label} must be a safe relative path")
    return path


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NavigationIndexError(f"{label} must be non-empty text")
    return value.strip()
