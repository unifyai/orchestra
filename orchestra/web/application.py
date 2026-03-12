import logging
from importlib import metadata

import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import UJSONResponse
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

from orchestra.settings import settings
from orchestra.web.api.router import api_router
from orchestra.web.api.utils.production_traffic_middleware import (
    ProductionTrafficMiddleware,
)
from orchestra.web.api.utils.prometheus_middleware import PrometheusMiddleware, metrics
from orchestra.web.api.utils.request_trace_middleware import RequestTraceMiddleware
from orchestra.web.lifetime import register_shutdown_event, register_startup_event


def get_app() -> FastAPI:
    """
    Get FastAPI application.

    This is the main factory function of the application.

    :return: application.
    """
    import os

    if os.environ.get("ON_PREM") and os.environ.get("GCP_PROJECT_ID") == "saas-368716":
        raise RuntimeError(
            "ON_PREM must not be set in cloud deployments. "
            "This flag disables authentication and is only for self-hosted instances.",
        )

    if (
        os.environ.get("SKIP_STRIPE_SIGNATURE_VERIFICATION", "").lower() == "true"
        and os.environ.get("GCP_PROJECT_ID") == "saas-368716"
    ):
        raise RuntimeError(
            "SKIP_STRIPE_SIGNATURE_VERIFICATION must not be set in cloud deployments. "
            "This flag disables Stripe webhook security.",
        )

    if settings.sentry_dsn:
        # Enables sentry integration.
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            traces_sample_rate=settings.sentry_sample_rate,
            environment=settings.environment,
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

    # IP-based rate limiting for admin and auth endpoints
    import time as _time
    from collections import defaultdict

    from starlette.middleware.base import BaseHTTPMiddleware

    class RateLimitMiddleware(BaseHTTPMiddleware):
        """Limits requests per IP on sensitive paths (admin, webhooks, metrics)."""

        def __init__(self, app, max_requests: int = 60, window_seconds: int = 60):
            super().__init__(app)
            self.max_requests = max_requests
            self.window_seconds = window_seconds
            self._requests: dict[str, list[float]] = defaultdict(list)

        async def dispatch(self, request, call_next):
            # Bypass rate limits in staging and dev environments
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
                from starlette.responses import JSONResponse

                return JSONResponse(
                    {"detail": "Rate limit exceeded"},
                    status_code=429,
                    headers={"Retry-After": str(self.window_seconds)},
                )
            self._requests[client_ip].append(now)
            return await call_next(request)

    app.add_middleware(RateLimitMiddleware, max_requests=60, window_seconds=60)

    # Security headers middleware

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
            # X-XSS-Protection deliberately omitted: the browser XSS Auditor
            # it controlled was removed from Chrome 78+ (2019) and was never
            # implemented in Firefox. Setting "1; mode=block" on legacy
            # browsers is itself exploitable (auditor can be abused to
            # selectively disable page scripts). CSP above is the modern
            # replacement. See https://owasp.org/www-project-secure-headers/
            return response

    app.add_middleware(SecurityHeadersMiddleware)
    # Add Prometheus metrics middleware
    app.add_middleware(
        PrometheusMiddleware,
        app_name="orchestra",
    )
    # Add Production Traffic middleware
    app.add_middleware(
        ProductionTrafficMiddleware,
    )
    # Add request tracing middleware (captures body/params for debugging)
    app.add_middleware(
        RequestTraceMiddleware,
    )
    # Register startup and shutdown events
    register_startup_event(app)
    register_shutdown_event(app)

    # Register API router
    app.include_router(router=api_router, prefix="/v0")

    # Add Prometheus metrics endpoint
    app.add_api_route("/metrics", metrics)

    return app
