"""Generalize org calls into the unified call session store.

Renames ``org_call_session``/``org_call_participant`` to
``call_session``/``call_participant``, relaxes ``organization_id`` to
nullable (personal-workspace assistant calls have no org), extends the scope
check with ``assistant_dm``, drops the legacy ``dm_thread_id`` column, and
adds ``created_by_assistant_id`` (assistant-initiated rings) plus
``opening_config`` (voice-agent opening carried through redispatch).

Revision ID: unified_call_sessions
Revises: field_type_ui_editable
Create Date: 2026-08-07 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "unified_call_sessions"
down_revision = "field_type_ui_editable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- table renames (data preserving) ---
    op.rename_table("org_call_session", "call_session")
    op.rename_table("org_call_participant", "call_participant")

    # --- call_session shape changes ---
    op.alter_column("call_session", "organization_id", nullable=True)
    op.drop_column("call_session", "dm_thread_id")
    op.add_column(
        "call_session",
        sa.Column(
            "created_by_assistant_id",
            sa.Integer(),
            sa.ForeignKey(
                "assistants.agent_id",
                ondelete="SET NULL",
                name="fk_call_session_created_by_assistant_id",
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "call_session",
        sa.Column("opening_config", JSONB(), nullable=True),
    )

    # --- constraint / index renames onto the new table names ---
    op.execute(
        "ALTER TABLE call_session DROP CONSTRAINT IF EXISTS ck_org_call_session_scope",
    )
    op.create_check_constraint(
        "ck_call_session_scope",
        "call_session",
        "scope IN ('dm', 'team', 'group', 'assistant_dm')",
    )
    op.execute(
        "ALTER TABLE call_session RENAME CONSTRAINT "
        "ck_org_call_session_status TO ck_call_session_status",
    )
    op.execute(
        "ALTER INDEX ix_org_call_session_org_status RENAME TO ix_call_session_org_status",
    )
    op.execute(
        "ALTER INDEX ix_org_call_session_team_status RENAME TO ix_call_session_team_status",
    )
    op.execute(
        "ALTER INDEX ix_org_call_session_group_status RENAME TO ix_call_session_group_status",
    )
    op.execute(
        "ALTER TABLE call_participant RENAME CONSTRAINT "
        "ck_org_call_participant_role TO ck_call_participant_role",
    )
    op.execute(
        "ALTER TABLE call_participant RENAME CONSTRAINT "
        "ck_org_call_participant_status TO ck_call_participant_status",
    )
    op.execute(
        "ALTER TABLE call_participant RENAME CONSTRAINT "
        "uq_org_call_participant_call_user TO uq_call_participant_call_user",
    )
    op.execute(
        "ALTER INDEX ix_org_call_participant_user_status "
        "RENAME TO ix_call_participant_user_status",
    )


def downgrade() -> None:
    op.execute(
        "ALTER INDEX ix_call_participant_user_status "
        "RENAME TO ix_org_call_participant_user_status",
    )
    op.execute(
        "ALTER TABLE call_participant RENAME CONSTRAINT "
        "uq_call_participant_call_user TO uq_org_call_participant_call_user",
    )
    op.execute(
        "ALTER TABLE call_participant RENAME CONSTRAINT "
        "ck_call_participant_status TO ck_org_call_participant_status",
    )
    op.execute(
        "ALTER TABLE call_participant RENAME CONSTRAINT "
        "ck_call_participant_role TO ck_org_call_participant_role",
    )
    op.execute(
        "ALTER INDEX ix_call_session_group_status RENAME TO ix_org_call_session_group_status",
    )
    op.execute(
        "ALTER INDEX ix_call_session_team_status RENAME TO ix_org_call_session_team_status",
    )
    op.execute(
        "ALTER INDEX ix_call_session_org_status RENAME TO ix_org_call_session_org_status",
    )
    op.execute(
        "ALTER TABLE call_session RENAME CONSTRAINT "
        "ck_call_session_status TO ck_org_call_session_status",
    )
    op.execute(
        "ALTER TABLE call_session DROP CONSTRAINT IF EXISTS ck_call_session_scope",
    )
    op.execute("DELETE FROM call_session WHERE scope = 'assistant_dm'")
    op.create_check_constraint(
        "ck_org_call_session_scope",
        "call_session",
        "scope IN ('dm', 'team', 'group')",
    )
    op.drop_column("call_session", "opening_config")
    op.drop_column("call_session", "created_by_assistant_id")
    op.add_column(
        "call_session",
        sa.Column(
            "dm_thread_id",
            sa.Integer(),
            sa.ForeignKey("dm_thread.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.execute("DELETE FROM call_session WHERE organization_id IS NULL")
    op.alter_column("call_session", "organization_id", nullable=False)
    op.rename_table("call_participant", "org_call_participant")
    op.rename_table("call_session", "org_call_session")
