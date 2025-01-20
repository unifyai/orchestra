import os
import time
from typing import Tuple

from fastapi import HTTPException
from opentelemetry import trace
from prometheus_client import REGISTRY, Counter, Gauge, Histogram
from prometheus_client.openmetrics.exposition import (
    CONTENT_TYPE_LATEST,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_500_INTERNAL_SERVER_ERROR
from starlette.types import ASGIApp

INFO = Gauge(
    "orchestra_app_info",
    "Orchestra application information.",
    ["app_name"],
)

REQUESTS = Counter(
    "orchestra_requests_total",
    "Total count of requests by method and path.",
    ["method", "path", "app_name"],
)

RESPONSES = Counter(
    "orchestra_responses_total",
    "Total count of responses by method, path and status codes.",
    ["method", "path", "status_code", "app_name"],
)

REQUESTS_PROCESSING_TIME = Histogram(
    "orchestra_requests_duration_seconds",
    "Histogram of requests processing time by path (in seconds)",
    ["method", "path", "app_name"],
)

EXCEPTIONS = Counter(
    "orchestra_exceptions_total",
    "Total count of exceptions raised by path and exception type",
    ["method", "path", "exception_type", "app_name"],
)

REQUESTS_IN_PROGRESS = Gauge(
    "orchestra_requests_in_progress",
    "Gauge of requests by method and path currently being processed",
    ["method", "path", "app_name"],
)


class PrometheusMiddleware(BaseHTTPMiddleware):
    """
    Middleware that collects and exposes Prometheus-style metrics about
    incoming requests to the Orchestra (FastAPI) application.
    """

    def __init__(self, app: ASGIApp, app_name: str = "orchestra") -> None:
        super().__init__(app)
        self.app_name = app_name
        # This sets a one-time gauge indicating the app is running
        INFO.labels(app_name=self.app_name).inc()

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        method = request.method
        path, is_handled_path = self.get_path(request)

        if not is_handled_path:
            # If it's not matched by a known route, skip metrics
            return await call_next(request)

        REQUESTS_IN_PROGRESS.labels(
            method=method,
            path=path,
            app_name=self.app_name,
        ).inc()

        REQUESTS.labels(
            method=method,
            path=path,
            app_name=self.app_name,
        ).inc()

        before_time = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as e:
            status_code = HTTP_500_INTERNAL_SERVER_ERROR
            EXCEPTIONS.labels(
                method=method,
                path=path,
                exception_type=type(e).__name__,
                app_name=self.app_name,
            ).inc()
            raise e
        else:
            status_code = response.status_code
            after_time = time.perf_counter()

            # Retrieve trace id (if using OpenTelemetry)
            span = trace.get_current_span()
            trace_id = trace.format_trace_id(span.get_span_context().trace_id)

            REQUESTS_PROCESSING_TIME.labels(
                method=method,
                path=path,
                app_name=self.app_name,
            ).observe(
                after_time - before_time,
                exemplar={"TraceID": trace_id},
            )
        finally:
            RESPONSES.labels(
                method=method,
                path=path,
                status_code=status_code,
                app_name=self.app_name,
            ).inc()

            REQUESTS_IN_PROGRESS.labels(
                method=method,
                path=path,
                app_name=self.app_name,
            ).dec()

        return response

    @staticmethod
    def get_path(request: Request) -> Tuple[str, bool]:
        """
        Try to match the request with one of the FastAPI routes.
        If it matches, return (route_path, True).
        Otherwise, return (request.url.path, False).
        """
        for route in request.app.routes:
            match, _child_scope = route.matches(request.scope)
            if match == Match.FULL:
                return route.path, True

        return request.url.path, False


def metrics(request: Request) -> Response:
    """
    Endpoint that returns the aggregated Prometheus metrics.
    Includes simple Bearer-token authentication to secure the endpoint.
    """
    # Bearer token required
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )

    incoming_token = auth_header[len("Bearer ") :]

    expected_token = os.getenv("PROMETHEUS_METRICS_TOKEN")
    if not expected_token or (incoming_token != expected_token):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    return Response(
        generate_latest(REGISTRY),
        media_type=CONTENT_TYPE_LATEST,
    )
