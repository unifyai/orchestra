"""PII scrubbing for the observability periphery (Sentry + application logs).

This module enforces the pseudonymisation procedure at the points where
Customer Personal Data is at risk of leaving its primary processing purpose:
error/trace events sent to Sentry, and application log lines. See the brain
compliance docs (`documents/pseudonymisation-procedure.md`).

It lives in orchestra-platform, not orchestra-core, on purpose: Sentry and the
managed Cloud Logging pipeline are hosted-product concerns. The public
orchestra-core kernel carries no observability-sink credentials or
compliance configuration, and the one-way dependency (platform imports core,
never the reverse) keeps it that way.

The module is intentionally dependency-free (stdlib ``re``/``logging`` only) so
that it can be (a) imported anywhere in the platform without import-order
concerns, and (b) loaded by path by the compliance evidence generator, which
exercises the real redaction logic rather than re-implementing it.
"""

from __future__ import annotations

import logging
import re
from typing import Any

# The constant every redacted value collapses to. Lossy by design: there is no
# re-identification key for the scrubbed observability copy (the original value
# remains in Cloud SQL / GCS under its normal classification).
REDACTION = "[REDACTED]"

# Keys whose *values* are always redacted, regardless of value shape. Matched
# case-insensitively against dict keys and structured-log field names. This is
# the "deny-list of PII keys" referenced by the pseudonymisation procedure.
PII_KEY_DENYLIST: frozenset[str] = frozenset(
    {
        "email",
        "email_address",
        "e_mail",
        "phone",
        "phone_number",
        "telephone",
        "mobile",
        "msisdn",
        "name",
        "first_name",
        "last_name",
        "full_name",
        "username",
        "address",
        "street",
        "postal_code",
        "zip",
        "zipcode",
        "dob",
        "date_of_birth",
        "birthdate",
        "ssn",
        "national_id",
        "passport",
        "password",
        "secret",
        "token",
        "api_key",
        "authorization",
    },
)

# Value-shape patterns. Deliberately conservative so we do not scrub useful
# non-PII (opaque IDs, timestamps, trace IDs): an email pattern and an
# E.164 / separated-digit phone pattern, matching the procedure's
# "email-pattern, e.164-phone-pattern" description.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{6,}\d(?!\w)")

# Cap recursion so a pathological nested structure can never hang the hook.
_MAX_DEPTH = 8


def redact_text(value: str) -> str:
    """Replace email- and phone-shaped substrings with the redaction marker."""
    if not value:
        return value
    value = EMAIL_RE.sub(REDACTION, value)
    value = PHONE_RE.sub(REDACTION, value)
    return value


def _is_pii_key(key: Any) -> bool:
    return isinstance(key, str) and key.lower() in PII_KEY_DENYLIST


def redact_data(obj: Any, _depth: int = 0) -> Any:
    """Recursively redact a JSON-ish structure (dict / list / tuple / str).

    Dict values whose key is in :data:`PII_KEY_DENYLIST` are replaced wholesale;
    every string is run through :func:`redact_text` so shaped values are caught
    even when they appear under a non-PII key or inside free text.
    """
    if _depth > _MAX_DEPTH:
        return obj
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        redacted: dict[Any, Any] = {}
        for key, val in obj.items():
            if _is_pii_key(key):
                redacted[key] = REDACTION
            else:
                redacted[key] = redact_data(val, _depth + 1)
        return redacted
    if isinstance(obj, list):
        return [redact_data(v, _depth + 1) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact_data(v, _depth + 1) for v in obj)
    return obj


def scrub_sentry_event(event: Any, hint: Any = None) -> Any:
    """Sentry ``before_send`` hook — redact PII before the event is transmitted.

    Returns the (redacted) event so it is still sent; returning ``None`` would
    drop the event entirely, which we do not want. Never raises: a scrubber
    failure must not take down error reporting.
    """
    try:
        return redact_data(event)
    except Exception:  # noqa: BLE001 - never let scrubbing break reporting
        return event


def scrub_sentry_breadcrumb(crumb: Any, hint: Any = None) -> Any:
    """Sentry ``before_breadcrumb`` hook — redact PII from breadcrumbs."""
    try:
        return redact_data(crumb)
    except Exception:  # noqa: BLE001
        return crumb


def _redact_record_in_place(record: logging.LogRecord) -> None:
    if isinstance(record.msg, str):
        record.msg = redact_text(record.msg)
    if record.args:
        if isinstance(record.args, dict):
            record.args = {
                k: (redact_text(v) if isinstance(v, str) else v)
                for k, v in record.args.items()
            }
        else:
            record.args = tuple(
                redact_text(a) if isinstance(a, str) else a for a in record.args
            )


class PiiRedactionFilter(logging.Filter):
    """Logging filter that scrubs the rendered message text of a record.

    Attach to a handler when you want redaction scoped to that sink. For
    process-wide coverage (including third-party and orchestra-core loggers)
    prefer :func:`install_log_redaction`, which is handler-independent.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            _redact_record_in_place(record)
        except Exception:  # noqa: BLE001
            pass
        return True


_redaction_installed = False


def install_log_redaction() -> bool:
    """Install process-wide log redaction. Idempotent; returns True if newly set.

    Wraps the :class:`logging.LogRecord` factory so that every record created
    anywhere in the process has its message text scrubbed, regardless of which
    logger or handler emits it. This covers application logs that reach Cloud
    Logging (stdout on Cloud Run) without depending on a specific handler being
    configured. Structured correlation fields (e.g. ``request_id``) are left
    intact; the ``user_email`` correlation field is retained for the
    restricted-access log bucket, per the pseudonymisation procedure.
    """
    global _redaction_installed
    if _redaction_installed:
        return False
    old_factory = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = old_factory(*args, **kwargs)
        try:
            _redact_record_in_place(record)
        except Exception:  # noqa: BLE001
            pass
        return record

    logging.setLogRecordFactory(factory)
    _redaction_installed = True
    return True
