"""Read-only fact exports behind ``/admin/metrics/export/*``.

Each export returns one keyset page of raw rows for the company KPI
dashboard, which owns every rollup, cohort and ratio downstream. Nothing
here aggregates across accounts beyond the per-(user, UTC day) activity
bucket, and nothing here writes: a KPI computed inside Orchestra would be a
second definition of the metric that drifts from the dashboard's, and a
fact re-derived per pull cannot.

Paging is by ``(sort_ts, id)`` keyset, carried in an opaque cursor, so a
caller resumes exactly where it stopped however many rows land in between.
Every response timestamp is UTC with an explicit ``Z``; naive columns
(``user.created_at``, ``recharge.at``) are UTC by convention and tz-aware
ones are converted before they are compared, bucketed or rendered.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable

from sqlalchemy import (
    ColumnElement,
    Date,
    cast,
    distinct,
    func,
    or_,
    select,
    true,
    tuple_,
)
from sqlalchemy.orm import Session, aliased

from orchestra.db.models.enums import RECHARGE_TYPE_PROMO
from orchestra.db.models.orchestra_models import (
    Account,
    Assistant,
    BillingAccount,
    BillingPlanAssignment,
    BillingPlanTemplate,
    CreditGrantLinkClaim,
    CreditTransaction,
    EmailAccount,
    OnboardingStatus,
    OneTimeCreditGrantLink,
    Organization,
    OrganizationMember,
    Recharge,
    ReferralAttribution,
    ReferralCode,
    User,
)
from orchestra.settings import settings

#: Debit categories that represent product usage; ``seat``, ``subscription``
#: and the forfeit categories move credits without anyone using anything.
BILLABLE_CATEGORIES = ("llm", "hire", "resources", "media")

#: How far back the activity export reaches when the caller gives no ``since``.
ACTIVITY_DEFAULT_WINDOW = timedelta(days=45)

#: Console writes these into ``onboarding_status.step_data`` at signup.
ATTRIBUTION_KEYS = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "referrer",
    "landing_url",
)


class InvalidCursor(ValueError):
    """The caller passed back a cursor this module did not mint."""


def encode_cursor(sort_key: str, row_id: str | int) -> str:
    """Pack the last row's sort key and id into an opaque, URL-safe token."""
    payload = json.dumps({"k": sort_key, "id": row_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_cursor(cursor: str) -> tuple[str, str | int]:
    """Unpack :func:`encode_cursor`; anything else is an :class:`InvalidCursor`."""
    # binascii.Error, JSONDecodeError and UnicodeDecodeError are all ValueErrors.
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except ValueError as exc:
        raise InvalidCursor("cursor is not one this endpoint issued") from exc
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("k"), str)
        or not isinstance(payload.get("id"), (str, int))
    ):
        raise InvalidCursor("cursor is not one this endpoint issued")
    return payload["k"], payload["id"]


def _cursor_after(
    cursor: str | None,
    parse_key: Callable[[str], Any],
    parse_id: Callable[[Any], Any],
) -> tuple[Any, Any] | None:
    """Decode ``cursor`` into the typed ``(sort_key, id)`` a keyset predicate needs."""
    if cursor is None:
        return None
    key, row_id = decode_cursor(cursor)
    try:
        return parse_key(key), parse_id(row_id)
    except (TypeError, ValueError) as exc:
        raise InvalidCursor("cursor is not one this endpoint issued") from exc


def _as_utc(moment: datetime) -> datetime:
    """Naive input is UTC by contract; aware input is converted."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _naive_utc(moment: datetime) -> datetime:
    """UTC wall-clock value for comparing against a naive TIMESTAMP column."""
    return _as_utc(moment).replace(tzinfo=None)


def _iso_z(moment: datetime) -> str:
    return _naive_utc(moment).isoformat() + "Z"


def _iso_z_or_none(moment: datetime | None) -> str | None:
    return None if moment is None else _iso_z(moment)


def _aware_from_iso(text: str) -> datetime:
    return _as_utc(datetime.fromisoformat(text))


def _naive_from_iso(text: str) -> datetime:
    return _naive_utc(datetime.fromisoformat(text))


def _number(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _page(
    rows: list[dict[str, Any]],
    limit: int,
    cursor_of: Callable[[dict[str, Any]], str],
) -> dict[str, Any]:
    """Trim the ``limit + 1`` probe row and mint the cursor only when it existed."""
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "rows": rows,
        "next_cursor": cursor_of(rows[-1]) if has_more else None,
        "generated_at": _iso_z(datetime.now(timezone.utc)),
    }


# ---------------------------------------------------------------------------
# Billing-account → owner resolution (payments, plans)
# ---------------------------------------------------------------------------


def _account_owner_laterals(billing_account_id: ColumnElement[int]) -> tuple[Any, Any]:
    """LATERAL lookups from a billing account to the user or org that owns it.

    ``User`` and ``Organization`` both point *at* ``billing_account`` and
    exactly one of them does per account, but neither FK is unique at the
    DB level. ``LIMIT 1`` keeps the join 1:1 so a keyset page can never
    duplicate a fact.
    """
    owner_user = (
        select(User.id.label("user_id"))
        .where(User.billing_account_id == billing_account_id)
        .limit(1)
        .lateral("owner_user")
    )
    owner_org = (
        select(
            Organization.id.label("organization_id"),
            Organization.owner_id.label("owner_id"),
        )
        .where(Organization.billing_account_id == billing_account_id)
        .limit(1)
        .lateral("owner_org")
    )
    return owner_user, owner_org


def _account_fields(
    user_id: str | None,
    organization_id: int | None,
    org_owner_id: str | None,
) -> dict[str, Any]:
    """Attribution wants a person, so an org account resolves to its owner."""
    if user_id is not None:
        return {"account_kind": "user", "user_id": user_id, "organization_id": None}
    if organization_id is not None:
        return {
            "account_kind": "organization",
            "user_id": org_owner_id,
            "organization_id": organization_id,
        }
    return {"account_kind": "unknown", "user_id": None, "organization_id": None}


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def export_users(
    session: Session,
    *,
    since: datetime | None,
    until: datetime | None,
    cursor: str | None,
    limit: int,
) -> dict[str, Any]:
    """One page of users with signup provenance, ordered by ``(created_at, id)``."""
    after = _cursor_after(cursor, _naive_from_iso, str)
    conditions = []
    if since is not None:
        conditions.append(User.created_at >= _naive_utc(since))
    if until is not None:
        conditions.append(User.created_at < _naive_utc(until))
    if after is not None:
        conditions.append(tuple_(User.created_at, User.id) > tuple_(*after))

    # Select the page first, then decorate only those rows: the correlated
    # lookups below run once per emitted user rather than once per user.
    page = (
        select(
            User.id,
            User.email,
            User.created_at,
            User.billing_account_id,
            User.signup_ip,
        )
        .where(*conditions)
        .order_by(User.created_at, User.id)
        .limit(limit + 1)
        .subquery("page")
    )
    user_id = page.c.id

    oauth_provider = (
        select(Account.provider)
        .where(Account.user_id == user_id)
        .order_by(Account.provider)
        .limit(1)
        .scalar_subquery()
    )
    has_email_login = (
        select(EmailAccount.id).where(EmailAccount.user_id == user_id).exists()
    )
    first_organization_id = (
        select(OrganizationMember.organization_id)
        .where(OrganizationMember.user_id == user_id)
        .order_by(OrganizationMember.created_at, OrganizationMember.id)
        .limit(1)
        .scalar_subquery()
    )
    owns_organization = (
        select(Organization.id).where(Organization.owner_id == user_id).exists()
    )
    grant_link_name = (
        select(OneTimeCreditGrantLink.name)
        .join(
            CreditGrantLinkClaim,
            CreditGrantLinkClaim.link_id == OneTimeCreditGrantLink.id,
        )
        .where(CreditGrantLinkClaim.user_id == user_id)
        .order_by(CreditGrantLinkClaim.claimed_at, CreditGrantLinkClaim.id)
        .limit(1)
        .scalar_subquery()
    )
    first_assistant_created_at = (
        select(func.min(Assistant.created_at))
        .where(Assistant.user_id == user_id)
        .scalar_subquery()
    )

    stmt = (
        select(
            page.c.id,
            page.c.email,
            page.c.created_at,
            page.c.billing_account_id,
            page.c.signup_ip,
            oauth_provider.label("oauth_provider"),
            has_email_login.label("has_email_login"),
            first_organization_id.label("organization_id"),
            owns_organization.label("is_org_owner"),
            OnboardingStatus.current_step,
            OnboardingStatus.step_data,
            ReferralAttribution.code.label("referral_code"),
            ReferralAttribution.status.label("referral_status"),
            ReferralCode.label.label("referral_label"),
            grant_link_name.label("grant_link_name"),
            first_assistant_created_at.label("first_assistant_created_at"),
        )
        .outerjoin(OnboardingStatus, OnboardingStatus.user_id == user_id)
        .outerjoin(ReferralAttribution, ReferralAttribution.referee_user_id == user_id)
        .outerjoin(ReferralCode, ReferralCode.code == ReferralAttribution.code)
        .order_by(page.c.created_at, page.c.id)
    )

    rows = []
    for row in session.execute(stmt):
        step_data = row.step_data or {}
        rows.append(
            {
                "user_id": row.id,
                "email": row.email,
                "created_at": _iso_z(row.created_at),
                "auth_provider": row.oauth_provider
                or ("email" if row.has_email_login else "unknown"),
                "billing_account_id": row.billing_account_id,
                "organization_id": row.organization_id,
                "is_org_owner": row.is_org_owner,
                "onboarding_step": row.current_step,
                "heard_about": step_data.get("heard_about"),
                "heard_about_detail": step_data.get("heard_about_detail"),
                "attribution": {key: step_data.get(key) for key in ATTRIBUTION_KEYS},
                "referral_code": row.referral_code,
                "referral_label": row.referral_label,
                "referral_status": row.referral_status,
                "grant_link_name": row.grant_link_name,
                "first_assistant_created_at": _iso_z_or_none(
                    row.first_assistant_created_at,
                ),
                # The raw address is abuse-correlation input, not a KPI fact;
                # the hash still lets the dashboard count shared origins.
                "signup_ip_hash": (
                    hashlib.sha256(row.signup_ip.encode()).hexdigest()
                    if row.signup_ip
                    else None
                ),
            },
        )
    return _page(rows, limit, lambda r: encode_cursor(r["created_at"], r["user_id"]))


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


def export_payments(
    session: Session,
    *,
    since: datetime | None,
    until: datetime | None,
    statuses: Iterable[str],
    cursor: str | None,
    limit: int,
) -> dict[str, Any]:
    """One page of money-carrying recharges, ordered by ``(at, id)``.

    Zero-USD rows are never money and are excluded outright, and so is every
    ``promo`` row: a promotional grant is recorded as a PAID recharge carrying
    the credits' notional USD value, which nobody paid. The status filter is
    the caller's, defaulting upstream to ``PAID`` + ``DISPUTED`` so a later
    flip on an exported row shows up on the next pull.
    """
    after = _cursor_after(cursor, _naive_from_iso, int)
    conditions = [
        Recharge.amount_usd > 0,
        Recharge.type != RECHARGE_TYPE_PROMO,
        Recharge.status.in_(list(statuses)),
    ]
    if since is not None:
        conditions.append(Recharge.at >= _naive_utc(since))
    if until is not None:
        conditions.append(Recharge.at < _naive_utc(until))
    if after is not None:
        conditions.append(tuple_(Recharge.at, Recharge.id) > tuple_(*after))

    owner_user, owner_org = _account_owner_laterals(Recharge.billing_account_id)

    # Metered rows name their assignment; everything else falls back to
    # whichever assignment was active when the recharge happened.
    recharge_plan = aliased(BillingPlanAssignment, name="recharge_plan")
    recharge_template = aliased(BillingPlanTemplate, name="recharge_template")
    at_utc = func.timezone("UTC", Recharge.at)
    active_plan = (
        select(BillingPlanTemplate.name.label("name"))
        .join(
            BillingPlanAssignment,
            BillingPlanAssignment.template_id == BillingPlanTemplate.id,
        )
        .where(
            BillingPlanAssignment.billing_account_id == Recharge.billing_account_id,
            BillingPlanAssignment.started_at <= at_utc,
            or_(
                BillingPlanAssignment.ended_at.is_(None),
                BillingPlanAssignment.ended_at > at_utc,
            ),
        )
        .order_by(BillingPlanAssignment.started_at.desc())
        .limit(1)
        .lateral("active_plan")
    )

    stmt = (
        select(
            Recharge.id,
            Recharge.at,
            Recharge.billing_account_id,
            Recharge.type,
            Recharge.status,
            Recharge.amount_usd,
            Recharge.quantity,
            Recharge.stripe_invoice_id,
            owner_user.c.user_id,
            owner_org.c.organization_id,
            owner_org.c.owner_id,
            func.coalesce(recharge_template.name, active_plan.c.name).label(
                "plan_template_name",
            ),
        )
        .outerjoin(recharge_plan, recharge_plan.id == Recharge.plan_id)
        .outerjoin(recharge_template, recharge_template.id == recharge_plan.template_id)
        .outerjoin(owner_user, true())
        .outerjoin(owner_org, true())
        .outerjoin(active_plan, true())
        .where(*conditions)
        .order_by(Recharge.at, Recharge.id)
        .limit(limit + 1)
    )

    rows = [
        {
            "recharge_id": row.id,
            "at": _iso_z(row.at),
            "billing_account_id": row.billing_account_id,
            **_account_fields(row.user_id, row.organization_id, row.owner_id),
            "type": row.type,
            "status": row.status,
            "amount_usd": _number(row.amount_usd),
            "credits": _number(row.quantity),
            "stripe_invoice_id": row.stripe_invoice_id,
            "plan_template_name": row.plan_template_name,
        }
        for row in session.execute(stmt)
    ]
    return _page(rows, limit, lambda r: encode_cursor(r["at"], r["recharge_id"]))


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------


def export_activity(
    session: Session,
    *,
    since: datetime | None,
    until: datetime | None,
    cursor: str | None,
    limit: int,
) -> dict[str, Any]:
    """One page of per-(user, UTC day) billable debits, ordered by ``(day, user_id)``."""
    after = _cursor_after(cursor, date.fromisoformat, str)
    if since is None:
        today = datetime.now(timezone.utc).date()
        since = datetime.combine(
            today - ACTIVITY_DEFAULT_WINDOW,
            time.min,
            tzinfo=timezone.utc,
        )

    day = cast(func.timezone("UTC", CreditTransaction.at), Date)
    spent = -CreditTransaction.amount

    def spent_in(category: str) -> ColumnElement[Decimal]:
        return func.coalesce(
            func.sum(spent).filter(CreditTransaction.category == category),
            0,
        )

    conditions = [
        CreditTransaction.amount < 0,
        CreditTransaction.category.in_(BILLABLE_CATEGORIES),
        CreditTransaction.user_id.is_not(None),
        CreditTransaction.at >= _as_utc(since),
    ]
    if until is not None:
        conditions.append(CreditTransaction.at < _as_utc(until))
    if after is not None:
        conditions.append(tuple_(day, CreditTransaction.user_id) > tuple_(*after))

    stmt = (
        select(
            CreditTransaction.user_id,
            day.label("day"),
            func.sum(spent).label("credits_spent"),
            spent_in("llm").label("llm_credits"),
            spent_in("hire").label("hire_credits"),
            spent_in("resources").label("resources_credits"),
            spent_in("media").label("media_credits"),
            func.count().label("n_debits"),
            func.count(distinct(CreditTransaction.assistant_id)).label("n_assistants"),
        )
        .where(*conditions)
        .group_by(CreditTransaction.user_id, day)
        .order_by(day, CreditTransaction.user_id)
        .limit(limit + 1)
    )

    rows = [
        {
            "user_id": row.user_id,
            "day": row.day.isoformat(),
            "credits_spent": float(row.credits_spent),
            "llm_credits": float(row.llm_credits),
            "hire_credits": float(row.hire_credits),
            "resources_credits": float(row.resources_credits),
            "media_credits": float(row.media_credits),
            "n_debits": row.n_debits,
            "n_assistants": row.n_assistants,
        }
        for row in session.execute(stmt)
    ]
    return _page(rows, limit, lambda r: encode_cursor(r["day"], r["user_id"]))


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def export_plans(
    session: Session,
    *,
    since: datetime | None,
    cursor: str | None,
    limit: int,
) -> dict[str, Any]:
    """One page of plan-assignment history, ordered by ``(started_at, id)``.

    ``since`` selects assignments whose latest lifecycle event (start, or
    end when set — ``ended_at >= started_at`` is CHECK-enforced) falls at
    or after it, so a caller can pull only the history that moved.
    """
    after = _cursor_after(cursor, _aware_from_iso, int)
    conditions = []
    if since is not None:
        latest_event = func.coalesce(
            BillingPlanAssignment.ended_at,
            BillingPlanAssignment.started_at,
        )
        conditions.append(latest_event >= _as_utc(since))
    if after is not None:
        conditions.append(
            tuple_(BillingPlanAssignment.started_at, BillingPlanAssignment.id)
            > tuple_(*after),
        )

    owner_user, owner_org = _account_owner_laterals(
        BillingPlanAssignment.billing_account_id,
    )
    stmt = (
        select(
            BillingPlanAssignment.id,
            BillingPlanAssignment.billing_account_id,
            BillingPlanAssignment.started_at,
            BillingPlanAssignment.ended_at,
            BillingPlanTemplate.name,
            BillingPlanTemplate.display_name,
            BillingPlanTemplate.billing_mode,
            BillingPlanTemplate.commit_amount,
            BillingPlanTemplate.commit_period,
            BillingPlanTemplate.commit_schedule,
            BillingPlanTemplate.currency,
            BillingAccount.account_status,
            BillingAccount.stripe_subscription_id,
            owner_user.c.user_id,
            owner_org.c.organization_id,
            owner_org.c.owner_id,
        )
        .join(
            BillingPlanTemplate,
            BillingPlanTemplate.id == BillingPlanAssignment.template_id,
        )
        .join(
            BillingAccount,
            BillingAccount.id == BillingPlanAssignment.billing_account_id,
        )
        .outerjoin(owner_user, true())
        .outerjoin(owner_org, true())
        .where(*conditions)
        .order_by(BillingPlanAssignment.started_at, BillingPlanAssignment.id)
        .limit(limit + 1)
    )

    rows = [
        {
            "assignment_id": row.id,
            "billing_account_id": row.billing_account_id,
            **_account_fields(row.user_id, row.organization_id, row.owner_id),
            "template_name": row.name,
            "display_name": row.display_name,
            "billing_mode": row.billing_mode,
            "commit_amount": _number(row.commit_amount),
            "commit_period": row.commit_period,
            "commit_schedule": row.commit_schedule,
            "currency": row.currency,
            "started_at": _iso_z(row.started_at),
            "ended_at": _iso_z_or_none(row.ended_at),
            "is_trial": row.name == settings.trial_tier_template_name,
            "account_status": row.account_status,
            "stripe_subscription_id": row.stripe_subscription_id,
        }
        for row in session.execute(stmt)
    ]
    return _page(
        rows,
        limit,
        lambda r: encode_cursor(r["started_at"], r["assignment_id"]),
    )
