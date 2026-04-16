"""Re-create assistant_secrets table for storing external service credentials.

Revision ID: readd_assistant_secrets
Revises: ms365_email_provider
Create Date: 2026-04-14 12:00:00.000000

The table was originally created in add_assistant_secrets and dropped in
drop_assistant_secrets.  Communication still writes OAuth tokens here, so
the table is being restored with the same schema.
"""

import sqlalchemy as sa
from alembic import op

revision = "readd_assistant_secrets"
down_revision = "ms365_email_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["assistants.agent_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "agent_id", "secret_name"),
    )
    op.create_index(
        "ix_assistant_secrets_user_id",
        "assistant_secrets",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_assistant_secrets_agent_id",
        "assistant_secrets",
        ["agent_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_assistant_secrets_agent_id", table_name="assistant_secrets")
    op.drop_index("ix_assistant_secrets_user_id", table_name="assistant_secrets")
    op.drop_table("assistant_secrets")
