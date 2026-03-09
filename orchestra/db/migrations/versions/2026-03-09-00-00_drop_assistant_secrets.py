"""Drop unused assistant_secrets table.

Secrets are managed via the Unify context system, not this dedicated
table. The table, DAO, and API endpoints have been removed from the
application code.

Revision ID: drop_assistant_secrets
Revises: add_api_msg_att_tags
Create Date: 2026-03-09 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "drop_assistant_secrets"
down_revision = "add_api_msg_att_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("assistant_secrets")


def downgrade() -> None:
    op.create_table(
        "assistant_secrets",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("secret_name", sa.String(), nullable=False),
        sa.Column("secret_value", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("user_id", "agent_id", "secret_name"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["assistants.agent_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_assistant_secrets_user_id", "assistant_secrets", ["user_id"])
    op.create_index("ix_assistant_secrets_agent_id", "assistant_secrets", ["agent_id"])
