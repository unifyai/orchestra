"""Add demo_assistant_meta table and demo_id column to assistants.

This migration adds support for demo assistants:
- Creates demo_assistant_meta table to store metadata about demo assistants
- Adds demo_id column to assistants table as FK to demo_assistant_meta

Demo assistants are used by Unify employees to demonstrate the product
to prospects who haven't signed up yet.

Revision ID: demo_assistant_meta_001
Revises: add_spending_limit_notifications
Create Date: 2026-02-10 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "demo_assistant_meta_001"
down_revision = "9b2c3d4e5f6a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create demo_assistant_meta table
    op.create_table(
        "demo_assistant_meta",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "source_assistant_id",
            sa.Integer(),
            nullable=False,
            comment="The assistant this demo was cloned from",
        ),
        sa.Column(
            "demoer_user_id",
            sa.String(),
            nullable=False,
            comment="The user who created this demo assistant",
        ),
        sa.Column(
            "label",
            sa.String(),
            nullable=False,
            comment="Human-readable label for this demo (e.g. 'Richard Branson demo')",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["source_assistant_id"],
            ["assistants.agent_id"],
            name="fk_demo_meta_source_assistant",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["demoer_user_id"],
            ["auth_user.id"],
            name="fk_demo_meta_demoer",
            ondelete="CASCADE",
        ),
    )

    # Create index for efficient lookups by demoer
    op.create_index(
        "idx_demo_meta_demoer",
        "demo_assistant_meta",
        ["demoer_user_id"],
    )

    # Add demo_id column to assistants table
    op.add_column(
        "assistants",
        sa.Column(
            "demo_id",
            sa.Integer(),
            nullable=True,
            comment="FK to demo_assistant_meta if this is a demo assistant",
        ),
    )

    # Create FK constraint
    op.create_foreign_key(
        "fk_assistants_demo_meta",
        "assistants",
        "demo_assistant_meta",
        ["demo_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Create index for demo_id lookups
    op.create_index(
        "idx_assistants_demo_id",
        "assistants",
        ["demo_id"],
    )


def downgrade() -> None:
    # Drop index on assistants.demo_id
    op.drop_index("idx_assistants_demo_id", table_name="assistants")

    # Drop FK constraint
    op.drop_constraint("fk_assistants_demo_meta", "assistants", type_="foreignkey")

    # Drop demo_id column from assistants
    op.drop_column("assistants", "demo_id")

    # Drop index on demo_assistant_meta
    op.drop_index("idx_demo_meta_demoer", table_name="demo_assistant_meta")

    # Drop demo_assistant_meta table
    op.drop_table("demo_assistant_meta")
