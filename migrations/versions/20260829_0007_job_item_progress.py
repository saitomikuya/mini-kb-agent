"""Persist live per-file conversion progress.

Revision ID: 20260829_0007
Revises: 20260829_0006
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260829_0007"
down_revision: str | None = "20260829_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("job_items", sa.Column("progress_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("job_items", "progress_json")
