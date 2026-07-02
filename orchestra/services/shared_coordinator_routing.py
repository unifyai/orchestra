"""Shared coordinator owner-routing helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Assistant, AssistantContact, User


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
    return {"action": "reject_cold"}
