"""Async version of credit_card_fingerprint for use with AsyncSession."""

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import CreditCardFingerprint


class AsyncCreditCardFingerprintDAO:
    """Class for accessing custom api key table."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, user_id: str, fingerprint: str) -> None:
        self.session.add(
            CreditCardFingerprint(user_id=user_id, fingerprint=fingerprint),
        )

    async def filter(
        self,
        id: Optional[int] = None,
        user_id: Optional[str] = None,
        fingerprint: Optional[str] = None,
    ) -> List[CreditCardFingerprint]:
        query = select(CreditCardFingerprint)
        if id:
            query = query.where(CreditCardFingerprint.id == id)
        if user_id:
            query = query.where(CreditCardFingerprint.user_id == user_id)
        if fingerprint:
            query = query.where(CreditCardFingerprint.fingerprint == fingerprint)

        raw_credit_card_fingerprints = await self.session.execute(query)

        return list(raw_credit_card_fingerprints.scalars().fetchall())
