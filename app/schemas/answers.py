"""Strict answer-generation output and public answer result schemas."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AnswerSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Citation(AnswerSchema):
    document_id: str
    part_id: str
    anchor: str
    label: str

    @field_validator("document_id", "part_id", "anchor", "label")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return _non_blank(value)


class ConflictValue(AnswerSchema):
    value: str
    document_id: str
    anchor: str

    @field_validator("value", "document_id", "anchor")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return _non_blank(value)


class Conflict(AnswerSchema):
    subject: str
    values: list[ConflictValue] = Field(min_length=2)
    analysis: str

    @field_validator("subject", "analysis")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return _non_blank(value)


class Download(AnswerSchema):
    document_id: str
    filename: str
    relative_directory: str

    @field_validator("document_id", "filename")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return _non_blank(value)

    @field_validator("relative_directory")
    @classmethod
    def normalize_relative_directory(cls, value: str) -> str:
        return value.strip()


class ResearchHandoff(AnswerSchema):
    """A safe, user-reviewable prompt for a separate web-enabled model."""

    reason: str
    known_information: list[str]
    missing_information: list[str] = Field(min_length=1)
    prompt: str

    @field_validator("reason", "prompt")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return _non_blank(value)

    @field_validator("known_information", "missing_information")
    @classmethod
    def normalize_information(cls, value: list[str]) -> list[str]:
        return [_non_blank(item) for item in value]


class AnswerResult(AnswerSchema):
    answer_markdown: str
    citations: list[Citation]
    conflicts: list[Conflict]
    downloads: list[Download]
    research_handoff: ResearchHandoff | None

    @field_validator("answer_markdown")
    @classmethod
    def normalize_answer(cls, value: str) -> str:
        return _non_blank(value)


class DownloadIntent(AnswerSchema):
    """Model-facing download choice; backend metadata is deliberately absent."""

    document_id: str

    @field_validator("document_id")
    @classmethod
    def normalize_document_id(cls, value: str) -> str:
        return _non_blank(value)


class AnswerModelOutput(AnswerSchema):
    """Untrusted model output before evidence and source-record validation."""

    answer_markdown: str
    citations: list[Citation]
    conflicts: list[Conflict]
    downloads: list[DownloadIntent]
    research_handoff: ResearchHandoff | None

    @field_validator("answer_markdown")
    @classmethod
    def normalize_answer(cls, value: str) -> str:
        return _non_blank(value)


def _non_blank(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("value must not be blank")
    return stripped
