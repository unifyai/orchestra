"""Universal Discord contact helpers for Coordinator assistants.

Mirrors ``universal_unity_whatsapp`` but for the shared Coordinator Discord
bot. The bot ID and token are configured via Secret Manager on Orchestra
only; Unity's gateway pulls them down through the existing shared-pool sync
(``GET /admin/discord/pool?include_auth=true``), so the secret never has to
be duplicated into the Unity (gcp-project-runtime) project.
"""

from __future__ import annotations

import logging
import os

import httpx
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    SharedPoolNumber,
)
from orchestra.settings import settings

logger = logging.getLogger(__name__)

UNIVERSAL_UNITY_DISCORD_METADATA = {"universal_unity": True}
DISCORD_PLATFORM = "discord"


def get_universal_unity_discord_bot() -> tuple[str, str | None] | None:
    """Return ``(bot_id, token)`` for the universal Coordinator bot, if set."""
    bot_id = (settings.unity_coordinator_discord_id or "").strip()
    if not bot_id:
        return None
    token = (settings.unity_coordinator_discord_token or "").strip() or None
    return bot_id, token


def get_universal_unity_discord_bot_id() -> str | None:
    bot = get_universal_unity_discord_bot()
    return bot[0] if bot else None


def is_universal_unity_discord_bot(bot_id: str | None) -> bool:
    universal_bot_id = get_universal_unity_discord_bot_id()
    if not universal_bot_id or not bot_id:
        return False
    return str(bot_id).strip() == universal_bot_id


def ensure_universal_unity_discord_pool(
    session: Session,
) -> tuple[SharedPoolNumber | None, bool]:
    """Ensure the shared Coordinator Discord bot exists in the pool.

    Returns ``(pool, changed)`` where ``changed`` is ``True`` when the row
    was created or its token/status was updated -- the signal used to decide
    whether Unity needs a re-sync.
    """
    bot = get_universal_unity_discord_bot()
    if bot is None:
        return None, False
    bot_id, token = bot

    changed = False
    pool = (
        session.query(SharedPoolNumber)
        .filter(
            SharedPoolNumber.platform == DISCORD_PLATFORM,
            SharedPoolNumber.number == bot_id,
        )
        .first()
    )
    if pool is None:
        pool = SharedPoolNumber(
            platform=DISCORD_PLATFORM,
            number=bot_id,
            auth_token=token,
            status="active",
        )
        session.add(pool)
        changed = True
    else:
        if pool.status != "active":
            pool.status = "active"
            changed = True
        if token is not None and pool.auth_token != token:
            pool.auth_token = token
            changed = True
    session.flush()
    return pool, changed


def ensure_coordinator_universal_discord_contact(
    session: Session,
    *,
    coordinator: Assistant,
) -> AssistantContact | None:
    # Multiplayer twins run on dedicated identities; shared pools must
    # never re-attach to them (heal/repair paths funnel through here).
    if not coordinator.is_coordinator or coordinator.is_multiplayer:
        return None

    pool, _changed = ensure_universal_unity_discord_pool(session)
    if pool is None:
        return None

    contact = AssistantContactDAO(session).upsert_assistant_contact(
        assistant_id=coordinator.agent_id,
        contact_type=DISCORD_PLATFORM,
        contact_value=pool.number,
        provider=DISCORD_PLATFORM,
        provisioned_by="platform",
        metadata=UNIVERSAL_UNITY_DISCORD_METADATA,
    )
    session.flush()
    return contact


async def notify_comms_discord_sync() -> None:
    """Best-effort: ask the Unity gateway to re-sync its Discord bot pool."""
    comms_url = os.environ.get("UNITY_COMMS_URL")
    admin_key = os.environ.get("ORCHESTRA_ADMIN_KEY")
    if not comms_url or not admin_key:
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{comms_url}/discord/sync",
                headers={"Authorization": f"Bearer {admin_key}"},
                timeout=10,
            )
    except Exception:
        logger.warning("Failed to notify Unity of Discord pool sync")
