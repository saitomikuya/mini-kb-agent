"""Schemas returned by the background-job administration API."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.jobs import JobControlState, JobStatus


class _JobSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class ConvertChangedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retry: bool = False


class JobItemRead(_JobSchema):
    id: int
    job_id: int
    source_file_id: int | None
    status: JobStatus
    attempts: int
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    progress: dict[str, Any] | None = Field(validation_alias="progress_json")


class JobRead(_JobSchema):
    id: int
    job_type: str
    status: JobStatus
    control_state: JobControlState
    total_items: int
    completed_items: int
    failed_items: int
    current_file_id: int | None
    heartbeat_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    created_at: datetime


class JobDetailRead(JobRead):
    items: list[JobItemRead]
