from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import CreditCardFingerprint


class CreditCardFingerprintDAO:
    """Class for accessing credit card fingerprint table."""

    def __init__(self, session: Session):
        self.session = session

    def create(self, billing_account_id: int, fingerprint: str) -> None:
        self.session.add(
            CreditCardFingerprint(
                billing_account_id=billing_account_id,
                fingerprint=fingerprint,
            ),
        )

    def filter(
        self,
        id: Optional[int] = None,
        billing_account_id: Optional[int] = None,
        fingerprint: Optional[str] = None,
    ) -> List[CreditCardFingerprint]:
        query = select(CreditCardFingerprint)
        if id:
            query = query.where(CreditCardFingerprint.id == id)
        if billing_account_id:
            query = query.where(
                CreditCardFingerprint.billing_account_id == billing_account_id,
            )
        if fingerprint:
            query = query.where(CreditCardFingerprint.fingerprint == fingerprint)

        raw_credit_card_fingerprints = self.session.execute(query)

        return list(raw_credit_card_fingerprints.scalars().fetchall())
