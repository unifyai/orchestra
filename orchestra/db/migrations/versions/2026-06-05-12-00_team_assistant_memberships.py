"""Add team assistant memberships and team-scoped contact overlays.

Revision ID: 2026_team_assistant_memberships
Revises: 2026_plot_table_context_fks
Create Date: 2026-06-05 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "2026_team_assistant_memberships"
down_revision = "2026_plot_table_context_fks"
branch_labels = None
depends_on = None

TEAM_STATUS_ACTIVE = "active"


def upgrade() -> None:
    op.add_column(
        "team",
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=TEAM_STATUS_ACTIVE,
        ),
    )

    op.create_table(
        "team_assistant_memberships",
        sa.Column(
            "team_id",
            sa.Integer(),
            sa.ForeignKey("team.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assistant_id",
            sa.Integer(),
            sa.ForeignKey("assistants.agent_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("added_by", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "team_id",
            "assistant_id",
            name="pk_team_assistant_memberships",
        ),
    )
    op.create_index(
        "ix_team_assistant_memberships_assistant_id",
        "team_assistant_memberships",
        ["assistant_id"],
    )

    op.add_column(
        "contact_memberships",
        sa.Column(
            "target_team_id",
            sa.Integer(),
            sa.ForeignKey("team.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_contact_memberships_target_team_id",
        "contact_memberships",
        ["target_team_id"],
        postgresql_where=sa.text("target_team_id IS NOT NULL"),
    )
    op.create_index(
        "ix_contact_memberships_assistant_team_target",
        "contact_memberships",
        ["assistant_id", "target_team_id"],
        postgresql_where=sa.text("target_scope = 'team'"),
    )
    op.create_index(
        "ux_contact_memberships_team_pair",
        "contact_memberships",
        ["assistant_id", "contact_id", "target_team_id"],
        unique=True,
        postgresql_where=sa.text("target_scope = 'team'"),
    )

    op.drop_constraint(
        "ck_contact_memberships_target_scope",
        "contact_memberships",
        type_="check",
    )
    op.drop_constraint(
        "ck_contact_memberships_scope_space_consistency",
        "contact_memberships",
        type_="check",
    )
    op.create_check_constraint(
        "ck_contact_memberships_target_scope",
        "contact_memberships",
        "target_scope IN ('personal', 'space', 'team')",
    )
    op.create_check_constraint(
        "ck_contact_memberships_scope_target_consistency",
        "contact_memberships",
        "("
        "(target_scope = 'personal' AND target_space_id IS NULL AND target_team_id IS NULL) OR "
        "(target_scope = 'space' AND target_space_id IS NOT NULL AND target_team_id IS NULL) OR "
        "(target_scope = 'team' AND target_team_id IS NOT NULL AND target_space_id IS NULL)"
        ")",
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM contact_memberships WHERE target_scope = 'team'",
    )
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
        "target_scope IN ('personal', 'space')",
    )
    op.create_check_constraint(
        "ck_contact_memberships_scope_space_consistency",
        "contact_memberships",
        "("
        "target_scope NOT IN ('personal', 'space') OR ("
        "target_scope = 'space' AND target_space_id IS NOT NULL"
        ") OR ("
        "target_scope = 'personal' AND target_space_id IS NULL"
        ")"
        ")",
    )

    op.drop_index(
        "ux_contact_memberships_team_pair",
        table_name="contact_memberships",
    )
    op.drop_index(
        "ix_contact_memberships_assistant_team_target",
        table_name="contact_memberships",
    )
    op.drop_index(
        "ix_contact_memberships_target_team_id",
        table_name="contact_memberships",
    )
    op.drop_column("contact_memberships", "target_team_id")

    op.drop_index(
        "ix_team_assistant_memberships_assistant_id",
        table_name="team_assistant_memberships",
    )
    op.drop_table("team_assistant_memberships")
    op.drop_column("team", "status")
