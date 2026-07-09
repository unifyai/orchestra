"""Grace-period enforcement for managed Computer Use add-ons."""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

from sqlalchemy.orm import Session

from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.models.orchestra_models import Assistant, BillingAccount, BillingMode
from orchestra.routines.assistant_contact_levy import _get_billing_account_for_assistant
from orchestra.routines.assistant_contact_notifications import (
    DELETION_SUBJECT,
    NOTIFICATION_DAYS,
    NOTIFICATION_SCHEDULE,
    build_deletion_email,
    build_warning_email,
    get_account_label_for_ba,
    get_notification_emails_for_ba,
    send_notification_emails,
)
from orchestra.services.managed_desktop_service import disable_managed_desktop

logger = logging.getLogger(__name__)

GRACE_PERIOD_DAYS = 14


@dataclass
class ManagedDesktopSuspensionAccountResult:
    billing_account_id: int
    restored_desktops: int = 0
    disabled_desktops: int = 0
    notifications_sent: List[int] = field(default_factory=list)
    deletion_email_sent: bool = False
    errors: List[str] = field(default_factory=list)


@dataclass
class ManagedDesktopSuspensionResult:
    total_grace_desktops_found: int = 0
    accounts_processed: int = 0
    desktops_restored: int = 0
    desktops_disabled: int = 0
    reminders_sent: int = 0
    deletion_emails_sent: int = 0
    account_results: List[ManagedDesktopSuspensionAccountResult] = field(
        default_factory=list,
    )


def _group_grace_desktops_by_ba(
    session: Session,
    assistants: List[Assistant],
) -> Dict[int, Tuple[BillingAccount, List[Assistant]]]:
    assistant_ba_cache: Dict[int, BillingAccount | None] = {}
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
            continue
        if ba.id not in groups:
            groups[ba.id] = (ba, [])
        groups[ba.id][1].append(assistant)

    return groups


async def suspend_overdue_managed_desktops(
    session: Session,
) -> ManagedDesktopSuspensionResult:
    """Disable managed desktops that remain unpaid after the grace period."""
    result = ManagedDesktopSuspensionResult()
    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff = now - _dt.timedelta(days=GRACE_PERIOD_DAYS)

    grace_assistants: List[Assistant] = (
        session.query(Assistant)
        .filter(Assistant.managed_desktop_status == "grace_period")
        .all()
    )
    if not grace_assistants:
        return result

    result.total_grace_desktops_found = len(grace_assistants)
    groups = _group_grace_desktops_by_ba(session, grace_assistants)

    for ba_id, (ba, assistants) in groups.items():
        ar = await _process_ba_grace_desktops(session, ba, assistants, now, cutoff)
        result.account_results.append(ar)
        result.accounts_processed += 1
        result.desktops_restored += ar.restored_desktops
        result.desktops_disabled += ar.disabled_desktops
        if ar.notifications_sent:
            result.reminders_sent += len(ar.notifications_sent)
        if ar.deletion_email_sent:
            result.deletion_emails_sent += 1

    return result


async def _process_ba_grace_desktops(
    session: Session,
    ba: BillingAccount,
    assistants: List[Assistant],
    now: _dt.datetime,
    cutoff: _dt.datetime,
) -> ManagedDesktopSuspensionAccountResult:
    ar = ManagedDesktopSuspensionAccountResult(billing_account_id=ba.id)

    is_metered = (
        BillingAccountDAO(session).resolve_billing_mode(ba) == BillingMode.METERED
    )
    if is_metered:
        return ar

    if ba.credits >= 0:
        assistant_ids_to_reawaken: Set[int] = set()
        for assistant in assistants:
            assistant.managed_desktop_status = "active"
            assistant.managed_desktop_grace_period_started_at = None
            ar.restored_desktops += 1
            assistant_ids_to_reawaken.add(assistant.agent_id)

        from orchestra.web.api.utils.assistant_infra import reawaken_assistant

        for aid in assistant_ids_to_reawaken:
            try:
                await reawaken_assistant(str(aid))
            except Exception as exc:
                ar.errors.append(f"reawaken {aid}: {exc}")
        return ar

    disabled_names: List[str] = []
    for assistant in assistants:
        grace_started = assistant.managed_desktop_grace_period_started_at
        if grace_started is None:
            continue
        if grace_started.tzinfo is None:
            grace_started = grace_started.replace(tzinfo=_dt.timezone.utc)
        days_elapsed = (now - grace_started).days

        if days_elapsed in NOTIFICATION_DAYS and days_elapsed < GRACE_PERIOD_DAYS:
            schedule_entry = NOTIFICATION_SCHEDULE[days_elapsed]
            recipients = get_notification_emails_for_ba(session, ba)
            if recipients:
                name = f"{assistant.first_name or ''} {assistant.surname or ''}".strip()
                await send_notification_emails(
                    recipients,
                    schedule_entry["subject"],
                    build_warning_email(
                        schedule_entry["days_remaining"],
                        get_account_label_for_ba(session, ba),
                        assistant_names=[name or f"Assistant {assistant.agent_id}"],
                    ),
                )
                ar.notifications_sent.append(days_elapsed)

        if grace_started <= cutoff:
            name = f"{assistant.first_name or ''} {assistant.surname or ''}".strip()
            disabled_names.append(name or f"Assistant {assistant.agent_id}")
            disable_managed_desktop(assistant)
            ar.disabled_desktops += 1

    if ar.disabled_desktops:
        recipients = get_notification_emails_for_ba(session, ba)
        if recipients:
            await send_notification_emails(
                recipients,
                DELETION_SUBJECT,
                build_deletion_email(
                    get_account_label_for_ba(session, ba),
                    assistant_names=disabled_names,
                ),
            )
            ar.deletion_email_sent = True

        from orchestra.web.api.utils.assistant_infra import reawaken_assistant

        for assistant in assistants:
            if assistant.managed_desktop_status == "disabled":
                try:
                    await reawaken_assistant(str(assistant.agent_id))
                except Exception as exc:
                    ar.errors.append(
                        f"reawaken {assistant.agent_id} after disable: {exc}",
                    )

    return ar
