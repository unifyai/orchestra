"""DAO for the AssistantContact and AssistantContactCost tables.

The ``assistant_contacts`` table is the single source of truth for contact
details.  This class manages the lifecycle of contact rows (create,
update, soft-delete), cost lookups, and grace-period clearing.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Set

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    AssistantContactCost,
    BillingAccount,
    BillingMode,
    Organization,
    User,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider inference (module-level constant)
# ---------------------------------------------------------------------------
_CONTACT_TYPE_PROVIDER: dict[str, str] = {
    "phone": "twilio",
    "whatsapp": "twilio",
}


class AssistantContactDAO:
    """Data access object for AssistantContact and AssistantContactCost operations."""

    def __init__(self, session: Session):
        self.session = session

    # ------------------------------------------------------------------
    # Provider inference
    # ------------------------------------------------------------------

    @staticmethod
    def infer_provider(contact_type: str) -> str | None:
        """Return the default provider for a given contact type."""
        return _CONTACT_TYPE_PROVIDER.get(contact_type)

    # ------------------------------------------------------------------
    # Cost lookup
    # ------------------------------------------------------------------

    def list_all_costs(self) -> list[AssistantContactCost]:
        """Return all contact type cost rows, ordered by type then country."""
        return (
            self.session.query(AssistantContactCost)
            .order_by(
                AssistantContactCost.contact_type,
                AssistantContactCost.provider,
                AssistantContactCost.country_code,
            )
            .all()
        )

    def upsert_cost(
        self,
        contact_type: str,
        *,
        provider: str | None = None,
        country_code: str | None = None,
        monthly_cost: Decimal,
        one_time_cost: Decimal,
    ) -> AssistantContactCost:
        """Create or update a cost row for the given type/provider/country.

        If a row with the same ``(contact_type, provider, country_code)``
        already exists it is updated in place; otherwise a new row is
        inserted.  Returns the (possibly new) row.
        """
        row = (
            self.session.query(AssistantContactCost)
            .filter(
                AssistantContactCost.contact_type == contact_type,
                (
                    AssistantContactCost.provider == provider
                    if provider is not None
                    else AssistantContactCost.provider.is_(None)
                ),
                (
                    AssistantContactCost.country_code == country_code
                    if country_code is not None
                    else AssistantContactCost.country_code.is_(None)
                ),
            )
            .first()
        )
        if row:
            row.monthly_cost = monthly_cost
            row.one_time_cost = one_time_cost
        else:
            row = AssistantContactCost(
                contact_type=contact_type,
                provider=provider,
                country_code=country_code,
                monthly_cost=monthly_cost,
                one_time_cost=one_time_cost,
            )
            self.session.add(row)
        self.session.flush()
        return row

    def delete_cost(self, cost_id: int) -> bool:
        """Delete a cost row by primary key.  Returns True if a row was removed."""
        row = self.session.query(AssistantContactCost).get(cost_id)
        if row is None:
            return False
        self.session.delete(row)
        self.session.flush()
        return True

    def get_contact_cost(
        self,
        contact_type: str,
        *,
        provider: str | None = None,
        country_code: str | None = None,
    ) -> AssistantContactCost | None:
        """Look up the cost row for the given contact type + provider + country.

        Falls back to the ``country_code IS NULL`` row if no country-specific
        row exists, and further to the ``provider IS NULL`` row if no
        provider-specific row exists.
        """
        if provider is None:
            provider = self.infer_provider(contact_type)

        # Try exact match first (type + provider + country)
        row = (
            self.session.query(AssistantContactCost)
            .filter(
                AssistantContactCost.contact_type == contact_type,
                AssistantContactCost.provider == provider,
                AssistantContactCost.country_code == country_code,
            )
            .first()
        )
        if row:
            return row

        # Fallback: provider match without country
        if country_code is not None:
            row = (
                self.session.query(AssistantContactCost)
                .filter(
                    AssistantContactCost.contact_type == contact_type,
                    AssistantContactCost.provider == provider,
                    AssistantContactCost.country_code.is_(None),
                )
                .first()
            )
            if row:
                return row

        # Fallback: type only (provider and country both NULL)
        return (
            self.session.query(AssistantContactCost)
            .filter(
                AssistantContactCost.contact_type == contact_type,
                AssistantContactCost.provider.is_(None),
                AssistantContactCost.country_code.is_(None),
            )
            .first()
        )

    def get_contact_monthly_cost(
        self,
        contact_type: str,
        *,
        provider: str | None = None,
        country_code: str | None = None,
    ) -> Decimal:
        """Return the monthly cost for a contact type, or ``Decimal("0")``."""
        row = self.get_contact_cost(
            contact_type,
            provider=provider,
            country_code=country_code,
        )
        return Decimal(str(row.monthly_cost)) if row else Decimal("0")

    def get_contact_one_time_cost(
        self,
        contact_type: str,
        *,
        provider: str | None = None,
        country_code: str | None = None,
    ) -> Decimal:
        """Return the one-time (setup) cost for a contact type, or ``Decimal("0")``."""
        row = self.get_contact_cost(
            contact_type,
            provider=provider,
            country_code=country_code,
        )
        return Decimal(str(row.one_time_cost)) if row else Decimal("0")

    def get_contact_by_assistant_and_type(
        self,
        assistant_id: int,
        contact_type: str,
    ) -> AssistantContact | None:
        """Return the active contact of the given type for an assistant, if any."""
        return (
            self.session.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id == assistant_id,
                AssistantContact.contact_type == contact_type,
                AssistantContact.status != "deleted",
            )
            .first()
        )

    # ------------------------------------------------------------------
    # Contact lifecycle
    # ------------------------------------------------------------------

    def upsert_assistant_contact(
        self,
        *,
        assistant_id: int,
        contact_type: str,
        contact_value: str,
        provider: str | None = None,
        country_code: str | None = None,
        provisioned_by: str = "platform",
        metadata: dict | None = None,
    ) -> AssistantContact:
        """Create or re-activate an ``AssistantContact`` row.

        If a *deleted* row for the same ``(assistant_id, contact_type)``
        already exists, it is recycled (un-deleted) to avoid
        partial-unique-index violations.  Otherwise a new row is created.

        User-side contact info (phone, whatsapp) is stored on the ``User``
        model rather than per-contact; see ``User.phone_number`` and
        ``User.whatsapp_number``.
        """
        if provider is None:
            provider = self.infer_provider(contact_type)

        # Check for an existing active row (shouldn't happen in well-behaved flows)
        existing_active = (
            self.session.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id == assistant_id,
                AssistantContact.contact_type == contact_type,
                AssistantContact.status != "deleted",
            )
            .first()
        )
        if existing_active:
            existing_active.contact_value = contact_value
            existing_active.provider = provider
            existing_active.country_code = country_code
            existing_active.provisioned_by = provisioned_by
            existing_active.metadata_ = metadata or {}
            existing_active.updated_at = datetime.utcnow()
            return existing_active

        # Check for a previously deleted row to recycle
        deleted_row = (
            self.session.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id == assistant_id,
                AssistantContact.contact_type == contact_type,
                AssistantContact.status == "deleted",
            )
            .first()
        )
        if deleted_row:
            deleted_row.contact_value = contact_value
            deleted_row.provider = provider
            deleted_row.country_code = country_code
            deleted_row.provisioned_by = provisioned_by
            deleted_row.metadata_ = metadata or {}
            deleted_row.status = "active"
            deleted_row.deleted_at = None
            deleted_row.grace_period_started_at = None
            deleted_row.last_billed_month = None
            deleted_row.monthly_cost = None
            deleted_row.updated_at = datetime.utcnow()
            return deleted_row

        # Create fresh row
        contact = AssistantContact(
            assistant_id=assistant_id,
            contact_type=contact_type,
            contact_value=contact_value,
            provider=provider,
            provisioned_by=provisioned_by,
            country_code=country_code,
            status="active",
            metadata_=metadata or {},
        )
        self.session.add(contact)
        return contact

    def soft_delete_assistant_contact(
        self,
        *,
        assistant_id: int,
        contact_type: str,
    ) -> AssistantContact | None:
        """Soft-delete an active ``AssistantContact`` row.

        Returns the row if found, ``None`` otherwise.
        """
        row = (
            self.session.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id == assistant_id,
                AssistantContact.contact_type == contact_type,
                AssistantContact.status != "deleted",
            )
            .first()
        )
        if row:
            row.status = "deleted"
            row.deleted_at = datetime.utcnow()
        return row

    def soft_delete_all_contacts_for_assistant(
        self,
        assistant_id: int,
    ) -> list[AssistantContact]:
        """Soft-delete every active contact for the given assistant.

        Returns the list of rows that were soft-deleted.
        """
        rows = (
            self.session.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id == assistant_id,
                AssistantContact.status != "deleted",
            )
            .all()
        )
        now = datetime.utcnow()
        for row in rows:
            row.status = "deleted"
            row.deleted_at = now
        return rows

    def get_active_contacts_for_assistant(
        self,
        assistant_id: int,
    ) -> list[AssistantContact]:
        """Return all active (non-deleted) contacts for the given assistant."""
        return (
            self.session.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id == assistant_id,
                AssistantContact.status != "deleted",
            )
            .all()
        )

    def get_active_contacts_for_assistants(
        self,
        assistant_ids: list[int],
    ) -> list[AssistantContact]:
        """Return all active contacts for a list of assistants (batch query)."""
        if not assistant_ids:
            return []
        return (
            self.session.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id.in_(assistant_ids),
                AssistantContact.status != "deleted",
            )
            .all()
        )

    def has_grace_period_contacts(
        self,
        assistant_id: int,
    ) -> bool:
        """Return ``True`` if the assistant has any contacts in ``grace_period``."""
        return (
            self.session.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id == assistant_id,
                AssistantContact.status == "grace_period",
            )
            .first()
            is not None
        )

    def soft_delete_contacts_for_user(
        self,
        user_id: str,
    ) -> list[AssistantContact]:
        """Soft-delete all active contacts across all assistants owned by a user.

        Used when a user account is deleted.
        """
        rows = (
            self.session.query(AssistantContact)
            .join(Assistant, AssistantContact.assistant_id == Assistant.agent_id)
            .filter(
                Assistant.user_id == user_id,
                Assistant.organization_id.is_(None),
                AssistantContact.status != "deleted",
            )
            .all()
        )
        now = datetime.utcnow()
        for row in rows:
            row.status = "deleted"
            row.deleted_at = now
        return rows

    def soft_delete_contacts_for_organization(
        self,
        organization_id: int,
    ) -> list[AssistantContact]:
        """Soft-delete all active contacts across all assistants owned by an org.

        Used when an organization is deleted.
        """
        rows = (
            self.session.query(AssistantContact)
            .join(Assistant, AssistantContact.assistant_id == Assistant.agent_id)
            .filter(
                Assistant.organization_id == organization_id,
                AssistantContact.status != "deleted",
            )
            .all()
        )
        now = datetime.utcnow()
        for row in rows:
            row.status = "deleted"
            row.deleted_at = now
        return rows

    # ------------------------------------------------------------------
    # Grace-period management
    # ------------------------------------------------------------------

    def clear_grace_period_for_billing_account(
        self,
        ba: BillingAccount,
    ) -> Set[int]:
        """Clear the grace period on all contacts for the given billing account.

        Called when credits are added (e.g. via Stripe webhook) and the
        billing account has positive credits again.

        Returns the set of assistant IDs whose contacts were restored (so
        the caller can trigger ``reawaken_assistant()`` for each).
        """
        # Find all assistants belonging to this billing account
        #
        # Personal user case:
        user = self.session.query(User).filter(User.billing_account_id == ba.id).first()
        # Org case:
        org = (
            self.session.query(Organization)
            .filter(Organization.billing_account_id == ba.id)
            .first()
        )

        assistant_ids: Set[int] = set()

        if user:
            personal_assistants = (
                self.session.query(Assistant.agent_id)
                .filter(
                    Assistant.user_id == user.id,
                    Assistant.organization_id.is_(None),
                )
                .all()
            )
            assistant_ids.update(a_id for (a_id,) in personal_assistants)

        if org:
            org_assistants = (
                self.session.query(Assistant.agent_id)
                .filter(Assistant.organization_id == org.id)
                .all()
            )
            assistant_ids.update(a_id for (a_id,) in org_assistants)

        if not assistant_ids:
            return set()

        # Clear grace period on all grace_period contacts for these assistants
        grace_contacts = (
            self.session.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id.in_(assistant_ids),
                AssistantContact.status == "grace_period",
            )
            .all()
        )

        affected_assistant_ids: Set[int] = set()
        for contact in grace_contacts:
            contact.status = "active"
            contact.grace_period_started_at = None
            affected_assistant_ids.add(contact.assistant_id)

        if affected_assistant_ids:
            logger.info(
                "Cleared grace period on %d contacts for billing account %d "
                "(assistants: %s).",
                len(grace_contacts),
                ba.id,
                affected_assistant_ids,
            )

        return affected_assistant_ids

    def maybe_clear_grace_period(
        self,
        ba: BillingAccount,
    ) -> None:
        """Clear grace period on contacts if credits have been restored.

        Called after credits are added (checkout or invoice payment succeeded).
        If credits are now >= 0, restores all contacts from
        ``grace_period`` to ``active``.

        For METERED accounts the wallet is frozen and may carry any
        leftover balance from a prior CREDITS phase, so the wallet
        balance is not a reliable signal. Grace period for METERED
        accounts is driven by ``invoice.payment_succeeded`` /
        ``invoice.payment_failed`` webhooks instead — clear
        unconditionally here when called for a METERED account
        (the caller has already established that an invoice was paid).

        Does NOT change ``account_status`` — that is only set by
        dispute/fraud events.

        Note: ``reawaken_assistant()`` is *not* called here because the Stripe
        webhook handler is synchronous.  The daily suspension routine will
        reawaken affected assistants on its next run; contacts in grace_period
        still have provisioned resources, so they remain functional.
        """
        from orchestra.db.dao.billing_account_dao import BillingAccountDAO

        self.session.refresh(ba)

        is_metered = (
            BillingAccountDAO(self.session).resolve_billing_mode(ba)
            == BillingMode.METERED
        )

        if is_metered or ba.credits >= 0:
            try:
                affected = self.clear_grace_period_for_billing_account(ba)
                if affected:
                    logger.info(
                        {
                            "message": "Grace period cleared after credit top-up",
                            "billing_account_id": ba.id,
                            "affected_assistants": list(affected),
                        },
                    )
            except Exception as e:
                logger.warning(
                    {
                        "message": "Failed to clear grace period after credit top-up (non-fatal)",
                        "billing_account_id": ba.id,
                        "error": str(e),
                    },
                )
