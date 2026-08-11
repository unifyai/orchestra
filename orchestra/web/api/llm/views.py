"""LLM gateway routes.

Assistant containers call this instead of talking to OpenRouter directly, so
the provider key never leaves Orchestra. Every call is authenticated with the
caller's Unify API key, gated on the billing balance *before* the provider is
touched, metered from OpenRouter's authoritative ``usage.cost``, and settled
against the credit ledger with the platform markup.

v1 proxies OpenRouter models only (``openai/…@openrouter`` or bare
``openai/…``). Other providers still route through unillm until the gateway is
extended; see the LLM-gateway design spec.
"""

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.dependencies import transient_request_db_session
from orchestra.lib.billing import get_billing_entity
from orchestra.settings import settings
from orchestra.web.api.llm.schema import (
    AuthorizeRequest,
    AuthorizeResponse,
    ChatCompletionRequest,
    SettleRequest,
    SettleResponse,
)
from orchestra.web.api.utils.http_client import get_async_client

router = APIRouter()
logger = logging.getLogger(__name__)

_OPENROUTER_SUFFIX = "@openrouter"
_LLM_CATEGORY = "llm"
# Read timeout must comfortably exceed slow-brain completions; connect stays
# short so a provider outage fails fast rather than hanging the caller.
_UPSTREAM_TIMEOUT = httpx.Timeout(600.0, connect=10.0)
# Bounded tail retained while streaming so the terminal usage chunk can be
# parsed without buffering the whole response in memory.
_USAGE_TAIL_LIMIT = 16_384


@dataclass(frozen=True)
class _BillingCtx:
    """Scalars captured while the DB session is open, safe to use after close."""

    billing_account_id: Optional[int]
    charges: bool


def _normalize_model(model: str) -> str:
    """Map a UniLLM endpoint string to an OpenRouter model id.

    ``openai/gpt-5.6-sol@openrouter`` -> ``openai/gpt-5.6-sol``; a bare id is
    returned unchanged. A non-OpenRouter provider suffix (e.g. ``@vertex-ai``)
    is rejected — the gateway does not hold those keys yet.
    """
    model = model.strip()
    if model.endswith(_OPENROUTER_SUFFIX):
        return model[: -len(_OPENROUTER_SUFFIX)]
    if "@" in model:
        provider = model.rsplit("@", 1)[1]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Provider '{provider}' is not available through the LLM "
                "gateway yet. Only OpenRouter models are supported."
            ),
        )
    return model


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


def _require_anthropic_key() -> str:
    key = settings.routing_anthropic_api_key
    if not key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM gateway is not configured for Anthropic (no provider key).",
        )
    return key


def _require_openrouter_key() -> str:
    key = settings.openrouter_api_key
    if not key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM gateway is not configured (no provider key).",
        )
    return key


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


def _build_payload(body: ChatCompletionRequest) -> dict[str, Any]:
    """Forward the request verbatim, normalising the model and forcing usage
    accounting so the provider returns ``usage.cost`` for metering."""
    payload = body.model_dump(exclude_none=True)
    payload.pop("assistant_id", None)
    payload["model"] = _normalize_model(body.model)
    # Ask OpenRouter to include cost in the response (and, for streams, in a
    # terminal usage chunk) so we can meter from the authoritative number.
    payload["usage"] = {"include": True}
    if body.stream:
        payload["stream"] = True
        stream_options = dict(payload.get("stream_options") or {})
        stream_options["include_usage"] = True
        payload["stream_options"] = stream_options
    return payload


@router.post("/llm/chat/completions", tags=["LLM Gateway"])
async def chat_completions(request: Request, body: ChatCompletionRequest):
    """OpenAI-compatible chat completions, brokered through Orchestra.

    Point any OpenAI-compatible client (unillm, the OpenAI SDK, litellm) at
    ``<orchestra>/v0/llm`` with a Unify API key as the bearer token.
    """
    key = _require_openrouter_key()
    ctx = _precheck(request, body.assistant_id)

    user_id = getattr(request.state, "user_id", None)
    organization_id = getattr(request.state, "organization_id", None)
    assistant_id = body.assistant_id
    payload = _build_payload(body)
    model = payload["model"]

    url = f"{settings.openrouter_api_base.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    client = get_async_client()

    if not body.stream:
        try:
            resp = await client.post(
                url,
                headers=headers,
                json=payload,
                timeout=_UPSTREAM_TIMEOUT,
            )
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Upstream LLM provider error: {exc}",
            ) from exc

        data = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            # Never surface the provider response with our key context; forward
            # the status and the provider's own error body.
            return JSONResponse(status_code=resp.status_code, content=data)

        cost = _usage_cost(data.get("usage"))
        if cost is not None:
            _charge_usage(
                request.app.state.db_session_factory,
                ctx=ctx,
                user_id=user_id,
                organization_id=organization_id,
                assistant_id=assistant_id,
                model=model,
                raw_cost=cost,
            )
        return JSONResponse(status_code=resp.status_code, content=data)

    # Streaming: pass provider bytes straight through, sniff the terminal usage
    # chunk from a bounded tail, and settle once the stream completes.
    session_factory = request.app.state.db_session_factory

    async def _proxy_stream() -> AsyncIterator[bytes]:
        tail = ""
        try:
            async with client.stream(
                "POST",
                url,
                headers=headers,
                json=payload,
                timeout=_UPSTREAM_TIMEOUT,
            ) as upstream:
                if upstream.status_code >= 400:
                    err = await upstream.aread()
                    yield err
                    return
                async for chunk in upstream.aiter_bytes():
                    yield chunk
                    tail = (tail + chunk.decode("utf-8", "ignore"))[-_USAGE_TAIL_LIMIT:]
        except httpx.RequestError as exc:
            logger.warning("LLM gateway stream error: %s", exc)
            return

        cost = _cost_from_stream_tail(tail)
        if cost is not None:
            _charge_usage(
                session_factory,
                ctx=ctx,
                user_id=user_id,
                organization_id=organization_id,
                assistant_id=assistant_id,
                model=model,
                raw_cost=cost,
            )

    return StreamingResponse(_proxy_stream(), media_type="text/event-stream")


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


@router.post("/llm/anthropic/v1/messages", tags=["LLM Gateway"])
async def anthropic_messages(request: Request):
    """Anthropic's Messages API, brokered through Orchestra.

    Deliberately a verbatim proxy of Anthropic's own protocol rather than an
    OpenAI-shaped endpoint that translates. Anthropic publishes no
    OpenAI-compatible surface, so translating would mean owning a mapping of
    messages, system blocks, tools, and two different SSE grammars on the
    inference path for every Claude call -- a large surface whose failures
    would be ours and would look like model misbehaviour. Forwarding the
    bytes keeps this leg the same shape as the OpenRouter one: hold the key,
    gate the spend, meter the result.

    Callers reach it by pointing an Anthropic client's ``api_base`` here; the
    client's own credential is ignored and replaced, since the point is that
    it does not have one.
    """
    key = _require_anthropic_key()

    body = await request.json()
    model = str(body.get("model") or "").strip()
    if not model:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="`model` is required.",
        )

    # Priced before the provider is touched. A model we cannot price is
    # refused rather than served: serving it would be unmetered inference,
    # which is the exact failure this gateway exists to prevent.
    rates = _anthropic_token_rates(model)
    if rates is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Model '{model}' is not available through the LLM gateway. "
                "Only curated Anthropic models can be metered."
            ),
        )

    assistant_id = body.pop("assistant_id", None)
    if assistant_id is not None:
        assistant_id = int(assistant_id)
    ctx = _precheck(request, assistant_id)

    user_id = getattr(request.state, "user_id", None)
    organization_id = getattr(request.state, "organization_id", None)
    stream = bool(body.get("stream"))

    url = f"{settings.anthropic_api_base.rstrip('/')}/v1/messages"
    # The caller's inbound credential is never forwarded: it authenticated to
    # us, and Anthropic must see ours instead.
    headers = {
        "x-api-key": key,
        "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
        "content-type": "application/json",
        "accept": "text/event-stream" if stream else "application/json",
    }
    beta = request.headers.get("anthropic-beta")
    if beta:
        headers["anthropic-beta"] = beta
    client = get_async_client()

    if not stream:
        try:
            resp = await client.post(
                url,
                headers=headers,
                json=body,
                timeout=_UPSTREAM_TIMEOUT,
            )
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Upstream LLM provider error: {exc}",
            ) from exc

        data = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            return JSONResponse(status_code=resp.status_code, content=data)

        cost = _anthropic_usage_cost(data.get("usage"), rates)
        if cost is not None:
            _charge_usage(
                request.app.state.db_session_factory,
                ctx=ctx,
                user_id=user_id,
                organization_id=organization_id,
                assistant_id=assistant_id,
                model=model,
                raw_cost=cost,
            )
        return JSONResponse(status_code=resp.status_code, content=data)

    session_factory = request.app.state.db_session_factory

    async def _proxy_stream() -> AsyncIterator[bytes]:
        tail = ""
        try:
            async with client.stream(
                "POST",
                url,
                headers=headers,
                json=body,
                timeout=_UPSTREAM_TIMEOUT,
            ) as upstream:
                if upstream.status_code >= 400:
                    yield await upstream.aread()
                    return
                async for chunk in upstream.aiter_bytes():
                    yield chunk
                    tail = (tail + chunk.decode("utf-8", "ignore"))[-_USAGE_TAIL_LIMIT:]
        except httpx.RequestError as exc:
            logger.warning("LLM gateway stream error (anthropic): %s", exc)
            return

        cost = _anthropic_cost_from_stream_tail(tail, rates)
        if cost is not None:
            _charge_usage(
                session_factory,
                ctx=ctx,
                user_id=user_id,
                organization_id=organization_id,
                assistant_id=assistant_id,
                model=model,
                raw_cost=cost,
            )

    return StreamingResponse(_proxy_stream(), media_type="text/event-stream")


def _anthropic_cost_from_stream_tail(
    tail: str,
    rates: tuple[float, float],
) -> Optional[float]:
    """Price the terminal usage of an Anthropic SSE stream.

    Anthropic splits usage across the stream: ``message_start`` carries the
    input tokens and the final ``message_delta`` carries the output count.
    Only the tail is retained, so the two are merged as they are found
    rather than assumed to arrive together.
    """
    merged: dict[str, Any] = {}
    for line in tail.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        for source in (obj.get("usage"), (obj.get("message") or {}).get("usage")):
            if isinstance(source, dict):
                for name, value in source.items():
                    if isinstance(value, (int, float)):
                        merged[name] = value
    return _anthropic_usage_cost(merged, rates) if merged else None


def _cost_from_stream_tail(tail: str) -> Optional[float]:
    """Scan buffered SSE ``data:`` lines for the terminal usage cost."""
    cost: Optional[float] = None
    for line in tail.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        found = _usage_cost(obj.get("usage"))
        if found is not None:
            cost = found
    return cost


@router.get("/llm/models", tags=["LLM Gateway"])
async def list_models(request: Request):
    """List available models — proxied from OpenRouter with no key exposed.

    Lets container code discover routable models without holding a provider
    key (the old ``list_llms()`` path read keys straight from the env).
    """
    key = _require_openrouter_key()
    url = f"{settings.openrouter_api_base.rstrip('/')}/models"
    client = get_async_client()
    try:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=httpx.Timeout(30.0),
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Upstream LLM provider error: {exc}",
        ) from exc
    return JSONResponse(
        status_code=resp.status_code,
        content=resp.json() if resp.content else {},
    )
