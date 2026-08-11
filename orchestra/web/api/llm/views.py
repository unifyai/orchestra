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
from orchestra.web.api.llm.schema import ChatCompletionRequest
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
