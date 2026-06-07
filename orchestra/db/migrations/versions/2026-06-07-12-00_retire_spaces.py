"""Retire shared-space tables and contact overlay scope.

Revision ID: 2026_retire_spaces
Revises: 2026_team_assistant_memberships
Create Date: 2026-06-07 12:00:00.000000
"""

from alembic import op

revision = "2026_retire_spaces"
down_revision = "2026_team_assistant_memberships"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM contact_memberships WHERE target_scope = 'space'")

    op.drop_index(
        "ux_contact_memberships_space_pair",
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

    op.drop_constraint(
        "contact_memberships_target_space_id_fkey",
        "contact_memberships",
        type_="foreignkey",
    )
    op.drop_column("contact_memberships", "target_space_id")

    op.drop_constraint(
        "ck_contact_memberships_scope_target_consistency",
        "contact_memberships",
        type_="check",
    )
    op.drop_constraint(
        "ck_contact_memberships_target_scope",
        "contact_memberships",
        type_="check",
    )
    op.create_check_constraint(
        "ck_contact_memberships_target_scope",
        "contact_memberships",
        "target_scope IN ('personal', 'team')",
    )
    op.create_check_constraint(
        "ck_contact_memberships_scope_target_consistency",
        "contact_memberships",
        "("
        "(target_scope = 'personal' AND target_team_id IS NULL) OR "
        "(target_scope = 'team' AND target_team_id IS NOT NULL)"
        ")",
    )

    op.drop_table("assistant_space_memberships")
    op.drop_table("spaces")


def downgrade() -> None:
    raise NotImplementedError(
        "Shared-space tables were removed; downgrade is not supported.",
    )
