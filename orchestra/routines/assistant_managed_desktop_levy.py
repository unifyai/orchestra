"""Monthly levy for managed Computer Use add-ons."""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.models.orchestra_models import Assistant, BillingAccount, BillingMode
from orchestra.routines.assistant_contact_levy import _get_billing_account_for_assistant
from orchestra.routines.assistant_contact_notifications import (
    LEVY_INSUFFICIENT_CREDITS_SUBJECT,
    build_insufficient_credits_email,
    get_account_label_for_ba,
    get_notification_emails_for_ba,
    send_notification_emails_sync,
)
from orchestra.services.managed_desktop_service import (
    MANAGED_DESKTOP_MODES,
    _in_grandfather_period,
    get_managed_desktop_monthly_cost,
)
from orchestra.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class ManagedDesktopLevyAccountResult:
    billing_account_id: int
    desktops_billed: int = 0
    total_amount: Decimal = field(default_factory=lambda: Decimal("0"))
    ubuntu_count: int = 0
    ubuntu_cost: Decimal = field(default_factory=lambda: Decimal("0"))
    windows_count: int = 0
    windows_cost: Decimal = field(default_factory=lambda: Decimal("0"))
    grace_period_desktops: int = 0
    marked_past_due: bool = False
    insufficient_credits_notified: bool = False
    credits_before: Decimal = field(default_factory=lambda: Decimal("0"))
    credits_after: Decimal = field(default_factory=lambda: Decimal("0"))


@dataclass
class ManagedDesktopLevyResult:
    billing_month: str
    total_desktops_billed: int = 0
    total_amount: Decimal = field(default_factory=lambda: Decimal("0"))
    accounts_processed: int = 0
    accounts_failed: int = 0
    accounts_marked_past_due: int = 0
    notifications_sent: int = 0
    account_results: List[ManagedDesktopLevyAccountResult] = field(
        default_factory=list,
    )


def _fetch_billable_desktops(
    session: Session,
    billing_month: str,
) -> List[Assistant]:
    return (
        session.query(Assistant)
        .filter(
            Assistant.desktop_mode.in_(tuple(MANAGED_DESKTOP_MODES)),
            Assistant.managed_desktop_status.in_(["active", "grace_period"]),
            (
                Assistant.managed_desktop_last_billed_month.is_(None)
                | (Assistant.managed_desktop_last_billed_month != billing_month)
            ),
        )
        .with_for_update()
        .all()
    )


def _group_desktops_by_billing_account(
    session: Session,
    assistants: List[Assistant],
) -> Dict[int, Tuple[BillingAccount, List[Assistant]]]:
    assistant_ba_cache: Dict[int, Optional[BillingAccount]] = {}
    groups: Dict[int, Tuple[BillingAccount, List[Assistant]]] = {}

    for assistant in assistants:
        aid = assistant.agent_id
        if aid not in assistant_ba_cache:
            assistant_ba_cache[aid] = _get_billing_account_for_assistant(
                session,
                assistant,
            )
        ba = assistant_ba_cache[aid]
        if ba is None:
            logger.warning(
                "No billing account for assistant %d managed desktop – skipping",
                aid,
            )
            continue
        if ba.id not in groups:
            groups[ba.id] = (ba, [])
        groups[ba.id][1].append(assistant)

    return groups


def _process_managed_desktop_billing_account(
    session: Session,
    ba: BillingAccount,
    assistants: List[Assistant],
    billing_month: str,
    *,
    now: _dt.datetime,
) -> ManagedDesktopLevyAccountResult:
    ar = ManagedDesktopLevyAccountResult(
        billing_account_id=ba.id,
        credits_before=Decimal(str(ba.credits)),
    )
    total_levy = Decimal("0")
    billable: List[Assistant] = []

    for assistant in assistants:
        if _in_grandfather_period(assistant, now=now):
            continue
        desktop_mode = assistant.desktop_mode
        if desktop_mode not in MANAGED_DESKTOP_MODES:
            continue
        cost = get_managed_desktop_monthly_cost(session, desktop_mode)
        total_levy += cost
        assistant.managed_desktop_last_billed_month = billing_month
        assistant.managed_desktop_monthly_cost = cost
        billable.append(assistant)
        if desktop_mode == "ubuntu":
            ar.ubuntu_count += 1
            ar.ubuntu_cost += cost
        else:
            ar.windows_count += 1
            ar.windows_cost += cost

    ar.desktops_billed = len(billable)
    ar.total_amount = total_levy

    if total_levy == 0:
        ar.credits_after = ar.credits_before
        return ar

    new_balance = BillingAccountDAO(session).deduct_credits(
        ba.id,
        float(total_levy),
        category="managed_desktop",
        description=f"Computer Use levy ({billing_month})",
        detail={
            "event": "managed_desktop_levy",
            "billing_month": billing_month,
            "ubuntu_count": ar.ubuntu_count,
            "ubuntu_cost": float(ar.ubuntu_cost),
            "windows_count": ar.windows_count,
            "windows_cost": float(ar.windows_cost),
        },
    )
    if new_balance is not None:
        ar.credits_after = Decimal(str(new_balance))
    else:
        ar.credits_after = ar.credits_before

    is_metered = (
        BillingAccountDAO(session).resolve_billing_mode(ba) == BillingMode.METERED
    )
    if not is_metered and ba.credits < 0:
        for assistant in billable:
            if assistant.managed_desktop_status == "active":
                assistant.managed_desktop_status = "grace_period"
                assistant.managed_desktop_grace_period_started_at = now
                ar.grace_period_desktops += 1
        ar.marked_past_due = True

    return ar


def levy_managed_desktops(
    session: Session,
    *,
    billing_month: str,
) -> ManagedDesktopLevyResult:
    """Bill managed Computer Use add-ons for the target month."""
    result = ManagedDesktopLevyResult(billing_month=billing_month)
    if not settings.charges_billing:
        return result

    now = _dt.datetime.now(_dt.timezone.utc)
    billable = _fetch_billable_desktops(session, billing_month)
    if not billable:
        return result

    groups = _group_desktops_by_billing_account(session, billable)
    for ba_id, (ba, assistants) in groups.items():
        try:
            account_result = _process_managed_desktop_billing_account(
                session,
                ba,
                assistants,
                billing_month,
                now=now,
            )
            session.commit()
            result.account_results.append(account_result)
            result.total_desktops_billed += account_result.desktops_billed
            result.total_amount += account_result.total_amount
            result.accounts_processed += 1
            if account_result.marked_past_due:
                result.accounts_marked_past_due += 1
                _send_insufficient_credits_notification(session, account_result)
                if account_result.insufficient_credits_notified:
                    result.notifications_sent += 1
        except Exception:
            session.rollback()
            result.accounts_failed += 1
            logger.exception(
                "Managed desktop levy failed for billing account %s (%s)",
                ba_id,
                billing_month,
            )

    return result


def _send_insufficient_credits_notification(
    session: Session,
    account_result: ManagedDesktopLevyAccountResult,
) -> None:
    ba = session.get(BillingAccount, account_result.billing_account_id)
    if ba is None:
        return
    emails = get_notification_emails_for_ba(session, ba)
    if not emails:
        return
    send_notification_emails_sync(
        emails,
        LEVY_INSUFFICIENT_CREDITS_SUBJECT,
        build_insufficient_credits_email(get_account_label_for_ba(session, ba)),
    )
    account_result.insufficient_credits_notified = True
