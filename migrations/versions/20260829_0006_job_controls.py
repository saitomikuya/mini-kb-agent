"""Add durable pause and stop controls to background jobs.

Revision ID: 20260829_0006
Revises: 20260828_0005
Create Date: 2026-08-29
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260829_0006"
down_revision: str | None = "20260828_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "control_state",
            sa.String(length=16),
            server_default="ACTIVE",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("jobs", "control_state")
