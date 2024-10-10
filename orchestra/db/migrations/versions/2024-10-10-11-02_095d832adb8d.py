"""add new dataset entry table

Revision ID: 095d832adb8d
Revises: 0fc27545b83d
Create Date: 2024-10-10 11:02:13.942791

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "095d832adb8d"
down_revision = "0fc27545b83d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dataset_entry",
        sa.Column("id", sa.String(length=10), nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=True),
        sa.Column("entry", sa.String(), nullable=False),
        sa.Column("entry_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["dataset_id"], ["dataset.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dataset_id", "entry_hash", name="uq_dataset_entry_hash"),
    )
    op.create_index(
        op.f("ix_dataset_entry_dataset_id"),
        "dataset_entry",
        ["dataset_id"],
        unique=False,
    )
    op.add_column(
        "dataset",
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("dataset", "created_at")
    op.drop_index(op.f("ix_dataset_entry_dataset_id"), table_name="dataset_entry")
    op.drop_table("dataset_entry")
