"""Add per-role reasoning effort configuration.

Revision ID: 20260830_0011
Revises: 20260829_0010
Create Date: 2026-08-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260830_0011"
down_revision: str | None = "20260829_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_role_bindings",
        sa.Column("reasoning_effort", sa.String(length=32), nullable=True),
    )
    op.execute(
        "UPDATE model_role_bindings SET reasoning_effort = 'low' "
        "WHERE role IN ('document_conversion', 'index_generation') "
        "AND reasoning_effort IS NULL"
    )
    op.execute(
        "UPDATE model_role_bindings SET reasoning_effort = 'model_default' "
        "WHERE role IN ('query_router', 'answer_generation') "
        "AND reasoning_effort IS NULL"
    )


def downgrade() -> None:
    op.drop_column("model_role_bindings", "reasoning_effort")
