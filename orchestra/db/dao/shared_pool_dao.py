"""DAO for shared-pool number management, inbound routing, and conflict resolution.

Implements the two-tier routing algorithm:
  Tier 1 — Dynamic user lookup (platform users/org members)
  Tier 2 — Static route table (external contacts)

Handles inline conflict resolution: when a route creation would collide
with another assistant on the same pool number, the initiating assistant
is automatically reassigned to a different pool number and its existing
routes are migrated — all within a single database transaction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    ConflictEvent,
    DecommissionedRoute,
    OrganizationMember,
    SharedPlatformRoute,
    SharedPoolNumber,
    User,
)

logger = logging.getLogger(__name__)


@dataclass
class ConflictResolution:
    """Result of an inline conflict resolution."""

    conflict_type: str
    affected_assistant_ids: list[int]
    old_pool_assignments: dict[int, str]  # assistant_id → old pool number
    new_pool_assignments: dict[int, str]  # assistant_id → new pool number
    notification_recipients: list[dict] = field(default_factory=list)
    conflict_event_id: int | None = None


class SharedPoolDAO:
    """Data access object for shared pool numbers, routing, and conflict resolution."""

    def __init__(self, session: Session, platform: str = "whatsapp"):
        self.session = session
        self.platform = platform

    # ------------------------------------------------------------------
    # Pool management
    # ------------------------------------------------------------------

    def list_pool_numbers(self) -> list[SharedPoolNumber]:
        return (
            self.session.query(SharedPoolNumber)
            .filter(SharedPoolNumber.platform == self.platform)
            .order_by(SharedPoolNumber.id)
            .all()
        )

    def get_pool_number_by_value(self, number: str) -> SharedPoolNumber | None:
        return (
            self.session.query(SharedPoolNumber)
            .filter(
                SharedPoolNumber.number == number,
                SharedPoolNumber.platform == self.platform,
            )
            .first()
        )

    def add_pool_number(
        self,
        number: str,
        twilio_sender_sid: str | None = None,
    ) -> SharedPoolNumber:
        if self.get_pool_number_by_value(number):
            raise ValueError(f"Pool number {number} already exists.")
        pool = SharedPoolNumber(
            platform=self.platform,
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
    ) -> SharedPoolNumber:
        pool = (
            self.session.query(SharedPoolNumber)
            .filter(SharedPoolNumber.id == pool_id)
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
        pool = (
            self.session.query(SharedPoolNumber)
            .filter(SharedPoolNumber.id == pool_id)
            .first()
        )
        if not pool:
            raise ValueError(f"Pool number with id {pool_id} not found.")
        active_contacts = (
            self.session.query(AssistantContact)
            .filter(
                AssistantContact.contact_type == self.platform,
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
            self.session.query(SharedPlatformRoute)
            .filter(SharedPlatformRoute.pool_number_id == pool_id)
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
        """Resolve an inbound message to an assistant.

        Tier 1: match sender to platform user identity (unique, preferred).
        Tier 2: static route table for external contacts.

        Returns ``{"assistant_id": int, "role": str}``
        or ``{"action": "auto_reply"}`` for decommissioned routes
        or ``{"action": "reject_cold"}`` for unknown senders on shared pools
        or ``None`` for unknown senders on dedicated pools.
        """
        now = datetime.now(timezone.utc)

        # Tier 1: platform-specific user identity match
        user = self._find_user_by_platform_identity(sender)

        if user:
            accessible = self._find_accessible_assistants(user.id, pool_number)
            assistant_id = None
            if len(accessible) == 1:
                assistant_id = accessible[0]
            elif len(accessible) > 1:
                latest = (
                    self.session.query(AssistantContact.assistant_id)
                    .filter(
                        AssistantContact.assistant_id.in_(accessible),
                        AssistantContact.contact_type == self.platform,
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
            self.session.query(SharedPlatformRoute)
            .filter(
                SharedPlatformRoute.pool_number_id == pool.id,
                SharedPlatformRoute.contact_number == sender,
            )
            .first()
        )
        if route:
            route.last_inbound_at = now
            self.session.flush()
            return {"assistant_id": route.assistant_id, "role": "contact"}

        # Check decommissioned routes
        decomm = (
            self.session.query(DecommissionedRoute)
            .filter(
                DecommissionedRoute.pool_number_id == pool.id,
                DecommissionedRoute.contact_identifier == sender,
            )
            .first()
        )
        if decomm:
            return {"action": "auto_reply"}

        # Cold-message detection: shared pool (>1 assistant) rejects unknown senders
        assistant_count = (
            self.session.query(AssistantContact)
            .filter(
                AssistantContact.contact_type == self.platform,
                AssistantContact.contact_value == pool_number,
                AssistantContact.status == "active",
            )
            .count()
        )
        if assistant_count > 1:
            return {"action": "reject_cold"}

        return None

    def _find_user_by_platform_identity(self, sender: str) -> User | None:
        if self.platform == "whatsapp":
            return (
                self.session.query(User).filter(User.whatsapp_number == sender).first()
            )
        return None

    def _touch_inbound(
        self,
        pool_number_str: str,
        contact_number: str,
        assistant_id: int,
        now: datetime,
    ) -> None:
        pool = self.get_pool_number_by_value(pool_number_str)
        if pool is None:
            return

        route = (
            self.session.query(SharedPlatformRoute)
            .filter(
                SharedPlatformRoute.pool_number_id == pool.id,
                SharedPlatformRoute.contact_number == contact_number,
            )
            .first()
        )
        if route:
            route.last_inbound_at = now
        else:
            route = SharedPlatformRoute(
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
        user_org_ids = select(OrganizationMember.organization_id).where(
            OrganizationMember.user_id == user_id,
        )

        rows = (
            self.session.query(AssistantContact.assistant_id)
            .join(
                Assistant,
                AssistantContact.assistant_id == Assistant.agent_id,
            )
            .filter(
                AssistantContact.contact_type == self.platform,
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
    ) -> list[SharedPoolNumber]:
        active_pool = (
            self.session.query(SharedPoolNumber)
            .filter(
                SharedPoolNumber.status == "active",
                SharedPoolNumber.platform == self.platform,
            )
            .order_by(SharedPoolNumber.id)
            .all()
        )

        eligible = []
        for pool in active_pool:
            has_conflict = False
            for uid in accessible_user_ids:
                conflicting = self._find_accessible_assistants(uid, pool.number)
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
    ) -> SharedPoolNumber:
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
    # External contact routes (with inline conflict resolution)
    # ------------------------------------------------------------------

    def get_or_create_route(
        self,
        assistant_id: int,
        contact_number: str,
    ) -> tuple[SharedPlatformRoute, ConflictResolution | None]:
        """Get or create a route for an outbound message.

        If a conflict is detected (another assistant on the same pool already
        routes to this contact), the conflict is resolved inline: the
        initiating assistant is reassigned to a new pool number and all its
        routes are migrated.

        Returns ``(route, conflict_resolution)`` where conflict_resolution
        is ``None`` if no conflict occurred.
        """
        contact = (
            self.session.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id == assistant_id,
                AssistantContact.contact_type == self.platform,
                AssistantContact.status == "active",
            )
            .first()
        )
        if not contact:
            raise ValueError("Assistant does not have WhatsApp enabled.")

        pool = self.get_pool_number_by_value(contact.contact_value)
        if not pool:
            raise ValueError(f"Pool number {contact.contact_value} not found.")

        # Existing route for this assistant → return it
        existing = (
            self.session.query(SharedPlatformRoute)
            .filter(
                SharedPlatformRoute.assistant_id == assistant_id,
                SharedPlatformRoute.contact_number == contact_number,
            )
            .first()
        )
        if existing:
            return existing, None

        # Check for conflict: another assistant already has this (pool, contact) pair
        conflict_route = (
            self.session.query(SharedPlatformRoute)
            .filter(
                SharedPlatformRoute.pool_number_id == pool.id,
                SharedPlatformRoute.contact_number == contact_number,
            )
            .first()
        )

        resolution = None
        if conflict_route:
            # Check if the target is a platform user (Case 2) or external (Case 1)
            target_user = self._find_user_by_platform_identity(contact_number)
            if target_user:
                resolution = self._resolve_user_to_user_conflict(
                    assistant_id,
                    conflict_route.assistant_id,
                    pool,
                )
            else:
                resolution = self._resolve_contact_overlap_conflict(
                    assistant_id,
                    pool,
                )

            if resolution is None:
                raise ValueError(
                    "Conflict detected but could not be resolved "
                    "(insufficient pool numbers).",
                )

            # Refresh the contact to get the new pool number
            self.session.refresh(contact)
            pool = self.get_pool_number_by_value(contact.contact_value)

        # Create the route on the (potentially new) pool
        route = SharedPlatformRoute(
            pool_number_id=pool.id,
            contact_number=contact_number,
            assistant_id=assistant_id,
        )
        self.session.add(route)
        self.session.flush()
        return route, resolution

    def _resolve_contact_overlap_conflict(
        self,
        initiator_assistant_id: int,
        current_pool: SharedPoolNumber,
    ) -> ConflictResolution | None:
        """Case 1: Reassign the initiating assistant to a new pool number."""
        assistant = (
            self.session.query(Assistant)
            .filter(Assistant.agent_id == initiator_assistant_id)
            .first()
        )
        accessible_user_ids = self._get_accessible_user_ids(assistant)
        new_pool = self._find_alternative_pool(
            initiator_assistant_id,
            current_pool.id,
            accessible_user_ids,
        )
        if new_pool is None:
            return None

        old_number = current_pool.number
        new_number = new_pool.number

        self._migrate_assistant_to_pool(
            initiator_assistant_id,
            current_pool,
            new_pool,
        )

        resolution = ConflictResolution(
            conflict_type="contact_overlap",
            affected_assistant_ids=[initiator_assistant_id],
            old_pool_assignments={initiator_assistant_id: old_number},
            new_pool_assignments={initiator_assistant_id: new_number},
        )
        resolution.notification_recipients = self._gather_notification_recipients(
            [initiator_assistant_id],
            {initiator_assistant_id: (old_number, new_number)},
        )
        resolution.conflict_event_id = self._record_conflict_event(resolution)
        return resolution

    def _resolve_user_to_user_conflict(
        self,
        initiator_assistant_id: int,
        other_assistant_id: int,
        current_pool: SharedPoolNumber,
    ) -> ConflictResolution | None:
        """Case 2: Reassign both assistants to new unique pool numbers."""
        assistants = {}
        for aid in [initiator_assistant_id, other_assistant_id]:
            a = self.session.query(Assistant).filter(Assistant.agent_id == aid).first()
            assistants[aid] = a

        old_number = current_pool.number

        # Find two distinct new pool numbers
        new_pools = {}
        used_pool_ids = {current_pool.id}
        for aid in [initiator_assistant_id, other_assistant_id]:
            accessible = self._get_accessible_user_ids(assistants[aid])
            new_pool = self._find_alternative_pool(aid, used_pool_ids, accessible)
            if new_pool is None:
                return None
            new_pools[aid] = new_pool
            used_pool_ids.add(new_pool.id)

        # Migrate both
        assignments_old = {}
        assignments_new = {}
        change_map = {}
        for aid in [initiator_assistant_id, other_assistant_id]:
            assignments_old[aid] = old_number
            assignments_new[aid] = new_pools[aid].number
            change_map[aid] = (old_number, new_pools[aid].number)
            self._migrate_assistant_to_pool(aid, current_pool, new_pools[aid])

        resolution = ConflictResolution(
            conflict_type="user_to_user",
            affected_assistant_ids=[initiator_assistant_id, other_assistant_id],
            old_pool_assignments=assignments_old,
            new_pool_assignments=assignments_new,
        )
        resolution.notification_recipients = self._gather_notification_recipients(
            [initiator_assistant_id, other_assistant_id],
            change_map,
        )
        resolution.conflict_event_id = self._record_conflict_event(resolution)
        return resolution

    def _find_alternative_pool(
        self,
        assistant_id: int,
        exclude_pool_ids: int | set[int],
        accessible_user_ids: list[str],
    ) -> SharedPoolNumber | None:
        """Find an active pool number that doesn't conflict, excluding given IDs."""
        if isinstance(exclude_pool_ids, int):
            exclude_pool_ids = {exclude_pool_ids}

        active_pool = (
            self.session.query(SharedPoolNumber)
            .filter(
                SharedPoolNumber.status == "active",
                SharedPoolNumber.platform == self.platform,
                ~SharedPoolNumber.id.in_(exclude_pool_ids),
            )
            .order_by(SharedPoolNumber.id)
            .all()
        )

        for pool in active_pool:
            has_conflict = False
            for uid in accessible_user_ids:
                conflicting = self._find_accessible_assistants(uid, pool.number)
                conflicting = [a for a in conflicting if a != assistant_id]
                if conflicting:
                    has_conflict = True
                    break
            if not has_conflict:
                return pool
        return None

    def _migrate_assistant_to_pool(
        self,
        assistant_id: int,
        old_pool: SharedPoolNumber,
        new_pool: SharedPoolNumber,
    ) -> None:
        """Move an assistant's routes from old_pool to new_pool and update its contact."""
        # Migrate existing routes
        routes = (
            self.session.query(SharedPlatformRoute)
            .filter(
                SharedPlatformRoute.assistant_id == assistant_id,
                SharedPlatformRoute.pool_number_id == old_pool.id,
            )
            .all()
        )
        for route in routes:
            # Record decommissioned route for auto-reply
            self.session.add(
                DecommissionedRoute(
                    platform=self.platform,
                    pool_number_id=old_pool.id,
                    contact_identifier=route.contact_number,
                    old_assistant_id=assistant_id,
                    new_pool_number_id=new_pool.id,
                ),
            )
            # Check if the new (pool, contact) pair already exists
            existing_on_new = (
                self.session.query(SharedPlatformRoute)
                .filter(
                    SharedPlatformRoute.pool_number_id == new_pool.id,
                    SharedPlatformRoute.contact_number == route.contact_number,
                )
                .first()
            )
            if existing_on_new:
                # Another assistant already has this contact on the new pool;
                # delete the old route (the decommissioned entry preserves history)
                self.session.delete(route)
            else:
                route.pool_number_id = new_pool.id

        # Update the AssistantContact to point to the new pool number
        contact = (
            self.session.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id == assistant_id,
                AssistantContact.contact_type == self.platform,
                AssistantContact.status == "active",
            )
            .first()
        )
        if contact:
            contact.contact_value = new_pool.number

        self.session.flush()

    # ------------------------------------------------------------------
    # Org membership conflict detection
    # ------------------------------------------------------------------

    def detect_membership_conflicts(
        self,
        user_id: str,
        org_id: int,
    ) -> list[tuple[int, int, SharedPoolNumber]]:
        """Detect pool conflicts created by a user joining an org.

        Returns list of ``(personal_assistant_id, org_assistant_id, pool)``
        tuples where the user now has access to two assistants on the same
        pool number.
        """
        # Personal assistants for this user with active pool contacts
        personal_assistants = (
            self.session.query(Assistant.agent_id, AssistantContact.contact_value)
            .join(
                AssistantContact,
                AssistantContact.assistant_id == Assistant.agent_id,
            )
            .filter(
                Assistant.user_id == user_id,
                Assistant.organization_id.is_(None),
                AssistantContact.contact_type == self.platform,
                AssistantContact.status == "active",
            )
            .all()
        )

        # Org assistants with active pool contacts
        org_assistants = (
            self.session.query(Assistant.agent_id, AssistantContact.contact_value)
            .join(
                AssistantContact,
                AssistantContact.assistant_id == Assistant.agent_id,
            )
            .filter(
                Assistant.organization_id == org_id,
                AssistantContact.contact_type == self.platform,
                AssistantContact.status == "active",
            )
            .all()
        )

        conflicts = []
        org_pool_map = {cv: aid for aid, cv in org_assistants}
        for personal_aid, personal_cv in personal_assistants:
            if personal_cv in org_pool_map:
                pool = self.get_pool_number_by_value(personal_cv)
                if pool:
                    conflicts.append(
                        (personal_aid, org_pool_map[personal_cv], pool),
                    )
        return conflicts

    def resolve_membership_conflicts(
        self,
        conflicts: list[tuple[int, int, SharedPoolNumber]],
    ) -> list[ConflictResolution]:
        """Resolve conflicts from org membership changes.

        Reassigns the personal assistant (less disruptive than moving the
        org assistant which affects all org members).
        """
        resolutions = []
        for personal_aid, org_aid, pool in conflicts:
            assistant = (
                self.session.query(Assistant)
                .filter(Assistant.agent_id == personal_aid)
                .first()
            )
            accessible = self._get_accessible_user_ids(assistant)
            new_pool = self._find_alternative_pool(
                personal_aid,
                pool.id,
                accessible,
            )
            if new_pool is None:
                logger.warning(
                    "Cannot resolve org membership conflict for assistant %d: "
                    "no available pool numbers",
                    personal_aid,
                )
                continue

            old_number = pool.number
            new_number = new_pool.number
            self._migrate_assistant_to_pool(personal_aid, pool, new_pool)

            resolution = ConflictResolution(
                conflict_type="org_membership",
                affected_assistant_ids=[personal_aid],
                old_pool_assignments={personal_aid: old_number},
                new_pool_assignments={personal_aid: new_number},
            )
            resolution.notification_recipients = self._gather_notification_recipients(
                [personal_aid],
                {personal_aid: (old_number, new_number)},
            )
            resolution.conflict_event_id = self._record_conflict_event(resolution)
            resolutions.append(resolution)
        return resolutions

    def cleanup_departed_member_routes(
        self,
        user_id: str,
        org_id: int,
    ) -> int:
        """Clean up after a user leaves an org.

        Removes Tier 1 route rows where the departing user's identity was
        the contact_number for an org assistant. Returns count of deleted rows.
        """
        user = self.session.query(User).filter(User.id == user_id).first()
        if not user:
            return 0

        identity = self._get_user_platform_identity(user)
        if not identity:
            return 0

        org_assistant_ids = [
            r[0]
            for r in self.session.query(Assistant.agent_id)
            .filter(Assistant.organization_id == org_id)
            .all()
        ]
        if not org_assistant_ids:
            return 0

        count = (
            self.session.query(SharedPlatformRoute)
            .filter(
                SharedPlatformRoute.assistant_id.in_(org_assistant_ids),
                SharedPlatformRoute.contact_number == identity,
            )
            .delete(synchronize_session="fetch")
        )
        self.session.flush()
        return count

    # ------------------------------------------------------------------
    # Route management helpers
    # ------------------------------------------------------------------

    def delete_routes_for_assistant(self, assistant_id: int) -> int:
        count = (
            self.session.query(SharedPlatformRoute)
            .filter(SharedPlatformRoute.assistant_id == assistant_id)
            .delete()
        )
        return count

    def get_routes_for_assistant(self, assistant_id: int) -> list[SharedPlatformRoute]:
        return (
            self.session.query(SharedPlatformRoute)
            .filter(SharedPlatformRoute.assistant_id == assistant_id)
            .all()
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_accessible_user_ids(self, assistant: Assistant) -> list[str]:
        user_ids = [assistant.user_id]
        if assistant.organization_id is not None:
            members = (
                self.session.query(OrganizationMember.user_id)
                .filter(
                    OrganizationMember.organization_id == assistant.organization_id,
                )
                .all()
            )
            for (uid,) in members:
                if uid not in user_ids:
                    user_ids.append(uid)
        return user_ids

    def _get_user_platform_identity(self, user: User) -> str | None:
        if self.platform == "whatsapp":
            return user.whatsapp_number
        return None

    def _gather_notification_recipients(
        self,
        assistant_ids: list[int],
        change_map: dict[int, tuple[str, str]],
    ) -> list[dict]:
        """Gather WhatsApp notification recipients for affected assistants.

        Returns list of dicts: ``{to, user_name, agent_name, old_contact, new_contact}``
        """
        recipients = []
        seen_numbers = set()

        for aid in assistant_ids:
            assistant = (
                self.session.query(Assistant).filter(Assistant.agent_id == aid).first()
            )
            if not assistant:
                continue

            old_contact, new_contact = change_map[aid]
            agent_name = assistant.first_name or "your assistant"

            # Collect all users who can access this assistant
            user_ids = self._get_accessible_user_ids(assistant)
            for uid in user_ids:
                user = self.session.query(User).filter(User.id == uid).first()
                if not user:
                    continue
                wa_number = self._get_user_platform_identity(user)
                if not wa_number or wa_number in seen_numbers:
                    continue
                seen_numbers.add(wa_number)
                recipients.append(
                    {
                        "to": wa_number,
                        "user_name": user.name or "there",
                        "agent_name": agent_name,
                        "old_contact": old_contact,
                        "new_contact": new_contact,
                    },
                )
        return recipients

    def _record_conflict_event(self, resolution: ConflictResolution) -> int:
        event = ConflictEvent(
            platform=self.platform,
            conflict_type=resolution.conflict_type,
            trigger_assistant_id=(
                resolution.affected_assistant_ids[0]
                if resolution.affected_assistant_ids
                else None
            ),
            affected_assistant_ids=resolution.affected_assistant_ids,
            old_pool_assignments={
                str(k): v for k, v in resolution.old_pool_assignments.items()
            },
            new_pool_assignments={
                str(k): v for k, v in resolution.new_pool_assignments.items()
            },
            notification_recipients=resolution.notification_recipients,
            status="notifying",
        )
        self.session.add(event)
        self.session.flush()
        return event.id

    def update_notification_status(
        self,
        conflict_event_id: int,
        recipient_number: str,
        message_sid: str,
        status: str,
    ) -> ConflictEvent | None:
        """Update delivery status for a notification recipient."""
        event = (
            self.session.query(ConflictEvent)
            .filter(ConflictEvent.id == conflict_event_id)
            .first()
        )
        if not event:
            return None

        current = event.notification_status or {}
        current[recipient_number] = {
            "sid": message_sid,
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        event.notification_status = current

        # Check if all recipients have a terminal status
        all_terminal = all(
            entry.get("status") in ("delivered", "read", "failed", "undelivered")
            for entry in current.values()
        )
        if all_terminal:
            any_failed = any(
                entry.get("status") in ("failed", "undelivered")
                for entry in current.values()
            )
            event.status = "notification_failed" if any_failed else "resolved"
            event.resolved_at = datetime.now(timezone.utc)

        self.session.flush()
        return event
