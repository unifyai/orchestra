"""Shared coordinator owner-routing helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Assistant, AssistantContact, User

# How long after a multiplayer flip the retired shared-pool address still
# answers the boss with a redirect notice instead of a silent drop.
TWIN_POOL_GRACE = timedelta(days=60)


def find_user_by_shared_identity(
    session: Session,
    *,
    platform: str,
    sender: str,
) -> User | None:
    if platform == "whatsapp":
        return session.query(User).filter(User.whatsapp_number == sender).first()
    if platform == "email":
        return session.query(User).filter(User.email == sender).first()
    if platform == "phone":
        return session.query(User).filter(User.phone_number == sender).first()
    if platform == "discord":
        return session.query(User).filter(User.discord_id == sender).first()
    return None


def find_owned_shared_coordinators(
    session: Session,
    *,
    user_id: str,
    contact_type: str,
    contact_value: str,
) -> list[int]:
    rows = (
        session.query(Assistant.agent_id)
        .join(
            AssistantContact,
            AssistantContact.assistant_id == Assistant.agent_id,
        )
        .filter(
            Assistant.user_id == user_id,
            Assistant.is_coordinator.is_(True),
            # Multiplayer twins left the shared pools; a stale pool contact
            # row must not route pool traffic to them.
            Assistant.is_multiplayer.is_(False),
            AssistantContact.contact_type == contact_type,
            AssistantContact.contact_value == contact_value,
            AssistantContact.status == "active",
        )
        .order_by(Assistant.agent_id.asc())
        .all()
    )
    return [row[0] for row in rows]


def _pick_most_recently_active_coordinator(
    session: Session,
    candidate_ids: list[int],
) -> int | None:
    """Return the candidate with the latest ``last_correspondence_at``.

    When activity timestamps tie, the highest ``agent_id`` wins so routing
    stays deterministic (typically the most recently provisioned Coordinator).
    """
    rows = (
        session.query(Assistant.agent_id, Assistant.last_correspondence_at)
        .filter(Assistant.agent_id.in_(candidate_ids))
        .all()
    )
    if len(rows) != len(candidate_ids):
        return None

    def activity_rank(row: tuple[int, datetime | None]) -> tuple[datetime, int]:
        agent_id, last_at = row
        if last_at is None:
            active_at = datetime.min.replace(tzinfo=timezone.utc)
        elif last_at.tzinfo is None:
            active_at = last_at.replace(tzinfo=timezone.utc)
        else:
            active_at = last_at
        return (active_at, agent_id)

    return max(rows, key=activity_rank)[0]


def _multiplayer_moved_route(session: Session, *, user_id: str) -> dict | None:
    """Redirect notice for a verified owner whose twin left the pools.

    Only within the post-flip grace window: the boss's muscle memory (and
    address book) points at the retired shared address, so a silent drop
    is replaced with the twin's dedicated coordinates. Cold senders never
    reach this path — the caller resolves the sender to a platform user
    first.
    """
    twins = (
        session.query(Assistant)
        .filter(
            Assistant.user_id == user_id,
            Assistant.is_coordinator.is_(True),
            Assistant.is_multiplayer.is_(True),
        )
        .order_by(Assistant.agent_id.asc())
        .all()
    )
    if not twins:
        return None
    if len(twins) > 1:
        winner_id = _pick_most_recently_active_coordinator(
            session,
            [t.agent_id for t in twins],
        )
        twin = next((t for t in twins if t.agent_id == winner_id), twins[0])
    else:
        twin = twins[0]

    alias_row = (
        session.query(AssistantContact)
        .filter(
            AssistantContact.assistant_id == twin.agent_id,
            AssistantContact.contact_type == "email",
            AssistantContact.status == "active",
        )
        .first()
    )
    if alias_row is None:
        return None
    flipped_at_raw = (alias_row.metadata_ or {}).get("flipped_at")
    if flipped_at_raw:
        try:
            flipped_at = datetime.fromisoformat(flipped_at_raw)
        except ValueError:
            flipped_at = None
        if flipped_at is not None and datetime.now(timezone.utc) - flipped_at > (
            TWIN_POOL_GRACE
        ):
            return None
    twin_name = " ".join(
        part for part in (twin.first_name, twin.surname) if part
    ).strip()
    return {
        "action": "coordinator_multiplayer_moved",
        "alias_email": alias_row.contact_value,
        "twin_name": twin_name,
    }


def resolve_shared_coordinator_owner(
    session: Session,
    *,
    platform: str,
    contact_type: str,
    contact_value: str,
    sender: str,
) -> dict:
    user = find_user_by_shared_identity(session, platform=platform, sender=sender)
    if user is None:
        return {"action": "reject_cold"}

    candidates = find_owned_shared_coordinators(
        session,
        user_id=user.id,
        contact_type=contact_type,
        contact_value=contact_value,
    )
    if len(candidates) == 1:
        return {"assistant_id": candidates[0], "role": "owner"}
    if len(candidates) > 1:
        winner = _pick_most_recently_active_coordinator(session, candidates)
        if winner is not None:
            return {"assistant_id": winner, "role": "owner"}
        return {"action": "reject_ambiguous"}
    moved = _multiplayer_moved_route(session, user_id=user.id)
    if moved is not None:
        return moved
    return {"action": "reject_cold"}
