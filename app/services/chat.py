"""Stateless public projection helpers for chat responses."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
import re
from typing import Any

from app.config import Settings
from app.schemas.answers import AnswerResult
from app.schemas.navigation import NavigationIntent, NavigationResult


def navigation_event_payloads(
    navigation: NavigationResult,
) -> list[tuple[str, dict[str, Any]]]:
    """Project navigation results into a concise, user-facing public trace."""
    intent_messages = {
        NavigationIntent.ANSWER: "已识别为知识问答请求",
        NavigationIntent.DOWNLOAD: "已识别为文件下载请求",
        NavigationIntent.LIST_FILES: "已识别为文件查询请求",
    }
    payloads: list[tuple[str, dict[str, Any]]] = [
        (
            "intent_detected",
            {
                "message": intent_messages[navigation.intent],
                "intent": navigation.intent.value,
            },
        ),
        (
            "folders_selected",
            {
                "message": (
                    f"已定位资料范围：{len(navigation.folders)} 个资料目录"
                    if navigation.folders
                    else "未定位到相关资料目录"
                ),
                "count": len(navigation.folders),
                "folders": [
                    {
                        "folder_id": folder.folder_id,
                        "source_directory": folder.source_directory,
                    }
                    for folder in navigation.folders
                ],
            },
        ),
    ]
    payloads.append(
        (
            "documents_selected",
            {
                "message": f"找到 {len(navigation.documents)} 个可能相关文件",
                "count": len(navigation.documents),
                "documents": [
                    {
                        "document_id": document.document_id,
                        "title": document.title,
                        "source_path": document.source_path,
                    }
                    for document in navigation.documents
                ],
            },
        )
    )
    return payloads


def public_answer_payload(
    answer: AnswerResult,
    settings: Settings,
    navigation: NavigationResult | None = None,
) -> dict[str, Any]:
    payload = answer.model_dump(mode="json")
    documents = {
        document.document_id: document
        for document in (navigation.documents if navigation is not None else [])
    }
    public_citations: list[dict[str, Any]] = []
    for citation in answer.citations:
        citation_payload = citation.model_dump(mode="json")
        document = documents.get(citation.document_id)
        source_path = document.source_path if document is not None else None
        source_filename = (
            PurePosixPath(source_path).name
            if source_path
            else (document.title if document is not None else None)
        )
        source_location = human_source_location(citation.anchor)
        download_url = _download_url(citation.document_id)
        citation_payload.update(
            {
                "source_filename": source_filename,
                "source_path": source_path,
                "display_path": (
                    str(settings.source_display_root / source_path)
                    if settings.source_display_root is not None and source_path
                    else source_path
                ),
                "source_location": source_location,
                "download_url": download_url,
            }
        )
        public_citations.append(citation_payload)
    payload["citations"] = public_citations
    payload["answer_markdown"] = link_citation_markers(
        answer.answer_markdown,
        public_citations,
    )

    public_downloads: list[dict[str, Any]] = []
    for download in answer.downloads:
        relative_path = _relative_download_path(
            download.relative_directory,
            download.filename,
        )
        public_downloads.append(
            {
                **download.model_dump(mode="json"),
                "relative_path": relative_path,
                "display_path": (
                    str(settings.source_display_root / relative_path)
                    if settings.source_display_root is not None
                    else None
                ),
                # This ID-based endpoint performs its own source-root validation.
                # Neither relative_path nor display_path is ever used for download.
                "download_url": f"/api/files/{download.document_id}/download",
            }
        )
    payload["downloads"] = public_downloads
    return payload


def human_source_location(anchor: str) -> str:
    """Turn a canonical artifact anchor into concise user-facing Chinese."""
    try:
        value = json.loads(anchor)
    except (TypeError, json.JSONDecodeError):
        return "原文相关位置"
    if not isinstance(value, dict):
        return "原文相关位置"

    locations: list[str] = []
    page = value.get("page")
    if isinstance(page, int) and not isinstance(page, bool):
        locations.append(f"第 {page} 页")
    slide = value.get("slide")
    if isinstance(slide, int) and not isinstance(slide, bool):
        locations.append(f"第 {slide} 张幻灯片")
    sheet = value.get("sheet")
    if isinstance(sheet, str) and sheet.strip():
        locations.append(f"工作表“{sheet.strip()}”")
    rows = value.get("rows")
    if isinstance(rows, str) and rows.strip() and rows.strip() != "empty":
        locations.append(f"第 {rows.strip()} 行")
    section = value.get("section")
    if isinstance(section, str) and section.strip():
        normalized_section = section.strip()
        numbered = re.fullmatch(r"section-(\d+)", normalized_section, re.IGNORECASE)
        if numbered:
            locations.append(f"第 {numbered.group(1)} 部分")
        elif normalized_section == "document":
            locations.append("全文")
        elif normalized_section == "image":
            locations.append("图片内容")
        else:
            locations.append(f"章节“{normalized_section}”")
    segment = value.get("segment")
    if isinstance(segment, str) and re.fullmatch(r"\d+/\d+", segment.strip()):
        locations.append(f"片段 {segment.strip()}")
    return "·".join(locations) or "原文相关位置"


def link_citation_markers(
    markdown: str,
    citations: list[dict[str, Any]],
) -> str:
    """Replace internal bracket markers with original-source download links."""
    replacements: dict[str, str] = {}
    for citation in citations:
        download_url = citation.get("download_url")
        source_filename = citation.get("source_filename")
        if not isinstance(download_url, str) or not isinstance(source_filename, str):
            continue
        location = citation.get("source_location")
        link_label = source_filename
        if isinstance(location, str) and location:
            link_label = f"{link_label}（{location}）"
        replacement = f"[{_escape_markdown_label(link_label)}]({download_url})"
        aliases = [citation.get("label"), citation.get("part_id")]
        aliases.extend(_anchor_aliases(citation.get("anchor")))
        for alias in aliases:
            if isinstance(alias, str) and alias.strip():
                replacements.setdefault(alias.strip(), replacement)

    if not replacements:
        return markdown

    marker_pattern = re.compile(r"(?<!!)\[([^\]\n]+)\](?!\s*\()")
    lines = markdown.splitlines(keepends=True)
    in_fence = False
    rewritten: list[str] = []
    for line in lines:
        if re.match(r"^\s*```", line):
            in_fence = not in_fence
            rewritten.append(line)
            continue
        if in_fence:
            rewritten.append(line)
            continue
        rewritten.append(
            marker_pattern.sub(
                lambda match: replacements.get(match.group(1).strip(), match.group(0)),
                line,
            )
        )
    return "".join(rewritten)


def _anchor_aliases(anchor: Any) -> list[str]:
    if not isinstance(anchor, str):
        return []
    try:
        value = json.loads(anchor)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, dict):
        return []
    aliases: list[str] = []
    for key, raw_value in value.items():
        if key == "section" and isinstance(raw_value, str):
            aliases.append(raw_value)
        elif key == "page" and isinstance(raw_value, int):
            aliases.extend((f"Page {raw_value}", f"page {raw_value}", f"第{raw_value}页"))
        elif key == "slide" and isinstance(raw_value, int):
            aliases.extend((f"Slide {raw_value}", f"slide {raw_value}", f"第{raw_value}页"))
    return aliases


def _escape_markdown_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _download_url(document_id: str) -> str | None:
    if not document_id.isascii() or not document_id.isdigit():
        return None
    numeric_id = int(document_id)
    if numeric_id <= 0 or str(numeric_id) != document_id:
        return None
    return f"/api/files/{document_id}/download"


def _relative_download_path(relative_directory: str, filename: str) -> str:
    if not relative_directory:
        return filename
    return (PurePosixPath(relative_directory) / filename).as_posix()
