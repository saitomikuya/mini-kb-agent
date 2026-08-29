"""Persistence models for durable background-job state."""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.jobs import JobControlState


_STATUS_VALUES = "'PENDING', 'QUEUED', 'RUNNING', 'COMPLETED', 'FAILED'"


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_STATUS_VALUES})",
            name="ck_jobs_status",
        ),
        CheckConstraint("total_items >= 0", name="ck_jobs_total_items"),
        CheckConstraint("completed_items >= 0", name="ck_jobs_completed_items"),
        CheckConstraint("failed_items >= 0", name="ck_jobs_failed_items"),
        CheckConstraint(
            "completed_items + failed_items <= total_items",
            name="ck_jobs_progress",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="PENDING",
        server_default="PENDING",
        index=True,
    )
    control_state: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=JobControlState.ACTIVE,
        server_default=JobControlState.ACTIVE.value,
    )
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    paused_seconds: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        server_default="0",
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )

    items: Mapped[list["JobItem"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="JobItem.id",
    )

    @property
    def elapsed_seconds(self) -> int:
        """Return wall time spent active, excluding paused/stopped intervals."""
        if self.started_at is None:
            return 0

        if self.finished_at is not None:
            end = self.finished_at
        elif self.control_state == JobControlState.PAUSED and self.paused_at:
            end = self.paused_at
        else:
            end = datetime.now(timezone.utc)

        start, compatible_end = _compatible_datetimes(self.started_at, end)
        active_seconds = (
            compatible_end - start
        ).total_seconds() - self.paused_seconds
        return max(0, round(active_seconds))


class JobItem(Base):
    __tablename__ = "job_items"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_STATUS_VALUES})",
            name="ck_job_items_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_job_items_attempts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="PENDING",
        server_default="PENDING",
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
    )

    job: Mapped[Job] = relationship(back_populates="items")


def _compatible_datetimes(
    first: datetime,
    second: datetime,
) -> tuple[datetime, datetime]:
    """Normalize SQLite's naive UTC values before datetime arithmetic."""
    if first.tzinfo is None and second.tzinfo is not None:
        second = second.astimezone(timezone.utc).replace(tzinfo=None)
    elif first.tzinfo is not None and second.tzinfo is None:
        first = first.astimezone(timezone.utc).replace(tzinfo=None)
    return first, second
