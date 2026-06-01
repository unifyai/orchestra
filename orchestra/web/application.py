"""Platform FastAPI application factory.

Layers platform-only middleware (rate limiting, staging gate, Sentry) on top
of the kernel middleware stack provided by orchestra-core, then mounts the
full platform router.
"""

import json as _json
import logging
import time as _time
from collections import defaultdict
from importlib import metadata

import sentry_sdk
from fastapi import FastAPI
from fastapi.responses import UJSONResponse
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from orchestra.pii_scrub import (
    install_log_redaction,
    scrub_sentry_breadcrumb,
    scrub_sentry_event,
)
from orchestra.settings import settings
from orchestra.web.api.router import api_router
from orchestra_core.observability.prometheus_middleware import metrics
from orchestra_core.web.application import core_middlewares
from orchestra.web.lifetime import register_shutdown_event, register_startup_event


def get_app() -> FastAPI:
    """
    Get FastAPI application.

    This is the main factory function for the platform server. The kernel
    middleware (CORS, security headers, prometheus, request trace) is
    applied via `core_middlewares`; this function adds the platform-only
    middleware (rate limiting, staging gate) and mounts the platform router.
    """
    import os

    # Enforce pseudonymisation at the observability periphery before anything
    # can emit a log line: scrub email/phone-shaped PII from every log record
    # process-wide (covers Cloud Logging via stdout). Idempotent.
    install_log_redaction()

    cloud_project = os.environ.get("GCP_PROJECT_ID", settings.gcp_project)
    managed_project = os.environ.get("ORCHESTRA_MANAGED_GCP_PROJECT", "gcp-project-saas")

    if os.environ.get("ON_PREM") and cloud_project == managed_project:
        raise RuntimeError(
            "ON_PREM must not be set in cloud deployments. "
            "This flag overrides GCP project/location settings "
            "and is only for self-hosted instances.",
        )

    if (
        os.environ.get("SKIP_STRIPE_SIGNATURE_VERIFICATION", "").lower() == "true"
        and cloud_project == managed_project
    ):
        raise RuntimeError(
            "SKIP_STRIPE_SIGNATURE_VERIFICATION must not be set in cloud deployments. "
            "This flag disables Stripe webhook security.",
        )

    if settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            traces_sample_rate=settings.sentry_sample_rate,
            environment=settings.environment,
            # Pseudonymisation procedure: never let the SDK attach default PII
            # (request bodies, cookies, user IP), and run every event /
            # breadcrumb through the PII scrubber before transmission.
            send_default_pii=False,
            before_send=scrub_sentry_event,
            before_breadcrumb=scrub_sentry_breadcrumb,
            integrations=[
                FastApiIntegration(transaction_style="endpoint"),
                LoggingIntegration(
                    level=logging.INFO,
                    event_level=logging.ERROR,
                ),
                SqlalchemyIntegration(),
            ],
        )
    app = FastAPI(
        title="UnifyAI HTTP API Reference",
        version=metadata.version("orchestra"),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        swagger_ui_parameters={"defaultModelsExpandDepth": -1},
        default_response_class=UJSONResponse,
    )

    core_middlewares(app)

    class RateLimitMiddleware(BaseHTTPMiddleware):
        """Limit requests per IP on sensitive paths (admin, webhooks, metrics)."""

        def __init__(self, app, max_requests: int = 60, window_seconds: int = 60):
            super().__init__(app)
            self.max_requests = max_requests
            self.window_seconds = window_seconds
            self._requests: dict[str, list[float]] = defaultdict(list)

        async def dispatch(self, request, call_next):
            if settings.is_staging or settings.environment == "dev":
                return await call_next(request)

            path = request.url.path
            if not (
                path.startswith("/v0/admin")
                or path == "/metrics"
                or path.startswith("/v0/webhooks")
            ):
                return await call_next(request)

            client_ip = request.client.host if request.client else "unknown"
            now = _time.monotonic()
            window_start = now - self.window_seconds
            timestamps = self._requests[client_ip]
            self._requests[client_ip] = [t for t in timestamps if t > window_start]
            if len(self._requests[client_ip]) >= self.max_requests:
                return JSONResponse(
                    {"detail": "Rate limit exceeded"},
                    status_code=429,
                    headers={"Retry-After": str(self.window_seconds)},
                )
            self._requests[client_ip].append(now)
            return await call_next(request)

    app.add_middleware(RateLimitMiddleware, max_requests=60, window_seconds=60)

    from orchestra.web.api.dependencies import is_staging_allowed_email

    class UnifyMembersOnlyMiddleware(BaseHTTPMiddleware):
        GATED_PATHS = frozenset(
            {
                "/v0/admin/auth/register",
                "/v0/admin/user",
            },
        )

        async def dispatch(self, request, call_next):
            if not settings.is_staging:
                return await call_next(request)
            if request.method != "POST" or request.url.path not in self.GATED_PATHS:
                return await call_next(request)

            body = await request.body()

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            request._receive = receive  # type: ignore[attr-defined]

            try:
                payload = _json.loads(body) if body else {}
            except (ValueError, TypeError):
                return await call_next(request)

            email = (payload.get("email") or "").strip().lower()
            if not is_staging_allowed_email(email):
                return JSONResponse(
                    {
                        "detail": (
                            "This environment is restricted to Unify AI members only."
                        ),
                    },
                    status_code=403,
                )
            return await call_next(request)

    app.add_middleware(UnifyMembersOnlyMiddleware)

    register_startup_event(app)
    register_shutdown_event(app)

    app.include_router(router=api_router, prefix="/v0")
    app.add_api_route("/metrics", metrics)

    return app
