"""Retire referral origins that recorded the caller rather than the referee.

``referral_attribution.signup_ip`` was filled from the connecting peer.
Attribution is reached through Console's server, so every row holds
Console rather than the person referred — an origin that would put the
whole programme at one address the moment anything scored against it.

Attribution now copies the referee's own signup origin, which changes
what the column means. Rows written under the old meaning are not merely
imprecise, they contradict the new one, and a stale value here is worse
than an absent one: abuse scoring reads a shared address as evidence of
a ring, so leaving them would manufacture exactly the false cluster the
column exists to detect. They are cleared rather than migrated, because
the referee's address at attribution time was never recorded anywhere
and cannot be recovered.

Nothing reads the column yet, so this costs no behaviour today. It is
done now so that whoever builds the scoring inherits an empty column
instead of a confident, uniform, wrong one.

Revision ID: referral_origin_meaning
Revises: assistant_deployment_target
Create Date: 2026-08-12 01:05:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "referral_origin_meaning"
down_revision = "assistant_deployment_target"
branch_labels = None
depends_on = None

_NEW_COMMENT = "Referee's own signup origin, copied here for abuse scoring"
_OLD_COMMENT = "Referee IP at attribution time (velocity/abuse scoring)"


def upgrade() -> None:
    conn = op.get_bind()
    cleared = conn.execute(
        sa.text(
            """
            UPDATE referral_attribution
            SET signup_ip = NULL
            WHERE signup_ip IS NOT NULL
            RETURNING id
            """,
        ),
    ).fetchall()
    print(
        f"cleared {len(cleared)} referral origin(s) recorded from the caller",
    )

    op.alter_column(
        "referral_attribution",
        "signup_ip",
        existing_type=sa.String(),
        existing_nullable=True,
        comment=_NEW_COMMENT,
        existing_comment=_OLD_COMMENT,
    )


def downgrade() -> None:
    # The cleared addresses described Console, not the referees; there is
    # nothing worth restoring. Only the wording goes back.
    op.alter_column(
        "referral_attribution",
        "signup_ip",
        existing_type=sa.String(),
        existing_nullable=True,
        comment=_OLD_COMMENT,
        existing_comment=_NEW_COMMENT,
    )
