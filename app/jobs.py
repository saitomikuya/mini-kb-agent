"""Shared background-job state values."""

from enum import StrEnum


class JobStatus(StrEnum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class JobControlState(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


ACTIVE_JOB_STATUSES = (
    JobStatus.PENDING,
    JobStatus.QUEUED,
    JobStatus.RUNNING,
)
