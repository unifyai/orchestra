"""DAO for WhatsApp pool number management and inbound routing.

Implements the two-tier routing algorithm:
  Tier 1 — Dynamic user lookup (platform users/org members)
  Tier 2 — Static route table (external contacts)
"""

from __future__ import annotations

import logging

from sqlalchemy import and_
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    OrganizationMember,
    User,
    WhatsAppPoolNumber,
    WhatsAppRoute,
)

logger = logging.getLogger(__name__)


class WhatsAppRouteDAO:
    """Data access object for WhatsApp pool numbers and routing."""

    def __init__(self, session: Session):
        self.session = session

    # ------------------------------------------------------------------
    # Pool management
    # ------------------------------------------------------------------

    def list_pool_numbers(self) -> list[WhatsAppPoolNumber]:
        """Return all pool numbers ordered by id."""
        return (
            self.session.query(WhatsAppPoolNumber).order_by(WhatsAppPoolNumber.id).all()
        )

    def get_pool_number_by_value(self, number: str) -> WhatsAppPoolNumber | None:
        """Look up a pool number by its E.164 value."""
        return (
            self.session.query(WhatsAppPoolNumber)
            .filter(WhatsAppPoolNumber.number == number)
            .first()
        )

    # ------------------------------------------------------------------
    # Inbound routing (Tier 1 + Tier 2)
    # ------------------------------------------------------------------

    def resolve_inbound(
        self,
        pool_number: str,
        sender: str,
    ) -> dict | None:
        """Resolve an inbound WhatsApp message to an assistant.

        Implements the two-tier algorithm:
        1. Look up sender as a platform user → find accessible assistants
           with WhatsApp enabled on this pool number.
        2. Fall back to the static route table for external contacts.

        Returns ``{"assistant_id": int, "role": str}`` or ``None``.
        """
        # Tier 1: dynamic user lookup
        user = self.session.query(User).filter(User.whatsapp_number == sender).first()

        if user:
            # Find assistants this user can access that have WhatsApp
            # on the given pool number
            accessible = self._find_accessible_assistants(
                user.id,
                pool_number,
            )
            if len(accessible) == 1:
                return {"assistant_id": accessible[0], "role": "owner"}
            if len(accessible) > 1:
                # Ambiguity — pick the most recently activated contact
                latest = (
                    self.session.query(AssistantContact.assistant_id)
                    .filter(
                        AssistantContact.assistant_id.in_(accessible),
                        AssistantContact.contact_type == "whatsapp",
                        AssistantContact.contact_value == pool_number,
                        AssistantContact.status == "active",
                    )
                    .order_by(AssistantContact.created_at.desc())
                    .first()
                )
                if latest:
                    return {"assistant_id": latest[0], "role": "owner"}

        # Tier 2: static route table
        pool = self.get_pool_number_by_value(pool_number)
        if pool is None:
            return None

        route = (
            self.session.query(WhatsAppRoute)
            .filter(
                WhatsAppRoute.pool_number_id == pool.id,
                WhatsAppRoute.contact_number == sender,
            )
            .first()
        )
        if route:
            return {"assistant_id": route.assistant_id, "role": "contact"}

        return None

    def _find_accessible_assistants(
        self,
        user_id: str,
        pool_number: str,
    ) -> list[int]:
        """Find assistant IDs this user can access with WhatsApp on pool_number.

        An assistant is accessible if:
        - Personal: ``assistant.user_id == user_id`` and
          ``assistant.organization_id IS NULL``
        - Org: ``assistant.organization_id`` is in the set of orgs
          the user belongs to
        """
        # Build subquery for org IDs the user belongs to
        user_org_ids = (
            self.session.query(OrganizationMember.organization_id)
            .filter(OrganizationMember.user_id == user_id)
            .subquery()
        )

        rows = (
            self.session.query(AssistantContact.assistant_id)
            .join(
                Assistant,
                AssistantContact.assistant_id == Assistant.agent_id,
            )
            .filter(
                AssistantContact.contact_type == "whatsapp",
                AssistantContact.contact_value == pool_number,
                AssistantContact.status == "active",
                (
                    and_(
                        Assistant.user_id == user_id,
                        Assistant.organization_id.is_(None),
                    )
                    | Assistant.organization_id.in_(user_org_ids)
                ),
            )
            .all()
        )
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # Pool assignment (activation)
    # ------------------------------------------------------------------

    def find_eligible_pool_numbers(
        self,
        assistant_id: int,
        accessible_user_ids: list[str],
    ) -> list[WhatsAppPoolNumber]:
        """Find pool numbers with no conflicts for the given assistant.

        A pool number is ineligible if any accessible user already has
        another assistant (≠ assistant_id) with WhatsApp on that number.
        """
        active_pool = (
            self.session.query(WhatsAppPoolNumber)
            .filter(WhatsAppPoolNumber.status == "active")
            .order_by(WhatsAppPoolNumber.id)
            .all()
        )

        eligible = []
        for pool in active_pool:
            has_conflict = False
            for uid in accessible_user_ids:
                conflicting = self._find_accessible_assistants(uid, pool.number)
                # Remove the assistant being activated
                conflicting = [a for a in conflicting if a != assistant_id]
                if conflicting:
                    has_conflict = True
                    break
            if not has_conflict:
                eligible.append(pool)

        return eligible

    def assign_pool_number(
        self,
        assistant_id: int,
        accessible_user_ids: list[str],
    ) -> WhatsAppPoolNumber:
        """Assign the first eligible pool number for the assistant.

        Raises ``ValueError`` if no pool number is available.
        """
        eligible = self.find_eligible_pool_numbers(
            assistant_id,
            accessible_user_ids,
        )
        if not eligible:
            raise ValueError(
                "All WhatsApp lines are currently assigned. "
                "More numbers coming soon.",
            )
        return eligible[0]

    # ------------------------------------------------------------------
    # External contact routes
    # ------------------------------------------------------------------

    def get_or_create_route(
        self,
        assistant_id: int,
        contact_number: str,
    ) -> WhatsAppRoute:
        """Get or create a route for an outbound message to an external contact.

        The pool number is resolved from the assistant's active WhatsApp contact.

        Raises ``ValueError`` if the assistant has no WhatsApp contact or
        if the route conflicts with another assistant.
        """
        contact = (
            self.session.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id == assistant_id,
                AssistantContact.contact_type == "whatsapp",
                AssistantContact.status == "active",
            )
            .first()
        )
        if not contact:
            raise ValueError("Assistant does not have WhatsApp enabled.")

        pool = self.get_pool_number_by_value(contact.contact_value)
        if not pool:
            raise ValueError(
                f"Pool number {contact.contact_value} not found.",
            )

        # Check for existing route
        existing = (
            self.session.query(WhatsAppRoute)
            .filter(
                WhatsAppRoute.assistant_id == assistant_id,
                WhatsAppRoute.contact_number == contact_number,
            )
            .first()
        )
        if existing:
            return existing

        # Check for conflict: another assistant already has this
        # (pool_number, contact) pair
        conflict = (
            self.session.query(WhatsAppRoute)
            .filter(
                WhatsAppRoute.pool_number_id == pool.id,
                WhatsAppRoute.contact_number == contact_number,
            )
            .first()
        )
        if conflict:
            raise ValueError(
                f"Contact {contact_number} is already routed to assistant "
                f"{conflict.assistant_id} on this pool number.",
            )

        route = WhatsAppRoute(
            pool_number_id=pool.id,
            contact_number=contact_number,
            assistant_id=assistant_id,
        )
        self.session.add(route)
        self.session.flush()
        return route

    def delete_routes_for_assistant(self, assistant_id: int) -> int:
        """Delete all routes for an assistant. Returns the count deleted."""
        count = (
            self.session.query(WhatsAppRoute)
            .filter(WhatsAppRoute.assistant_id == assistant_id)
            .delete()
        )
        return count

    def get_routes_for_assistant(self, assistant_id: int) -> list[WhatsAppRoute]:
        """Return all routes for an assistant."""
        return (
            self.session.query(WhatsAppRoute)
            .filter(WhatsAppRoute.assistant_id == assistant_id)
            .all()
        )
