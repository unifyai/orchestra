"""
Middleware to capture HTTP request details for OpenTelemetry tracing.

Adds request body, query parameters, and path parameters as span attributes,
making debugging easier by showing exactly what was sent to each endpoint.
"""

import json
import logging
from typing import Any

from opentelemetry import trace
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match

logger = logging.getLogger(__name__)

# Maximum size of request body to capture (to avoid memory issues with large uploads)
MAX_BODY_SIZE = 64 * 1024  # 64KB
# Maximum size for individual attribute values (OTel has limits)
MAX_ATTR_VALUE_SIZE = 8 * 1024  # 8KB


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
    Middleware that captures request details and adds them as OpenTelemetry span attributes.

    Captures:
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
        span = trace.get_current_span()

        # Only add attributes if we have an active span
        if span.is_recording():
            try:
                await self._add_request_attributes(request, span)
            except Exception as e:
                # Never fail the request due to tracing issues
                logger.debug(f"Failed to capture request attributes: {e}")

        return await call_next(request)

    async def _add_request_attributes(self, request: Request, span: Any) -> None:
        """Extract and add request details as span attributes."""

        # Query parameters
        if request.query_params:
            query_dict = dict(request.query_params)
            span.set_attribute(
                "http.request.query_params",
                _truncate(_safe_json_dumps(query_dict)),
            )

        # Path parameters (from route matching)
        path_params = self._get_path_params(request)
        if path_params:
            span.set_attribute(
                "http.request.path_params",
                _truncate(_safe_json_dumps(path_params)),
            )

        # Selected headers (useful for debugging, avoiding sensitive ones)
        headers_to_capture = ["content-type", "accept", "user-agent", "x-request-id"]
        captured_headers = {
            k: v for k, v in request.headers.items() if k.lower() in headers_to_capture
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

    async def _read_body(self, request: Request) -> str | None:
        """Read request body if it's JSON and not too large."""
        # Check content-length first to avoid reading huge bodies
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_BODY_SIZE:
                    return f"[body too large: {content_length} bytes]"
            except ValueError:
                pass

        try:
            # Read raw body bytes
            body_bytes = await request.body()

            if not body_bytes:
                return None

            if len(body_bytes) > MAX_BODY_SIZE:
                return f"[body too large: {len(body_bytes)} bytes]"

            # Try to parse and re-serialize for pretty formatting
            body_str = body_bytes.decode("utf-8")
            try:
                parsed = json.loads(body_str)
                # Re-serialize with indentation for readability
                return json.dumps(parsed, indent=2, default=str, ensure_ascii=False)
            except json.JSONDecodeError:
                # Return raw string if not valid JSON
                return body_str

        except Exception as e:
            logger.debug(f"Failed to read request body: {e}")
            return None

    @staticmethod
    def _get_path_params(request: Request) -> dict:
        """Extract path parameters from the matched route."""
        for route in request.app.routes:
            match, child_scope = route.matches(request.scope)
            if match == Match.FULL:
                return child_scope.get("path_params", {})
        return {}
