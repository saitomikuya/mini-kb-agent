"""Incremental document cards and immutable hierarchical JSON indexes."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, text as sql_text, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.indexes import IndexGenerationStatus
from app.llm.clients import ModelClient
from app.llm.prompts import default_role_prompts, prompt_for_client
from app.llm.registry import ModelRegistryError
from app.llm.types import ModelRole
from app.models.index_generation import IndexGeneration
from app.models.source_file import SourceFile
from app.services.lexical_index import (
    LEXICAL_INDEX_FILENAME,
    LexicalIndexError,
    LexicalPartRecord,
    build_lexical_index,
    validate_lexical_index,
)
from app.source_files import ConversionStatus, IndexStatus, SourceStatus


MAX_CARD_SUMMARY_CHARS = 4_000
MAX_PART_SUMMARY_CHARS = 2_000
MAX_FOLDER_SUMMARY_CHARS = 800
MAX_FOLDER_DOCUMENT_SUMMARY_CHARS = 600
MAX_FOLDER_INDEX_BYTES = 1_000_000
MAX_ROOT_INDEX_BYTES = 4_000_000
MAX_ROOT_DOCUMENT_TYPES = 64
MAX_ROOT_TOPICS = 128
MAX_ROOT_ENTITIES = 128
MAX_ROOT_REPRESENTATIVE_TITLES = 64
INDEX_CARD_CACHE_VERSION = "index-card-v2"
INDEX_BATCH_CACHE_VERSION = "index-part-batch-v1"
INDEX_MODEL_CONCURRENCY = 4
INDEX_MODEL_HEARTBEAT_SECONDS = 5.0
INDEX_MODEL_MAX_ATTEMPTS = 2
INDEX_SMALL_DOCUMENT_MAX_CHARS = 60_000
INDEX_PART_BATCH_MAX_CHARS = 30_000
INDEX_PART_BATCH_MAX_PARTS = 24
INDEX_CARD_MAX_OUTPUT_TOKENS = 4_096
INDEX_BATCH_MAX_OUTPUT_TOKENS = 8_192
INDEX_METADATA_MAX_OUTPUT_TOKENS = 2_048
INDEX_REASONING_EFFORT = "low"

_INDEX_DEFAULT_PROMPTS = default_role_prompts(ModelRole.INDEX_GENERATION)

Heartbeat = Callable[[], None]
ModelResolver = Callable[[ModelRole], ModelClient]
ActivationHook = Callable[[int], None]
ProgressReporter = Callable[[Mapping[str, Any]], None]


class IndexGenerationError(RuntimeError):
    """Base expected failure while building a non-current generation."""


class MarkdownArtifactError(IndexGenerationError):
    """A READY source record did not have a matching complete artifact."""


class DocumentCardError(IndexGenerationError):
    """The index model did not produce a valid document card."""


class IndexValidationError(IndexGenerationError):
    """A staged canonical JSON index failed structural validation."""


@dataclass(frozen=True, slots=True)
class IndexBuildResult:
    generation_number: int
    root_index_path: str
    document_count: int


@dataclass(frozen=True, slots=True)
class ArtifactPart:
    part_id: str
    filename: str
    anchors: Mapping[str, Any]
    markdown: str


@dataclass(frozen=True, slots=True)
class CurrentFolder:
    entry: Mapping[str, Any]
    payload: Mapping[str, Any]
    json_path: Path
    md_path: Path


@dataclass(frozen=True, slots=True)
class DocumentPlan:
    record: SourceFile
    parts: tuple[ArtifactPart, ...]
    batches: tuple[tuple[ArtifactPart, ...], ...]
    cached_batches: Mapping[int, Mapping[str, Any]]
    hierarchical: bool

    @property
    def is_small(self) -> bool:
        return not self.hierarchical


CARD_MODEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "title",
        "document_type",
        "summary",
        "topics",
        "entities",
        "parts",
    ],
    "properties": {
        "title": {"type": "string"},
        "document_type": {"type": "string"},
        "summary": {"type": "string"},
        "topics": {"type": "array", "items": {"type": "string"}},
        "entities": {"type": "array", "items": {"type": "string"}},
        "parts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["part_id", "label", "summary"],
                "properties": {
                    "part_id": {"type": "string"},
                    "label": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
        },
    },
}

DOCUMENT_METADATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "document_type", "summary", "topics", "entities"],
    "properties": {
        "title": {"type": "string"},
        "document_type": {"type": "string"},
        "summary": {"type": "string"},
        "topics": {"type": "array", "items": {"type": "string"}},
        "entities": {"type": "array", "items": {"type": "string"}},
    },
}


def _part_batch_schema(part_count: int) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["batch_summary", "parts"],
        "properties": {
            "batch_summary": {"type": "string"},
            "parts": {
                "type": "array",
                "minItems": part_count,
                "maxItems": part_count,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["part_id", "label", "summary"],
                    "properties": {
                        "part_id": {"type": "string"},
                        "label": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                },
            },
        },
    }


class IndexGenerationService:
    """Build one isolated generation and make it current only after validation."""

    def __init__(
        self,
        settings: Settings,
        session: Session,
        *,
        model_resolver: ModelResolver,
        activation_hook: ActivationHook | None = None,
    ) -> None:
        self.settings = settings
        self.session = session
        self.model_resolver = model_resolver
        self.activation_hook = activation_hook
        self._progress_reporter: ProgressReporter = lambda _progress: None
        self._progress_state: dict[str, Any] = {}

    def build_and_activate(
        self,
        *,
        heartbeat: Heartbeat = lambda: None,
        progress: ProgressReporter = lambda _progress: None,
    ) -> IndexBuildResult:
        """Build, validate, and atomically activate a canonical JSON generation."""
        self._progress_reporter = progress
        self._progress_state = {"kind": "index", "phase": "preparing"}
        generation = self._create_generation()
        number = generation.generation_number
        staging_dir = self._staging_dir(number)
        final_dir = self._generation_dir(number)
        activated = False

        try:
            staging_dir.mkdir(parents=True, exist_ok=False)
            heartbeat()
            self._emit_progress(
                phase="inventory",
                generation_number=number,
                total_documents=0,
                documents_to_refresh=0,
                documents_reused=0,
                document_card_cache_hits=0,
                documents_completed=0,
                model_requests_total=0,
                model_requests_completed=0,
                model_cache_hits=0,
            )
            records = self.session.scalars(
                select(SourceFile).order_by(SourceFile.relative_path, SourceFile.id)
            ).all()
            eligible = [record for record in records if _is_eligible(record)]
            eligible_snapshot = {
                record.id: (record.relative_path, record.sha256)
                for record in eligible
            }
            refreshed_ids = {
                record.id
                for record in eligible
                if record.index_status != IndexStatus.INDEXED
            }

            client: ModelClient | None = None
            if refreshed_ids:
                try:
                    client = self.model_resolver(ModelRole.INDEX_GENERATION)
                except ModelRegistryError as exc:
                    raise DocumentCardError(
                        "ModelRole.INDEX_GENERATION is not configured with a usable "
                        "text model"
                    ) from exc

            cards: dict[int, dict[str, Any]] = {}
            artifacts: dict[int, tuple[ArtifactPart, ...]] = {}
            for record in eligible:
                heartbeat()
                parts = tuple(self._load_artifact(record))
                artifacts[record.id] = parts
                if record.id not in refreshed_ids:
                    card = self._load_existing_card(record)
                    self._validate_card(record, parts, card)
                    cards[record.id] = card

            model_key = _index_model_cache_key(client) if client is not None else ""
            plans: list[DocumentPlan] = []
            document_card_cache_hits = 0
            batch_cache_hits = 0
            model_requests_total = 0
            for record in eligible:
                if record.id not in refreshed_ids:
                    continue
                if client is None:  # pragma: no cover - guarded above
                    raise DocumentCardError("The index_generation model is unavailable")
                parts = artifacts[record.id]
                cached_card = self._load_cached_card(record, parts, model_key)
                if cached_card is not None:
                    self._validate_card(record, parts, cached_card)
                    cards[record.id] = cached_card
                    document_card_cache_hits += 1
                    continue
                plan = self._plan_document(record, parts, model_key)
                plans.append(plan)
                batch_cache_hits += len(plan.cached_batches)
                model_requests_total += (
                    1
                    if plan.is_small
                    else len(plan.batches) - len(plan.cached_batches) + 1
                )

            documents_reused = len(eligible) - len(refreshed_ids)
            self._emit_progress(
                phase="document_cards" if plans else "folder_indexes",
                total_documents=len(eligible),
                documents_to_refresh=len(refreshed_ids),
                documents_reused=documents_reused,
                document_card_cache_hits=document_card_cache_hits,
                documents_completed=documents_reused + document_card_cache_hits,
                model_requests_total=model_requests_total,
                model_requests_completed=0,
                model_cache_hits=batch_cache_hits + document_card_cache_hits,
            )
            if plans:
                generated_cards = asyncio.run(
                    self._generate_planned_cards(
                        plans,
                        client,
                        model_key,
                        heartbeat,
                        initially_completed=documents_reused
                        + document_card_cache_hits,
                        model_requests_total=model_requests_total,
                        initial_cache_hits=batch_cache_hits
                        + document_card_cache_hits,
                    )
                )
                cards.update(generated_cards)

            for record in eligible:
                parts = artifacts[record.id]
                card = cards[record.id]
                self._validate_card(record, parts, card)

            current_folders = self._load_current_folders()
            grouped = _group_cards(eligible, cards)
            folder_payloads: dict[str, dict[str, Any]] = {}
            folders_reused = 0
            folders_rebuilt = 0
            self._emit_progress(
                phase="folder_indexes",
                current_document_id=None,
                current_document_name=None,
                total_folders=len(grouped),
                folders_completed=0,
                folders_reused=0,
                folders_rebuilt=0,
            )
            for folder_number, (source_directory, group) in enumerate(
                sorted(grouped.items()),
                start=1,
            ):
                heartbeat()
                folder_id = folder_id_for_source_directory(source_directory)
                current = current_folders.get(source_directory)
                desired_ids = {record.id for record, _card in group}
                current_ids = (
                    {
                        int(document["document_id"])
                        for document in current.payload["documents"]
                    }
                    if current is not None
                    else set()
                )
                can_reuse = (
                    current is not None
                    and current.entry["folder_id"] == folder_id
                    and desired_ids == current_ids
                    and refreshed_ids.isdisjoint(desired_ids)
                )
                if can_reuse:
                    payload = dict(current.payload)
                    self._copy_current_folder(current, staging_dir, folder_id)
                    folders_reused += 1
                else:
                    payload = self._build_folder_payload(
                        folder_id,
                        source_directory,
                        group,
                    )
                    self._write_folder(staging_dir, payload)
                    folders_rebuilt += 1
                folder_payloads[source_directory] = payload
                self._emit_progress(
                    folders_completed=folder_number,
                    folders_reused=folders_reused,
                    folders_rebuilt=folders_rebuilt,
                )

            root = self._build_root(folder_payloads)
            _write_json(staging_dir / "root.json", root, MAX_ROOT_INDEX_BYTES)
            _write_text(staging_dir / "root.md", _render_root_preview(root))
            self._emit_progress(phase="lexical_index")
            self._build_lexical_index(
                staging_dir,
                eligible,
                cards,
                artifacts,
            )
            self._emit_progress(phase="validating")
            self._validate_generation(staging_dir, eligible, cards)

            generation.status = IndexGenerationStatus.VALIDATED
            generation.document_count = len(eligible)
            self.session.commit()
            heartbeat()

            self._emit_progress(phase="publishing")
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            _assert_within(self.settings.index_dir / "generations", final_dir)
            if os.path.lexists(final_dir):
                raise IndexGenerationError(
                    f"Generation directory already exists: {number}"
                )
            os.replace(staging_dir, final_dir)
            _fsync_directory(final_dir.parent)
            if self.activation_hook is not None:
                self.activation_hook(number)
            self._activate(generation, eligible_snapshot)
            activated = True
            self._emit_progress(
                phase="completed",
                current_document_id=None,
                current_document_name=None,
                documents_completed=len(eligible),
            )
            return IndexBuildResult(
                generation_number=number,
                root_index_path=generation.root_index_path,
                document_count=len(eligible),
            )
        except BaseException as exc:
            self.session.rollback()
            if not activated:
                self._mark_failed(number)
            if os.path.lexists(staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True)
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, IndexGenerationError):
                raise
            raise IndexGenerationError("Index generation failed before activation") from exc

    def _emit_progress(self, **updates: Any) -> None:
        self._progress_state.update(updates)
        self._progress_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._progress_reporter(dict(self._progress_state))

    def _create_generation(self) -> IndexGeneration:
        self.settings.index_dir.mkdir(parents=True, exist_ok=True)
        self.session.execute(
            update(IndexGeneration)
            .where(
                IndexGeneration.status.in_(
                    (
                        IndexGenerationStatus.BUILDING,
                        IndexGenerationStatus.VALIDATED,
                    )
                )
            )
            .values(status=IndexGenerationStatus.FAILED)
        )
        latest = self.session.scalar(select(func.max(IndexGeneration.generation_number)))
        number = int(latest or 0) + 1
        generation = IndexGeneration(
            generation_number=number,
            status=IndexGenerationStatus.BUILDING,
            root_index_path=f"generations/{number}/root.json",
            document_count=0,
        )
        self.session.add(generation)
        self.session.commit()
        return generation

    def _mark_failed(self, number: int) -> None:
        try:
            generation = self.session.get(IndexGeneration, number)
            if generation is not None and generation.status != IndexGenerationStatus.ACTIVE:
                generation.status = IndexGenerationStatus.FAILED
                generation.activated_at = None
                self.session.commit()
        except Exception:
            self.session.rollback()

    def _load_artifact(self, record: SourceFile) -> list[ArtifactPart]:
        artifact_dir = self.settings.markdown_dir / str(record.id)
        _assert_within(self.settings.markdown_dir, artifact_dir)
        manifest_path = artifact_dir / "manifest.json"
        if artifact_dir.is_symlink() or manifest_path.is_symlink():
            raise MarkdownArtifactError(
                f"Document {record.id} has an unsafe Markdown artifact"
            )
        manifest = _read_json(manifest_path, "Markdown manifest")
        if (
            manifest.get("status") != "READY"
            or str(manifest.get("document_id")) != str(record.id)
            or manifest.get("source_path") != record.relative_path
            or manifest.get("source_sha256") != record.sha256
        ):
            raise MarkdownArtifactError(
                f"Document {record.id} Markdown manifest does not match the READY source"
            )
        manifest_parts = manifest.get("parts")
        if not isinstance(manifest_parts, list) or not manifest_parts:
            raise MarkdownArtifactError(
                f"Document {record.id} Markdown manifest has no parts"
            )

        parts: list[ArtifactPart] = []
        seen: set[str] = set()
        for raw_part in manifest_parts:
            if not isinstance(raw_part, Mapping):
                raise MarkdownArtifactError("Markdown manifest part must be an object")
            part_id = _required_text(raw_part.get("part_id"), "manifest part_id", 100)
            filename = _safe_generated_filename(raw_part.get("path"))
            anchors = raw_part.get("anchors")
            if not isinstance(anchors, Mapping):
                raise MarkdownArtifactError("Markdown manifest anchors must be an object")
            if part_id in seen:
                raise MarkdownArtifactError("Markdown manifest part ids must be unique")
            seen.add(part_id)
            part_path = artifact_dir / filename
            _assert_within(artifact_dir, part_path)
            if part_path.is_symlink() or not part_path.is_file():
                raise MarkdownArtifactError(f"Markdown part is missing: {filename}")
            part_bytes = part_path.read_bytes()
            if hashlib.sha256(part_bytes).hexdigest() != raw_part.get("sha256"):
                raise MarkdownArtifactError(f"Markdown part hash mismatch: {filename}")
            try:
                markdown = part_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MarkdownArtifactError(
                    f"Markdown part is not UTF-8: {filename}"
                ) from exc
            parts.append(
                ArtifactPart(
                    part_id=part_id,
                    filename=filename,
                    anchors=dict(anchors),
                    markdown=markdown,
                )
            )
        return parts

    def _plan_document(
        self,
        record: SourceFile,
        parts: tuple[ArtifactPart, ...],
        model_key: str,
    ) -> DocumentPlan:
        total_characters = sum(len(part.markdown) for part in parts)
        hierarchical = (
            total_characters > INDEX_SMALL_DOCUMENT_MAX_CHARS
            or len(parts) > INDEX_PART_BATCH_MAX_PARTS
        )
        batches = (
            tuple(_batch_index_parts(parts))
            if hierarchical
            else (parts,)
        )
        cached_batches: dict[int, Mapping[str, Any]] = {}
        if hierarchical:
            for batch_number, batch in enumerate(batches, start=1):
                cache_path = self._part_batch_cache_path(
                    record,
                    batch,
                    model_key,
                )
                cached = _read_optional_json(cache_path)
                if cached is None:
                    continue
                try:
                    cached_batches[batch_number] = _normalize_part_batch(
                        batch,
                        cached,
                    )
                except DocumentCardError:
                    continue
        return DocumentPlan(
            record=record,
            parts=parts,
            batches=batches,
            cached_batches=cached_batches,
            hierarchical=hierarchical,
        )

    async def _generate_planned_cards(
        self,
        plans: Sequence[DocumentPlan],
        client: ModelClient,
        model_key: str,
        heartbeat: Heartbeat,
        *,
        initially_completed: int,
        model_requests_total: int,
        initial_cache_hits: int,
    ) -> dict[int, dict[str, Any]]:
        semaphore = asyncio.Semaphore(INDEX_MODEL_CONCURRENCY)
        documents_completed = initially_completed
        model_requests_completed = 0
        role_prompts = {
            task: prompt_for_client(client, ModelRole.INDEX_GENERATION, task)
            for task in _INDEX_DEFAULT_PROMPTS
        }

        async def request_json(
            prompt: str,
            schema: Mapping[str, Any],
            *,
            max_output_tokens: int,
        ) -> Mapping[str, Any]:
            nonlocal model_requests_completed
            generated = None
            async with semaphore:
                for attempt in range(INDEX_MODEL_MAX_ATTEMPTS):
                    try:
                        generated = await _await_index_model(
                            client.generate_json(
                                prompt,
                                json_schema=schema,
                                max_output_tokens=max_output_tokens,
                                reasoning_effort=INDEX_REASONING_EFFORT,
                            ),
                            heartbeat,
                        )
                        break
                    except Exception:
                        if attempt + 1 >= INDEX_MODEL_MAX_ATTEMPTS:
                            raise
                        heartbeat()
                        await asyncio.sleep(1.0 * (2**attempt))
            if generated is None:  # pragma: no cover - loop guard
                raise AssertionError("index model retry loop did not return")
            if not isinstance(generated.value, Mapping):
                raise DocumentCardError(
                    "index_generation model returned no JSON object"
                )
            model_requests_completed += 1
            self._emit_progress(
                model_requests_total=model_requests_total,
                model_requests_completed=model_requests_completed,
                model_cache_hits=initial_cache_hits,
            )
            return generated.value

        async def generate_one(plan: DocumentPlan) -> tuple[int, dict[str, Any]]:
            nonlocal documents_completed
            record = plan.record
            self._emit_progress(
                current_document_id=record.id,
                current_document_name=record.relative_path,
                current_document_parts=len(plan.parts),
                current_document_batches=len(plan.batches),
            )
            heartbeat()
            try:
                if plan.is_small:
                    value = await request_json(
                        _card_prompt(
                            record,
                            plan.parts,
                            instruction=role_prompts["document_card"],
                        ),
                        CARD_MODEL_SCHEMA,
                        max_output_tokens=INDEX_CARD_MAX_OUTPUT_TOKENS,
                    )
                    card = _normalize_model_card(record, plan.parts, value)
                else:
                    batch_results: list[Mapping[str, Any] | None] = [
                        None
                    ] * len(plan.batches)
                    for batch_number, cached in plan.cached_batches.items():
                        batch_results[batch_number - 1] = cached

                    async def summarize_batch(
                        batch_index: int,
                        batch: tuple[ArtifactPart, ...],
                    ) -> None:
                        value = await request_json(
                            _part_batch_prompt(
                                record,
                                batch,
                                instruction=role_prompts["part_batch"],
                            ),
                            _part_batch_schema(len(batch)),
                            max_output_tokens=min(
                                INDEX_BATCH_MAX_OUTPUT_TOKENS,
                                max(2_048, len(batch) * 384),
                            ),
                        )
                        normalized = _normalize_part_batch(batch, value)
                        batch_results[batch_index] = normalized
                        _atomic_write_json(
                            self._part_batch_cache_path(
                                record,
                                batch,
                                model_key,
                            ),
                            normalized,
                        )

                    await asyncio.gather(
                        *(
                            summarize_batch(batch_index, batch)
                            for batch_index, batch in enumerate(plan.batches)
                            if batch_index + 1 not in plan.cached_batches
                        )
                    )
                    normalized_batches = [
                        batch
                        for batch in batch_results
                        if batch is not None
                    ]
                    if len(normalized_batches) != len(plan.batches):
                        raise DocumentCardError(
                            "index_generation did not produce every part batch"
                        )
                    metadata = await request_json(
                        _document_metadata_prompt(
                            record,
                            normalized_batches,
                            instruction=role_prompts["document_metadata"],
                        ),
                        DOCUMENT_METADATA_SCHEMA,
                        max_output_tokens=INDEX_METADATA_MAX_OUTPUT_TOKENS,
                    )
                    combined_parts = [
                        part
                        for batch in normalized_batches
                        for part in batch["parts"]
                    ]
                    card = _normalize_model_card(
                        record,
                        plan.parts,
                        {**metadata, "parts": combined_parts},
                    )
            except DocumentCardError:
                raise
            except Exception as exc:
                raise DocumentCardError(
                    f"index_generation model failed for document {record.id}"
                ) from exc

            _atomic_write_json(self._card_path(record.id), card)
            _atomic_write_json(
                self._card_meta_path(record.id),
                _card_cache_metadata(record, plan.parts, model_key),
            )
            documents_completed += 1
            self._emit_progress(
                documents_completed=documents_completed,
                current_document_id=record.id,
                current_document_name=record.relative_path,
                current_document_parts=len(plan.parts),
                current_document_batches=len(plan.batches),
            )
            heartbeat()
            return record.id, card

        generated = await asyncio.gather(*(generate_one(plan) for plan in plans))
        return dict(generated)

    def _load_cached_card(
        self,
        record: SourceFile,
        parts: Sequence[ArtifactPart],
        model_key: str,
    ) -> dict[str, Any] | None:
        metadata = _read_optional_json(self._card_meta_path(record.id))
        if metadata != _card_cache_metadata(record, parts, model_key):
            return None
        card = _read_optional_json(self._card_path(record.id))
        return dict(card) if card is not None else None

    def _card_meta_path(self, document_id: int) -> Path:
        path = self.settings.markdown_dir / str(document_id) / "card.meta.json"
        _assert_within(self.settings.markdown_dir, path)
        return path

    def _part_batch_cache_path(
        self,
        record: SourceFile,
        parts: Sequence[ArtifactPart],
        model_key: str,
    ) -> Path:
        digest = hashlib.sha256()
        digest.update(INDEX_BATCH_CACHE_VERSION.encode("utf-8"))
        digest.update(b"\0")
        digest.update(model_key.encode("utf-8"))
        digest.update(b"\0")
        digest.update(record.sha256.encode("ascii"))
        for part in parts:
            digest.update(b"\0")
            digest.update(part.part_id.encode("utf-8"))
            digest.update(b"\0")
            digest.update(part.markdown.encode("utf-8"))
        path = (
            self.settings.markdown_dir
            / str(record.id)
            / ".index-cache"
            / f"{digest.hexdigest()}.json"
        )
        _assert_within(self.settings.markdown_dir, path)
        return path

    def _generate_card(
        self,
        record: SourceFile,
        parts: Sequence[ArtifactPart],
        client: ModelClient,
    ) -> dict[str, Any]:
        prompt = _card_prompt(
            record,
            parts,
            instruction=prompt_for_client(
                client,
                ModelRole.INDEX_GENERATION,
                "document_card",
            ),
        )
        try:
            generated = asyncio.run(
                client.generate_json(
                    prompt,
                    json_schema=CARD_MODEL_SCHEMA,
                    max_output_tokens=INDEX_CARD_MAX_OUTPUT_TOKENS,
                    reasoning_effort=INDEX_REASONING_EFFORT,
                )
            )
        except Exception as exc:
            raise DocumentCardError(
                f"index_generation model failed for document {record.id}"
            ) from exc
        value = generated.value
        if not isinstance(value, Mapping):
            raise DocumentCardError("index_generation model returned no JSON object")
        return _normalize_model_card(record, parts, value)

    def _load_existing_card(self, record: SourceFile) -> dict[str, Any]:
        return _read_json(self._card_path(record.id), f"document {record.id} card")

    def _validate_card(
        self,
        record: SourceFile,
        parts: Sequence[ArtifactPart],
        card: Mapping[str, Any],
    ) -> None:
        expected_keys = {
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
        if set(card) != expected_keys:
            raise DocumentCardError(f"Document {record.id} card has an invalid shape")
        if card["document_id"] != str(record.id):
            raise DocumentCardError(f"Document {record.id} card id does not match")
        if card["source_path"] != record.relative_path:
            raise DocumentCardError(f"Document {record.id} card source path does not match")
        _required_text(card["title"], "card title", 300)
        _required_text(card["document_type"], "card document_type", 120)
        _required_text(card["summary"], "card summary", MAX_CARD_SUMMARY_CHARS)
        _string_list(card["topics"], "card topics", maximum=32, item_limit=120)
        _string_list(card["entities"], "card entities", maximum=64, item_limit=200)
        try:
            datetime.fromisoformat(_required_text(card["updated_at"], "updated_at", 64))
        except ValueError as exc:
            raise DocumentCardError("Card updated_at must be ISO-8601") from exc
        card_parts = card["parts"]
        if not isinstance(card_parts, list) or len(card_parts) != len(parts):
            raise DocumentCardError("Card parts must match the Markdown manifest")
        for expected, actual in zip(parts, card_parts, strict=True):
            if not isinstance(actual, Mapping) or set(actual) != {
                "part_id",
                "label",
                "summary",
                "md_path",
                "source_anchors",
            }:
                raise DocumentCardError("Card part has an invalid shape")
            if actual["part_id"] != expected.part_id:
                raise DocumentCardError("Card part order/id does not match the manifest")
            _required_text(actual["label"], "card part label", 300)
            _required_text(
                actual["summary"],
                "card part summary",
                MAX_PART_SUMMARY_CHARS,
            )
            expected_path = f"md/{record.id}/{expected.filename}"
            if actual["md_path"] != expected_path:
                raise DocumentCardError("Card Markdown path does not match the manifest")
            if actual["source_anchors"] != [dict(expected.anchors)]:
                raise DocumentCardError("Card source anchors do not match the manifest")

    def _load_current_folders(self) -> dict[str, CurrentFolder]:
        pointer_path = self.settings.index_dir / "current.json"
        if not pointer_path.exists():
            return {}
        pointer = _read_json(pointer_path, "current index pointer")
        if set(pointer) != {"generation_number", "root_index_path", "activated_at"}:
            raise IndexValidationError("Current index pointer has an invalid shape")
        root_relative = _safe_relative_path(pointer["root_index_path"])
        root_path = self.settings.index_dir / root_relative
        _assert_within(self.settings.index_dir / "generations", root_path)
        root = _read_json(root_path, "current root index")
        if set(root) != {"folders"} or not isinstance(root["folders"], list):
            raise IndexValidationError("Current root index has an invalid shape")
        folders: dict[str, CurrentFolder] = {}
        for entry in root["folders"]:
            _validate_root_entry(entry)
            source_directory = entry["source_directory"]
            relative = _safe_relative_path(entry["index_path"])
            json_path = root_path.parent / relative
            _assert_within(root_path.parent, json_path)
            payload = _read_json(json_path, "current folder index")
            _validate_folder_payload(payload)
            if payload["source_directory"] != source_directory:
                raise IndexValidationError("Current folder source directory mismatch")
            folders[source_directory] = CurrentFolder(
                entry=entry,
                payload=payload,
                json_path=json_path,
                md_path=json_path.with_suffix(".md"),
            )
        return folders

    def _copy_current_folder(
        self,
        current: CurrentFolder,
        staging_dir: Path,
        folder_id: str,
    ) -> None:
        destination_dir = staging_dir / "folders"
        destination_dir.mkdir(parents=True, exist_ok=True)
        if (
            current.json_path.is_symlink()
            or current.md_path.is_symlink()
            or not current.md_path.is_file()
        ):
            raise IndexValidationError("Current folder preview is missing or unsafe")
        shutil.copy2(current.json_path, destination_dir / f"{folder_id}.json")
        shutil.copy2(current.md_path, destination_dir / f"{folder_id}.md")

    def _build_folder_payload(
        self,
        folder_id: str,
        source_directory: str,
        group: Sequence[tuple[SourceFile, Mapping[str, Any]]],
    ) -> dict[str, Any]:
        documents = [_compact_card(record, card) for record, card in group]
        return {
            "folder_id": folder_id,
            "source_directory": source_directory,
            "summary": _folder_summary(
                source_directory,
                documents,
                maximum_topics=self.settings.folder_summary_topics,
            ),
            "document_count": len(documents),
            "documents": documents,
        }

    def _write_folder(self, staging_dir: Path, payload: Mapping[str, Any]) -> None:
        folder_dir = staging_dir / "folders"
        folder_dir.mkdir(parents=True, exist_ok=True)
        folder_id = payload["folder_id"]
        _write_json(
            folder_dir / f"{folder_id}.json",
            payload,
            MAX_FOLDER_INDEX_BYTES,
        )
        _write_text(
            folder_dir / f"{folder_id}.md",
            _render_folder_preview(payload),
        )

    def _build_root(
        self,
        folder_payloads: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        topic_folder_counts = Counter(
            topic
            for payload in folder_payloads.values()
            for topic in {
                str(topic)
                for document in payload["documents"]
                for topic in document["topics"]
            }
        )
        entity_folder_counts = Counter(
            entity
            for payload in folder_payloads.values()
            for entity in {
                str(entity)
                for document in payload["documents"]
                for entity in document["entities"]
            }
        )
        return {
            "folders": [
                {
                    "folder_id": payload["folder_id"],
                    "source_directory": payload["source_directory"],
                    "summary": payload["summary"],
                    "document_count": payload["document_count"],
                    **_root_routing_metadata(
                        payload["documents"],
                        topic_folder_counts=topic_folder_counts,
                        entity_folder_counts=entity_folder_counts,
                        maximum_document_types=self.settings.root_max_document_types,
                        maximum_topics=self.settings.root_max_topics,
                        maximum_entities=self.settings.root_max_entities,
                        maximum_titles=self.settings.root_max_representative_titles,
                    ),
                    "index_path": f"folders/{payload['folder_id']}.json",
                }
                for _directory, payload in sorted(folder_payloads.items())
            ]
        }

    def _build_lexical_index(
        self,
        staging_dir: Path,
        eligible: Sequence[SourceFile],
        cards: Mapping[int, Mapping[str, Any]],
        artifacts: Mapping[int, Sequence[ArtifactPart]],
    ) -> None:
        records: list[LexicalPartRecord] = []
        for record in eligible:
            parent = PurePosixPath(record.relative_path).parent.as_posix()
            source_directory = "." if parent == "." else parent
            folder_id = folder_id_for_source_directory(source_directory)
            card = cards[record.id]
            for artifact, part in zip(
                artifacts[record.id],
                card["parts"],
                strict=True,
            ):
                records.append(
                    LexicalPartRecord(
                        folder_id=folder_id,
                        document_id=str(record.id),
                        part_id=artifact.part_id,
                        source_path=record.relative_path,
                        title=str(card["title"]),
                        document_type=str(card["document_type"]),
                        topics=tuple(str(value) for value in card["topics"]),
                        entities=tuple(str(value) for value in card["entities"]),
                        label=str(part["label"]),
                        summary=str(part["summary"]),
                        body=artifact.markdown,
                    )
                )
        try:
            build_lexical_index(
                staging_dir / LEXICAL_INDEX_FILENAME,
                records,
            )
        except LexicalIndexError as exc:
            raise IndexGenerationError("Could not build the local lexical index") from exc

    def _validate_generation(
        self,
        staging_dir: Path,
        eligible: Sequence[SourceFile],
        cards: Mapping[int, Mapping[str, Any]],
    ) -> None:
        root_path = staging_dir / "root.json"
        root = _read_json(root_path, "staged root index")
        if set(root) != {"folders"} or not isinstance(root["folders"], list):
            raise IndexValidationError("Root index must contain only a folders array")
        if root_path.stat().st_size > MAX_ROOT_INDEX_BYTES:
            raise IndexValidationError("Root index exceeds its size limit")
        seen_folders: set[str] = set()
        seen_documents: set[int] = set()
        for entry in root["folders"]:
            _validate_root_entry(entry)
            folder_id = entry["folder_id"]
            if folder_id in seen_folders:
                raise IndexValidationError("Root folder ids must be unique")
            seen_folders.add(folder_id)
            relative = _safe_relative_path(entry["index_path"])
            folder_path = staging_dir / relative
            _assert_within(staging_dir, folder_path)
            folder = _read_json(folder_path, "staged folder index")
            _validate_folder_payload(folder)
            if folder_path.stat().st_size > MAX_FOLDER_INDEX_BYTES:
                raise IndexValidationError("Folder index exceeds its size limit")
            if (
                folder["folder_id"] != folder_id
                or folder["source_directory"] != entry["source_directory"]
                or folder["summary"] != entry["summary"]
                or folder["document_count"] != entry["document_count"]
            ):
                raise IndexValidationError("Root and folder index metadata differ")
            preview_path = folder_path.with_suffix(".md")
            if not preview_path.is_file() or preview_path.is_symlink():
                raise IndexValidationError("Folder Markdown preview is missing")
            for document in folder["documents"]:
                document_id = int(document["document_id"])
                if document_id in seen_documents:
                    raise IndexValidationError("A document appears in multiple folders")
                seen_documents.add(document_id)
                if document_id not in cards:
                    raise IndexValidationError("Folder references an ineligible document")
        expected_ids = {record.id for record in eligible}
        if seen_documents != expected_ids:
            raise IndexValidationError("Generation document set is incomplete")
        if sum(entry["document_count"] for entry in root["folders"]) != len(eligible):
            raise IndexValidationError("Root document count does not match folders")
        if not (staging_dir / "root.md").is_file():
            raise IndexValidationError("Root Markdown preview is missing")
        expected_lexical_parts = {
            (
                folder_id_for_source_directory(
                    (
                        "."
                        if PurePosixPath(record.relative_path).parent.as_posix() == "."
                        else PurePosixPath(record.relative_path).parent.as_posix()
                    )
                ),
                str(record.id),
                str(part["part_id"]),
            )
            for record in eligible
            for part in cards[record.id]["parts"]
        }
        try:
            validate_lexical_index(
                staging_dir / LEXICAL_INDEX_FILENAME,
                expected_lexical_parts,
            )
        except LexicalIndexError as exc:
            raise IndexValidationError("Local lexical index validation failed") from exc

    def _activate(
        self,
        generation: IndexGeneration,
        eligible_snapshot: Mapping[int, tuple[str, str]],
    ) -> None:
        current_path = self.settings.index_dir / "current.json"
        previous = current_path.read_bytes() if current_path.exists() else None
        activated_at = datetime.now(timezone.utc)
        pointer = {
            "generation_number": generation.generation_number,
            "root_index_path": generation.root_index_path,
            "activated_at": activated_at.isoformat(),
        }
        pointer_replaced = False
        try:
            # Hold SQLite's writer lock while rechecking the source snapshot and
            # switching the filesystem pointer/database state. A conversion or
            # inventory update that won the race makes this build fail instead
            # of activating a card for different source bytes.
            self.session.execute(sql_text("BEGIN IMMEDIATE"))
            records = self.session.scalars(
                select(SourceFile).order_by(SourceFile.id)
            ).all()
            by_id = {record.id: record for record in records}
            for document_id, (source_path, source_hash) in eligible_snapshot.items():
                record = by_id.get(document_id)
                if (
                    record is None
                    or not _is_eligible(record)
                    or record.relative_path != source_path
                    or record.sha256 != source_hash
                ):
                    raise IndexGenerationError(
                        "Source state changed during index generation; build again"
                    )

            _atomic_write_json(current_path, pointer)
            pointer_replaced = True
            self.session.execute(
                update(IndexGeneration)
                .where(
                    IndexGeneration.status == IndexGenerationStatus.ACTIVE,
                    IndexGeneration.generation_number != generation.generation_number,
                )
                .values(status=IndexGenerationStatus.SUPERSEDED)
            )
            generation.status = IndexGenerationStatus.ACTIVE
            generation.activated_at = activated_at
            generation.document_count = len(eligible_snapshot)
            eligible_ids = set(eligible_snapshot)
            for record in records:
                if record.id in eligible_ids:
                    record.index_status = IndexStatus.INDEXED
                elif record.index_status == IndexStatus.INDEXED:
                    record.index_status = IndexStatus.STALE
            self.session.commit()
        except Exception:
            self.session.rollback()
            if pointer_replaced:
                if previous is None:
                    current_path.unlink(missing_ok=True)
                    _fsync_directory(current_path.parent)
                else:
                    _atomic_write_bytes(current_path, previous)
            raise

    def _card_path(self, document_id: int) -> Path:
        path = self.settings.markdown_dir / str(document_id) / "card.json"
        _assert_within(self.settings.markdown_dir, path)
        return path

    def _staging_dir(self, generation_number: int) -> Path:
        return self.settings.index_dir / f".generation-{generation_number}-{uuid4().hex}.tmp"

    def _generation_dir(self, generation_number: int) -> Path:
        return self.settings.index_dir / "generations" / str(generation_number)


def folder_id_for_source_directory(source_directory: str) -> str:
    """Return a stable, compact id without exposing path separators."""
    digest = hashlib.sha256(source_directory.encode("utf-8")).hexdigest()[:16]
    return f"folder-{digest}"


def _is_eligible(record: SourceFile) -> bool:
    return (
        record.source_status == SourceStatus.PRESENT
        and record.conversion_status == ConversionStatus.READY
    )


def _group_cards(
    records: Sequence[SourceFile],
    cards: Mapping[int, Mapping[str, Any]],
) -> dict[str, list[tuple[SourceFile, Mapping[str, Any]]]]:
    grouped: dict[str, list[tuple[SourceFile, Mapping[str, Any]]]] = {}
    for record in records:
        parent = PurePosixPath(record.relative_path).parent.as_posix()
        source_directory = "." if parent == "." else parent
        grouped.setdefault(source_directory, []).append((record, cards[record.id]))
    return grouped


def _batch_index_parts(
    parts: Sequence[ArtifactPart],
) -> list[tuple[ArtifactPart, ...]]:
    batches: list[tuple[ArtifactPart, ...]] = []
    current: list[ArtifactPart] = []
    current_characters = 0
    for part in parts:
        part_characters = len(part.markdown)
        if current and (
            len(current) >= INDEX_PART_BATCH_MAX_PARTS
            or current_characters + part_characters > INDEX_PART_BATCH_MAX_CHARS
        ):
            batches.append(tuple(current))
            current = []
            current_characters = 0
        current.append(part)
        current_characters += part_characters
    if current:
        batches.append(tuple(current))
    return batches


async def _await_index_model(
    awaitable: Awaitable[Any],
    heartbeat: Heartbeat,
) -> Any:
    task = asyncio.ensure_future(awaitable)
    try:
        while True:
            done, _pending = await asyncio.wait(
                {task},
                timeout=INDEX_MODEL_HEARTBEAT_SECONDS,
            )
            if done:
                return task.result()
            heartbeat()
    except BaseException:
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        raise


def _index_model_cache_key(client: ModelClient | None) -> str:
    if client is None:
        return ""
    profile = getattr(client, "profile", None)
    prompt_digest = hashlib.sha256(
        json.dumps(
            {
                task: prompt_for_client(client, ModelRole.INDEX_GENERATION, task)
                for task in _INDEX_DEFAULT_PROMPTS
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return ":".join(
        (
            str(getattr(profile, "id", "")),
            str(
                getattr(
                    profile,
                    "remote_model_name",
                    client.__class__.__qualname__,
                )
            ),
            INDEX_REASONING_EFFORT,
            prompt_digest,
        )
    )


def _card_cache_metadata(
    record: SourceFile,
    parts: Sequence[ArtifactPart],
    model_key: str,
) -> dict[str, Any]:
    return {
        "version": INDEX_CARD_CACHE_VERSION,
        "model_key": model_key,
        "source_path": record.relative_path,
        "source_sha256": record.sha256,
        "part_ids": [part.part_id for part in parts],
    }


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _render_prompt_parts(parts: Sequence[ArtifactPart]) -> str:
    rendered_parts = []
    for part in parts:
        rendered_parts.append(
            "\n".join(
                (
                    f"<markdown-part id={json.dumps(part.part_id, ensure_ascii=False)}>",
                    f"anchors: {json.dumps(dict(part.anchors), ensure_ascii=False)}",
                    part.markdown,
                    "</markdown-part>",
                )
            )
        )
    return "\n\n".join(rendered_parts)


def _card_prompt(
    record: SourceFile,
    parts: Sequence[ArtifactPart],
    *,
    instruction: str | None = None,
) -> str:
    return (
        f"{instruction or _INDEX_DEFAULT_PROMPTS['document_card']}\n\n"
        f"source_path: {json.dumps(record.relative_path, ensure_ascii=False)}\n"
        f"source_extension: {json.dumps(record.extension, ensure_ascii=False)}\n\n"
        + _render_prompt_parts(parts)
    )


def _part_batch_prompt(
    record: SourceFile,
    parts: Sequence[ArtifactPart],
    *,
    instruction: str | None = None,
) -> str:
    return (
        f"{instruction or _INDEX_DEFAULT_PROMPTS['part_batch']}\n\n"
        f"source_path: {json.dumps(record.relative_path, ensure_ascii=False)}\n"
        f"source_extension: {json.dumps(record.extension, ensure_ascii=False)}\n\n"
        + _render_prompt_parts(parts)
    )


def _document_metadata_prompt(
    record: SourceFile,
    batches: Sequence[Mapping[str, Any]],
    *,
    instruction: str | None = None,
) -> str:
    compact = [
        {
            "batch": number,
            "first_part_id": batch["parts"][0]["part_id"],
            "last_part_id": batch["parts"][-1]["part_id"],
            "summary": batch["batch_summary"],
        }
        for number, batch in enumerate(batches, start=1)
    ]
    return (
        f"{instruction or _INDEX_DEFAULT_PROMPTS['document_metadata']}\n\n"
        f"source_path: {json.dumps(record.relative_path, ensure_ascii=False)}\n"
        f"source_extension: {json.dumps(record.extension, ensure_ascii=False)}\n"
        f"batch_summaries: {json.dumps(compact, ensure_ascii=False)}"
    )


def _normalize_part_batch(
    parts: Sequence[ArtifactPart],
    value: Mapping[str, Any],
) -> dict[str, Any]:
    if set(value) != {"batch_summary", "parts"}:
        raise DocumentCardError("index_generation part batch has an invalid shape")
    model_parts = value["parts"]
    if not isinstance(model_parts, list) or len(model_parts) != len(parts):
        raise DocumentCardError("index_generation part batch count does not match")
    normalized_parts: list[dict[str, str]] = []
    for artifact, model_part in zip(parts, model_parts, strict=True):
        if not isinstance(model_part, Mapping) or set(model_part) != {
            "part_id",
            "label",
            "summary",
        }:
            raise DocumentCardError("index_generation part batch entry is invalid")
        if model_part["part_id"] != artifact.part_id:
            raise DocumentCardError("index_generation changed a batch part_id")
        normalized_parts.append(
            {
                "part_id": artifact.part_id,
                "label": _bounded_model_text(model_part["label"], "part label", 300),
                "summary": _bounded_model_text(
                    model_part["summary"],
                    "part summary",
                    600,
                ),
            }
        )
    return {
        "batch_summary": _bounded_model_text(
            value["batch_summary"],
            "batch summary",
            1_500,
        ),
        "parts": normalized_parts,
    }


def _normalize_model_card(
    record: SourceFile,
    parts: Sequence[ArtifactPart],
    value: Mapping[str, Any],
) -> dict[str, Any]:
    expected_model_keys = {
        "title",
        "document_type",
        "summary",
        "topics",
        "entities",
        "parts",
    }
    if set(value) != expected_model_keys:
        raise DocumentCardError("index_generation card JSON has an invalid shape")
    model_parts = value["parts"]
    if not isinstance(model_parts, list) or len(model_parts) != len(parts):
        raise DocumentCardError("index_generation card parts do not match Markdown parts")

    normalized_parts: list[dict[str, Any]] = []
    for artifact, model_part in zip(parts, model_parts, strict=True):
        if not isinstance(model_part, Mapping) or set(model_part) != {
            "part_id",
            "label",
            "summary",
        }:
            raise DocumentCardError("index_generation part JSON has an invalid shape")
        if model_part["part_id"] != artifact.part_id:
            raise DocumentCardError("index_generation changed or reordered a part_id")
        normalized_parts.append(
            {
                "part_id": artifact.part_id,
                "label": _bounded_model_text(model_part["label"], "part label", 300),
                "summary": _bounded_model_text(
                    model_part["summary"],
                    "part summary",
                    MAX_PART_SUMMARY_CHARS,
                ),
                "md_path": f"md/{record.id}/{artifact.filename}",
                "source_anchors": [dict(artifact.anchors)],
            }
        )

    return {
        "document_id": str(record.id),
        "source_path": record.relative_path,
        "title": _bounded_model_text(value["title"], "title", 300),
        "document_type": _bounded_model_text(
            value["document_type"],
            "document_type",
            120,
        ),
        "summary": _bounded_model_text(
            value["summary"],
            "summary",
            MAX_CARD_SUMMARY_CHARS,
        ),
        "topics": _bounded_model_string_list(
            value["topics"], "topics", maximum=32, item_limit=120
        ),
        "entities": _bounded_model_string_list(
            value["entities"],
            "entities",
            maximum=64,
            item_limit=200,
        ),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "parts": normalized_parts,
    }


def _compact_card(record: SourceFile, card: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "document_id": str(record.id),
        "source_path": card["source_path"],
        "title": card["title"],
        "document_type": card["document_type"],
        "summary": card["summary"][:MAX_FOLDER_DOCUMENT_SUMMARY_CHARS],
        "topics": card["topics"][:12],
        "entities": card["entities"][:16],
        "updated_at": card["updated_at"],
        "card_path": f"md/{record.id}/card.json",
    }


def _folder_summary(
    source_directory: str,
    documents: Sequence[Mapping[str, Any]],
    *,
    maximum_topics: int,
) -> str:
    topics = Counter(
        topic
        for document in documents
        for topic in document["topics"]
        if isinstance(topic, str)
    )
    topic_text = ", ".join(
        topic for topic, _count in topics.most_common(maximum_topics)
    )
    if topic_text:
        summary = f"{source_directory}: {len(documents)} document(s). Topics: {topic_text}."
    else:
        titles = ", ".join(str(document["title"]) for document in documents[:5])
        summary = f"{source_directory}: {len(documents)} document(s). {titles}"
    return summary[:MAX_FOLDER_SUMMARY_CHARS]


def _root_routing_metadata(
    documents: Sequence[Mapping[str, Any]],
    *,
    topic_folder_counts: Mapping[str, int],
    entity_folder_counts: Mapping[str, int],
    maximum_document_types: int,
    maximum_topics: int,
    maximum_entities: int,
    maximum_titles: int,
) -> dict[str, list[str]]:
    """Build compact, discriminative root fields with document coverage."""
    return {
        "document_types": _unique_document_values(
            documents,
            "document_type",
            maximum=maximum_document_types,
        ),
        "topics": _rank_root_terms(
            documents,
            "topics",
            topic_folder_counts,
            maximum=maximum_topics,
        ),
        "entities": _rank_root_terms(
            documents,
            "entities",
            entity_folder_counts,
            maximum=maximum_entities,
        ),
        "representative_titles": _unique_document_values(
            documents,
            "title",
            maximum=maximum_titles,
        ),
    }


def _unique_document_values(
    documents: Sequence[Mapping[str, Any]],
    field: str,
    *,
    maximum: int,
) -> list[str]:
    values: list[str] = []
    for document in documents:
        value = str(document[field]).strip()
        if value and value not in values:
            values.append(value)
        if len(values) >= maximum:
            break
    return values


def _rank_root_terms(
    documents: Sequence[Mapping[str, Any]],
    field: str,
    folder_counts: Mapping[str, int],
    *,
    maximum: int,
) -> list[str]:
    document_coverage: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    position = 0
    for document in documents:
        values = dict.fromkeys(
            str(value).strip()
            for value in document[field]
            if isinstance(value, str) and value.strip()
        )
        for value in values:
            document_coverage[value] += 1
            first_seen.setdefault(value, position)
            position += 1
    ranked = sorted(
        document_coverage,
        key=lambda value: (
            folder_counts.get(value, 1),
            -document_coverage[value],
            first_seen[value],
            value,
        ),
    )
    return ranked[:maximum]


def _validate_root_entry(entry: Any) -> None:
    base_fields = {
        "folder_id",
        "source_directory",
        "summary",
        "document_count",
        "index_path",
    }
    routing_fields = {
        "document_types",
        "topics",
        "entities",
        "representative_titles",
    }
    if not isinstance(entry, Mapping) or set(entry) not in {
        frozenset(base_fields),
        frozenset(base_fields | routing_fields),
    }:
        raise IndexValidationError("Root entries must contain only folder metadata")
    _required_text(entry["folder_id"], "root folder_id", 100)
    _required_text(entry["source_directory"], "root source_directory", 2_000)
    _required_text(entry["summary"], "root summary", MAX_FOLDER_SUMMARY_CHARS)
    if not isinstance(entry["document_count"], int) or entry["document_count"] < 0:
        raise IndexValidationError("Root document_count must be non-negative")
    if routing_fields.issubset(entry):
        _string_list(
            entry["document_types"],
            "root document_types",
            maximum=MAX_ROOT_DOCUMENT_TYPES,
            item_limit=120,
        )
        _string_list(
            entry["topics"],
            "root topics",
            maximum=MAX_ROOT_TOPICS,
            item_limit=120,
        )
        _string_list(
            entry["entities"],
            "root entities",
            maximum=MAX_ROOT_ENTITIES,
            item_limit=200,
        )
        _string_list(
            entry["representative_titles"],
            "root representative_titles",
            maximum=MAX_ROOT_REPRESENTATIVE_TITLES,
            item_limit=300,
        )
    _safe_relative_path(entry["index_path"])


def _validate_folder_payload(payload: Any) -> None:
    if not isinstance(payload, Mapping) or set(payload) != {
        "folder_id",
        "source_directory",
        "summary",
        "document_count",
        "documents",
    }:
        raise IndexValidationError("Folder index has an invalid shape")
    _required_text(payload["folder_id"], "folder_id", 100)
    _required_text(payload["source_directory"], "source_directory", 2_000)
    _required_text(payload["summary"], "folder summary", MAX_FOLDER_SUMMARY_CHARS)
    documents = payload["documents"]
    if not isinstance(documents, list):
        raise IndexValidationError("Folder documents must be an array")
    if payload["document_count"] != len(documents):
        raise IndexValidationError("Folder document_count does not match documents")
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
    for document in documents:
        if not isinstance(document, Mapping) or set(document) != expected_document_keys:
            raise IndexValidationError("Compact folder card has an invalid shape")
        _required_text(document["document_id"], "document_id", 100)
        _required_text(document["source_path"], "source_path", 2_000)
        _required_text(document["title"], "title", 300)
        _required_text(document["document_type"], "document_type", 120)
        _required_text(
            document["summary"],
            "document summary",
            MAX_FOLDER_DOCUMENT_SUMMARY_CHARS,
        )
        _string_list(document["topics"], "topics", maximum=12, item_limit=120)
        _string_list(document["entities"], "entities", maximum=16, item_limit=200)
        _required_text(document["updated_at"], "updated_at", 64)
        _safe_relative_path(document["card_path"])


def _required_text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DocumentCardError(f"{field} must be non-empty text")
    normalized = value.strip()
    if len(normalized) > limit:
        raise DocumentCardError(f"{field} exceeds {limit} characters")
    return normalized


def _bounded_model_text(value: Any, field: str, limit: int) -> str:
    """Normalize model prose while keeping structural validation strict."""
    if not isinstance(value, str) or not value.strip():
        raise DocumentCardError(f"{field} must be non-empty text")
    return value.strip()[:limit]


def _bounded_model_string_list(
    value: Any,
    field: str,
    *,
    maximum: int,
    item_limit: int,
) -> list[str]:
    if not isinstance(value, list):
        raise DocumentCardError(f"{field} must be an array")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _bounded_model_text(item, field, item_limit)
        if text not in seen:
            result.append(text)
            seen.add(text)
        if len(result) >= maximum:
            break
    return result


def _string_list(
    value: Any,
    field: str,
    *,
    maximum: int,
    item_limit: int,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise DocumentCardError(f"{field} must be an array with at most {maximum} items")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _required_text(item, field, item_limit)
        if text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _safe_generated_filename(value: Any) -> str:
    if not isinstance(value, str):
        raise MarkdownArtifactError("Markdown manifest part path must be text")
    path = PurePosixPath(value)
    if path.is_absolute() or len(path.parts) != 1 or path.name in {"", ".", ".."}:
        raise MarkdownArtifactError("Markdown manifest part path is unsafe")
    return path.name


def _safe_relative_path(value: Any) -> Path:
    if not isinstance(value, str):
        raise IndexValidationError("Index path must be text")
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise IndexValidationError("Index path must be a safe relative path")
    return Path(*pure.parts)


def _assert_within(root: Path, path: Path) -> None:
    root_resolved = root.resolve(strict=False)
    path_resolved = path.resolve(strict=False)
    if path_resolved != root_resolved and root_resolved not in path_resolved.parents:
        raise IndexGenerationError("Generated index path escapes its configured root")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise IndexValidationError(f"{label} is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IndexValidationError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise IndexValidationError(f"{label} must be a JSON object")
    return value


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_json(path: Path, value: Mapping[str, Any], size_limit: int) -> None:
    payload = _json_bytes(value)
    if len(payload) > size_limit:
        raise IndexValidationError(f"Index exceeds its {size_limit}-byte size limit")
    _write_bytes(path, payload)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write_bytes(path, _json_bytes(value))


def _write_text(path: Path, value: str) -> None:
    _write_bytes(path, value.encode("utf-8"))


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise IndexGenerationError("Refusing to replace a generated symlink")
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise IndexGenerationError("Refusing to replace a generated symlink")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _preview_text(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _render_root_preview(root: Mapping[str, Any]) -> str:
    lines = [
        "# Knowledge Index",
        "",
        "> Administrator preview only. `root.json` is canonical.",
        "",
        "| Folder | Source directory | Summary | Documents | JSON index |",
        "|---|---|---|---:|---|",
    ]
    for entry in root["folders"]:
        lines.append(
            "| "
            + " | ".join(
                _preview_text(value)
                for value in (
                    entry["folder_id"],
                    entry["source_directory"],
                    entry["summary"],
                    entry["document_count"],
                    entry["index_path"],
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _render_folder_preview(folder: Mapping[str, Any]) -> str:
    lines = [
        f"# {_preview_text(folder['source_directory'])}",
        "",
        "> Administrator preview only. The sibling JSON file is canonical.",
        "",
        _preview_text(folder["summary"]),
        "",
        "| Document | Type | Summary | Source |",
        "|---|---|---|---|",
    ]
    for document in folder["documents"]:
        lines.append(
            "| "
            + " | ".join(
                _preview_text(value)
                for value in (
                    document["title"],
                    document["document_type"],
                    document["summary"],
                    document["source_path"],
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"
