"""Add providers, model profiles, and model role bindings.

Revision ID: 20260828_0002
Revises: 20260828_0001
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0002"
down_revision: str | None = "20260828_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_providers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("provider_type", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column(
            "protocol_preference",
            sa.String(length=32),
            server_default="auto",
            nullable=False,
        ),
        sa.Column("extra_headers_json", sa.JSON(), nullable=False),
        sa.Column(
            "azure_mode",
            sa.String(length=16),
            server_default="v1",
            nullable=False,
        ),
        sa.Column("azure_api_version", sa.String(length=64), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default="1",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provider_type IN ('openai_compatible', 'azure_openai', 'sub2api')",
            name="ck_api_providers_provider_type",
        ),
        sa.CheckConstraint(
            "protocol_preference IN ('auto', 'responses', 'chat_completions')",
            name="ck_api_providers_protocol_preference",
        ),
        sa.CheckConstraint(
            "azure_mode IN ('v1', 'legacy')",
            name="ck_api_providers_azure_mode",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_api_providers_name"),
    )
    op.create_table(
        "model_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("remote_model_name", sa.String(length=300), nullable=False),
        sa.Column("protocol_override", sa.String(length=32), nullable=True),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_effort", sa.String(length=32), nullable=True),
        sa.Column("extra_request_json", sa.JSON(), nullable=False),
        sa.Column(
            "supports_text",
            sa.Boolean(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "supports_vision",
            sa.Boolean(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "supports_structured_output",
            sa.Boolean(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("tested_protocol", sa.String(length=32), nullable=True),
        sa.Column("last_test_status", sa.String(length=16), nullable=True),
        sa.Column("last_test_latency_ms", sa.Integer(), nullable=True),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default="1",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "protocol_override IS NULL OR "
            "protocol_override IN ('auto', 'responses', 'chat_completions')",
            name="ck_model_profiles_protocol_override",
        ),
        sa.CheckConstraint(
            "tested_protocol IS NULL OR "
            "tested_protocol IN ('responses', 'chat_completions')",
            name="ck_model_profiles_tested_protocol",
        ),
        sa.CheckConstraint(
            "last_test_status IS NULL OR "
            "last_test_status IN ('passed', 'partial', 'failed')",
            name="ck_model_profiles_last_test_status",
        ),
        sa.CheckConstraint(
            "context_window IS NULL OR context_window > 0",
            name="ck_model_profiles_context_window",
        ),
        sa.CheckConstraint(
            "max_output_tokens IS NULL OR max_output_tokens > 0",
            name="ck_model_profiles_max_output_tokens",
        ),
        sa.CheckConstraint(
            "last_test_latency_ms IS NULL OR last_test_latency_ms >= 0",
            name="ck_model_profiles_last_test_latency_ms",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["api_providers.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_id",
            "name",
            name="uq_model_profiles_provider_name",
        ),
    )
    op.create_index(
        op.f("ix_model_profiles_provider_id"),
        "model_profiles",
        ["provider_id"],
        unique=False,
    )
    op.create_table(
        "model_role_bindings",
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("model_profile_id", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('document_conversion', 'index_generation', "
            "'query_router', 'answer_generation')",
            name="ck_model_role_bindings_role",
        ),
        sa.ForeignKeyConstraint(
            ["model_profile_id"],
            ["model_profiles.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("role"),
    )
    op.create_index(
        op.f("ix_model_role_bindings_model_profile_id"),
        "model_role_bindings",
        ["model_profile_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_model_role_bindings_model_profile_id"),
        table_name="model_role_bindings",
    )
    op.drop_table("model_role_bindings")
    op.drop_index(
        op.f("ix_model_profiles_provider_id"),
        table_name="model_profiles",
    )
    op.drop_table("model_profiles")
    op.drop_table("api_providers")
