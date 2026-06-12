"""Persist integration tool execution approval state.

Revision ID: integration_tool_exec_approvals
Revises: integration_tool_behavior_hints
Create Date: 2026-06-19 01:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "integration_tool_exec_approvals"
down_revision = "integration_tool_behavior_hints"
branch_labels = None
depends_on = None

JSON_EMPTY_ARRAY = sa.text("'[]'::jsonb")
JSON_EMPTY_OBJECT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.add_column(
        "provider_action_audits",
        sa.Column("provider_connection_id", sa.String(), nullable=True),
    )
    op.add_column(
        "provider_action_audits",
        sa.Column("tool_id", sa.String(), nullable=True),
    )
    op.add_column(
        "provider_action_audits",
        sa.Column(
            "behavior_hints_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_ARRAY,
        ),
    )
    op.add_column(
        "provider_action_audits",
        sa.Column("arguments_hash", sa.String(), nullable=True),
    )
    op.add_column(
        "provider_action_audits",
        sa.Column(
            "arguments_summary_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_OBJECT,
        ),
    )
    op.add_column(
        "provider_action_audits",
        sa.Column("approval_scope", sa.String(), nullable=True),
    )
    op.add_column(
        "provider_action_audits",
        sa.Column("approval_level", sa.String(), nullable=True),
    )
    op.add_column(
        "provider_action_audits",
        sa.Column("approved_by", sa.String(), nullable=True),
    )
    op.add_column(
        "provider_action_audits",
        sa.Column("denied_by", sa.String(), nullable=True),
    )
    op.add_column(
        "provider_action_audits",
        sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "provider_action_audits",
        sa.Column("denied_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "provider_action_audits",
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "provider_action_audits",
        sa.Column("provider_status_code", sa.Integer(), nullable=True),
    )
    op.add_column(
        "provider_action_audits",
        sa.Column("provider_response_body", sa.Text(), nullable=True),
    )
    op.add_column(
        "provider_action_audits",
        sa.Column(
            "provider_request_summary_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_OBJECT,
        ),
    )
    for column_name in [
        "provider_connection_id",
        "tool_id",
        "arguments_hash",
        "approval_scope",
        "expires_at",
    ]:
        op.create_index(
            f"ix_provider_action_audits_{column_name}",
            "provider_action_audits",
            [column_name],
        )


def downgrade() -> None:
    for column_name in [
        "expires_at",
        "approval_scope",
        "arguments_hash",
        "tool_id",
        "provider_connection_id",
    ]:
        op.drop_index(
            f"ix_provider_action_audits_{column_name}",
            table_name="provider_action_audits",
        )
    for column_name in [
        "provider_request_summary_json",
        "provider_response_body",
        "provider_status_code",
        "expires_at",
        "denied_at",
        "approved_at",
        "denied_by",
        "approved_by",
        "approval_level",
        "approval_scope",
        "arguments_summary_json",
        "arguments_hash",
        "behavior_hints_json",
        "tool_id",
        "provider_connection_id",
    ]:
        op.drop_column("provider_action_audits", column_name)
