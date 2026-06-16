"""Platform FastAPI application factory.

Layers platform-only middleware (staging gate, Sentry) on top of the kernel
middleware stack provided by orchestra, then mounts the full platform router.
"""

import json as _json
import logging

import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import UJSONResponse
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from orchestra.observability.prometheus_middleware import PrometheusMiddleware, metrics
from orchestra.observability.request_trace_middleware import RequestTraceMiddleware
from orchestra.pii_scrub import (
    install_log_redaction,
    scrub_sentry_breadcrumb,
    scrub_sentry_event,
)
from orchestra.settings import settings
from orchestra.web.api.router import api_router
from orchestra.web.lifetime import register_shutdown_event, register_startup_event


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'"
        )
        return response


def core_middlewares(app: FastAPI) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Accept",
            "Origin",
            "X-Requested-With",
        ],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(PrometheusMiddleware, app_name="orchestra")
    app.add_middleware(RequestTraceMiddleware)


def get_app() -> FastAPI:
    """
    Get FastAPI application.

    This is the main factory function for the platform server. The kernel
    middleware (CORS, security headers, prometheus, request trace) is
    applied via `core_middlewares`; this function adds the platform-only
    middleware (staging gate) and mounts the platform router.
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
        version="dev",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        swagger_ui_parameters={"defaultModelsExpandDepth": -1},
        default_response_class=UJSONResponse,
    )

    core_middlewares(app)

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
