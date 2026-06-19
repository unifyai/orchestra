"""Aggregate helpers across the per-channel universal Coordinator contacts.

The individual ``universal_droid_{email,phone,whatsapp,discord}`` modules each
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

from orchestra.services.universal_droid_discord import (
    get_universal_droid_discord_bot_id,
    is_universal_droid_discord_bot,
)
from orchestra.services.universal_droid_email import (
    get_universal_droid_email_address,
    is_universal_droid_email_address,
)
from orchestra.services.universal_droid_phone import (
    get_universal_droid_phone_numbers,
    is_universal_droid_phone_number,
)
from orchestra.services.universal_droid_whatsapp import (
    get_universal_droid_whatsapp_number,
    is_universal_droid_whatsapp_number,
)

# Stable channel ordering so the response (and any UI built on it) is
# deterministic.
UNIVERSAL_CONTACT_TYPES: tuple[str, ...] = ("email", "phone", "whatsapp", "discord")


def get_configured_universal_contact_types() -> set[str]:
    """Return the universal contact channels configured for this deployment."""
    configured: set[str] = set()
    if get_universal_droid_email_address():
        configured.add("email")
    if get_universal_droid_phone_numbers():
        configured.add("phone")
    if get_universal_droid_whatsapp_number():
        configured.add("whatsapp")
    if get_universal_droid_discord_bot_id():
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


# Per-channel matcher used to tell whether a stored ``contact_value`` still
# equals the value currently configured for this deployment. Each matcher is
# normalisation-aware for its channel (email casing, ``whatsapp:`` prefix,
# phone E.164 formatting, Discord bot snowflake).
_UNIVERSAL_CONTACT_MATCHERS = {
    "email": is_universal_droid_email_address,
    "phone": is_universal_droid_phone_number,
    "whatsapp": is_universal_droid_whatsapp_number,
    "discord": is_universal_droid_discord_bot,
}


def _is_universal_managed_contact(contact: object) -> bool:
    """True when ``contact`` is a platform-managed universal-pool contact."""
    metadata = getattr(contact, "metadata_", None) or {}
    return bool(metadata.get("universal_droid"))


def drifted_universal_coordinator_contact_types(
    present_contacts: Iterable[object],
) -> list[str]:
    """Universal channels whose provisioned value has drifted from settings.

    A channel counts as *drifted* when the Coordinator already has a
    universal-managed contact of that type but its stored ``contact_value`` no
    longer matches the value currently configured for this deployment (Secret
    Manager / settings) -- e.g. the shared Coordinator email was repointed to a
    new address, or a phone/WhatsApp/Discord pool identifier was rotated.

    Only channels still configured for this deployment are considered, and only
    platform-managed (``universal_droid``) contacts are inspected, so a
    manually-set contact is never reconciled out from under the user. Any value
    that can't be evaluated (malformed/unparsable) is treated as drift so the
    heal path re-provisions it from settings.

    Args:
        present_contacts: the Coordinator's currently active contacts
            (``AssistantContact`` rows or any objects exposing
            ``contact_type``, ``contact_value`` and ``metadata_``).

    Returns:
        Drifted channels in :data:`UNIVERSAL_CONTACT_TYPES` order. Empty when
        every configured universal contact already matches settings.
    """
    configured = get_configured_universal_contact_types()

    # First universal-managed contact per configured type wins (there is at
    # most one active contact per type in well-behaved data).
    universal_by_type: dict[str, object] = {}
    for contact in present_contacts:
        contact_type = getattr(contact, "contact_type", None)
        if (
            contact_type in configured
            and contact_type not in universal_by_type
            and _is_universal_managed_contact(contact)
        ):
            universal_by_type[contact_type] = contact

    drifted: set[str] = set()
    for contact_type, contact in universal_by_type.items():
        matcher = _UNIVERSAL_CONTACT_MATCHERS.get(contact_type)
        if matcher is None:
            continue
        try:
            matches = matcher(getattr(contact, "contact_value", None))
        except Exception:
            matches = False
        if not matches:
            drifted.add(contact_type)

    return [
        contact_type
        for contact_type in UNIVERSAL_CONTACT_TYPES
        if contact_type in drifted
    ]
