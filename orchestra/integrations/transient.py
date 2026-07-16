"""Classify and retry transient provider-integration failures.

This is the shared policy for Orchestra ``run_tool`` (and any caller that wants
the same rules). Agents and task scripts must not reimplement provider flakiness
handling — one-shot ``execute`` is enough once this layer absorbs retries.

Transient classes (retry):
- HTTP 408 / 429 / 500 / 502 / 503 / 504
- Transport / timeout / connection / empty-body style messages
- Explicit rate-limit wording
- GraphQL platform errors (``Something went wrong while executing your query``)

Permanent / human-gated (do not retry):
- Auth (401), missing scope (403), connect / confirmation / policy envelopes
- Validation / not-found style failures

Write safety:
- ``read`` action class: retry both transport errors and GraphQL-in-result
  platform failures.
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
    r"rate\s*limit|too many requests|retry[- ]?after|"
    r"timed?\s*out|timeout|temporar(?:y|ily)|unavailable|"
    r"connection\s*(?:reset|refused|aborted|error)|"
    r"broken\s*pipe|network|dns|name\s*resolution|"
    r"bad\s*gateway|gateway\s*timeout|service\s*unavailable|"
    r"expecting\s*value|empty\s*response|connection\s*broken|"
    r"something went wrong while executing your query|"
    r"this may be the result of a timeout|"
    r"could be a github bug"
    r")",
    re.IGNORECASE,
)

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


def is_transient_provider_error(error: dict[str, Any] | None) -> bool:
    """True when an adapter ``error`` dict should be retried."""

    if not error or not isinstance(error, dict):
        return False
    code = str(error.get("code") or "")
    if code in _NON_RETRY_CODES:
        return False
    if is_transient_http_status(error.get("provider_status_code")):
        return True
    blob = _message_blob(
        error.get("message"),
        error.get("provider_response_body"),
        code,
    )
    return is_transient_message(blob)


def is_safe_to_retry_ok_result(*, action_class: str | None) -> bool:
    """Only auto-retry ``ok`` payloads that embed transient GraphQL errors for reads."""

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
    platform = graphql_platform_errors(result)
    if platform:
        return True, "transient_graphql_platform"
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
