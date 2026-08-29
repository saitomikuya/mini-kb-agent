"""Schemas for source-file inventory and management APIs."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.source_files import ConversionStatus, IndexStatus, SourceStatus


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceFileRead(StrictSchema):
    id: int
    relative_path: str
    filename: str
    extension: str
    size: int
    mtime_ns: int
    sha256: str
    source_status: SourceStatus
    conversion_status: ConversionStatus
    index_status: IndexStatus
    last_error: str | None
    converted_at: datetime | None
    created_at: datetime
    updated_at: datetime
    display_path: str


class SourceScanRead(StrictSchema):
    scanned: int
    new: int
    changed: int
    unchanged: int
    removed: int
    missing: int
    unsafe_skipped: int


class SourceFileBatchRequest(StrictSchema):
    file_ids: list[int] = Field(min_length=1, max_length=500)

    @field_validator("file_ids")
    @classmethod
    def validate_file_ids(cls, value: list[int]) -> list[int]:
        if any(file_id <= 0 for file_id in value):
            raise ValueError("file ids must be positive")
        return list(dict.fromkeys(value))


class SourceReferenceBatchRequest(StrictSchema):
    """Bounded lookup used to restore source names in browser-local history."""

    document_ids: list[int] = Field(min_length=1, max_length=100)

    @field_validator("document_ids")
    @classmethod
    def validate_document_ids(cls, value: list[int]) -> list[int]:
        if any(document_id <= 0 for document_id in value):
            raise ValueError("document ids must be positive")
        return list(dict.fromkeys(value))


class SourceReferenceRead(StrictSchema):
    document_id: str
    filename: str
    relative_path: str
    relative_directory: str
    display_path: str | None
    download_url: str | None
    available: bool


class SourceLibraryFileRead(StrictSchema):
    """Read-only knowledge-file metadata exposed to authenticated chat users."""

    id: int
    relative_path: str
    filename: str
    extension: str
    size: int
    index_status: IndexStatus
    converted_at: datetime | None
    available: bool
    view_url: str | None
    download_url: str | None


class SourceFolderRequest(StrictSchema):
    folder_path: str = Field(min_length=1, max_length=2_000)

    @field_validator("folder_path")
    @classmethod
    def validate_folder_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("folder path must not be blank")
        return value


class SourceFolderDeleteRead(StrictSchema):
    folder_path: str
    affected_files: int
    deleted_records: int
    marked_missing: int
