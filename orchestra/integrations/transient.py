"""Classify and retry transient provider-integration failures.

This is the shared policy for Orchestra ``run_tool`` (and any caller that wants
the same rules). Agents and task scripts must not reimplement provider flakiness
handling — one-shot ``execute`` is enough once this layer absorbs retries.

Applies to **every** provider adapter (Composio, Pipedream, …) that returns
``ProviderExecutionResult``.

Transient classes (retry):
- HTTP 408 / 429 / 500 / 502 / 503 / 504 (via ``provider_status_code`` or message)
- Transport / timeout / connection / empty-body style messages
- Explicit rate-limit / throttle wording
- GraphQL platform errors (``Something went wrong while executing your query``)
- Pipedream action-level failures embedded in an HTTP-ok body when attribution
  looks like network / upstream 5xx-or-429 (reads only)

Permanent / human-gated (do not retry):
- Auth (401), missing scope (403), connect / confirmation / policy envelopes
- Validation / not-found / component_code style failures

Write safety:
- ``read`` action class: retry transport errors and embedded-in-ok platform
  failures (GraphQL or Pipedream action errors).
- Other action classes: retry only clear pre-success transport/provider HTTP
  failures (never retry an ``ok`` payload that might have had side effects).
"""

from __future__ import annotations

import random
import re
import time
from typing import Any, Callable, Optional, TypeVar

T = TypeVar("T")

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_DELAY_SECONDS = 0.75
DEFAULT_MAX_DELAY_SECONDS = 20.0

_TRANSIENT_HTTP_CODES = frozenset({408, 429, 500, 502, 503, 504})

_TRANSIENT_MESSAGE_RE = re.compile(
    r"("
    r"rate\s*limit|too many requests|throttl(?:e|ed|ing)|retry[- ]?after|"
    r"timed?\s*out|timeout|temporar(?:y|ily)|unavailable|"
    r"connection\s*(?:reset|refused|aborted|error)|"
    r"broken\s*pipe|network|dns|name\s*resolution|"
    r"bad\s*gateway|gateway\s*timeout|service\s*unavailable|"
    r"internal\s*server(?:\s*error)?|server\s*error|"
    r"status\s*code\s*(?:408|429|500|502|503|504)|"
    r"expecting\s*value|empty\s*response|connection\s*broken|"
    r"something went wrong while executing your query|"
    r"this may be the result of a timeout|"
    r"could be a github bug"
    r")",
    re.IGNORECASE,
)

_PIPEDREAM_PERMANENT_ORIGINS = frozenset({"component_code", "response_parsing"})

_GRAPHQL_PLATFORM_RE = re.compile(
    r"something went wrong while executing your query",
    re.IGNORECASE,
)

_NON_RETRY_CODES = frozenset(
    {
        "connect_required",
        "reconnect_required",
        "missing_scope",
        "blocked_by_policy",
        "confirmation_required",
        "provider_not_configured",
        "provider_endpoint_not_configured",
        "provider_connection_missing",
        "action_disabled_by_overlay",
        "action_disabled_for_connection",
    },
)


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _message_blob(*parts: Any) -> str:
    return " ".join(str(p) for p in parts if p)


def is_transient_http_status(status_code: Any) -> bool:
    code = _as_int(status_code)
    return code in _TRANSIENT_HTTP_CODES if code is not None else False


def is_transient_message(message: Any) -> bool:
    text = str(message or "")
    if not text:
        return False
    return bool(_TRANSIENT_MESSAGE_RE.search(text))


def is_graphql_platform_error_message(message: Any) -> bool:
    return bool(_GRAPHQL_PLATFORM_RE.search(str(message or "")))


def extract_graphql_errors(result: Any) -> list[Any]:
    """Return GraphQL ``errors`` lists nested in common Composio/GitHub shapes."""

    if not isinstance(result, dict):
        return []
    found: list[Any] = []

    def _consume(node: Any) -> None:
        if not isinstance(node, dict):
            return
        errors = node.get("errors")
        if isinstance(errors, list) and errors:
            found.extend(errors)
        for key in ("data", "result", "response", "payload"):
            inner = node.get(key)
            if isinstance(inner, dict):
                _consume(inner)

    _consume(result)
    return found


def graphql_platform_errors(result: Any) -> list[Any]:
    """GraphQL errors that look like GitHub/platform transients (not NOT_FOUND)."""

    out: list[Any] = []
    for err in extract_graphql_errors(result):
        if isinstance(err, dict):
            if err.get("type") == "NOT_FOUND":
                continue
            message = err.get("message") or ""
            if "Could not resolve to a User" in str(message):
                continue
            if is_graphql_platform_error_message(message) or is_transient_message(
                message,
            ):
                out.append(err)
        elif is_transient_message(err):
            out.append(err)
    return out


def extract_pipedream_action_error(result: Any) -> dict[str, Any] | None:
    """Return Pipedream's embedded ``error`` object from an HTTP-ok action body."""

    if not isinstance(result, dict):
        return None
    err = result.get("error")
    if isinstance(err, dict) and err:
        return err
    # Some Connect responses nest the action envelope under ``data``.
    data = result.get("data")
    if isinstance(data, dict):
        nested = data.get("error")
        if isinstance(nested, dict) and nested:
            return nested
    return None


def is_transient_pipedream_action_error(error: dict[str, Any] | None) -> bool:
    """True when an embedded Pipedream action error looks retryable."""

    if not error or not isinstance(error, dict):
        return False
    attribution = error.get("attribution")
    if not isinstance(attribution, dict):
        attribution = {}
    origin = str(attribution.get("origin") or "").strip().lower()
    if origin in _PIPEDREAM_PERMANENT_ORIGINS:
        return False
    if origin == "network_io":
        return True
    if origin == "upstream_api":
        last_call = attribution.get("last_call")
        if isinstance(last_call, dict) and is_transient_http_status(
            last_call.get("status"),
        ):
            return True
    return is_transient_message(error.get("message"))


def pipedream_transient_action_errors(result: Any) -> list[dict[str, Any]]:
    err = extract_pipedream_action_error(result)
    if err is None or not is_transient_pipedream_action_error(err):
        return []
    return [err]


def embedded_transient_ok_failures(
    result: Any,
) -> tuple[str | None, list[Any]]:
    """Classify retryable failures embedded in an ``ok`` provider result.

    Returns ``(reason, details)`` or ``(None, [])``.
    """

    platform = graphql_platform_errors(result)
    if platform:
        return "transient_graphql_platform", platform
    pipedream = pipedream_transient_action_errors(result)
    if pipedream:
        return "transient_pipedream_action", pipedream
    return None, []


def is_transient_provider_error(error: dict[str, Any] | None) -> bool:
    """True when an adapter ``error`` dict should be retried."""

    if not error or not isinstance(error, dict):
        return False
    code = str(error.get("code") or "")
    if code in _NON_RETRY_CODES:
        return False
    if is_transient_http_status(error.get("provider_status_code")):
        return True
    # Pipedream historically returned only ``message`` (e.g. ``500 Server Error``).
    # Also accept bare status digits when adapters omit provider_status_code.
    blob = _message_blob(
        error.get("message"),
        error.get("provider_response_body"),
        code,
    )
    if is_transient_message(blob):
        return True
    if re.search(r"\b(408|429|500|502|503|504)\b", blob):
        return True
    return False


def is_safe_to_retry_ok_result(*, action_class: str | None) -> bool:
    """Only auto-retry ``ok`` payloads that embed transient failures for reads."""

    return (action_class or "read").lower() == "read"


def should_retry_adapter_result(
    *,
    action_class: str | None,
    status: str,
    error: dict[str, Any] | None,
    result: Any,
) -> tuple[bool, str | None]:
    """Return ``(retry?, reason)`` for one provider adapter outcome."""

    if status != "ok":
        if is_transient_provider_error(error):
            return True, "transient_provider_error"
        return False, None

    if not is_safe_to_retry_ok_result(action_class=action_class):
        return False, None
    reason, _details = embedded_transient_ok_failures(result)
    if reason:
        return True, reason
    return False, None


def compute_retry_delay_seconds(
    attempt_index: int,
    *,
    base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS,
    max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
    retry_after_seconds: float | None = None,
) -> float:
    """Exponential backoff with jitter; honors ``Retry-After`` when provided."""

    if retry_after_seconds is not None and retry_after_seconds >= 0:
        return min(float(retry_after_seconds), max_delay_seconds)
    exp = min(
        max_delay_seconds,
        base_delay_seconds * (2 ** max(0, attempt_index)),
    )
    jitter = random.uniform(0.0, min(0.35, exp * 0.25))
    return min(max_delay_seconds, exp + jitter)


def parse_retry_after_seconds(error: dict[str, Any] | None) -> float | None:
    if not error or not isinstance(error, dict):
        return None
    for key in ("retry_after", "retry_after_seconds"):
        value = error.get(key)
        parsed = _as_int(value)
        if parsed is not None and parsed >= 0:
            return float(parsed)
    headers = error.get("provider_response_headers")
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() == "retry-after":
                parsed = _as_int(value)
                if parsed is not None and parsed >= 0:
                    return float(parsed)
    body = str(error.get("provider_response_body") or "")
    match = re.search(r"retry[- ]after[:\s]+(\d+)", body, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None


def call_with_transient_retries(
    fn: Callable[[], T],
    *,
    should_retry: Callable[[T], tuple[bool, str | None]],
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, str | None, float], None] | None = None,
    retry_after_from_value: Callable[[T], float | None] | None = None,
) -> tuple[T, int]:
    """Run ``fn`` up to ``max_attempts``, returning ``(value, attempts_used)``."""

    attempts = max(1, int(max_attempts))
    last: T | None = None
    for index in range(attempts):
        value = fn()
        last = value
        retry, reason = should_retry(value)
        if not retry or index >= attempts - 1:
            return value, index + 1
        delay = compute_retry_delay_seconds(
            index,
            retry_after_seconds=(
                retry_after_from_value(value) if retry_after_from_value else None
            ),
        )
        if on_retry is not None:
            on_retry(index + 1, reason, delay)
        sleep(delay)
    assert last is not None
    return last, attempts
