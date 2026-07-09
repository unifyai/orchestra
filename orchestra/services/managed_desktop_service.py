"""Managed Computer Use paid add-on billing helpers."""

from __future__ import annotations

import datetime as _dt
import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from fastapi import HTTPException, status

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.models.orchestra_models import Assistant
from orchestra.settings import settings

if TYPE_CHECKING:
    from orchestra.lib.billing import BillingEntity

logger = logging.getLogger(__name__)

MANAGED_DESKTOP_MODES = frozenset({"ubuntu", "windows"})
MANAGED_DESKTOP_GRANDFATHER_DAYS = 30


def managed_desktop_entitled(assistant: Assistant) -> bool:
    """Return whether the assistant may receive a managed pool VM."""
    return (
        assistant.desktop_mode in MANAGED_DESKTOP_MODES
        and assistant.managed_desktop_status == "active"
    )


def get_managed_desktop_monthly_cost(session, desktop_mode: str) -> Decimal:
    """Resolve monthly managed-desktop cost from contact_type_costs."""
    contact_dao = AssistantContactDAO(session)
    return contact_dao.get_contact_monthly_cost(
        "managed_desktop",
        provider=desktop_mode,
    )


def _validate_desktop_mode(desktop_mode: str) -> None:
    if desktop_mode not in MANAGED_DESKTOP_MODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="desktop_mode must be 'ubuntu' or 'windows'.",
        )


def _in_grandfather_period(
    assistant: Assistant,
    *,
    now: _dt.datetime | None = None,
) -> bool:
    if assistant.managed_desktop_enabled_at is None:
        return False
    if assistant.managed_desktop_last_billed_month is not None:
        return False
    now = now or _dt.datetime.now(_dt.timezone.utc)
    enabled_at = assistant.managed_desktop_enabled_at
    if enabled_at.tzinfo is None:
        enabled_at = enabled_at.replace(tzinfo=_dt.timezone.utc)
    return enabled_at + _dt.timedelta(days=MANAGED_DESKTOP_GRANDFATHER_DAYS) > now


def charge_managed_desktop_first_month(
    session,
    *,
    assistant: Assistant,
    desktop_mode: str,
    billing_entity: BillingEntity,
    user_id: str,
    organization_id: int | None,
) -> Decimal:
    """Charge the first month and activate managed-desktop billing fields."""
    _validate_desktop_mode(desktop_mode)
    monthly_cost = get_managed_desktop_monthly_cost(session, desktop_mode)
    now = _dt.datetime.now(_dt.timezone.utc)

    if settings.charges_billing and monthly_cost > 0:
        if not billing_entity.has_sufficient_credits(monthly_cost):
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=(
                    f"Insufficient credits. Enabling Computer Use ({desktop_mode}) "
                    f"requires ${monthly_cost} for the first month."
                ),
            )
        BillingAccountDAO(session).deduct_credits(
            billing_entity.billing_account_id,
            float(monthly_cost),
            category="managed_desktop",
            assistant_id=assistant.agent_id,
            user_id=user_id,
            organization_id=organization_id,
            description=f"Computer Use ({desktop_mode}) — first month",
            detail={
                "event": "managed_desktop_enable",
                "desktop_mode": desktop_mode,
                "assistant_id": assistant.agent_id,
            },
        )

    assistant.desktop_mode = desktop_mode
    assistant.managed_desktop_status = "active"
    assistant.managed_desktop_monthly_cost = monthly_cost
    assistant.managed_desktop_grace_period_started_at = None
    assistant.managed_desktop_enabled_at = now
    return monthly_cost


def disable_managed_desktop(assistant: Assistant) -> None:
    """Disable managed Computer Use billing and clear desktop_mode."""
    assistant.desktop_mode = None
    assistant.managed_desktop_status = "disabled"
    assistant.managed_desktop_grace_period_started_at = None
