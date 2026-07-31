"""The one-way coordinator multiplayer flip.

A single-player coordinator is private: shared-pool contact details, the
default name and voice, invisible to everyone but its owner. Flipping to
multiplayer trades that for a hire-like outward identity — an owned name,
voice, and avatar, a dedicated platform alias email, and no shared pools at
all. The flip is atomic within the caller's session/transaction and cannot
be reverted (see ``Assistant._validate_is_multiplayer_one_way``).

Alias addresses live on ``settings.unity_twin_alias_email_domain``, a
catch-all domain delivered into one shared mailbox and routed by recipient
address — the inverse of the shared coordinator address, which routes by
verified sender. That inversion is what makes an open audience safe: the
recipient address alone identifies the twin, so no sender lookup is needed.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.db.models.orchestra_models import Assistant, AssistantContact
from orchestra.services.coordinator_service import COORDINATOR_DEFAULT_FIRST_NAME
from orchestra.settings import settings

TWIN_ALIAS_EMAIL_METADATA = {"twin_alias": True}

_MAX_LOCAL_PART_LENGTH = 64


class MultiplayerFlipError(ValueError):
    """A flip request that cannot be honored (bad state or bad identity)."""


def is_reserved_coordinator_name(name: str | None) -> bool:
    """Whether a name collides with the shared single-player identity.

    Reserved at flip time and for every later rename of a multiplayer
    twin: a roster showing the fixed coordinator default next to real
    multiplayer twins would recreate the ambiguity the flip ceremony
    exists to prevent.
    """
    return (name or "").strip().lower() == COORDINATOR_DEFAULT_FIRST_NAME.lower()


def display_name_conflict(
    session: Session,
    *,
    user_id: str,
    organization_id: int | None,
    first_name: str,
    surname: str | None,
    exclude_agent_id: int | None = None,
) -> Assistant | None:
    """Return the assistant already holding this normalized name, if any.

    Reuses the natural-name key assistant creation enforces — org-wide for
    organization assistants, per-user for personal ones — so create,
    rename, and the multiplayer flip all share one uniqueness semantics.
    """
    conflict = AssistantDAO(session).find_by_natural_key(
        user_id=user_id,
        organization_id=organization_id,
        first_name=first_name,
        surname=surname,
    )
    if conflict is None or conflict.agent_id == exclude_agent_id:
        return None
    return conflict


def is_twin_alias_email_address(address: str | None) -> bool:
    """Whether an address lives on the twin alias catch-all domain."""
    domain = (settings.unity_twin_alias_email_domain or "").strip().lower()
    if not domain or not address:
        return False
    return address.strip().lower().endswith(f"@{domain}")


def find_twin_by_alias_email(session: Session, address: str) -> Assistant | None:
    """Resolve an alias address to its multiplayer twin (recipient routing)."""
    if not is_twin_alias_email_address(address):
        return None
    return (
        session.query(Assistant)
        .join(AssistantContact, AssistantContact.assistant_id == Assistant.agent_id)
        .filter(
            Assistant.is_multiplayer.is_(True),
            AssistantContact.contact_type == "email",
            AssistantContact.contact_value == address.strip().lower(),
            AssistantContact.status == "active",
        )
        .first()
    )


def _slugify_local_part(*parts: str | None) -> str:
    """Fold name parts into a dot-separated ASCII email local part."""
    words: list[str] = []
    for part in parts:
        if not part:
            continue
        folded = (
            unicodedata.normalize("NFKD", part)
            .encode("ascii", "ignore")
            .decode("ascii")
            .lower()
        )
        words.extend(w for w in re.split(r"[^a-z0-9]+", folded) if w)
    return ".".join(words)[:_MAX_LOCAL_PART_LENGTH].strip(".")


def _alias_in_use(session: Session, address: str) -> bool:
    return (
        session.query(AssistantContact.id)
        .filter(
            AssistantContact.contact_type == "email",
            AssistantContact.contact_value == address,
            AssistantContact.status != "deleted",
        )
        .first()
        is not None
    )


def generate_twin_alias_email(
    session: Session,
    *,
    coordinator: Assistant,
    first_name: str,
    surname: str | None,
) -> str:
    """Mint a unique alias address for a twin going multiplayer.

    Prefers the bare name slug, then disambiguates with the stable
    ``agent_id`` — deterministic, collision-free, and meaningless to
    strangers (it leaks no owner information).
    """
    domain = (settings.unity_twin_alias_email_domain or "").strip().lower()
    if not domain:
        raise MultiplayerFlipError(
            "Twin alias email domain is not configured for this deployment",
        )
    slug = _slugify_local_part(first_name, surname)
    if not slug:
        raise MultiplayerFlipError("Twin name does not yield a usable email slug")
    candidate = f"{slug}@{domain}"
    if not _alias_in_use(session, candidate):
        return candidate
    return f"{slug}.{coordinator.agent_id}@{domain}"


def flip_coordinator_to_multiplayer(
    session: Session,
    *,
    coordinator: Assistant,
    first_name: str,
    surname: str | None,
    voice_id: str,
    voice_provider: str,
    profile_photo: str | None = None,
) -> Assistant:
    """Apply the multiplayer flip to a single-player coordinator.

    Mutates identity fields, deactivates every shared-pool contact, and
    provisions the dedicated alias email. Flushes but does not commit — the
    caller owns the transaction boundary.
    """
    if not coordinator.is_coordinator:
        raise MultiplayerFlipError("Only coordinators can flip to multiplayer")
    if coordinator.is_multiplayer:
        raise MultiplayerFlipError("This coordinator is already multiplayer")

    cleaned_first = (first_name or "").strip()
    cleaned_surname = (surname or "").strip() or None
    if not cleaned_first:
        raise MultiplayerFlipError("A first name is required to go multiplayer")
    if is_reserved_coordinator_name(cleaned_first):
        raise MultiplayerFlipError(
            "The multiplayer name must differ from the shared coordinator default",
        )
    colliding = display_name_conflict(
        session,
        user_id=coordinator.user_id,
        organization_id=coordinator.organization_id,
        first_name=cleaned_first,
        surname=cleaned_surname,
        exclude_agent_id=coordinator.agent_id,
    )
    if colliding is not None:
        taken = " ".join(
            part for part in (colliding.first_name, colliding.surname) if part
        ).strip()
        raise MultiplayerFlipError(
            f"Another assistant in this workspace is already named "
            f"{taken!r}; pick a name teammates can tell apart",
        )

    alias_address = generate_twin_alias_email(
        session,
        coordinator=coordinator,
        first_name=cleaned_first,
        surname=cleaned_surname,
    )

    # Retire every shared-pool identity before the dedicated ones land, so
    # no moment exists where pool routing and dedicated routing overlap.
    dao = AssistantContactDAO(session)
    for contact in dao.get_active_contacts_for_assistant(coordinator.agent_id):
        if (contact.metadata_ or {}).get("universal_unity"):
            dao.soft_delete_assistant_contact(
                assistant_id=coordinator.agent_id,
                contact_type=contact.contact_type,
            )

    dao.upsert_assistant_contact(
        assistant_id=coordinator.agent_id,
        contact_type="email",
        contact_value=alias_address,
        provider="google_workspace",
        provisioned_by="platform",
        metadata={
            **TWIN_ALIAS_EMAIL_METADATA,
            # Anchors the pool-address grace window: for a while after the
            # flip, boss mail to the retired shared address gets a
            # redirect notice instead of a silent drop.
            "flipped_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    coordinator.first_name = cleaned_first
    coordinator.surname = cleaned_surname
    coordinator.voice_id = voice_id
    coordinator.voice_provider = voice_provider
    if profile_photo:
        coordinator.profile_photo = profile_photo
    coordinator.is_multiplayer = True

    # Mirror the per-assistant grant a hired org assistant gets at creation,
    # so a flipped twin's RBAC state matches a hire's exactly. Project-level
    # member grants already exist from the coordinator bootstrap.
    if coordinator.organization_id is not None:
        owner_role = RoleDAO(session).get_by_name("Owner", organization_id=None)
        if owner_role is not None:
            ResourceAccessDAO(session).grant_access(
                resource_type="assistant",
                resource_id=coordinator.agent_id,
                role_id=owner_role.id,
                grantee_type="user",
                grantee_id=coordinator.user_id,
            )

    session.flush()
    return coordinator
