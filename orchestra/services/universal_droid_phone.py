"""Universal phone contact helpers for Coordinator assistants."""

from __future__ import annotations

import re

import phonenumbers
from phonenumbers import PhoneNumberFormat
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    SharedPoolNumber,
    User,
)
from orchestra.settings import settings
from orchestra.web.api.utils.phone_number_validator import PhoneNumberValidator

UNIVERSAL_DROID_PHONE_METADATA = {"universal_droid": True}
_COUNTRY_CODE_RE = re.compile(r"^[A-Z]{2}$")


def _normalize_country(country: str | None) -> str | None:
    if not country:
        return None
    normalized = country.strip().upper()
    if not _COUNTRY_CODE_RE.match(normalized):
        return None
    return normalized


def _normalize_phone_number(number: str) -> str:
    valid, formatted, error = PhoneNumberValidator.validate_and_format(number)
    if not valid or not formatted:
        raise ValueError(f"Invalid universal Coordinator phone number: {error}")
    return formatted


def get_universal_droid_phone_numbers() -> dict[str, str]:
    # Discrete per-country Coordinator phone numbers, mounted per environment
    # from Secret Manager. The UK number is keyed under its ISO country code
    # ("GB") so it lines up with the country resolved from the visitor's IP by
    # the console.
    candidates = {
        "GB": settings.droid_coordinator_phone_uk,
        "US": settings.droid_coordinator_phone_us,
    }
    numbers: dict[str, str] = {}
    for country_code, number in candidates.items():
        if number and number.strip():
            numbers[country_code] = _normalize_phone_number(number)
    return numbers


def get_universal_droid_phone_number(country: str | None) -> str | None:
    country_code = _normalize_country(country)
    if country_code is None:
        return None
    return get_universal_droid_phone_numbers().get(country_code)


def is_universal_droid_phone_number(number: str | None) -> bool:
    if not number:
        return False
    normalized = _normalize_phone_number(number)
    return normalized in set(get_universal_droid_phone_numbers().values())


def infer_phone_country(number: str | None) -> str | None:
    if not number:
        return None
    parsed = phonenumbers.parse(number, None)
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.region_code_for_number(parsed)


def select_universal_droid_phone_country(
    *,
    preferred_country: str | None,
    user_phone_number: str | None,
) -> str | None:
    numbers = get_universal_droid_phone_numbers()
    if not numbers:
        return None

    candidates = [
        _normalize_country(preferred_country),
        infer_phone_country(user_phone_number),
        _normalize_country(settings.droid_coordinator_default_phone_country),
    ]
    for country in candidates:
        if country in numbers:
            return country

    return sorted(numbers)[0]


def ensure_universal_droid_phone_pool(
    session: Session,
    *,
    country: str,
) -> SharedPoolNumber | None:
    number = get_universal_droid_phone_number(country)
    if number is None:
        return None

    pool = (
        session.query(SharedPoolNumber)
        .filter(
            SharedPoolNumber.platform == "phone",
            SharedPoolNumber.number == number,
        )
        .first()
    )
    if pool is None:
        pool = SharedPoolNumber(
            platform="phone",
            number=number,
            status="active",
        )
        session.add(pool)
    elif pool.status != "active":
        pool.status = "active"
    session.flush()
    return pool


def _existing_universal_phone_contact(
    session: Session,
    *,
    coordinator: Assistant,
) -> AssistantContact | None:
    return (
        session.query(AssistantContact)
        .filter(
            AssistantContact.assistant_id == coordinator.agent_id,
            AssistantContact.contact_type == "phone",
            AssistantContact.status == "active",
            AssistantContact.metadata_["universal_droid"].astext == "true",
        )
        .first()
    )


def ensure_coordinator_universal_phone_contact(
    session: Session,
    *,
    coordinator: Assistant,
    preferred_country: str | None = None,
    assignment_source: str = "auto",
) -> AssistantContact | None:
    if not coordinator.is_coordinator:
        return None

    existing = _existing_universal_phone_contact(session, coordinator=coordinator)
    if existing is not None and preferred_country is None:
        existing_country = _normalize_country(existing.country_code)
        configured_number = (
            get_universal_droid_phone_number(existing_country)
            if existing_country
            else None
        )
        # Keep the Coordinator on its assigned country as long as that country
        # is still offered. If the number for that country was rotated in
        # settings, reconcile the stored value in place (preserving country)
        # rather than reselecting a — possibly different — country.
        if configured_number:
            pool = ensure_universal_droid_phone_pool(session, country=existing_country)
            if pool is not None and existing.contact_value != configured_number:
                reconciled = AssistantContactDAO(session).upsert_assistant_contact(
                    assistant_id=coordinator.agent_id,
                    contact_type="phone",
                    contact_value=configured_number,
                    provider="twilio",
                    provisioned_by="platform",
                    country_code=existing_country,
                    metadata={
                        **UNIVERSAL_DROID_PHONE_METADATA,
                        "country": existing_country,
                        "assignment_source": "reconcile",
                        "shared_pool_number_id": pool.id,
                    },
                )
                session.flush()
                return reconciled
            return existing

    user_phone_number = (
        session.query(User.phone_number).filter(User.id == coordinator.user_id).scalar()
    )
    country = select_universal_droid_phone_country(
        preferred_country=preferred_country,
        user_phone_number=user_phone_number,
    )
    if country is None:
        return None

    pool = ensure_universal_droid_phone_pool(session, country=country)
    if pool is None:
        return None

    metadata = {
        **UNIVERSAL_DROID_PHONE_METADATA,
        "country": country,
        "assignment_source": assignment_source,
        "shared_pool_number_id": pool.id,
    }
    contact = AssistantContactDAO(session).upsert_assistant_contact(
        assistant_id=coordinator.agent_id,
        contact_type="phone",
        contact_value=pool.number,
        provider="twilio",
        provisioned_by="platform",
        country_code=country,
        metadata=metadata,
    )
    session.flush()
    return contact
