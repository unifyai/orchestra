"""DAO for WhatsApp pool number management and inbound routing.

Implements the two-tier routing algorithm:
  Tier 1 — Dynamic user lookup (platform users/org members)
  Tier 2 — Static route table (external contacts)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

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

    def add_pool_number(
        self,
        number: str,
        twilio_sender_sid: str | None = None,
    ) -> WhatsAppPoolNumber:
        """Add a new number to the pool. Raises ValueError if it already exists."""
        if self.get_pool_number_by_value(number):
            raise ValueError(f"Pool number {number} already exists.")
        pool = WhatsAppPoolNumber(
            number=number,
            twilio_sender_sid=twilio_sender_sid,
        )
        self.session.add(pool)
        self.session.flush()
        return pool

    def update_pool_number(
        self,
        pool_id: int,
        status: str | None = None,
        twilio_sender_sid: str | None = ...,
    ) -> WhatsAppPoolNumber:
        """Update a pool number's status and/or Twilio SID."""
        pool = (
            self.session.query(WhatsAppPoolNumber)
            .filter(WhatsAppPoolNumber.id == pool_id)
            .first()
        )
        if not pool:
            raise ValueError(f"Pool number with id {pool_id} not found.")
        if status is not None:
            pool.status = status
        if twilio_sender_sid is not ...:
            pool.twilio_sender_sid = twilio_sender_sid
        self.session.flush()
        return pool

    def delete_pool_number(self, pool_id: int) -> int:
        """Delete a pool number if no active contacts reference it.

        Returns the number of routes cleaned up.
        """
        pool = (
            self.session.query(WhatsAppPoolNumber)
            .filter(WhatsAppPoolNumber.id == pool_id)
            .first()
        )
        if not pool:
            raise ValueError(f"Pool number with id {pool_id} not found.")
        active_contacts = (
            self.session.query(AssistantContact)
            .filter(
                AssistantContact.contact_type == "whatsapp",
                AssistantContact.contact_value == pool.number,
                AssistantContact.status == "active",
            )
            .count()
        )
        if active_contacts:
            raise ValueError(
                f"Cannot delete pool number {pool.number}: "
                f"{active_contacts} active assistant(s) use it.",
            )
        route_count = (
            self.session.query(WhatsAppRoute)
            .filter(WhatsAppRoute.pool_number_id == pool_id)
            .delete()
        )
        self.session.delete(pool)
        self.session.flush()
        return route_count

    # ------------------------------------------------------------------
    # Inbound routing (Tier 1 + Tier 2)
    # ------------------------------------------------------------------

    def resolve_inbound(
        self,
        pool_number: str,
        sender: str,
    ) -> dict | None:
        """Resolve an inbound WhatsApp message to an assistant.

        Tier 1a: match sender to User.whatsapp_number (unique, preferred).
        Tier 1b: fall back to User.phone_number (skip if ambiguous).
        Tier 2:  static route table for external contacts.

        On success, atomically updates ``last_inbound_at`` on the
        corresponding route row (creating one if needed for Tier 1)
        so the 24h session window can be computed on outbound.

        Returns ``{"assistant_id": int, "role": str}`` or ``None``.
        """
        now = datetime.now(timezone.utc)

        # Tier 1a: explicit whatsapp_number match (unique index → at most 1)
        user = self.session.query(User).filter(User.whatsapp_number == sender).first()

        # Tier 1b: phone_number fallback (not unique → must handle ambiguity)
        if not user:
            phone_matches = (
                self.session.query(User).filter(User.phone_number == sender).all()
            )
            if len(phone_matches) == 1:
                user = phone_matches[0]
            elif len(phone_matches) > 1:
                logger.warning(
                    "Ambiguous phone_number match for sender %s: %d users",
                    sender,
                    len(phone_matches),
                )

        if user:
            # Find assistants this user can access that have WhatsApp
            # on the given pool number
            accessible = self._find_accessible_assistants(
                user.id,
                pool_number,
            )
            assistant_id = None
            if len(accessible) == 1:
                assistant_id = accessible[0]
            elif len(accessible) > 1:
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
                    assistant_id = latest[0]

            if assistant_id is not None:
                self._touch_inbound(pool_number, sender, assistant_id, now)
                return {"assistant_id": assistant_id, "role": "owner"}

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
            route.last_inbound_at = now
            self.session.flush()
            return {"assistant_id": route.assistant_id, "role": "contact"}

        return None

    def _touch_inbound(
        self,
        pool_number_str: str,
        contact_number: str,
        assistant_id: int,
        now: datetime,
    ) -> None:
        """Record an inbound timestamp on the route row, creating it if needed."""
        pool = self.get_pool_number_by_value(pool_number_str)
        if pool is None:
            return

        route = (
            self.session.query(WhatsAppRoute)
            .filter(
                WhatsAppRoute.pool_number_id == pool.id,
                WhatsAppRoute.contact_number == contact_number,
            )
            .first()
        )
        if route:
            route.last_inbound_at = now
        else:
            route = WhatsAppRoute(
                pool_number_id=pool.id,
                contact_number=contact_number,
                assistant_id=assistant_id,
                last_inbound_at=now,
            )
            self.session.add(route)
        self.session.flush()

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
