"""added the interface table

Revision ID: a21317060542
Revises: 80402c775278
Create Date: 2024-12-31 14:51:27.497425

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a21317060542"
down_revision = "80402c775278"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "interface",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("auth_user.id", ondelete="CASCADE"),
            index=True,
        ),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            index=True,
        ),
        sa.Column("new_counter", sa.Integer()),
        sa.Column(
            "items",
            sa.String(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("interface")
