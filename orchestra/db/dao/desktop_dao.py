from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Assistant, UserDesktop


class DesktopDAO:
    """Data access object for UserDesktop operations."""

    def __init__(self, session: Session):
        self.session = session

    def create(
        self,
        user_id: str,
        name: str,
        url: str,
        os: str,
    ) -> UserDesktop:
        desktop = UserDesktop(
            user_id=user_id,
            name=name,
            url=url,
            os=os,
        )
        self.session.add(desktop)
        self.session.flush()
        return desktop

    def get_by_id(self, desktop_id: int, user_id: str) -> Optional[UserDesktop]:
        stmt = select(UserDesktop).where(
            UserDesktop.id == desktop_id,
            UserDesktop.user_id == user_id,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_for_user(self, user_id: str) -> List[UserDesktop]:
        stmt = select(UserDesktop).where(UserDesktop.user_id == user_id)
        return list(self.session.execute(stmt).scalars().all())

    def update(
        self,
        desktop_id: int,
        user_id: str,
        update_data: dict,
    ) -> Optional[UserDesktop]:
        desktop = self.get_by_id(desktop_id, user_id)
        if not desktop:
            return None
        for key, value in update_data.items():
            setattr(desktop, key, value)
        self.session.flush()
        return desktop

    def delete(self, desktop_id: int, user_id: str) -> bool:
        desktop = self.get_by_id(desktop_id, user_id)
        if not desktop:
            return False
        self.session.delete(desktop)
        self.session.flush()
        return True

    def get_assigned_assistant_id(self, desktop_id: int) -> Optional[int]:
        """Return the agent_id of the assistant assigned to this desktop, or None."""
        stmt = select(Assistant.agent_id).where(
            Assistant.user_desktop_id == desktop_id,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def unlink_from_assistant(self, desktop_id: int) -> None:
        """Clear user_desktop_id on any assistant linked to this desktop."""
        stmt = select(Assistant).where(Assistant.user_desktop_id == desktop_id)
        assistant = self.session.execute(stmt).scalar_one_or_none()
        if assistant:
            assistant.user_desktop_id = None
            self.session.flush()
