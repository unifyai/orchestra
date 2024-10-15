"""add dataset artifact table

Revision ID: bb2e75a19b27
Revises: f3fbdcf9c215
Create Date: 2024-10-15 08:15:33.182854

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "bb2e75a19b27"
down_revision = "f3fbdcf9c215"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dataset_artifact",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(["dataset_id"], ["dataset.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_dataset_artifact_dataset_id"),
        "dataset_artifact",
        ["dataset_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_dataset_artifact_dataset_id"), table_name="dataset_artifact")
    op.drop_table("dataset_artifact")
