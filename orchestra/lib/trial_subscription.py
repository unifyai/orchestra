"""Card-gated trial onboarding (auto-enrolling $-tier subscription).

Every new self-serve account must put a card on file before the platform
is usable. Signup flows into a Stripe Checkout Session that:

* collects a card (``payment_method_collection="always"``) and full
  billing address (required for Stripe Tax on later invoices),
* creates a subscription on the configured trial tier
  (``settings.trial_tier_template_name``) with
  ``trial_period_days=settings.trial_period_days``, so the first charge
  lands automatically at trial end unless the customer cancels first.

On ``checkout.session.completed`` the account is linked to the
subscription. The one-time signup credit grant is *not* applied here — a
card on file is a promise, not money — but on the first successfully
collected invoice (see
:func:`orchestra.lib.subscription_billing.apply_subscription_invoice_paid`),
stamped ``grant_kind="trial"`` and expiring with that first paid period.

Platform access is gated on :func:`has_platform_access`: an account with
neither a live subscription nor any real payment history is frozen out
of LLM usage (enforced through the spending-limit payload and the
account freeze sweep).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import stripe
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.enums import RECHARGE_TYPE_PAYMENT
from orchestra.db.models.orchestra_models import (
    BillingAccount,
    BillingPlanTemplate,
    Recharge,
    RechargeStatus,
)
from orchestra.lib.subscription_billing import (
    SubscriptionError,
    ensure_stripe_customer,
    is_annual,
    subscription_price_id,
    subscription_quantity,
)
from orchestra.settings import settings

logger = logging.getLogger(__name__)

# Recharge types that represent real money collected from the customer.
_PAID_RECHARGE_TYPES = (RECHARGE_TYPE_PAYMENT, "auto")


def resolve_trial_tier_template(session: Session) -> BillingPlanTemplate:
    """The tier every new signup is auto-enrolled on (e.g. ``tier_50``)."""
    template = session.execute(
        select(BillingPlanTemplate).where(
            BillingPlanTemplate.name == settings.trial_tier_template_name,
            BillingPlanTemplate.is_active.is_(True),
        ),
    ).scalar_one_or_none()
    if template is None:
        raise SubscriptionError(
            f"Trial tier template {settings.trial_tier_template_name!r} "
            "not found or inactive.",
        )
    return template


def has_ever_paid(session: Session, billing_account_id: int) -> bool:
    """Whether the account has any real (non-promo) payment history."""
    return (
        session.execute(
            select(Recharge.id).where(
                Recharge.billing_account_id == billing_account_id,
                Recharge.type.in_(_PAID_RECHARGE_TYPES),
                Recharge.status == RechargeStatus.PAID,
            ),
        ).first()
        is not None
    )


def has_free_trial_grant(session: Session, billing_account_id: int) -> bool:
    """Whether the account is on an admin-granted open-ended free trial.

    Toggled per-organization through ``PUT``/``DELETE
    /admin/organization/{id}/free-trial``. Distinct from
    :func:`is_internal_account`: that one identifies the platform's own
    staff by email domain, whereas this is an explicit, revocable grant
    on a *customer* org — white-glove onboarding where the customer
    evaluates the platform before any card is on file. The grant has no
    expiry; revoking it re-applies the card gate immediately.
    """
    from orchestra.db.models.orchestra_models import Organization

    return (
        session.execute(
            select(Organization.id).where(
                Organization.billing_account_id == billing_account_id,
                Organization.free_trial.is_(True),
            ),
        ).first()
        is not None
    )


def is_internal_account(session: Session, billing_account_id: int) -> bool:
    """Whether the billing account belongs to the platform's own team.

    True for the Unify organization's own billing account, and for the
    personal account of any of its members. Internal accounts are exempt
    from the trial anti-abuse gates: they are not burner signups, and
    gating them only breaks internal environments (shared staging
    tenants, benchmarks, smoke tests).

    Internal status requires *both* signals. The Unify-org scoping alone
    is spoofable — the name is claimable wherever the platform's own org
    does not exist — so the owner/member must also hold a verified
    unify.ai mailbox. The domain alone is not sufficient either: staff
    provision and own customer organizations during white-glove
    onboarding, and an owner-domain test on its own marked those real
    customer accounts internal, exempting them from the card gate and
    the daily burn ceiling. A customer that should skip the card gate
    gets an explicit, revocable grant instead — see
    :func:`has_free_trial_grant`.
    """
    from orchestra.db.models.orchestra_models import (
        Organization,
        OrganizationMember,
        User,
    )
    from orchestra.services.personal_workspace_service import (
        UNIFY_ORGANIZATION_NAME,
        UNIFY_STAFF_EMAIL_DOMAIN,
    )

    unify_org_id = session.execute(
        select(Organization.id).where(
            Organization.name == UNIFY_ORGANIZATION_NAME,
        ),
    ).scalar_one_or_none()
    if unify_org_id is None:
        return False

    # The org name is claimable by anyone wherever the platform's own org
    # does not exist, so the name alone proves nothing: the org's wallet is
    # internal only when its owner holds a verified unify.ai mailbox, and a
    # member's personal wallet only when that member does.
    owner_email = session.execute(
        select(User.email)
        .join(Organization, Organization.owner_id == User.id)
        .where(
            Organization.id == unify_org_id,
            Organization.billing_account_id == billing_account_id,
        ),
    ).scalar_one_or_none()
    if owner_email is not None and owner_email.lower().endswith(
        UNIFY_STAFF_EMAIL_DOMAIN,
    ):
        return True

    member_email = session.execute(
        select(User.email)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(
            User.billing_account_id == billing_account_id,
            OrganizationMember.organization_id == unify_org_id,
        ),
    ).scalar_one_or_none()
    return bool(member_email) and member_email.lower().endswith(
        UNIFY_STAFF_EMAIL_DOMAIN,
    )


def has_platform_access(session: Session, ba: BillingAccount) -> bool:
    """Whether the account may use metered platform features (LLM calls).

    True when the card gate is disabled globally, when a subscription is
    live (``stripe_subscription_id`` is cleared by the
    ``customer.subscription.deleted`` webhook, so its presence means
    trialing/active/past-due-in-dunning), when the account has real
    payment history (grandfathered pre-subscription payers), when the org
    holds an admin-granted free trial (see :func:`has_free_trial_grant`),
    or when the account is internal (see :func:`is_internal_account`).
    """
    if not settings.require_card_on_file:
        return True
    if ba.stripe_subscription_id:
        return True
    if has_ever_paid(session, ba.id):
        return True
    if has_free_trial_grant(session, ba.id):
        return True
    return is_internal_account(session, ba.id)


def has_api_access(session: Session, ba: Optional[BillingAccount]) -> bool:
    """Whether this account may spend credits outside the Console.

    Free credits are meant to be spent through the Console, where a human
    is present and abuse is observable. The programmatic surfaces — the
    UniLLM proxy and the runtime-starting endpoints — are the cheap route
    for burner accounts to extract them, so an account reaches them only
    once it is no longer purely on free money:

    * real payment history, or a live subscription;
    * an admin-granted free trial (a comped evaluation is a deliberate
      decision, and those accounts are known);
    * internal Unify accounts;
    * grandfathered accounts that were already calling the API before the
      gate existed (see the ``api_access_grandfather`` migration).

    Gating on payment rather than on credit balance is deliberate: a
    burner's problem is that signing up is free, not that credits run
    out.
    """
    if not settings.require_api_payment_history:
        return True
    if ba is None:
        return False
    if ba.api_access_grandfathered:
        return True
    if ba.stripe_subscription_id:
        return True
    if has_ever_paid(session, ba.id):
        return True
    if has_free_trial_grant(session, ba.id):
        return True
    return is_internal_account(session, ba.id)


def create_trial_checkout_session(
    session: Session,
    ba: BillingAccount,
    *,
    is_business: bool,
    fallback_email: Optional[str] = None,
    fallback_name: Optional[str] = None,
) -> str:
    """Create the signup Checkout Session; returns its hosted URL.

    The Console has no Stripe.js — card + address collection happens on
    the Stripe-hosted page. The subscription is created by Stripe when
    the session completes, in ``trialing`` status; local state is synced
    by the ``checkout.session.completed`` webhook.
    """
    if ba.stripe_subscription_id:
        raise SubscriptionError("Account already has a subscription.")

    template = resolve_trial_tier_template(session)
    customer_id = ensure_stripe_customer(
        session,
        ba,
        is_business=is_business,
        fallback_email=fallback_email,
        fallback_name=fallback_name,
    )

    checkout = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[
            {
                "price": subscription_price_id(
                    is_business,
                    annual=is_annual(template),
                ),
                "quantity": subscription_quantity(template),
            },
        ],
        subscription_data={
            "trial_period_days": settings.trial_period_days,
            "metadata": {
                "template_id": str(template.id),
                "billing_account_id": str(ba.id),
                "billing_interval": "monthly",
                "source": "signup_trial",
            },
        },
        # Card is mandatory even though the subscription starts on trial.
        payment_method_collection="always",
        billing_address_collection="required",
        customer_update={"address": "auto", "name": "auto"},
        automatic_tax={"enabled": True},
        metadata={"billing_account_id": str(ba.id)},
        success_url=(
            f"{settings.console_url}/billing/trial-started"
            "?session_id={CHECKOUT_SESSION_ID}"
        ),
        cancel_url=f"{settings.console_url}/billing/add-card",
    )
    return checkout.url


def apply_trial_checkout_completed(
    session: Session,
    ba: BillingAccount,
    checkout_data: dict,
) -> None:
    """Sync local state after the signup Checkout Session completes.

    Links the subscription and stamps the trial end. No credits are
    granted here: the first invoice at trial end flows through the normal
    ``invoice.paid`` handler, which activates the tier assignment, grants
    the monthly credits, and applies the one-time signup grant — so an
    account whose card never charges successfully never holds credits.
    """
    subscription_id = checkout_data.get("subscription")
    if not subscription_id:
        logger.warning(
            {
                "message": "Trial checkout completed without a subscription",
                "billing_account_id": ba.id,
                "checkout_session_id": checkout_data.get("id"),
            },
        )
        return

    ba.stripe_subscription_id = subscription_id
    ba.subscription_cancel_at_period_end = False

    subscription = stripe.Subscription.retrieve(subscription_id)
    trial_end = subscription.get("trial_end")
    if trial_end:
        ba.trial_end_at = datetime.fromtimestamp(int(trial_end), tz=timezone.utc)

    # A frozen never-paid account that completes checkout is reinstated.
    if ba.account_status == "SUSPENDED" and ba.suspension_reason == "card_required":
        ba.account_status = "ACTIVE"
        ba.suspension_reason = None

    logger.info(
        {
            "message": "Signup trial subscription linked",
            "billing_account_id": ba.id,
            "stripe_subscription_id": subscription_id,
            "trial_end_at": (ba.trial_end_at.isoformat() if ba.trial_end_at else None),
        },
    )


def trial_gate_fields(session: Session, ba: Optional[BillingAccount]) -> dict:
    """Anti-abuse fields for the spend endpoints' limit-check payload.

    * ``account_suspended`` — hard deny (frozen by an admin or a sweep).
    * ``never_paid`` — true while the account has no real payment history.
      The runtime holds paid-only providers behind it. False for internal
      accounts (see :func:`is_internal_account`) and for orgs holding an
      admin-granted free trial (see :func:`has_free_trial_grant`), so a
      comped evaluation keeps full model access.
    * ``api_access_allowed`` — false while the account is still purely on
      free credits (see :func:`has_api_access`). Only the gateway proxy
      acts on it; a false here must never block the Console's own work.
    """
    if ba is None:
        return {
            "account_suspended": False,
            "never_paid": False,
            "api_access_allowed": has_api_access(session, None),
        }

    return {
        "account_suspended": ba.account_status != "ACTIVE",
        "never_paid": (
            not has_ever_paid(session, ba.id)
            and not is_internal_account(session, ba.id)
            and not has_free_trial_grant(session, ba.id)
        ),
        # Consumed by the gateway proxy, which denies when false. The
        # runtime ignores it: work started from the Console is exactly
        # what free credits are for.
        "api_access_allowed": has_api_access(session, ba),
    }


def comms_gate_for_assistant(session: Session, assistant) -> dict:
    """Billing-gate state for an assistant's inbound comms channels.

    Returned to the unify-deploy adapters so a gated account's owner gets
    an explanatory auto-reply over the channel they wrote on (instead of
    a silent assistant). ``message`` is owner-facing, channel-agnostic,
    and short enough for a single SMS segment pair.
    """
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO
    from orchestra.db.models.enums import BillingMode
    from orchestra.db.models.orchestra_models import Organization, User

    not_gated = {"gated": False, "reason": None, "message": None}

    if not settings.charges_billing:
        return not_gated

    ba = None
    if assistant.organization_id is not None:
        org = session.get(Organization, assistant.organization_id)
        if org and org.billing_account_id:
            ba = session.get(BillingAccount, org.billing_account_id)
    else:
        user = session.get(User, assistant.user_id)
        if user and user.billing_account_id:
            ba = session.get(BillingAccount, user.billing_account_id)
    if ba is None:
        return not_gated

    console = settings.console_url

    if ba.account_status != "ACTIVE":
        if ba.suspension_reason == "card_required":
            return {
                "gated": True,
                "reason": "card_required",
                "message": (
                    "Your Unify assistant is paused because your account "
                    "has no active subscription yet. Add a payment method "
                    f"at {console}/billing/add-card to start your free "
                    "trial — your assistant will pick this conversation "
                    "right back up."
                ),
            }
        return {
            "gated": True,
            "reason": "suspended",
            "message": (
                "Your Unify account is currently suspended, so your "
                "assistant can't respond. Please contact support@unify.ai "
                "to restore access."
            ),
        }

    if not has_platform_access(session, ba):
        return {
            "gated": True,
            "reason": "card_required",
            "message": (
                "Your Unify assistant is paused because your account has "
                "no active subscription yet. Add a payment method at "
                f"{console}/billing/add-card to start your free trial — "
                "your assistant will pick this conversation right back up."
            ),
        }

    mode = BillingAccountDAO(session).resolve_billing_mode(ba)
    if mode == BillingMode.CREDITS and float(ba.credits) <= 0:
        return {
            "gated": True,
            "reason": "out_of_credits",
            "message": (
                "Your Unify assistant is paused because your credits have "
                f"run out. Visit {console} and open billing settings to "
                "subscribe or top up, and your assistant will pick this "
                "conversation right back up."
            ),
        }

    return not_gated
