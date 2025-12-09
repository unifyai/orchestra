"""Add organization_invite table for invitation flow

Revision ID: add_organization_invites
Revises: add_foreign_keys_to_context
Create Date: 2025-12-08 12:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_organization_invites"
down_revision = "add_foreign_keys_to_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Add organization_invite table for managing pending invitations.

    Invites are deleted when accepted or declined, so no status column is needed.
    Expired invites are cleaned up via admin endpoint.
    """
    op.create_table(
        "organization_invite",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("token", sa.String(), nullable=False, unique=True, index=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("invitee_email", sa.String(), nullable=False, index=True),
        sa.Column(
            "invitee_user_id",
            sa.String(),
            sa.ForeignKey("auth_user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "invited_by_user_id",
            sa.String(),
            sa.ForeignKey("auth_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role_id",
            sa.Integer(),
            sa.ForeignKey("role.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("level", sa.String(), nullable=False, server_default="user"),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Create index on organization_id + invitee_email for faster lookups
    op.create_index(
        "ix_org_invite_org_email",
        "organization_invite",
        ["organization_id", "invitee_email"],
    )


def downgrade() -> None:
    """Remove organization_invite table."""
    op.drop_index("ix_org_invite_org_email", table_name="organization_invite")
    op.drop_table("organization_invite")
