"""Mark assistants that a deployment cannot run without.

Some assistants are infrastructure: a client deployment names them as its
target, and Cloud Build reconciliation stops if the row is gone. Nothing on
the assistant said so, so an ordinary account cleanup could delete one and
leave every subsequent deploy of that environment failing on a 404 with no
trace of what had been removed.

``is_deployment_target`` carries that fact on the row itself, so the delete
endpoint can refuse the same way it refuses a coordinator. Deploy-time
reconciliation owns the flag: it sets it on the targets its clients declare
as required, which keeps the set accurate without a hardcoded list here.

Revision ID: assistant_deployment_target
Revises: drop_legacy_tasks_instance_id
Create Date: 2026-08-11 23:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "assistant_deployment_target"
down_revision = "drop_legacy_tasks_instance_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column(
            "is_deployment_target",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("assistants", "is_deployment_target")
