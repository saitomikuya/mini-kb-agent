"""Read models used only by the administration workspace."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MarkdownPartPreviewRead(StrictSchema):
    part_id: str
    path: str
    anchors: dict[str, Any]
    content: str


class SourceMarkdownPreviewRead(StrictSchema):
    source_file_id: int
    relative_path: str
    converted_at: datetime | None
    parts: list[MarkdownPartPreviewRead]


class IndexSummaryRead(StrictSchema):
    current_generation: int | None
    document_count: int
    folder_count: int
    last_generated: datetime | None


class TextPreviewRead(StrictSchema):
    filename: str
    media_type: str
    content: str
