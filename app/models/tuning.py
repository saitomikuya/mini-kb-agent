"""Persisted administrator-controlled retrieval and answer tuning."""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, JSON, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class KnowledgeTuningSetting(Base):
    __tablename__ = "knowledge_tuning_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    values_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
