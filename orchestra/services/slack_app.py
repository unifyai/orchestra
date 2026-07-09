"""Slack app-level operations against the Slack Web API.

Orchestra normally only *stores* Slack installs (outbound messaging lives in
the Unity gateway), but tearing an install down fully requires calling
``apps.uninstall`` with the workspace bot token plus the app's client
credentials — a triple only Orchestra holds together. This module is the one
place that talks to Slack directly, mirroring the outbound-httpx style of
``universal_unity_discord``.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

SLACK_API_BASE = "https://slack.com/api"

# Slack error codes that mean the app/token is already gone. Treated as a
# successful uninstall so a retry (or a stale token) is idempotent.
_ALREADY_GONE_ERRORS = frozenset(
    {
        "token_revoked",
        "account_inactive",
        "invalid_auth",
        "app_uninstalled",
    },
)


async def uninstall_slack_app(
    *,
    bot_token: str,
    client_id: str,
    client_secret: str,
) -> bool:
    """Remove the app from the workspace the ``bot_token`` belongs to.

    Best-effort: returns ``True`` when Slack confirms the app is uninstalled
    (or was already gone), ``False`` on any other Slack error or transport
    failure. Never raises — the caller always completes its local revoke.
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{SLACK_API_BASE}/apps.uninstall",
                params={"client_id": client_id, "client_secret": client_secret},
                headers={"Authorization": f"Bearer {bot_token}"},
                timeout=10,
            )
    except Exception:
        logger.warning("Slack apps.uninstall request failed", exc_info=True)
        return False

    try:
        body = resp.json()
    except ValueError:
        logger.warning(
            "Slack apps.uninstall returned non-JSON (status=%s)",
            resp.status_code,
        )
        return False

    if body.get("ok"):
        return True

    error = body.get("error") or ""
    if error in _ALREADY_GONE_ERRORS:
        return True

    logger.warning("Slack apps.uninstall failed: %s", error or "unknown_error")
    return False
