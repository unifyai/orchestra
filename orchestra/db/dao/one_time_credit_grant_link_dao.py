"""DAO for managing credit grant links and their claims."""

import datetime
import uuid
from typing import List, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    CreditGrantLinkClaim,
    OneTimeCreditGrantLink,
)
from orchestra.settings import settings


class OneTimeCreditGrantLinkDAO:
    """
    DAO for credit grant links.

    Links can be single-use (max_claims=1) or multi-use (max_claims>1).
    Individual redemptions are tracked in the ``credit_grant_link_claim`` table.
    """

    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def generate_token() -> str:
        return str(uuid.uuid4())

    def create(
        self,
        expires_at: datetime.datetime,
        credit_amount: Optional[float] = None,
        max_claims: int = 1,
        name: Optional[str] = None,
    ) -> OneTimeCreditGrantLink:
        if credit_amount is None:
            credit_amount = float(settings.assistant_creation_cost)

        token = self.generate_token()
        link = OneTimeCreditGrantLink(
            token=token,
            expires_at=expires_at,
            credit_amount=credit_amount,
            max_claims=max_claims,
            name=name or None,
        )
        self.session.add(link)
        return link

    def get_by_token(self, token: str) -> Optional[OneTimeCreditGrantLink]:
        query = select(OneTimeCreditGrantLink).where(
            OneTimeCreditGrantLink.token == token,
        )
        return self.session.execute(query).scalar_one_or_none()

    def get_by_id(self, link_id: str) -> Optional[OneTimeCreditGrantLink]:
        query = select(OneTimeCreditGrantLink).where(
            OneTimeCreditGrantLink.id == link_id,
        )
        return self.session.execute(query).scalar_one_or_none()

    # ------------------------------------------------------------------
    # Claim operations
    # ------------------------------------------------------------------

    def get_claim_count(self, link_id: str) -> int:
        """Return the number of claims for a given link."""
        query = (
            select(func.count())
            .select_from(CreditGrantLinkClaim)
            .where(CreditGrantLinkClaim.link_id == link_id)
        )
        return self.session.execute(query).scalar_one()

    def is_fully_redeemed(self, link: OneTimeCreditGrantLink) -> bool:
        return self.get_claim_count(link.id) >= link.max_claims

    def has_user_claimed_link(self, link_id: str, user_id: str) -> bool:
        """Check if a specific user has already claimed a specific link."""
        query = (
            select(CreditGrantLinkClaim.id)
            .where(
                CreditGrantLinkClaim.link_id == link_id,
                CreditGrantLinkClaim.user_id == user_id,
            )
            .limit(1)
        )
        return self.session.execute(query).scalar_one_or_none() is not None

    def has_user_claimed_any_link(self, user_id: str) -> bool:
        """Check if a user has already claimed any credit grant link."""
        query = (
            select(CreditGrantLinkClaim.id)
            .where(CreditGrantLinkClaim.user_id == user_id)
            .limit(1)
        )
        return self.session.execute(query).scalar_one_or_none() is not None

    def has_org_claimed_any_link(self, organization_id: int) -> bool:
        """Check if an organization has already received credits from any link."""
        query = (
            select(CreditGrantLinkClaim.id)
            .where(CreditGrantLinkClaim.organization_id == organization_id)
            .limit(1)
        )
        return self.session.execute(query).scalar_one_or_none() is not None

    def claim_link(
        self,
        token: str,
        user_id: str,
        organization_id: Optional[int] = None,
    ) -> Optional[CreditGrantLinkClaim]:
        """
        Record a claim against a link.

        Returns the new CreditGrantLinkClaim on success, or None if the link
        is invalid, expired, or fully redeemed.
        """
        link = self.get_by_token(token)
        if not link:
            return None
        if link.expires_at < datetime.datetime.now(datetime.timezone.utc):
            return None
        if self.is_fully_redeemed(link):
            return None

        claim = CreditGrantLinkClaim(
            link_id=link.id,
            user_id=user_id,
            organization_id=organization_id,
            claimed_at=datetime.datetime.now(datetime.timezone.utc),
        )
        self.session.add(claim)
        return claim

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def list_links(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> List[OneTimeCreditGrantLink]:
        query = (
            select(OneTimeCreditGrantLink)
            .limit(limit)
            .offset(offset)
            .order_by(OneTimeCreditGrantLink.created_at.desc())
        )
        return list(self.session.execute(query).scalars().all())

    def get_claims_for_link(
        self,
        link_id: str,
    ) -> List[CreditGrantLinkClaim]:
        query = (
            select(CreditGrantLinkClaim)
            .where(CreditGrantLinkClaim.link_id == link_id)
            .order_by(CreditGrantLinkClaim.claimed_at.desc())
        )
        return list(self.session.execute(query).scalars().all())

    def get_claims_for_user(self, user_id: str) -> List[CreditGrantLinkClaim]:
        query = (
            select(CreditGrantLinkClaim)
            .where(CreditGrantLinkClaim.user_id == user_id)
            .order_by(CreditGrantLinkClaim.claimed_at.desc())
        )
        return list(self.session.execute(query).scalars().all())

    def delete_link(self, link_id: str) -> bool:
        """Delete a link by ID. Returns True if deleted, False if not found."""
        link = self.get_by_id(link_id)
        if link:
            self.session.delete(link)
            return True
        return False

    def delete_expired_links(self) -> int:
        """Delete expired links that have no claims. Returns the number deleted."""
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        has_claims = (
            select(CreditGrantLinkClaim.id)
            .where(CreditGrantLinkClaim.link_id == OneTimeCreditGrantLink.id)
            .correlate(OneTimeCreditGrantLink)
            .exists()
        )
        stmt = (
            delete(OneTimeCreditGrantLink)
            .where(OneTimeCreditGrantLink.expires_at < now_utc)
            .where(~has_claims)
        )
        result = self.session.execute(stmt)
        return result.rowcount
