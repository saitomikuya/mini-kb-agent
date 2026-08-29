"""Safe, read-only access to generated artifacts needed by the Admin UI."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings
from app.models.index_generation import IndexGeneration
from app.models.source_file import SourceFile
from app.schemas.admin import (
    IndexSummaryRead,
    MarkdownPartPreviewRead,
    SourceMarkdownPreviewRead,
    TextPreviewRead,
)


class AdminArtifactServiceError(RuntimeError):
    status_code = 500


class AdminArtifactNotFoundError(AdminArtifactServiceError):
    status_code = 404


class AdminArtifactConflictError(AdminArtifactServiceError):
    status_code = 409


class AdminArtifactService:
    """Expose generated Markdown and active index previews without mutation."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def source_markdown_preview(self, source_file_id: int) -> SourceMarkdownPreviewRead:
        source = self.session.get(SourceFile, source_file_id)
        if source is None:
            raise AdminArtifactNotFoundError("Source file was not found")

        artifact_dir = self.settings.markdown_dir / str(source_file_id)
        if artifact_dir.is_symlink():
            raise AdminArtifactConflictError("The generated Markdown path is unsafe")
        manifest_path = _safe_generated_child(artifact_dir, "manifest.json")
        manifest = _read_json(manifest_path, "Markdown manifest")
        if (
            manifest.get("status") != "READY"
            or str(manifest.get("document_id")) != str(source_file_id)
            or manifest.get("source_path") != source.relative_path
        ):
            raise AdminArtifactConflictError(
                "The generated Markdown manifest does not match this source file"
            )

        raw_parts = manifest.get("parts")
        if not isinstance(raw_parts, list) or not raw_parts:
            raise AdminArtifactConflictError("The generated Markdown has no previewable parts")

        parts: list[MarkdownPartPreviewRead] = []
        for raw_part in raw_parts:
            if not isinstance(raw_part, dict):
                raise AdminArtifactConflictError("The Markdown manifest is invalid")
            part_id = raw_part.get("part_id")
            relative_path = raw_part.get("path")
            anchors = raw_part.get("anchors")
            if (
                not isinstance(part_id, str)
                or not part_id
                or not isinstance(relative_path, str)
                or not isinstance(anchors, dict)
            ):
                raise AdminArtifactConflictError("The Markdown manifest is invalid")
            part_path = _safe_generated_child(artifact_dir, relative_path)
            parts.append(
                MarkdownPartPreviewRead(
                    part_id=part_id,
                    path=relative_path,
                    anchors=anchors,
                    content=_read_text(part_path, "Markdown part"),
                )
            )

        return SourceMarkdownPreviewRead(
            source_file_id=source_file_id,
            relative_path=source.relative_path,
            converted_at=_parse_optional_datetime(manifest.get("converted_at")),
            parts=parts,
        )

    def index_summary(self) -> IndexSummaryRead:
        current = self._current_index()
        if current is None:
            return IndexSummaryRead(
                current_generation=None,
                document_count=0,
                folder_count=0,
                last_generated=None,
            )

        pointer, root, _root_path = current
        generation_number = pointer["generation_number"]
        generation = self.session.get(IndexGeneration, generation_number)
        root_document_count = 0
        for entry in root["folders"]:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("document_count"), int)
                or entry["document_count"] < 0
            ):
                raise AdminArtifactConflictError("The current root index is invalid")
            root_document_count += entry["document_count"]
        document_count = (
            generation.document_count
            if generation is not None
            else root_document_count
        )
        activated_at = (
            generation.activated_at
            if generation is not None and generation.activated_at is not None
            else _parse_optional_datetime(pointer.get("activated_at"))
        )
        return IndexSummaryRead(
            current_generation=generation_number,
            document_count=document_count,
            folder_count=len(root["folders"]),
            last_generated=activated_at,
        )

    def root_json_preview(self) -> TextPreviewRead:
        current = self._require_current_index()
        _pointer, _root, root_path = current
        return TextPreviewRead(
            filename="root.json",
            media_type="application/json",
            content=_read_text(root_path, "current root index"),
        )

    def root_markdown_preview(self) -> TextPreviewRead:
        current = self._require_current_index()
        _pointer, _root, root_path = current
        markdown_path = _safe_generated_child(root_path.parent, root_path.with_suffix(".md").name)
        return TextPreviewRead(
            filename="root.md",
            media_type="text/markdown",
            content=_read_text(markdown_path, "current root Markdown preview"),
        )

    def _require_current_index(self) -> tuple[dict[str, Any], dict[str, Any], Path]:
        current = self._current_index()
        if current is None:
            raise AdminArtifactNotFoundError("No active index generation is available")
        return current

    def _current_index(self) -> tuple[dict[str, Any], dict[str, Any], Path] | None:
        pointer_path = self.settings.index_dir / "current.json"
        if not pointer_path.exists():
            return None
        pointer = _read_json(pointer_path, "current index pointer")
        if set(pointer) != {"generation_number", "root_index_path", "activated_at"}:
            raise AdminArtifactConflictError("The current index pointer is invalid")
        if not isinstance(pointer["generation_number"], int) or pointer["generation_number"] <= 0:
            raise AdminArtifactConflictError("The current index generation is invalid")
        root_path = _safe_generated_child(
            self.settings.index_dir,
            pointer["root_index_path"],
        )
        root = _read_json(root_path, "current root index")
        if set(root) != {"folders"} or not isinstance(root["folders"], list):
            raise AdminArtifactConflictError("The current root index is invalid")
        return pointer, root, root_path


def _safe_generated_child(root: Path, relative_path: object) -> Path:
    if not isinstance(relative_path, str) or not relative_path or "\x00" in relative_path:
        raise AdminArtifactConflictError("Generated artifact path is invalid")
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or ".." in pure.parts:
        raise AdminArtifactConflictError("Generated artifact path is unsafe")

    resolved_root = root.resolve(strict=False)
    candidate = resolved_root.joinpath(*pure.parts)
    resolved_candidate = candidate.resolve(strict=False)
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise AdminArtifactConflictError("Generated artifact path is unsafe") from exc
    if candidate.is_symlink() or os.path.islink(candidate):
        raise AdminArtifactConflictError("Generated artifact path is unsafe")
    current = candidate.parent
    while current != resolved_root:
        if current.is_symlink():
            raise AdminArtifactConflictError("Generated artifact path is unsafe")
        if current == current.parent:
            break
        current = current.parent
    return candidate


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(_read_text(path, label))
    except json.JSONDecodeError as exc:
        raise AdminArtifactConflictError(f"The {label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise AdminArtifactConflictError(f"The {label} is invalid")
    return payload


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AdminArtifactNotFoundError(f"The {label} is not available") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise AdminArtifactConflictError(f"The {label} could not be read") from exc


def _parse_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AdminArtifactConflictError("Generated artifact timestamp is invalid")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise AdminArtifactConflictError("Generated artifact timestamp is invalid") from exc
