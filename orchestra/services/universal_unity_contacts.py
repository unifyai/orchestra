"""Aggregate helpers across the per-channel universal Coordinator contacts.

The individual ``universal_unity_{email,phone,whatsapp,discord}`` modules each
know how to provision their channel. This module answers the cross-channel
question the console needs for self-healing: *which* universal contacts are
configured for the platform but not yet present on a given Coordinator.

Keying the "expected" set off what's actually configured (Secret Manager /
settings) is what lets the console heal on *any* missing contact without
false-firing on channels that simply aren't set up in a given environment
(e.g. a deployment with no WhatsApp pool).
"""

from __future__ import annotations

from collections.abc import Iterable

from orchestra.services.universal_unity_discord import (
    get_universal_unity_discord_bot_id,
)
from orchestra.services.universal_unity_email import (
    get_universal_unity_email_address,
)
from orchestra.services.universal_unity_phone import (
    get_universal_unity_phone_numbers,
)
from orchestra.services.universal_unity_whatsapp import (
    get_universal_unity_whatsapp_number,
)

# Stable channel ordering so the response (and any UI built on it) is
# deterministic.
UNIVERSAL_CONTACT_TYPES: tuple[str, ...] = ("email", "phone", "whatsapp", "discord")


def get_configured_universal_contact_types() -> set[str]:
    """Return the universal contact channels configured for this deployment."""
    configured: set[str] = set()
    if get_universal_unity_email_address():
        configured.add("email")
    if get_universal_unity_phone_numbers():
        configured.add("phone")
    if get_universal_unity_whatsapp_number():
        configured.add("whatsapp")
    if get_universal_unity_discord_bot_id():
        configured.add("discord")
    return configured


def missing_universal_coordinator_contact_types(
    present_contact_types: Iterable[str],
) -> list[str]:
    """Configured universal channels that this Coordinator is still missing.

    Args:
        present_contact_types: ``contact_type`` values already provisioned on
            the Coordinator (any provider — a pre-existing contact of a type
            counts as present so we never clobber it).

    Returns:
        Channels that are configured platform-wide but absent on the
        Coordinator, in :data:`UNIVERSAL_CONTACT_TYPES` order. Empty when the
        Coordinator is fully provisioned for everything configured.
    """
    present = set(present_contact_types)
    configured = get_configured_universal_contact_types()
    return [
        contact_type
        for contact_type in UNIVERSAL_CONTACT_TYPES
        if contact_type in configured and contact_type not in present
    ]
