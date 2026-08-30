"""Strict model outputs and unified query-navigation results."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class NavigationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NavigationIntent(StrEnum):
    ANSWER = "answer"
    DOWNLOAD = "download"
    LIST_FILES = "list_files"
    SMALL_TALK = "small_talk"


class FolderSelection(NavigationSchema):
    """Phase-one structured output produced from the current root index."""

    intent: NavigationIntent
    selected_folders: list[str]
    display_reason: str = Field(max_length=500)
    need_more_information: bool

    @field_validator("selected_folders")
    @classmethod
    def normalize_folder_ids(cls, values: list[str]) -> list[str]:
        return _unique_non_blank(values, "folder id")

    @field_validator("display_reason")
    @classmethod
    def normalize_display_reason(cls, value: str) -> str:
        return value.strip()


class DocumentSelection(NavigationSchema):
    document_id: str
    part_ids: list[str]
    display_reason: str = Field(max_length=500)

    @field_validator("document_id")
    @classmethod
    def normalize_document_id(cls, value: str) -> str:
        return _non_blank(value, "document id")

    @field_validator("part_ids")
    @classmethod
    def normalize_part_ids(cls, values: list[str]) -> list[str]:
        return _unique_non_blank(values, "part id")

    @field_validator("display_reason")
    @classmethod
    def normalize_display_reason(cls, value: str) -> str:
        return value.strip()


class DocumentSelectionResult(NavigationSchema):
    """Phase-two structured output produced for one selected folder."""

    selected_documents: list[DocumentSelection]
    confidence: float = Field(ge=0.0, le=1.0)


class NavigationTokenBudget(NavigationSchema):
    context_window: int = Field(gt=0)
    root_budget: int = Field(gt=0)
    folder_budget: int = Field(gt=0)
    evidence_budget: int = Field(gt=0)
    output_reserve: int = Field(gt=0)
    answer_context_window: int | None = Field(default=None, gt=0)
    answer_output_reserve: int | None = Field(default=None, gt=0)
    router_safety_reserve: int = Field(default=0, ge=0)
    answer_safety_reserve: int = Field(default=0, ge=0)


class NavigatedFolder(NavigationSchema):
    folder_id: str
    source_directory: str
    summary: str
    display_reason: str


class NavigatedDocument(NavigationSchema):
    folder_id: str
    document_id: str
    source_path: str
    title: str
    document_type: str
    selected_part_ids: list[str]
    display_reason: str


class NavigatedPart(NavigationSchema):
    folder_id: str
    document_id: str
    part_id: str
    label: str
    summary: str
    md_path: str
    source_anchors: list[dict[str, Any]]
    content: str | None
    estimated_tokens: int = Field(ge=0)
    within_evidence_budget: bool


class NavigationResult(NavigationSchema):
    """The complete navigation result; it deliberately contains no answer."""

    intent: NavigationIntent
    folders: list[NavigatedFolder]
    documents: list[NavigatedDocument]
    parts: list[NavigatedPart]
    display_steps: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    need_more_information: bool = False
    token_budget: NavigationTokenBudget


def _non_blank(value: str, label: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{label} must not be blank")
    return stripped


def _unique_non_blank(values: list[str], label: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _non_blank(value, label)
        if normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result
