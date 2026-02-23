"""DAO for managing one-time credit grant links."""

import datetime
import uuid
from typing import List, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import OneTimeCreditGrantLink
from orchestra.settings import settings


class OneTimeCreditGrantLinkDAO:
    """
    DAO for managing one-time credit grant links.

    These links grant a specified amount of credits when claimed by a user.
    Each link can only be claimed once, and each user can only benefit from
    one link ever.
    """

    def __init__(self, session: Session):
        self.session = session

    def generate_token(self) -> str:
        return str(uuid.uuid4())

    def create(
        self,
        expires_at: datetime.datetime,
        credit_amount: Optional[float] = None,
    ) -> OneTimeCreditGrantLink:
        """
        Create a new one-time credit grant link.

        Args:
            expires_at: When the link expires
            credit_amount: Amount of credits to grant when claimed.
                          Defaults to settings.assistant_creation_cost.

        Returns:
            The created link
        """
        if credit_amount is None:
            credit_amount = float(settings.assistant_creation_cost)

        token = self.generate_token()
        link = OneTimeCreditGrantLink(
            token=token,
            expires_at=expires_at,
            credit_amount=credit_amount,
        )
        self.session.add(link)
        return link

    def get_by_token(self, token: str) -> Optional[OneTimeCreditGrantLink]:
        """Get a link by its token."""
        query = select(OneTimeCreditGrantLink).where(
            OneTimeCreditGrantLink.token == token,
        )
        return self.session.execute(query).scalar_one_or_none()

    def get_by_id(self, link_id: str) -> Optional[OneTimeCreditGrantLink]:
        """Get a link by its ID."""
        query = select(OneTimeCreditGrantLink).where(
            OneTimeCreditGrantLink.id == link_id,
        )
        return self.session.execute(query).scalar_one_or_none()

    def claim_link(
        self,
        token: str,
        user_id: str,
    ) -> Optional[OneTimeCreditGrantLink]:
        """
        Claim a link for a user.

        Args:
            token: The link token
            user_id: ID of the user claiming the link

        Returns:
            The claimed link, or None if invalid/expired/already claimed
        """
        link = self.get_by_token(token)
        if link:
            if link.user_id is not None:  # Already claimed
                return None
            if link.expires_at < datetime.datetime.now(
                datetime.timezone.utc,
            ):  # Expired
                return None

            link.user_id = user_id
            link.claimed_at = datetime.datetime.now(datetime.timezone.utc)
            return link
        return None

    def list_links(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> List[OneTimeCreditGrantLink]:
        """List all links, ordered by creation date (newest first)."""
        query = (
            select(OneTimeCreditGrantLink)
            .limit(limit)
            .offset(offset)
            .order_by(OneTimeCreditGrantLink.created_at.desc())
        )
        return list(self.session.execute(query).scalars().all())

    def delete_link(self, link_id: str) -> bool:
        """Delete a link by ID. Returns True if deleted, False if not found."""
        link = self.get_by_id(link_id)
        if link:
            self.session.delete(link)
            return True
        return False

    def has_user_claimed_any_link(self, user_id: str) -> bool:
        """Check if a user has already claimed any credit grant link."""
        query = (
            select(OneTimeCreditGrantLink.id)
            .where(
                OneTimeCreditGrantLink.user_id == user_id,
            )
            .limit(1)
        )
        return self.session.execute(query).scalar_one_or_none() is not None

    def get_links_for_user(self, user_id: str) -> List[OneTimeCreditGrantLink]:
        """Get all links claimed by a specific user."""
        query = (
            select(OneTimeCreditGrantLink)
            .where(OneTimeCreditGrantLink.user_id == user_id)
            .order_by(OneTimeCreditGrantLink.claimed_at.desc())
        )
        return list(self.session.execute(query).scalars().all())

    def delete_expired_links(self) -> int:
        """Delete expired and unclaimed links. Returns the number deleted."""
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        stmt = (
            delete(OneTimeCreditGrantLink)
            .where(OneTimeCreditGrantLink.expires_at < now_utc)
            .where(OneTimeCreditGrantLink.user_id.is_(None))
        )
        result = self.session.execute(stmt)
        return result.rowcount
