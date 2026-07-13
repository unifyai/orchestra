"""Scope MS Teams bot owner-tenant unique indexes to active installs.

The ``(organization_id, tenant_id)`` and ``(user_id, tenant_id)`` unique
indexes on ``ms_teams_bot_installs`` originally excluded only NULL owners,
so a *revoked* install (which keeps its owner + tenant for audit) still
reserved the slot. Re-adding the bot creates a fresh pending row and
binding it then collided with the revoked leftover
(``duplicate key value violates unique constraint
"ux_ms_teams_bot_install_user_tenant"``).

Both indexes are recreated with an added ``revoked_at IS NULL`` predicate
so uniqueness applies to *active* installs only — matching
``ux_ms_teams_bot_install_active_tenant``. Revoked rows are retained but no
longer block a reconnect.

Revision ID: ms_teams_bot_active_owner_idx
Revises: assistant_slow_brain_model
Create Date: 2026-07-19 00:00:00
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "ms_teams_bot_active_owner_idx"
down_revision = "assistant_slow_brain_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "ux_ms_teams_bot_install_org_tenant",
        table_name="ms_teams_bot_installs",
    )
    op.drop_index(
        "ux_ms_teams_bot_install_user_tenant",
        table_name="ms_teams_bot_installs",
    )
    op.create_index(
        "ux_ms_teams_bot_install_org_tenant",
        "ms_teams_bot_installs",
        ["organization_id", "tenant_id"],
        unique=True,
        postgresql_where=text("organization_id IS NOT NULL AND revoked_at IS NULL"),
    )
    op.create_index(
        "ux_ms_teams_bot_install_user_tenant",
        "ms_teams_bot_installs",
        ["user_id", "tenant_id"],
        unique=True,
        postgresql_where=text("user_id IS NOT NULL AND revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ux_ms_teams_bot_install_org_tenant",
        table_name="ms_teams_bot_installs",
    )
    op.drop_index(
        "ux_ms_teams_bot_install_user_tenant",
        table_name="ms_teams_bot_installs",
    )
    op.create_index(
        "ux_ms_teams_bot_install_org_tenant",
        "ms_teams_bot_installs",
        ["organization_id", "tenant_id"],
        unique=True,
        postgresql_where=text("organization_id IS NOT NULL"),
    )
    op.create_index(
        "ux_ms_teams_bot_install_user_tenant",
        "ms_teams_bot_installs",
        ["user_id", "tenant_id"],
        unique=True,
        postgresql_where=text("user_id IS NOT NULL"),
    )
