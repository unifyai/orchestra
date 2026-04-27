"""Add inactivity tracking columns to assistants.

Three nullable timestamp columns that drive the re-engagement follow-up
routine:

- ``last_correspondence_at`` — timestamp of the most recent inbound or
  outbound message across *all* contacts for this assistant. Written by
  the transcript hook on every logged message. Backfilled to ``now()``
  for existing rows so the inactivity clock starts on rollout rather
  than blasting follow-ups to historical assistants on day one.

- ``last_followup_sent_at`` — timestamp when the assistant dispatched
  its inactivity follow-up. Cleared back to ``NULL`` when fresh activity
  arrives so a re-abandoned conversation can re-trigger.

- ``termination_initiated_at`` — timestamp when the follow-up routine
  marked the assistant for cleanup. Kicks off the grace-period window
  before ``deprovision_assistant_contacts`` runs and the assistant is
  hard-deleted.

All three are nullable by design: existing install-base rows should not
have to pretend they sent a follow-up. Indexes target the two routine
queries (followup candidates, auto-cleanup candidates).

Revision ID: add_assistant_inactivity_columns
Revises: add_hives_and_assistant_hive_id
Create Date: 2026-04-27 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "add_assistant_inactivity_columns"
down_revision = "add_context_counter"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column(
            "last_correspondence_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "assistants",
        sa.Column(
            "last_followup_sent_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "assistants",
        sa.Column(
            "termination_initiated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_assistants_last_correspondence_at",
        "assistants",
        ["last_correspondence_at"],
    )
    op.create_index(
        "ix_assistants_termination_initiated_at",
        "assistants",
        ["termination_initiated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assistants_termination_initiated_at",
        table_name="assistants",
    )
    op.drop_index(
        "ix_assistants_last_correspondence_at",
        table_name="assistants",
    )
    op.drop_column("assistants", "termination_initiated_at")
    op.drop_column("assistants", "last_followup_sent_at")
    op.drop_column("assistants", "last_correspondence_at")
