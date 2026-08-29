"""Add durable background jobs and job items.

Revision ID: 20260828_0004
Revises: 20260828_0003
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0004"
down_revision: str | None = "20260828_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STATUS_VALUES = "'PENDING', 'QUEUED', 'RUNNING', 'COMPLETED', 'FAILED'"


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("total_items", sa.Integer(), nullable=False),
        sa.Column("completed_items", sa.Integer(), nullable=False),
        sa.Column("failed_items", sa.Integer(), nullable=False),
        sa.Column("current_file_id", sa.Integer(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"status IN ({_STATUS_VALUES})",
            name="ck_jobs_status",
        ),
        sa.CheckConstraint("total_items >= 0", name="ck_jobs_total_items"),
        sa.CheckConstraint(
            "completed_items >= 0",
            name="ck_jobs_completed_items",
        ),
        sa.CheckConstraint("failed_items >= 0", name="ck_jobs_failed_items"),
        sa.CheckConstraint(
            "completed_items + failed_items <= total_items",
            name="ck_jobs_progress",
        ),
        sa.ForeignKeyConstraint(
            ["current_file_id"],
            ["source_files.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"], unique=False)

    op.create_table(
        "job_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("source_file_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            f"status IN ({_STATUS_VALUES})",
            name="ck_job_items_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_job_items_attempts"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_file_id"],
            ["source_files.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_items_job_id", "job_items", ["job_id"], unique=False)
    op.create_index("ix_job_items_status", "job_items", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_job_items_status", table_name="job_items")
    op.drop_index("ix_job_items_job_id", table_name="job_items")
    op.drop_table("job_items")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_table("jobs")
