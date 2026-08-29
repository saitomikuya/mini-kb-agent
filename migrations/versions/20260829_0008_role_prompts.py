"""Add editable prompt settings for supported model-role tasks.

Revision ID: 20260829_0008
Revises: 20260829_0007
Create Date: 2026-08-29
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260829_0008"
down_revision: str | None = "20260829_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_role_prompt_settings",
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("prompts_json", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('document_conversion', 'index_generation', "
            "'query_router', 'answer_generation')",
            name="ck_model_role_prompt_settings_role",
        ),
        sa.PrimaryKeyConstraint("role"),
    )


def downgrade() -> None:
    op.drop_table("model_role_prompt_settings")
