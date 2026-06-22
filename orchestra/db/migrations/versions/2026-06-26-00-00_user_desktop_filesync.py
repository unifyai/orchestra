"""Per-link SSH key + SFTP tunnel coordinates for user-home filesystem access.

Adds the columns that back on-demand SFTP access to a linked user's home: a
per-(assistant, user-desktop) private key (server-held; only the derived public
key reaches the device) and the public SFTP tunnel host/port the CM dials.

Revision ID: user_desktop_filesync
Revises: org_wide_sharing
Create Date: 2026-06-26 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "user_desktop_filesync"
down_revision = "log_event_id_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistant_user_desktops",
        sa.Column("filesync_sshkey", sa.String(), nullable=True),
    )
    op.add_column(
        "assistant_user_desktops",
        sa.Column("sftp_tunnel_host", sa.String(), nullable=True),
    )
    op.add_column(
        "assistant_user_desktops",
        sa.Column("sftp_tunnel_port", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assistant_user_desktops", "sftp_tunnel_port")
    op.drop_column("assistant_user_desktops", "sftp_tunnel_host")
    op.drop_column("assistant_user_desktops", "filesync_sshkey")
