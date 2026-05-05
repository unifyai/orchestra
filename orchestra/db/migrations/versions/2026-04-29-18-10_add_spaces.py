"""Add spaces, memberships, and invitations.

Spaces are named shared memory pools owned by a user and optionally attached
to an organization. Memberships connect assistants to spaces as live rows,
while invitations persist the user-anchored state machine for cross-owner
membership requests.

Revision ID: add_spaces
Revises: drop_platform_email_cost_rows
Create Date: 2026-04-29 18:10:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "add_spaces"
down_revision = "drop_platform_email_cost_rows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "spaces",
        sa.Column("space_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 200",
            name="ck_spaces_name_length",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'deleting')",
            name="ck_spaces_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("space_id"),
    )
    op.create_index(
        "ix_spaces_organization_id",
        "spaces",
        ["organization_id"],
    )
    op.create_index(
        "ix_spaces_owner_user_id",
        "spaces",
        ["owner_user_id"],
    )

    op.create_table(
        "assistant_space_memberships",
        sa.Column("assistant_id", sa.Integer(), nullable=False),
        sa.Column("space_id", sa.BigInteger(), nullable=False),
        sa.Column("added_by", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["assistant_id"],
            ["assistants.agent_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["spaces.space_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("assistant_id", "space_id"),
    )
    op.create_index(
        "ix_asm_space_id",
        "assistant_space_memberships",
        ["space_id"],
    )

    op.create_table(
        "space_invites",
        sa.Column("invite_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("space_id", sa.BigInteger(), nullable=False),
        sa.Column("assistant_id", sa.Integer(), nullable=False),
        sa.Column("invited_by", sa.String(), nullable=False),
        sa.Column("invited_owner_id", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'declined', 'cancelled', 'expired')",
            name="ck_space_invites_status",
        ),
        sa.ForeignKeyConstraint(
            ["assistant_id"],
            ["assistants.agent_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by"],
            ["user.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_owner_id"],
            ["user.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["spaces.space_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("invite_id"),
    )
    op.create_index(
        "ix_space_invites_pending",
        "space_invites",
        ["space_id", "assistant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_space_invites_invited_owner",
        "space_invites",
        ["invited_owner_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_space_invites_invited_owner", table_name="space_invites")
    op.drop_index("ix_space_invites_pending", table_name="space_invites")
    op.drop_table("space_invites")
    op.drop_index("ix_asm_space_id", table_name="assistant_space_memberships")
    op.drop_table("assistant_space_memberships")
    op.drop_index("ix_spaces_owner_user_id", table_name="spaces")
    op.drop_index("ix_spaces_organization_id", table_name="spaces")
    op.drop_table("spaces")
