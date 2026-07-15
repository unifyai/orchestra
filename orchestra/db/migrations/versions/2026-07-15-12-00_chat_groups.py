"""Add chat_group tables and group-scoped org calls.

Revision ID: chat_groups
Revises: org_call_multiparty
Create Date: 2026-07-15 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "chat_groups"
down_revision = "org_call_multiparty"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_group",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_by_user_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'deleted')",
            name="ck_chat_group_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_chat_group_organization_id",
        "chat_group",
        ["organization_id"],
    )
    op.create_index(
        "ix_chat_group_org_status",
        "chat_group",
        ["organization_id", "status"],
    )

    op.create_table(
        "chat_group_member",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("assistant_id", sa.Integer(), nullable=True),
        sa.Column("role", sa.String(), nullable=False, server_default="member"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "(user_id IS NOT NULL AND assistant_id IS NULL) OR "
            "(user_id IS NULL AND assistant_id IS NOT NULL)",
            name="ck_chat_group_member_one_identity",
        ),
        sa.CheckConstraint(
            "role IN ('member')",
            name="ck_chat_group_member_role",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["chat_group.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assistant_id"],
            ["assistants.agent_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_group_member_group_id", "chat_group_member", ["group_id"])
    op.create_index(
        "uq_chat_group_member_user",
        "chat_group_member",
        ["group_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_index(
        "uq_chat_group_member_assistant",
        "chat_group_member",
        ["group_id", "assistant_id"],
        unique=True,
        postgresql_where=sa.text("assistant_id IS NOT NULL"),
    )

    op.drop_constraint("ck_org_call_session_scope", "org_call_session", type_="check")
    op.create_check_constraint(
        "ck_org_call_session_scope",
        "org_call_session",
        "scope IN ('dm', 'team', 'group')",
    )
    op.add_column(
        "org_call_session",
        sa.Column("group_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_org_call_session_group_id_chat_group",
        "org_call_session",
        "chat_group",
        ["group_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_org_call_session_group_status",
        "org_call_session",
        ["group_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_org_call_session_group_status", table_name="org_call_session")
    op.drop_constraint(
        "fk_org_call_session_group_id_chat_group",
        "org_call_session",
        type_="foreignkey",
    )
    op.drop_column("org_call_session", "group_id")
    op.drop_constraint("ck_org_call_session_scope", "org_call_session", type_="check")
    op.create_check_constraint(
        "ck_org_call_session_scope",
        "org_call_session",
        "scope IN ('dm', 'team')",
    )
    op.drop_index("uq_chat_group_member_assistant", table_name="chat_group_member")
    op.drop_index("uq_chat_group_member_user", table_name="chat_group_member")
    op.drop_index("ix_chat_group_member_group_id", table_name="chat_group_member")
    op.drop_table("chat_group_member")
    op.drop_index("ix_chat_group_org_status", table_name="chat_group")
    op.drop_index("ix_chat_group_organization_id", table_name="chat_group")
    op.drop_table("chat_group")
