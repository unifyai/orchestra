"""
Middleware to capture HTTP request details for OpenTelemetry tracing.

Creates a synthetic "http.request_received" span that completes immediately,
carrying all request parameters. This ensures request details are available
in trace files even while the request is still in-flight.
"""

import json
import logging
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import SpanKind
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match

logger = logging.getLogger(__name__)

# Maximum size of request body to capture (to avoid memory issues with large uploads)
MAX_BODY_SIZE = 64 * 1024  # 64KB
# Maximum size for individual attribute values (OTel has limits)
MAX_ATTR_VALUE_SIZE = 8 * 1024  # 8KB

# Tracer for creating the synthetic request span
_tracer = trace.get_tracer(__name__)


def _truncate(value: str, max_len: int = MAX_ATTR_VALUE_SIZE) -> str:
    """Truncate string if too long, adding indicator."""
    if len(value) <= max_len:
        return value
    return value[: max_len - 20] + f"... [truncated, {len(value)} total]"


def _safe_json_dumps(obj: Any) -> str:
    """Safely serialize object to JSON string."""
    try:
        return json.dumps(obj, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)


class RequestTraceMiddleware(BaseHTTPMiddleware):
    """
    Middleware that creates a synthetic span with request details.

    Creates an "http.request_received" span that completes immediately,
    ensuring request parameters are available in trace files even while
    the main request is still processing. This enables in-flight debugging
    of long-running requests.

    The span captures:
    - http.request.method: HTTP method
    - http.request.path: Request path
    - http.request.query_params: Query string parameters as JSON
    - http.request.path_params: Path parameters as JSON
    - http.request.body: Request body (for JSON content types, truncated if large)
    - http.request.headers: Selected headers (content-type, accept, user-agent)
    """

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        try:
            await self._create_request_received_span(request)
        except Exception as e:
            # Never fail the request due to tracing issues
            logger.debug(f"Failed to create request_received span: {e}")

        return await call_next(request)

    async def _create_request_received_span(self, request: Request) -> None:
        """Create a synthetic span that completes immediately with request details."""
        # Get route info for the span name
        route, path_params = self._get_route_info(request)
        route_display = route or request.url.path

        # Create a span that starts and ends immediately
        # This exports right away, making request params visible in in-progress traces
        with _tracer.start_as_current_span(
            f"http.request_received {request.method} {route_display}",
            kind=SpanKind.INTERNAL,
        ) as span:
            # Basic request info
            span.set_attribute("http.request.method", request.method)
            span.set_attribute("http.request.path", request.url.path)
            if route:
                span.set_attribute("http.request.route", route)

            # Query parameters
            if request.query_params:
                query_dict = dict(request.query_params)
                span.set_attribute(
                    "http.request.query_params",
                    _truncate(_safe_json_dumps(query_dict)),
                )

            # Path parameters
            if path_params:
                span.set_attribute(
                    "http.request.path_params",
                    _truncate(_safe_json_dumps(path_params)),
                )

            # Selected headers
            headers_to_capture = [
                "content-type",
                "accept",
                "user-agent",
                "x-request-id",
            ]
            captured_headers = {
                k: v
                for k, v in request.headers.items()
                if k.lower() in headers_to_capture
            }
            if captured_headers:
                span.set_attribute(
                    "http.request.headers",
                    _safe_json_dumps(captured_headers),
                )

            # Request body (only for JSON content types)
            content_type = request.headers.get("content-type", "")
            if "application/json" in content_type:
                body = await self._read_body(request)
                if body:
                    span.set_attribute("http.request.body", _truncate(body))

            # Span ends here and exports immediately!

    async def _read_body(self, request: Request) -> str | None:
        """Read request body if it's JSON and not too large."""
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_BODY_SIZE:
                    return f"[body too large: {content_length} bytes]"
            except ValueError:
                pass

        try:
            body_bytes = await request.body()

            if not body_bytes:
                return None

            if len(body_bytes) > MAX_BODY_SIZE:
                return f"[body too large: {len(body_bytes)} bytes]"

            body_str = body_bytes.decode("utf-8")
            try:
                parsed = json.loads(body_str)
                return json.dumps(parsed, indent=2, default=str, ensure_ascii=False)
            except json.JSONDecodeError:
                return body_str

        except Exception as e:
            logger.debug(f"Failed to read request body: {e}")
            return None

    @staticmethod
    def _get_route_info(request: Request) -> tuple[str | None, dict]:
        """Extract route pattern and path parameters from the matched route."""
        for route in request.app.routes:
            match, child_scope = route.matches(request.scope)
            if match == Match.FULL:
                return route.path, child_scope.get("path_params", {})
        return None, {}
