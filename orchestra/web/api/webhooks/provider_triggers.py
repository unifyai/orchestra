"""Public signed ingress for provider-event trigger deliveries."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.provider_triggers.ingress_acceptance import (
    IngressAuthenticationError,
    IngressRetryableError,
    process_provider_webhook_delivery,
)
from orchestra.provider_triggers.ingress_rate_limit import get_ingress_rate_limiter
from orchestra.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/webhooks/integrations/{backend_id}/{ingress_key}",
    include_in_schema=False,
)
async def provider_trigger_webhook(
    backend_id: str,
    ingress_key: str,
    request: Request,
    session: Session = Depends(get_db_session),
) -> JSONResponse:
    """Receive one signed provider delivery and durably accept or ignore it."""

    limiter = get_ingress_rate_limiter(
        limit_per_minute=settings.provider_trigger_ingress_rate_limit_per_minute,
    )
    if not limiter.allow(backend_id=backend_id, ingress_key=ingress_key):
        logger.info(
            {
                "event": "provider_trigger_ingress_rate_limited",
                "backend_id": backend_id,
            },
        )
        raise HTTPException(status_code=429, detail="rate_limited")

    max_bytes = settings.provider_trigger_ingress_max_body_bytes
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(status_code=413, detail="body_too_large")
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="invalid_content_length",
            ) from None
    body = await request.body()
    if len(body) > max_bytes:
        logger.info(
            {
                "event": "provider_trigger_ingress_body_too_large",
                "backend_id": backend_id,
                "size_bytes": len(body),
                "max_bytes": max_bytes,
            },
        )
        raise HTTPException(status_code=413, detail="body_too_large")

    headers = {key: value for key, value in request.headers.items()}
    try:
        result = process_provider_webhook_delivery(
            session,
            backend_id=backend_id,
            ingress_key=ingress_key,
            headers=headers,
            raw_body=body,
        )
        session.commit()
    except IngressAuthenticationError:
        session.rollback()
        raise HTTPException(status_code=401, detail="authentication_failed") from None
    except IngressRetryableError as exc:
        session.rollback()
        logger.exception(
            {
                "event": "provider_trigger_ingress_retryable",
                "backend_id": backend_id,
                "error": str(exc),
            },
        )
        raise HTTPException(status_code=503, detail="retryable_failure") from None
    except Exception:
        session.rollback()
        logger.exception(
            {
                "event": "provider_trigger_ingress_error",
                "backend_id": backend_id,
            },
        )
        raise HTTPException(status_code=503, detail="retryable_failure") from None

    return JSONResponse(
        content={
            "status": result.status,
            "receipt_id": result.receipt_id,
            "classification_reason": result.classification_reason,
        },
        status_code=200,
    )
