"""Data Access Object for user presence heartbeats."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import UserPresence

# A user is online when their last heartbeat is within this window. Console
# heartbeats every ~60s while the tab is visible, so 120s tolerates one
# missed beat without flapping.
PRESENCE_ONLINE_THRESHOLD_SECONDS = 120


def presence_is_online(last_seen_at: datetime | None) -> bool:
    """Whether a heartbeat timestamp counts as currently online."""
    if last_seen_at is None:
        return False
    if last_seen_at.tzinfo is None:
        last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)
    threshold = timedelta(seconds=PRESENCE_ONLINE_THRESHOLD_SECONDS)
    return datetime.now(timezone.utc) - last_seen_at <= threshold


class UserPresenceDAO:
    """DAO for reading and writing user presence heartbeats."""

    def __init__(self, session: Session):
        self.session = session

    def touch(self, user_id: str) -> None:
        """Upsert the user's heartbeat to now."""
        stmt = postgres_insert(UserPresence).values(
            user_id=user_id,
            last_seen_at=func.now(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[UserPresence.user_id],
            set_={"last_seen_at": func.now()},
        )
        self.session.execute(stmt)
        self.session.flush()

    def last_seen_map(self, user_ids: list[str]) -> dict[str, datetime]:
        """Map of user_id -> last_seen_at for the given users (missing = never)."""
        if not user_ids:
            return {}
        rows = self.session.execute(
            select(UserPresence.user_id, UserPresence.last_seen_at).where(
                UserPresence.user_id.in_(user_ids),
            ),
        ).all()
        return {user_id: last_seen for user_id, last_seen in rows}
