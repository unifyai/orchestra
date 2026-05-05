"""Add contact membership overlays.

Revision ID: add_contact_memberships
Revises: add_org_coordinator_singleton
Create Date: 2026-05-01 13:05:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "add_contact_memberships"
down_revision = "add_org_coordinator_singleton"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contact_memberships",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("assistant_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("target_scope", sa.Text(), nullable=False),
        sa.Column("target_space_id", sa.BigInteger(), nullable=True),
        sa.Column("relationship", sa.Text(), nullable=False),
        sa.Column(
            "should_respond",
            sa.Boolean(),
            server_default="true",
            nullable=False,
        ),
        sa.Column(
            "response_policy",
            sa.Text(),
            server_default="standard",
            nullable=False,
        ),
        sa.Column(
            "can_edit",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "target_scope IN ('personal', 'space')",
            name="ck_contact_memberships_target_scope",
        ),
        sa.CheckConstraint(
            "target_scope NOT IN ('personal', 'space') OR ("
            "target_scope = 'space' AND target_space_id IS NOT NULL"
            ") OR ("
            "target_scope = 'personal' AND target_space_id IS NULL"
            ")",
            name="ck_contact_memberships_scope_space_consistency",
        ),
        sa.CheckConstraint(
            "relationship IN ('self', 'boss', 'coworker', 'other')",
            name="ck_contact_memberships_relationship",
        ),
        sa.ForeignKeyConstraint(
            ["assistant_id"],
            ["assistants.agent_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_space_id"],
            ["spaces.space_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_contact_memberships_assistant_id",
        "contact_memberships",
        ["assistant_id"],
    )
    op.create_index(
        "ix_contact_memberships_target_space_id",
        "contact_memberships",
        ["target_space_id"],
        postgresql_where=sa.text("target_space_id IS NOT NULL"),
    )
    op.create_index(
        "ix_contact_memberships_assistant_space_target",
        "contact_memberships",
        ["assistant_id", "target_space_id"],
        postgresql_where=sa.text("target_scope = 'space'"),
    )
    op.create_index(
        "ix_contact_memberships_assistant_personal_self",
        "contact_memberships",
        ["assistant_id"],
        postgresql_where=sa.text(
            "target_scope = 'personal' AND relationship = 'self'",
        ),
    )
    op.create_index(
        "ux_contact_memberships_personal_pair",
        "contact_memberships",
        ["assistant_id", "contact_id"],
        unique=True,
        postgresql_where=sa.text("target_scope = 'personal'"),
    )
    op.create_index(
        "ux_contact_memberships_space_pair",
        "contact_memberships",
        ["assistant_id", "contact_id", "target_space_id"],
        unique=True,
        postgresql_where=sa.text("target_scope = 'space'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ux_contact_memberships_space_pair",
        table_name="contact_memberships",
    )
    op.drop_index(
        "ux_contact_memberships_personal_pair",
        table_name="contact_memberships",
    )
    op.drop_index(
        "ix_contact_memberships_assistant_personal_self",
        table_name="contact_memberships",
    )
    op.drop_index(
        "ix_contact_memberships_assistant_space_target",
        table_name="contact_memberships",
    )
    op.drop_index(
        "ix_contact_memberships_target_space_id",
        table_name="contact_memberships",
    )
    op.drop_index(
        "ix_contact_memberships_assistant_id",
        table_name="contact_memberships",
    )
    op.drop_table("contact_memberships")
