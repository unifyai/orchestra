"""Universal WhatsApp contact helpers for Coordinator assistants."""

from __future__ import annotations

from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    SharedPoolNumber,
)
from orchestra.settings import settings

UNIVERSAL_UNITY_WHATSAPP_METADATA = {"universal_unity": True}


def get_universal_unity_whatsapp_number() -> str | None:
    number = settings.unity_coordinator_whatsapp_number
    if not number:
        return None
    return number.replace("whatsapp:", "").strip() or None


def is_universal_unity_whatsapp_number(number: str | None) -> bool:
    universal_number = get_universal_unity_whatsapp_number()
    if not universal_number or not number:
        return False
    return number.replace("whatsapp:", "").strip() == universal_number


def ensure_universal_unity_whatsapp_pool(
    session: Session,
) -> SharedPoolNumber | None:
    number = get_universal_unity_whatsapp_number()
    if number is None:
        return None

    pool = (
        session.query(SharedPoolNumber)
        .filter(
            SharedPoolNumber.platform == "whatsapp",
            SharedPoolNumber.number == number,
        )
        .first()
    )
    if pool is None:
        pool = SharedPoolNumber(
            platform="whatsapp",
            number=number,
            status="active",
        )
        session.add(pool)
    elif pool.status != "active":
        pool.status = "active"
    session.flush()
    return pool


def ensure_coordinator_universal_whatsapp_contact(
    session: Session,
    *,
    coordinator: Assistant,
) -> AssistantContact | None:
    if not coordinator.is_coordinator:
        return None

    pool = ensure_universal_unity_whatsapp_pool(session)
    if pool is None:
        return None

    contact = AssistantContactDAO(session).upsert_assistant_contact(
        assistant_id=coordinator.agent_id,
        contact_type="whatsapp",
        contact_value=pool.number,
        provider="twilio",
        provisioned_by="platform",
        metadata=UNIVERSAL_UNITY_WHATSAPP_METADATA,
    )
    session.flush()
    return contact
