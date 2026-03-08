"""Drop desktop_url column from assistants table.

VMs are now assigned from a pool at job start and released at job end.
desktop_url is no longer an assistant property; it's session-scoped.

Revision ID: drop_desktop_url
Revises: add_org_image
Create Date: 2026-03-06 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "drop_desktop_url"
down_revision = "add_org_image"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("assistants", "desktop_url")


def downgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column("desktop_url", sa.String(), nullable=True),
    )
