from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import AssistantUserDesktop, UserDesktop


class DesktopDAO:
    """Data access object for UserDesktop and per-user assistant links."""

    def __init__(self, session: Session):
        self.session = session

    # ── UserDesktop registry (per-user) ──────────────────────────────────

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
        # Linked assistant rows cascade via the FK ON DELETE CASCADE.
        self.session.delete(desktop)
        self.session.flush()
        return True

    # ── Per-user assistant <-> desktop links ─────────────────────────────

    def list_assigned_assistant_ids(self, desktop_id: int) -> List[int]:
        """Return agent_ids of every assistant this desktop is linked to."""
        stmt = select(AssistantUserDesktop.assistant_id).where(
            AssistantUserDesktop.user_desktop_id == desktop_id,
        )
        return list(self.session.execute(stmt).scalars().all())

    def link(
        self,
        assistant_id: int,
        desktop_id: int,
        requesting_user_id: str,
        filesys_sync: bool = False,
    ) -> Optional[AssistantUserDesktop]:
        """Link the caller's own desktop to an assistant.

        Returns ``None`` when the desktop does not exist or is not owned by the
        requesting user.  Replaces any existing desktop the user has linked to
        this assistant (one machine per user per assistant).
        """
        desktop = self.get_by_id(desktop_id, requesting_user_id)
        if not desktop:
            return None

        existing = self.session.execute(
            select(AssistantUserDesktop).where(
                AssistantUserDesktop.assistant_id == assistant_id,
                AssistantUserDesktop.owner_user_id == requesting_user_id,
            ),
        ).scalar_one_or_none()
        if existing is not None:
            existing.user_desktop_id = desktop_id
            existing.filesys_sync = filesys_sync
            self.session.flush()
            return existing

        link = AssistantUserDesktop(
            assistant_id=assistant_id,
            user_desktop_id=desktop_id,
            owner_user_id=requesting_user_id,
            filesys_sync=filesys_sync,
        )
        self.session.add(link)
        self.session.flush()
        return link

    def unlink(self, assistant_id: int, requesting_user_id: str) -> bool:
        """Remove the caller's own desktop link from an assistant."""
        link = self.session.execute(
            select(AssistantUserDesktop).where(
                AssistantUserDesktop.assistant_id == assistant_id,
                AssistantUserDesktop.owner_user_id == requesting_user_id,
            ),
        ).scalar_one_or_none()
        if link is None:
            return False
        self.session.delete(link)
        self.session.flush()
        return True

    def get_link_for_user(
        self,
        assistant_id: int,
        user_id: str,
    ) -> Optional[Tuple[AssistantUserDesktop, UserDesktop]]:
        """Return the (link, desktop) a user has connected to an assistant."""
        stmt = (
            select(AssistantUserDesktop, UserDesktop)
            .join(UserDesktop, UserDesktop.id == AssistantUserDesktop.user_desktop_id)
            .where(
                AssistantUserDesktop.assistant_id == assistant_id,
                AssistantUserDesktop.owner_user_id == user_id,
            )
        )
        row = self.session.execute(stmt).first()
        if row is None:
            return None
        return (row[0], row[1])

    def list_links_for_assistant(
        self,
        assistant_id: int,
    ) -> List[Tuple[AssistantUserDesktop, UserDesktop]]:
        """Return every (link, desktop) connected to an assistant.

        Used to build the runtime payload that maps each user to their own
        machine for an assistant that several users share.
        """
        stmt = (
            select(AssistantUserDesktop, UserDesktop)
            .join(UserDesktop, UserDesktop.id == AssistantUserDesktop.user_desktop_id)
            .where(AssistantUserDesktop.assistant_id == assistant_id)
        )
        return [(row[0], row[1]) for row in self.session.execute(stmt).all()]
