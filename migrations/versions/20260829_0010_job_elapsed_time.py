"""Persist paused intervals for accurate background-job elapsed time.

Revision ID: 20260829_0010
Revises: 20260829_0009
Create Date: 2026-08-29
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260829_0010"
down_revision: str | None = "20260829_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "paused_seconds",
            sa.Float(),
            server_default="0",
            nullable=False,
        ),
    )
    # Existing paused rows have no historical pause timestamp. Freeze them at
    # migration time (or their last heartbeat when available) so they no longer
    # continue accumulating after this deployment.
    op.execute(
        "UPDATE jobs "
        "SET paused_at = COALESCE(heartbeat_at, CURRENT_TIMESTAMP) "
        "WHERE control_state = 'PAUSED' AND started_at IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("jobs", "paused_seconds")
    op.drop_column("jobs", "paused_at")
