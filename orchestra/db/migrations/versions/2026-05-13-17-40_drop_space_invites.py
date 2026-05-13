"""Drop space invitation persistence now that membership adds are direct only.

Revision ID: drop_space_invites
Revises: remove_org_default_space_kind
Create Date: 2026-05-13 17:40:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "drop_space_invites"
down_revision = "remove_org_default_space_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_space_invites_invited_owner", table_name="space_invites")
    op.drop_index("ix_space_invites_pending", table_name="space_invites")
    op.drop_table("space_invites")


def downgrade() -> None:
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
