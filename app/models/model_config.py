"""Persistence for API providers, model profiles, and role bindings."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class APIProvider(Base):
    __tablename__ = "api_providers"
    __table_args__ = (
        CheckConstraint(
            "provider_type IN ('openai_compatible', 'azure_openai', 'sub2api')",
            name="ck_api_providers_provider_type",
        ),
        CheckConstraint(
            "protocol_preference IN ('auto', 'responses', 'chat_completions')",
            name="ck_api_providers_protocol_preference",
        ),
        CheckConstraint(
            "azure_mode IN ('v1', 'legacy')",
            name="ck_api_providers_azure_mode",
        ),
        UniqueConstraint("name", name="uq_api_providers_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    protocol_preference: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="auto",
        server_default="auto",
    )
    extra_headers_json: Mapped[dict[str, str]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    azure_mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="v1",
        server_default="v1",
    )
    azure_api_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
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

    model_profiles: Mapped[list["ModelProfile"]] = relationship(
        back_populates="provider",
        passive_deletes=True,
    )


class ModelProfile(Base):
    __tablename__ = "model_profiles"
    __table_args__ = (
        CheckConstraint(
            "protocol_override IS NULL OR "
            "protocol_override IN ('auto', 'responses', 'chat_completions')",
            name="ck_model_profiles_protocol_override",
        ),
        CheckConstraint(
            "tested_protocol IS NULL OR "
            "tested_protocol IN ('responses', 'chat_completions')",
            name="ck_model_profiles_tested_protocol",
        ),
        CheckConstraint(
            "last_test_status IS NULL OR "
            "last_test_status IN ('passed', 'partial', 'failed')",
            name="ck_model_profiles_last_test_status",
        ),
        CheckConstraint(
            "context_window IS NULL OR context_window > 0",
            name="ck_model_profiles_context_window",
        ),
        CheckConstraint(
            "max_output_tokens IS NULL OR max_output_tokens > 0",
            name="ck_model_profiles_max_output_tokens",
        ),
        CheckConstraint(
            "last_test_latency_ms IS NULL OR last_test_latency_ms >= 0",
            name="ck_model_profiles_last_test_latency_ms",
        ),
        UniqueConstraint(
            "provider_id",
            "name",
            name="uq_model_profiles_provider_name",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(
        ForeignKey("api_providers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    remote_model_name: Mapped[str] = mapped_column(String(300), nullable=False)
    protocol_override: Mapped[str | None] = mapped_column(String(32), nullable=True)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    extra_request_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    supports_text: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    supports_vision: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    supports_structured_output: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    tested_protocol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_test_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_test_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
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

    provider: Mapped[APIProvider] = relationship(back_populates="model_profiles")
    role_bindings: Mapped[list["ModelRoleBinding"]] = relationship(
        back_populates="model_profile",
        passive_deletes=True,
    )


class ModelRoleBinding(Base):
    __tablename__ = "model_role_bindings"
    __table_args__ = (
        CheckConstraint(
            "role IN ('document_conversion', 'index_generation', "
            "'query_router', 'answer_generation')",
            name="ck_model_role_bindings_role",
        ),
    )

    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    model_profile_id: Mapped[int] = mapped_column(
        ForeignKey("model_profiles.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    model_profile: Mapped[ModelProfile] = relationship(back_populates="role_bindings")


class ModelRolePromptSetting(Base):
    """Editable prompts kept separate from the optional model binding."""

    __tablename__ = "model_role_prompt_settings"
    __table_args__ = (
        CheckConstraint(
            "role IN ('document_conversion', 'index_generation', "
            "'query_router', 'answer_generation')",
            name="ck_model_role_prompt_settings_role",
        ),
    )

    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    prompts_json: Mapped[dict[str, str]] = mapped_column(
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
