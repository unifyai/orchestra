"""Add durable assistant cleanup task outbox.

Revision ID: add_assistant_cleanup_tasks
Revises: shared_pool_conflict_resolution
Create Date: 2026-04-02 18:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "add_assistant_cleanup_tasks"
down_revision = "shared_pool_conflict_resolution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_cleanup_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("assistant_id", sa.Integer(), nullable=False),
        sa.Column("deploy_env", sa.String(), nullable=True),
        sa.Column("desktop_mode", sa.String(), nullable=True),
        sa.Column("source_flow", sa.String(), nullable=False),
        sa.Column(
            "cleanup_payload",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("last_result", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("next_retry_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("processing_started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_assistant_cleanup_task_status",
        ),
    )
    op.create_index(
        "ix_assistant_cleanup_tasks_status",
        "assistant_cleanup_tasks",
        ["status", "next_retry_at"],
    )
    op.create_index(
        "ix_assistant_cleanup_tasks_assistant",
        "assistant_cleanup_tasks",
        ["assistant_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assistant_cleanup_tasks_assistant",
        table_name="assistant_cleanup_tasks",
    )
    op.drop_index(
        "ix_assistant_cleanup_tasks_status",
        table_name="assistant_cleanup_tasks",
    )
    op.drop_table("assistant_cleanup_tasks")
