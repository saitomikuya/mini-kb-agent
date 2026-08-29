"""Persistence model for source-of-truth knowledge files."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SourceFile(Base):
    __tablename__ = "source_files"
    __table_args__ = (
        CheckConstraint(
            "source_status IN ('PRESENT', 'MISSING')",
            name="ck_source_files_source_status",
        ),
        CheckConstraint(
            "conversion_status IN "
            "('NEW', 'CHANGED', 'QUEUED', 'CONVERTING', 'READY', "
            "'FAILED', 'UNSUPPORTED')",
            name="ck_source_files_conversion_status",
        ),
        CheckConstraint(
            "index_status IN ('NOT_INDEXED', 'INDEXED', 'STALE')",
            name="ck_source_files_index_status",
        ),
        CheckConstraint("size >= 0", name="ck_source_files_size"),
        CheckConstraint("mtime_ns >= 0", name="ck_source_files_mtime_ns"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    extension: Mapped[str] = mapped_column(String(255), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="PRESENT",
        server_default="PRESENT",
    )
    conversion_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="NEW",
        server_default="NEW",
    )
    index_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="NOT_INDEXED",
        server_default="NOT_INDEXED",
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    converted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
