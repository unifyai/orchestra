"""Add job_title column to assistants.

Free-text label users assign to remember what an assistant is specialized
in (e.g. "Growth marketing", "QA engineer").  Nullable so existing rows
remain valid; surfaced by the console UI as a subtitle on the assistant
list/hover card.  Intentionally named ``job_title`` rather than ``role`` to
avoid collision with org RBAC roles and chat-message ``role`` semantics.

Revision ID: add_assistant_job_title
Revises: ms365_business_premium_pricing
Create Date: 2026-04-19 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "add_assistant_job_title"
down_revision = "ms365_business_premium_pricing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column(
            "job_title",
            sa.String(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("assistants", "job_title")
