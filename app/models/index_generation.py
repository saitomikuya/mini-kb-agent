"""Persistence metadata for immutable hierarchical-index generations."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class IndexGeneration(Base):
    __tablename__ = "index_generations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('BUILDING', 'VALIDATED', 'ACTIVE', 'SUPERSEDED', 'FAILED')",
            name="ck_index_generations_status",
        ),
        CheckConstraint(
            "generation_number > 0",
            name="ck_index_generations_number",
        ),
        CheckConstraint(
            "document_count >= 0",
            name="ck_index_generations_document_count",
        ),
    )

    generation_number: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    root_index_path: Mapped[str] = mapped_column(Text, nullable=False)
    document_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
