"""Universal email contact helpers for Coordinator assistants."""

from __future__ import annotations

from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.models.orchestra_models import Assistant, AssistantContact
from orchestra.settings import settings

UNIVERSAL_UNITY_EMAIL_METADATA = {"universal_unity": True}


def get_universal_unity_email_address() -> str | None:
    address = settings.unity_coordinator_email_address
    if not address:
        return None
    return address.strip().lower() or None


def is_universal_unity_email_address(address: str | None) -> bool:
    universal_address = get_universal_unity_email_address()
    if not universal_address or not address:
        return False
    return address.strip().lower() == universal_address


def ensure_coordinator_universal_email_contact(
    session: Session,
    *,
    coordinator: Assistant,
) -> AssistantContact | None:
    if not coordinator.is_coordinator:
        return None

    address = get_universal_unity_email_address()
    if address is None:
        return None

    contact = AssistantContactDAO(session).upsert_assistant_contact(
        assistant_id=coordinator.agent_id,
        contact_type="email",
        contact_value=address,
        provider="google_workspace",
        provisioned_by="platform",
        metadata=UNIVERSAL_UNITY_EMAIL_METADATA,
    )
    session.flush()
    return contact
