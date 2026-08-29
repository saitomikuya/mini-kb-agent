"""Add immutable hierarchical-index generation metadata.

Revision ID: 20260828_0005
Revises: 20260828_0004
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0005"
down_revision: str | None = "20260828_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "index_generations",
        sa.Column("generation_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("root_index_path", sa.Text(), nullable=False),
        sa.Column(
            "document_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('BUILDING', 'VALIDATED', 'ACTIVE', 'SUPERSEDED', 'FAILED')",
            name="ck_index_generations_status",
        ),
        sa.CheckConstraint(
            "generation_number > 0",
            name="ck_index_generations_number",
        ),
        sa.CheckConstraint(
            "document_count >= 0",
            name="ck_index_generations_document_count",
        ),
        sa.PrimaryKeyConstraint("generation_number"),
    )


def downgrade() -> None:
    op.drop_table("index_generations")
