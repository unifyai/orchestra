"""Clear accidental managed-desktop grandfather entitlements.

The ``managed_desktop_addon`` revision activated every assistant that already
had ``desktop_mode`` in ``('ubuntu', 'windows')``. That treated legacy mode
selection as a paid Computer Use opt-in. Nobody has been billed yet, so reset
all assistants to the same off state as ``disable_managed_desktop``: no
desktop mode, status ``disabled``, and cleared enable/billing timestamps.

Revision ID: clear_managed_desktop_gf
Revises: managed_desktop_addon
Create Date: 2026-07-17 00:00:00
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "clear_managed_desktop_gf"
down_revision = "managed_desktop_addon"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            """
            UPDATE assistants
            SET
                desktop_mode = NULL,
                managed_desktop_status = 'disabled',
                managed_desktop_enabled_at = NULL,
                managed_desktop_monthly_cost = NULL,
                managed_desktop_grace_period_started_at = NULL
            WHERE
                managed_desktop_status = 'active'
                OR desktop_mode IN ('ubuntu', 'windows')
            """,
        ),
    )


def downgrade() -> None:
    # Cannot restore prior desktop_mode / entitlement state.
    pass
