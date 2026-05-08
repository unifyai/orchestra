"""Discord webhook notifications for billing flows.

Formats reconciliation and health results into Discord embeds and
sends them via webhook.  Each flow calls its own function — they
produce independent, focused messages.

Configuration
~~~~~~~~~~~~~
Set ``DISCORD_BILLING_WEBHOOK_URL`` in the environment.  If unset,
notifications are silently skipped (the flows still run and log).

Optionally set ``DISCORD_BILLING_MENTION_IDS`` to a comma-separated
list of Discord user IDs to @mention when unfixed criticals exist
(e.g. ``"123456789,987654321"``).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, List, Optional

import requests

if TYPE_CHECKING:
    from orchestra.routines.billing_reconciliation import ReconciliationResult

logger = logging.getLogger(__name__)

WEBHOOK_URL_ENV = "DISCORD_BILLING_WEBHOOK_URL"
MENTION_IDS_ENV = "DISCORD_BILLING_MENTION_IDS"

COLOR_GREEN = 0x2ECC71
COLOR_YELLOW = 0xF1C40F
COLOR_RED = 0xE74C3C
COLOR_BLUE = 0x3498DB


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def notify_reconciliation(
    result: ReconciliationResult,
    *,
    environment: str = "",
) -> bool:
    """Send a Discord message summarising a reconciliation run.

    Only sends a notification when there are discrepancies or errors.
    Clean runs are silently skipped to reduce noise.

    Returns ``True`` if the message was sent (or skipped because no
    webhook is configured / nothing to report), ``False`` on delivery
    failure.
    """
    webhook_url = os.environ.get(WEBHOOK_URL_ENV)
    if not webhook_url:
        logger.debug("No Discord webhook configured, skipping notification")
        return True

    if not result.discrepancies and not result.errors:
        logger.info("Reconciliation clean — skipping Discord notification")
        return True

    env_tag = environment.upper() or _detect_environment()
    embed = _format_reconciliation_embed(result, env_tag)

    unfixed_criticals = result.critical_count - sum(
        1 for d in result.discrepancies if d.severity == "critical" and d.auto_fixed
    )
    content = ""
    if unfixed_criticals > 0:
        content = _build_mention_string(
            f"⚠️ **{unfixed_criticals} unfixed critical discrepancies** "
            f"in {env_tag} — manual review required",
        )

    return _send_webhook(webhook_url, content=content, embeds=[embed])


# ``notify_health`` and the matching ``_format_health_embed`` formatter
# were retired together with ``orchestra.routines.billing_health`` in
# the v2 billing refactor — health-snapshot KPIs now live in Grafana
# (``billing-dashboard``). Reconciliation discrepancies still flow
# through ``notify_reconciliation`` below.


# ---------------------------------------------------------------------------
# Embed formatters
# ---------------------------------------------------------------------------


def _format_reconciliation_embed(result: ReconciliationResult, env_tag: str) -> dict:
    """Build a Discord embed for a reconciliation result."""
    unfixed = [d for d in result.discrepancies if not d.auto_fixed]
    unfixed_critical = [d for d in unfixed if d.severity == "critical"]

    if result.critical_count == 0 and result.warning_count == 0:
        color = COLOR_GREEN
        title = f"✅ Reconciliation — {env_tag}"
    elif unfixed_critical:
        color = COLOR_RED
        title = f"🔴 Reconciliation — {env_tag}"
    else:
        color = COLOR_YELLOW
        title = f"⚠️ Reconciliation — {env_tag}"

    fields = [
        {
            "name": "\u200b",
            "value": (
                "*Checks for discrepancies between the Orchestra DB "
                "and Stripe for accounts with a Stripe customer.*"
            ),
            "inline": False,
        },
        {
            "name": "Checked",
            "value": (
                f"**{result.accounts_checked}** accounts · "
                f"**{result.recharges_checked}** recharges · "
                f"**{result.invoices_checked}** invoices · "
                f"**{result.events_checked}** events"
            ),
            "inline": False,
        },
        {
            "name": "Discrepancies",
            "value": (
                f"**{len(result.discrepancies)}** total · "
                f"**{result.critical_count}** critical · "
                f"**{result.warning_count}** warnings · "
                f"**{result.auto_fixed_count}** auto-fixed"
            ),
            "inline": False,
        },
    ]

    if unfixed:
        lines = []
        for d in unfixed[:10]:
            icon = "🔴" if d.severity == "critical" else "🟡"
            owner = _format_owner(d)
            stripe_link = f" [→ Stripe]({d.stripe_url})" if d.stripe_url else ""
            lines.append(
                f"{icon} `{d.category}`{owner}: {d.detail[:100]}{stripe_link}",
            )
        if len(unfixed) > 10:
            lines.append(f"… and {len(unfixed) - 10} more")
        fields.append(
            {
                "name": f"Unfixed issues ({len(unfixed)})",
                "value": "\n".join(lines),
                "inline": False,
            },
        )

    auto_fixed = [d for d in result.discrepancies if d.auto_fixed]
    if auto_fixed:
        lines = []
        for d in auto_fixed[:5]:
            owner = _format_owner(d)
            lines.append(f"🔧 `{d.category}`{owner}: {d.detail[:100]}")
        if len(auto_fixed) > 5:
            lines.append(f"… and {len(auto_fixed) - 5} more")
        fields.append(
            {
                "name": f"Auto-fixed ({len(auto_fixed)})",
                "value": "\n".join(lines),
                "inline": False,
            },
        )

    if result.errors:
        error_lines = [f"• {e[:120]}" for e in result.errors[:5]]
        if len(result.errors) > 5:
            error_lines.append(f"… and {len(result.errors) - 5} more")
        fields.append(
            {
                "name": f"Errors ({len(result.errors)})",
                "value": "\n".join(error_lines),
                "inline": False,
            },
        )

    return {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {
            "text": f"Stripe mode: {result.stripe_mode} · {result.started_at}",
        },
    }


# ``_format_health_embed`` was retired together with ``notify_health``
# (and the upstream ``orchestra.routines.billing_health`` module). The
# health-snapshot subsystem is replaced by Grafana dashboards;
# reconciliation discrepancies still flow through
# ``notify_reconciliation`` above.


# ---------------------------------------------------------------------------
# Rate-limited real-time billing failure notifications
# ---------------------------------------------------------------------------

COOLDOWN_SECONDS = 300  # 5 min per (failure_type, context_key)
_cooldown_cache: Dict[str, float] = {}


def notify_billing_event_failure(
    failure_type: str,
    *,
    error: str,
    context_id: str = "",
    billing_account_id: Optional[int] = None,
    environment: str = "",
) -> bool:
    """Send a rate-limited Discord alert for a real-time billing failure.

    Deduplicates by ``(failure_type, context_id)`` — the same combination
    will only fire once per :data:`COOLDOWN_SECONDS` window.

    Args:
        failure_type: Short label, e.g. ``"webhook_processing"``,
            ``"auto_recharge"``, ``"contact_levy"``, ``"contact_deprovisioning"``.
        error: The exception message.
        context_id: Deduplication key (event ID, billing account ID, etc.).
        billing_account_id: Optional BA ID for the embed.
        environment: Override for the environment tag.
    """
    import time

    webhook_url = os.environ.get(WEBHOOK_URL_ENV)
    if not webhook_url:
        return True

    cache_key = f"{failure_type}:{context_id}"
    now = time.monotonic()
    last_sent = _cooldown_cache.get(cache_key, 0.0)
    if now - last_sent < COOLDOWN_SECONDS:
        logger.debug(
            "Suppressed duplicate billing failure notification: %s",
            cache_key,
        )
        return True

    env_tag = environment.upper() or _detect_environment()

    ba_str = f" (BA {billing_account_id})" if billing_account_id else ""
    embed = {
        "title": f"⚡ {failure_type}{ba_str} — {env_tag}",
        "color": COLOR_RED,
        "fields": [
            {
                "name": "Failure type",
                "value": f"`{failure_type}`",
                "inline": True,
            },
            {
                "name": "Context",
                "value": context_id or "—",
                "inline": True,
            },
            {
                "name": "Error",
                "value": f"```\n{error[:800]}\n```",
                "inline": False,
            },
        ],
        "footer": {
            "text": datetime.now(timezone.utc).isoformat(),
        },
    }

    if billing_account_id:
        embed["fields"].insert(
            0,
            {
                "name": "Billing account",
                "value": str(billing_account_id),
                "inline": True,
            },
        )

    content = _build_mention_string(
        f"⚡ **{failure_type}** failure in {env_tag}",
    )

    sent = _send_webhook(webhook_url, content=content, embeds=[embed])
    if sent:
        _cooldown_cache[cache_key] = now

    return sent


# ---------------------------------------------------------------------------
# Routine execution failure notifications
# ---------------------------------------------------------------------------


def notify_failure(
    routine: str,
    error: str,
    *,
    environment: str = "",
) -> bool:
    """Send a Discord alert when a billing routine fails to run.

    Args:
        routine: Human-readable name (e.g. "Reconciliation", "Health Check").
        error: The exception message or traceback summary.
        environment: Override for the environment tag.

    Returns ``True`` if sent (or skipped), ``False`` on delivery failure.
    """
    webhook_url = os.environ.get(WEBHOOK_URL_ENV)
    if not webhook_url:
        return True

    env_tag = environment.upper() or _detect_environment()

    embed = {
        "title": f"💥 {routine} FAILED — {env_tag}",
        "color": COLOR_RED,
        "fields": [
            {
                "name": "Error",
                "value": f"```\n{error[:1000]}\n```",
                "inline": False,
            },
        ],
        "footer": {
            "text": datetime.now(timezone.utc).isoformat(),
        },
    }

    content = _build_mention_string(
        f"🔴 **{routine}** failed to run in {env_tag}",
    )

    return _send_webhook(webhook_url, content=content, embeds=[embed])


def _format_owner(d) -> str:
    """Format a compact owner identifier for a discrepancy."""
    if d.owner_type == "org":
        return (
            f" (**{d.owner_name}** org, {d.owner_email})"
            if d.owner_name
            else f" ({d.owner_email})"
        )
    if d.owner_type == "user":
        label = d.owner_name or d.owner_email
        return f" ({label})" if label else f" (BA {d.billing_account_id})"
    if d.billing_account_id:
        return f" (BA {d.billing_account_id})"
    return ""


def _detect_environment() -> str:
    """Best-effort detection of the deployment environment."""
    stripe_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if stripe_key.startswith("sk_live"):
        return "PRODUCTION"
    if stripe_key.startswith("sk_test"):
        return "STAGING"
    return "UNKNOWN"


def _build_mention_string(prefix: str) -> str:
    """Build a message string with optional @mentions."""
    mention_ids = os.environ.get(MENTION_IDS_ENV, "")
    if not mention_ids:
        return prefix

    mentions = " ".join(
        f"<@{uid.strip()}>" for uid in mention_ids.split(",") if uid.strip()
    )
    return f"{prefix}\n{mentions}"


def _send_webhook(
    url: str,
    *,
    content: str = "",
    embeds: Optional[List[dict]] = None,
) -> bool:
    """POST a message to a Discord webhook URL."""
    payload: dict = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds

    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            logger.info("Discord notification sent successfully")
            return True

        logger.warning(
            "Discord webhook returned %s: %s",
            resp.status_code,
            resp.text[:200],
        )
        return False
    except Exception as e:
        logger.warning("Failed to send Discord notification: %s", e)
        return False
