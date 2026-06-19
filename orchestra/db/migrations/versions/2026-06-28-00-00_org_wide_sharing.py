"""Add org-wide sharing mode metadata.

Revision ID: org_wide_sharing
Revises: reclassify_owner_keys
Create Date: 2026-06-28 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "org_wide_sharing"
down_revision = "reclassify_owner_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "team",
        sa.Column(
            "is_org_wide_sharing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "organization",
        sa.Column(
            "org_wide_sharing_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "organization",
        sa.Column("org_wide_sharing_team_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_organization_org_wide_sharing_team_id_team",
        "organization",
        "team",
        ["org_wide_sharing_team_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_organization_org_wide_sharing_team_id",
        "organization",
        ["org_wide_sharing_team_id"],
    )
    op.create_index(
        "ix_team_org_wide_sharing",
        "team",
        ["organization_id"],
        postgresql_where=sa.text("is_org_wide_sharing"),
    )


def downgrade() -> None:
    op.drop_index("ix_team_org_wide_sharing", table_name="team")
    op.drop_index("ix_organization_org_wide_sharing_team_id", table_name="organization")
    op.drop_constraint(
        "fk_organization_org_wide_sharing_team_id_team",
        "organization",
        type_="foreignkey",
    )
    op.drop_column("organization", "org_wide_sharing_team_id")
    op.drop_column("organization", "org_wide_sharing_enabled")
    op.drop_column("team", "is_org_wide_sharing")
