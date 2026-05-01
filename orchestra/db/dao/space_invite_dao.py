"""Data access helpers for space invitation lifecycle transitions."""

import sqlalchemy as sa
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import SpaceInvite

SPACE_INVITE_STATUS_PENDING = "pending"
SPACE_INVITE_STATUS_ACCEPTED = "accepted"
SPACE_INVITE_STATUS_DECLINED = "declined"
SPACE_INVITE_STATUS_CANCELLED = "cancelled"
SPACE_INVITE_STATUS_EXPIRED = "expired"


class SpaceInviteDAO:
    """Owns state-machine transitions for space invitation rows."""

    def __init__(self, session: Session):
        self.session = session

    def expire_pending_invites(self) -> int:
        """Mark every expired pending invitation as expired.

        The update is idempotent: only rows still in the pending state and past
        their expiry timestamp transition, and terminal rows remain unchanged.

        :return: Number of invitation rows transitioned.
        """

        statement = (
            sa.update(SpaceInvite)
            .where(
                SpaceInvite.status == SPACE_INVITE_STATUS_PENDING,
                SpaceInvite.expires_at < sa.func.now(),
            )
            .values(
                status=SPACE_INVITE_STATUS_EXPIRED,
                decided_at=sa.func.now(),
            )
            .returning(SpaceInvite.invite_id)
        )
        transitioned_invite_ids = self.session.execute(statement).scalars().all()
        self.session.flush()
        return len(transitioned_invite_ids)
