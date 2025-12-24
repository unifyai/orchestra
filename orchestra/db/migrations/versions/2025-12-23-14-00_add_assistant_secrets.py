"""Add assistant_secrets table for storing external service credentials

This migration creates a table for storing secrets (API keys, tokens, etc.)
that assistants need to access external services.

Revision ID: add_assistant_secrets
Revises: add_plot_table
Create Date: 2025-12-23 14:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_assistant_secrets"
down_revision = "add_plot_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Create assistant_secrets table for storing external service credentials.

    The table uses a composite primary key of (user_id, agent_id, secret_name)
    to allow multiple secrets per assistant while ensuring uniqueness.
    """
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
            ["auth_user.id"],
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
    """
    Drop assistant_secrets table.
    """
    op.drop_index("ix_assistant_secrets_agent_id", table_name="assistant_secrets")
    op.drop_index("ix_assistant_secrets_user_id", table_name="assistant_secrets")
    op.drop_table("assistant_secrets")
