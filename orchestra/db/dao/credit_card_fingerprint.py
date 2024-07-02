from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import CreditCardFingerprint


class CreditCardFingerprintDAO:
    """Class for accessing custom api key table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(self, user_id: str, fingerprint: str) -> None:
        self.session.add(
            CreditCardFingerprint(user_id=user_id, fingerprint=fingerprint),
        )

    def filter(
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

        raw_credit_card_fingerprints = self.session.execute(query)

        return list(raw_credit_card_fingerprints.scalars().fetchall())
