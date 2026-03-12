"""Add desktop_filesync_sshkey column to assistants table.

Stores the SSH private key used for desktop filesystem sync directly
on the assistant row, replacing the removed assistant_secrets table.

Revision ID: add_desktop_filesync_sshkey
Revises: add_org_free_trial
Create Date: 2026-03-12 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "add_desktop_filesync_sshkey"
down_revision = "add_org_free_trial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column("desktop_filesync_sshkey", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assistants", "desktop_filesync_sshkey")
