"""DAO for the referral program (codes + attributions).

Pure persistence layer. Reward computation and credit granting live in
:mod:`orchestra.lib.referrals` so the DB access and the cross-cutting
billing logic stay separated (mirrors the DAO / ``lib`` split used by the
rest of billing).
"""

import datetime
import secrets
import uuid
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import ReferralAttribution, ReferralCode

# Unambiguous code alphabet (no 0/O/1/I/L) for share-friendly codes.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8

STATUS_PENDING = "pending"
STATUS_REWARDED = "rewarded"
STATUS_REVERSED = "reversed"


class ReferralDAO:
    """DAO for referral codes and attributions."""

    def __init__(self, session: Session):
        self.session = session

    # ------------------------------------------------------------------
    # Codes
    # ------------------------------------------------------------------

    def generate_unique_code(self) -> str:
        """Return a short, share-friendly code not already in use."""
        for _ in range(10):
            candidate = "".join(
                secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH)
            )
            exists = self.session.execute(
                select(ReferralCode.id).where(ReferralCode.code == candidate),
            ).scalar_one_or_none()
            if exists is None:
                return candidate
        # Astronomically unlikely fallback.
        return uuid.uuid4().hex[:12].upper()

    def create_code(
        self,
        referrer_user_id: str,
        label: Optional[str] = None,
        organization_id: Optional[int] = None,
    ) -> ReferralCode:
        """Create a code.

        ``organization_id`` scopes the code to an org: rewards earned via it
        are credited to the org's billing account instead of the referrer's
        personal account.
        """
        code = ReferralCode(
            code=self.generate_unique_code(),
            referrer_user_id=referrer_user_id,
            referrer_organization_id=organization_id,
            label=label or None,
        )
        self.session.add(code)
        self.session.flush()
        return code

    def list_codes(
        self,
        referrer_user_id: str,
        organization_id: Optional[int] = None,
    ) -> List[ReferralCode]:
        """Codes for the given context (personal when ``organization_id`` is None)."""
        query = (
            select(ReferralCode)
            .where(
                ReferralCode.referrer_user_id == referrer_user_id,
                (
                    ReferralCode.referrer_organization_id == organization_id
                    if organization_id is not None
                    else ReferralCode.referrer_organization_id.is_(None)
                ),
            )
            .order_by(ReferralCode.created_at.asc())
        )
        return list(self.session.execute(query).scalars().all())

    def get_primary_code(
        self,
        referrer_user_id: str,
        organization_id: Optional[int] = None,
    ) -> Optional[ReferralCode]:
        """First (oldest) active code for the user/org context, if any."""
        org_filter = (
            ReferralCode.referrer_organization_id == organization_id
            if organization_id is not None
            else ReferralCode.referrer_organization_id.is_(None)
        )
        query = (
            select(ReferralCode)
            .where(
                ReferralCode.referrer_user_id == referrer_user_id,
                org_filter,
                ReferralCode.disabled_at.is_(None),
            )
            .order_by(ReferralCode.created_at.asc())
            .limit(1)
        )
        return self.session.execute(query).scalar_one_or_none()

    def get_or_create_primary_code(
        self,
        referrer_user_id: str,
        organization_id: Optional[int] = None,
    ) -> ReferralCode:
        existing = self.get_primary_code(referrer_user_id, organization_id)
        if existing is not None:
            return existing
        return self.create_code(referrer_user_id, organization_id=organization_id)

    def get_code(self, code: str) -> Optional[ReferralCode]:
        query = select(ReferralCode).where(ReferralCode.code == code)
        return self.session.execute(query).scalar_one_or_none()

    # ------------------------------------------------------------------
    # Attributions
    # ------------------------------------------------------------------

    def get_attribution_for_referee(
        self,
        referee_user_id: str,
    ) -> Optional[ReferralAttribution]:
        query = select(ReferralAttribution).where(
            ReferralAttribution.referee_user_id == referee_user_id,
        )
        return self.session.execute(query).scalar_one_or_none()

    def create_attribution(
        self,
        *,
        code: str,
        referrer_user_id: str,
        referee_user_id: str,
        referrer_organization_id: Optional[int] = None,
        referee_billing_account_id: Optional[int] = None,
        signup_ip: Optional[str] = None,
    ) -> ReferralAttribution:
        attribution = ReferralAttribution(
            code=code,
            referrer_user_id=referrer_user_id,
            referrer_organization_id=referrer_organization_id,
            referee_user_id=referee_user_id,
            referee_billing_account_id=referee_billing_account_id,
            signup_ip=signup_ip,
            status=STATUS_PENDING,
        )
        self.session.add(attribution)
        self.session.flush()
        return attribution

    def get_pending_for_billing_account(
        self,
        billing_account_id: int,
    ) -> Optional[ReferralAttribution]:
        query = select(ReferralAttribution).where(
            ReferralAttribution.referee_billing_account_id == billing_account_id,
            ReferralAttribution.status == STATUS_PENDING,
        )
        return self.session.execute(query).scalar_one_or_none()

    def get_rewarded_by_invoice(
        self,
        invoice_id: str,
    ) -> Optional[ReferralAttribution]:
        query = select(ReferralAttribution).where(
            ReferralAttribution.first_payment_invoice_id == invoice_id,
            ReferralAttribution.status == STATUS_REWARDED,
        )
        return self.session.execute(query).scalar_one_or_none()

    def count_rewarded_for_referrer(
        self,
        referrer_user_id: str,
        organization_id: Optional[int] = None,
    ) -> int:
        """Rewarded attributions counted for the per-referrer reward cap.

        Org-scoped codes share **one** cap across the whole organization (the
        org is the beneficiary, regardless of which member minted the code);
        personal codes are capped per user.
        """
        query = (
            select(func.count())
            .select_from(ReferralAttribution)
            .where(
                *self._referrer_scope(referrer_user_id, organization_id),
                ReferralAttribution.status == STATUS_REWARDED,
            )
        )
        return self.session.execute(query).scalar_one()

    def list_for_referrer(
        self,
        referrer_user_id: str,
        organization_id: Optional[int] = None,
    ) -> List[ReferralAttribution]:
        """Attributions for the given context.

        * **Personal** (``organization_id is None``): the caller's own
          attributions earned through personal (non-org) codes.
        * **Organization**: **all** attributions earned under the org's codes,
          regardless of which member minted them. The referral program is
          shared at the org level — rewards land on the org billing account —
          so every member sees the same org-wide aggregate.
        """
        query = (
            select(ReferralAttribution)
            .where(*self._referrer_scope(referrer_user_id, organization_id))
            .order_by(ReferralAttribution.created_at.desc())
        )
        return list(self.session.execute(query).scalars().all())

    def lock_attribution(
        self,
        attribution_id: str,
    ) -> Optional[ReferralAttribution]:
        """Row-lock an attribution to serialise reward/reverse transitions."""
        return (
            self.session.query(ReferralAttribution)
            .filter(ReferralAttribution.id == attribution_id)
            .with_for_update()
            .first()
        )

    def total_credits_earned(
        self,
        referrer_user_id: str,
        organization_id: Optional[int] = None,
    ) -> float:
        """Sum of rewarded referrer credits for the context.

        Org-wide in an organization context (see :meth:`list_for_referrer`),
        per-user for personal codes. Only ``rewarded`` rows count, so reversed
        attributions drop out of the total automatically.
        """
        query = select(
            func.coalesce(func.sum(ReferralAttribution.reward_amount), 0),
        ).where(
            *self._referrer_scope(referrer_user_id, organization_id),
            ReferralAttribution.status == STATUS_REWARDED,
        )
        return float(self.session.execute(query).scalar_one() or 0)

    @staticmethod
    def _referrer_scope(referrer_user_id: str, organization_id: Optional[int]):
        """WHERE clauses selecting a referrer context's attributions.

        Organization context is shared org-wide (every member sees the same
        rows); personal context is scoped to the individual user's non-org
        codes.
        """
        if organization_id is not None:
            return (ReferralAttribution.referrer_organization_id == organization_id,)
        return (
            ReferralAttribution.referrer_user_id == referrer_user_id,
            ReferralAttribution.referrer_organization_id.is_(None),
        )

    @staticmethod
    def now() -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc)
