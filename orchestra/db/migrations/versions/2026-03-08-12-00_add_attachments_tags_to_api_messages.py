"""Add attachments and tags columns to api_messages table.

Supports multi-file attachments and developer-supplied tags for both
inbound messages and outbound assistant responses.

Revision ID: add_attachments_tags_api_messages
Revises: add_api_messages
Create Date: 2026-03-08 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "add_api_msg_att_tags"
down_revision = "remove_field_desc_limit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_messages",
        sa.Column("tags", JSONB(), nullable=True, server_default="[]"),
    )
    op.add_column(
        "api_messages",
        sa.Column("attachments", JSONB(), nullable=True, server_default="[]"),
    )
    op.add_column(
        "api_messages",
        sa.Column("response_tags", JSONB(), nullable=True),
    )
    op.add_column(
        "api_messages",
        sa.Column("response_attachments", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("api_messages", "response_attachments")
    op.drop_column("api_messages", "response_tags")
    op.drop_column("api_messages", "attachments")
    op.drop_column("api_messages", "tags")
