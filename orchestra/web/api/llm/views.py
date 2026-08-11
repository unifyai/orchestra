"""LLM gateway: the metering, without the bytes.

Callers hold the provider credentials themselves -- the broker sidecar
running beside each assistant runtime -- and stream to the provider
directly. What they cannot do on their own is decide whether an account may
spend and what a call cost, so they ask here: ``authorize`` before, and
``settle`` after.

This service used to proxy the calls as well. It stopped because of what
that cost in capacity rather than latency: Orchestra serves 400 concurrent
requests in total, and a proxied generation held one of them for its whole
duration, so LLM volume contended with billing, logs and search on the same
pool and every voice turn paid a network hop. These two endpoints occupy a
slot for milliseconds.

Pricing stays on this side deliberately. A caller that priced its own calls
could declare any number, and refusing to serve a model we cannot price
would stop meaning anything.
"""

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request, status

from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.dependencies import transient_request_db_session
from orchestra.lib.billing import get_billing_entity
from orchestra.settings import settings
from orchestra.web.api.llm.schema import (
    AuthorizeRequest,
    AuthorizeResponse,
    SettleRequest,
    SettleResponse,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_OPENROUTER_SUFFIX = "@openrouter"
_LLM_CATEGORY = "llm"


@dataclass(frozen=True)
class _BillingCtx:
    """Scalars captured while the DB session is open, safe to use after close."""

    billing_account_id: Optional[int]
    charges: bool


def _usage_cost(usage: Any) -> Optional[float]:
    """Return OpenRouter's authoritative ``usage.cost`` (USD) when present."""
    if not isinstance(usage, dict):
        return None
    cost = usage.get("cost")
    if cost is None:
        return None
    try:
        value = float(cost)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _anthropic_token_rates(model: str) -> Optional[tuple[float, float]]:
    """Per-million input/output USD rates for a native-Anthropic model.

    Anthropic reports usage in tokens and never in money, so unlike the
    OpenRouter leg there is no authoritative cost to meter from and the
    price has to come from our own catalogue. The curated options are the
    whole of it: native ``@anthropic`` endpoints are only ever offered from
    there — model *search* returns ``@openrouter`` ids — so a model reaching
    this leg without a row is not a pricing gap but a model we do not sell.
    """
    from orchestra.web.api.assistant.default_models import DEFAULT_MODEL_OPTIONS

    wanted = model.strip().lower()
    for option in DEFAULT_MODEL_OPTIONS:
        if not option.model:
            continue
        bare = option.model.rsplit("@", 1)[0].strip().lower()
        if bare == wanted or option.model.strip().lower() == wanted:
            return (option.input_usd_per_m, option.output_usd_per_m)
    return None


def _anthropic_usage_cost(usage: Any, rates: tuple[float, float]) -> Optional[float]:
    """Price an Anthropic usage block, counting cache tokens as input.

    Cache reads and writes are billed by Anthropic at rates that differ from
    base input, so folding them in at the input rate is an approximation --
    but one that errs toward charging, and undercharging here is how a
    gateway quietly becomes free inference.
    """
    if not isinstance(usage, dict):
        return None
    input_per_m, output_per_m = rates

    def _count(*names: str) -> int:
        total = 0
        for name in names:
            value = usage.get(name)
            if isinstance(value, (int, float)):
                total += int(value)
        return total

    prompt = _count(
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    completion = _count("output_tokens")
    if prompt <= 0 and completion <= 0:
        return None
    return (prompt * input_per_m + completion * output_per_m) / 1_000_000


def _enforce_spending_caps(
    session: Any,
    *,
    user_id: Optional[str],
    organization_id: Optional[int],
    assistant_id: Optional[int],
) -> None:
    """Block the call when a monthly spending cap is already exhausted.

    Mirrors the caps unity reports today (personal user, org, org member, and
    assistant) but enforces them *server-side* at the gateway so a runaway or
    compromised caller is stopped at its own dollar limit — the control that
    would have bounded the incident that motivated this gateway.
    """
    from orchestra.db.dao.assistant_dao import AssistantDAO
    from orchestra.db.dao.organization_dao import OrganizationDAO
    from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
    from orchestra.db.dao.user_dao import UserDAO

    month = datetime.now(timezone.utc).strftime("%Y-%m")

    def _blocked(scope: str, cap: Optional[float], spend: float) -> None:
        if cap is not None and cap > 0 and spend >= cap:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Monthly spending limit reached for this {scope} "
                    f"(${spend:.2f} of ${cap:.2f}). Raise the limit to "
                    "continue."
                ),
            )

    if organization_id is not None:
        org_dao = OrganizationDAO(session)
        _blocked(
            "organization",
            org_dao.get_spending_cap(organization_id),
            org_dao.get_cumulative_spend(organization_id, month),
        )
        if user_id:
            member_dao = OrganizationMemberDAO(session)
            _blocked(
                "member",
                member_dao.get_spending_cap(user_id, organization_id),
                member_dao.get_cumulative_spend(user_id, organization_id, month),
            )
    elif user_id:
        user_dao = UserDAO(session)
        _blocked(
            "user",
            user_dao.get_spending_cap(user_id),
            user_dao.get_cumulative_spend(user_id, month),
        )

    if assistant_id is not None:
        assistant_dao = AssistantDAO(session)
        _blocked(
            "assistant",
            assistant_dao.get_spending_cap(assistant_id),
            assistant_dao.get_cumulative_spend(assistant_id, month),
        )


def _precheck(request: Request, assistant_id: Optional[int]) -> _BillingCtx:
    """Resolve billing and block *before* the provider call when out of credits.

    Runs in a transient session that is committed and closed before we await
    the provider, so no DB connection is held across the outbound call.
    """
    user_id = getattr(request.state, "user_id", None)
    organization_id = getattr(request.state, "organization_id", None)
    charges = settings.charges_billing

    with transient_request_db_session(request) as session:
        try:
            entity = get_billing_entity(session, user_id, organization_id)
        except ValueError:
            # Billing not set up. On a charging deployment that's a hard error;
            # otherwise (staging/self-host) proceed without metering.
            if charges:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Billing is not set up",
                )
            return _BillingCtx(billing_account_id=None, charges=False)

        billing_account_id = int(entity.billing_account_id)
        # CREDITS accounts are gated on a positive balance; METERED (enterprise)
        # accounts bill via the ledger and are never balance-blocked here.
        if charges and entity.billing_mode.value == "CREDITS":
            if float(entity.credits) <= 0:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail=(
                        "Insufficient credits. Top up to continue making " "LLM calls."
                    ),
                )

        if charges:
            _enforce_spending_caps(
                session,
                user_id=user_id,
                organization_id=organization_id,
                assistant_id=assistant_id,
            )

    return _BillingCtx(billing_account_id=billing_account_id, charges=charges)


def _charge_usage(
    session_factory: Any,
    *,
    ctx: _BillingCtx,
    user_id: Optional[str],
    organization_id: Optional[int],
    assistant_id: Optional[int],
    model: str,
    raw_cost: float,
) -> None:
    """Settle a completed call against the ledger with the platform markup."""
    if not ctx.charges or ctx.billing_account_id is None:
        return
    amount = float(raw_cost) * float(settings.chat_completions_markup_rate)
    if amount <= 0:
        return
    session = session_factory()
    try:
        BillingAccountDAO(session).deduct_credits(
            ctx.billing_account_id,
            amount,
            category=_LLM_CATEGORY,
            assistant_id=assistant_id,
            user_id=user_id,
            organization_id=organization_id,
            description="LLM gateway",
            detail={
                "model": model,
                "source": "gateway",
                "raw_cost": float(raw_cost),
                "markup": float(settings.chat_completions_markup_rate),
            },
        )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception(
            "LLM gateway: failed to record usage cost (call already served)",
        )
    finally:
        session.close()


def _price_usage_for(model: str, usage: Any) -> Optional[float]:
    """Cost a provider's own usage object, however that provider reports it.

    OpenRouter states an authoritative charged amount; Anthropic states
    tokens and never money. Keeping both here means the caller reports what
    the provider said and never computes a price -- the side holding the
    ledger stays the side that decides what things cost.
    """
    direct = _usage_cost(usage)
    if direct is not None:
        return direct
    rates = _anthropic_token_rates(model)
    if rates is None:
        return None
    return _anthropic_usage_cost(usage, rates)


def _is_meterable(model: str) -> bool:
    """Whether a completed call on this model could be priced at all.

    OpenRouter models report their own cost, so any of them can be settled.
    A native Anthropic model can only be settled if the catalogue prices it.
    """
    normalized = model.strip().lower()
    if normalized.endswith(_OPENROUTER_SUFFIX) or normalized.startswith("openrouter/"):
        return True
    return _anthropic_token_rates(model) is not None


@router.post(
    "/llm/authorize",
    response_model=AuthorizeResponse,
    tags=["LLM Gateway"],
)
def authorize(request: Request, body: AuthorizeRequest) -> AuthorizeResponse:
    """Decide whether a caller holding its own provider key may proceed.

    This is the gateway without the bytes. A pod-local broker streams
    directly to the provider -- so the account checks that the proxy routes
    run inline have to be answerable on their own, in milliseconds, rather
    than by routing a generation through this service.

    That split matters for more than latency: a proxied call occupies a
    request slot here for as long as the model is generating, so LLM volume
    would contend with billing, logs and search on the same pool. A metadata
    call occupies one for milliseconds.

    Refusals are returned as ``allowed: false`` rather than raised, so a
    caller can relay the reason to the user; genuine faults still raise.
    """
    if not _is_meterable(body.model):
        return AuthorizeResponse(
            allowed=False,
            reason=(
                f"Model '{body.model}' cannot be metered, so it is not "
                "available. Only OpenRouter models and curated Anthropic "
                "models can be priced."
            ),
        )

    try:
        _precheck(request, body.assistant_id)
    except HTTPException as exc:
        # 400/402 here are verdicts about the account, not failures of this
        # endpoint: the caller asked a question and this is the answer.
        if exc.status_code in (
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_402_PAYMENT_REQUIRED,
            status.HTTP_403_FORBIDDEN,
        ):
            return AuthorizeResponse(allowed=False, reason=str(exc.detail))
        raise

    return AuthorizeResponse(allowed=True, lease=secrets.token_urlsafe(16))


@router.post(
    "/llm/settle",
    response_model=SettleResponse,
    tags=["LLM Gateway"],
)
def settle(request: Request, body: SettleRequest) -> SettleResponse:
    """Charge a completed call the caller made with its own provider key.

    Deliberately not fire-and-forget on the caller's side: this is the only
    record that the call happened, so a caller that cannot reach it has to
    treat that as a failure to retry rather than a call that was free. The
    ledger write itself is idempotent only by lease, which is echoed from
    the authorising call so a settle can be traced to it.
    """
    ctx = _precheck_settle_context(request)
    cost = _price_usage_for(body.model, body.usage)
    if cost is None:
        logger.warning(
            "LLM gateway: settle carried no priceable usage (model=%s)",
            body.model,
        )
        return SettleResponse(charged=0.0, metered=False)

    _charge_usage(
        request.app.state.db_session_factory,
        ctx=ctx,
        user_id=getattr(request.state, "user_id", None),
        organization_id=getattr(request.state, "organization_id", None),
        assistant_id=body.assistant_id,
        model=body.model,
        raw_cost=cost,
    )
    charged = cost * float(settings.chat_completions_markup_rate)
    return SettleResponse(charged=charged, metered=bool(ctx.charges))


def _precheck_settle_context(request: Request) -> _BillingCtx:
    """Resolve the billing account for a settle, without re-gating the call.

    The balance and cap gates belong on the way in. Applying them here would
    refuse to record a call the provider has already served and already
    billed us for, which loses the charge precisely when the account is
    furthest overdrawn.
    """
    user_id = getattr(request.state, "user_id", None)
    organization_id = getattr(request.state, "organization_id", None)
    charges = settings.charges_billing

    with transient_request_db_session(request) as session:
        try:
            entity = get_billing_entity(session, user_id, organization_id)
        except ValueError:
            return _BillingCtx(billing_account_id=None, charges=False)
        return _BillingCtx(
            billing_account_id=int(entity.billing_account_id),
            charges=charges,
        )
