"""Rename provider-event *_activation_revision columns to *_revision.

Aligns ORM/DB vocabulary with the Tasks/Executions identity model
(desired_revision / observed_revision / accepted_revision).

Revision ID: rename_pe_revision_cols
Revises: unified_call_sessions
Create Date: 2026-08-08 00:00:00.000000
"""

from alembic import op

revision = "rename_pe_revision_cols"
down_revision = "unified_call_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "event_trigger_bindings",
        "desired_activation_revision",
        new_column_name="desired_revision",
    )
    op.alter_column(
        "event_trigger_bindings",
        "observed_activation_revision",
        new_column_name="observed_revision",
    )
    op.alter_column(
        "event_trigger_subscription_generations",
        "desired_activation_revision",
        new_column_name="desired_revision",
    )
    op.alter_column(
        "provider_event_receipts",
        "accepted_activation_revision",
        new_column_name="accepted_revision",
    )
    op.alter_column(
        "provider_event_dispatches",
        "accepted_activation_revision",
        new_column_name="accepted_revision",
    )


def downgrade() -> None:
    op.alter_column(
        "provider_event_dispatches",
        "accepted_revision",
        new_column_name="accepted_activation_revision",
    )
    op.alter_column(
        "provider_event_receipts",
        "accepted_revision",
        new_column_name="accepted_activation_revision",
    )
    op.alter_column(
        "event_trigger_subscription_generations",
        "desired_revision",
        new_column_name="desired_activation_revision",
    )
    op.alter_column(
        "event_trigger_bindings",
        "observed_revision",
        new_column_name="observed_activation_revision",
    )
    op.alter_column(
        "event_trigger_bindings",
        "desired_revision",
        new_column_name="desired_activation_revision",
    )
