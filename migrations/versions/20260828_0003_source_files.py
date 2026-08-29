"""Add source file inventory and processing state.

Revision ID: 20260828_0003
Revises: 20260828_0002
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0003"
down_revision: str | None = "20260828_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("extension", sa.String(length=255), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "source_status",
            sa.String(length=16),
            server_default="PRESENT",
            nullable=False,
        ),
        sa.Column(
            "conversion_status",
            sa.String(length=16),
            server_default="NEW",
            nullable=False,
        ),
        sa.Column(
            "index_status",
            sa.String(length=16),
            server_default="NOT_INDEXED",
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
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
            "source_status IN ('PRESENT', 'MISSING')",
            name="ck_source_files_source_status",
        ),
        sa.CheckConstraint(
            "conversion_status IN "
            "('NEW', 'CHANGED', 'QUEUED', 'CONVERTING', 'READY', "
            "'FAILED', 'UNSUPPORTED')",
            name="ck_source_files_conversion_status",
        ),
        sa.CheckConstraint(
            "index_status IN ('NOT_INDEXED', 'INDEXED', 'STALE')",
            name="ck_source_files_index_status",
        ),
        sa.CheckConstraint("size >= 0", name="ck_source_files_size"),
        sa.CheckConstraint("mtime_ns >= 0", name="ck_source_files_mtime_ns"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("relative_path"),
    )


def downgrade() -> None:
    op.drop_table("source_files")
