"""Add deploy_env column to assistants.

Allows staging Orchestra to store assistants that should route to either the
staging or preview runtime stacks without requiring a separate preview
Orchestra deployment.

Revision ID: add_assistant_deploy_env
Revises: add_field_type_context_id_idx
Create Date: 2026-03-18 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "add_assistant_deploy_env"
down_revision = "add_field_type_context_id_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column(
            "deploy_env",
            sa.String(),
            nullable=False,
            server_default="production",
        ),
    )


def downgrade() -> None:
    op.drop_column("assistants", "deploy_env")
