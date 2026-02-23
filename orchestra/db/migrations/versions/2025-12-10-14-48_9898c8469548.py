"""Add bio to user table

Revision ID: 9898c8469548
Revises: add_organization_invites
Create Date: 2025-12-10 14:48:13.854663

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "9898c8469548"
down_revision = "add_organization_invites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### Add bio column to auth_user table ###
    op.add_column("auth_user", sa.Column("bio", sa.String(), nullable=True))
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### Remove bio column from auth_user table ###
    op.drop_column("auth_user", "bio")
    # ### end Alembic commands ###
