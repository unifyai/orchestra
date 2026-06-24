"""Per-device SFTP tunnel id for server-side tunnel teardown.

Adds ``sftp_tunnel_id`` to ``user_desktops``. The SFTP server is per-device
(one rclone serve + one rathole client), so its relay tunnel id belongs on the
desktop row, not the per-assistant link. Deleting a desktop from the Console can
then deregister the raw-TCP tunnel from the relay, instead of leaking its port.

Revision ID: user_desktop_sftp_tunnel_id
Revises: user_desktop_filesync
Create Date: 2026-06-29 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "user_desktop_sftp_tunnel_id"
down_revision = "user_desktop_filesync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_desktops",
        sa.Column("sftp_tunnel_id", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_desktops", "sftp_tunnel_id")
