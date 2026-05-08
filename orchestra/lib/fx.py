"""Foreign-exchange conversion for multi-currency metered invoicing.

The metered invoicer needs to convert raw USD usage into the customer's
contract currency. The FX policy is per-template (see
:class:`orchestra.db.models.enums.FxPolicy`); this module
implements the live-fetch primitives the policy dispatcher consumes:

* :func:`fetch_spot` — single-date rate (used by ``FxPolicy.SPOT``).
* :func:`fetch_period_average` — average of business-day rates across a
  period (used by ``FxPolicy.PERIOD_AVERAGE``).

Both hit `Frankfurter <https://api.frankfurter.dev>`_ by default — a free,
no-API-key, ECB-sourced provider. The metered invoicer pins resolved
rates into ``Recharge.detail`` so re-runs are deterministic without a
snapshot table.

Same-currency calls short-circuit to a rate of ``1`` and never touch the
network. Provider failures raise :class:`FxProviderError`; the invoicer
treats this as a soft-skip for the affected account so a Frankfurter
outage delays a few invoices instead of blowing up the bulk run.

Resilience layer
----------------
A single bulk invoicer run typically asks for the same handful of
``(from, to, as_of)`` tuples once per non-USD account. To keep that
predictable in the face of transient Frankfurter blips we add two
narrowly-scoped guards on top of the raw HTTP call:

1. **Bounded retry with exponential backoff** — transient
   ``RequestException`` and ``5xx`` responses are retried up to
   :data:`_MAX_RETRIES` times with a short jittered backoff. The
   retry budget is kept low because the per-account skip path is
   already a safe fallback; we just don't want a 502 hiccup to skip
   an entire batch.
2. **Per-process in-memory cache** — successful resolutions are
   cached for the lifetime of the worker process, keyed on the full
   request shape. This makes a re-run of the bulk routine within a
   single process idempotent on the wire (no duplicate HTTP calls),
   and a re-run from a fresh process still hits the network so a
   stale cache can never persist across deploys. There is no
   long-lived disk snapshot; the invoicer pins resolved rates into
   ``Recharge.detail`` for replay.

The cache is intentionally process-local — no Redis, no shared
state. The bulk routine is single-process by design and the dataset
is tiny (one rate per non-USD currency per period).
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import random
import threading
import time
from decimal import Decimal
from typing import Optional

import requests

logger = logging.getLogger(__name__)


# Default upstream provider. Frankfurter is free, requires no API key,
# pulls from the ECB, and supports both single-date and time-series
# endpoints. The base URL is overridable via env so we can swap to a
# fallback (or a local mock in CI) without redeploying.
FRANKFURTER_BASE_URL = os.environ.get(
    "FRANKFURTER_BASE_URL",
    "https://api.frankfurter.dev/v1",
)
DEFAULT_PROVIDER = "frankfurter"

# Network call timeout. Frankfurter typically responds in <100ms; ten
# seconds is a generous upper bound for transient slowness without
# stalling the bulk invoicer.
_HTTP_TIMEOUT_SECONDS = 10

# Bounded retry budget — three attempts total (initial + 2 retries).
# Backoff schedule below caps total wait around ~3s in the worst case
# so a single recalcitrant lookup can't stall the bulk run.
_MAX_RETRIES = 2
_RETRY_BACKOFF_SECONDS = (0.4, 1.2)
_RETRY_JITTER_SECONDS = 0.2

# Process-local cache of resolved rates. Keys are tuples; values are
# either a ``Decimal`` (for ``fetch_spot``) or a
# ``(Decimal, list[date])`` pair (for ``fetch_period_average``).
# Reset across process restarts — there is intentionally no
# eviction; the hot keyspace is a few currencies per period.
_CACHE_LOCK = threading.Lock()
_SPOT_CACHE: dict[tuple[str, str, str, str], Decimal] = {}
_PERIOD_AVERAGE_CACHE: dict[
    tuple[str, str, str, str, str],
    tuple[Decimal, list[_dt.date]],
] = {}


def reset_run_cache() -> None:
    """Clear the in-memory FX cache.

    Tests call this between scenarios so a parameterised matrix can
    exercise the live-fetch path each iteration. Production callers
    don't need this — process restarts are the implicit eviction.
    """
    with _CACHE_LOCK:
        _SPOT_CACHE.clear()
        _PERIOD_AVERAGE_CACHE.clear()


def _is_retriable(exc: Exception) -> bool:
    """Return ``True`` for transport hiccups that warrant a retry.

    Connection / read-timeout / DNS errors are retried. HTTP 5xx
    responses are retried by virtue of ``raise_for_status`` raising
    ``HTTPError`` which we re-classify here. Application-level errors
    (4xx, missing rate keys, malformed JSON) are *not* retried —
    those are stable failures that won't fix themselves on the next
    request.
    """
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        return response is not None and 500 <= response.status_code < 600
    return False


def _sleep_for_retry(attempt: int) -> None:
    """Backoff with light jitter so concurrent runners don't lockstep."""
    base = _RETRY_BACKOFF_SECONDS[
        min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)
    ]
    jitter = random.uniform(0.0, _RETRY_JITTER_SECONDS)  # noqa: S311
    time.sleep(base + jitter)


def _http_get_json(url: str, params: dict, *, context: str) -> dict:
    """Issue a GET with retry on transient failures.

    Centralises the retry policy so both ``fetch_spot`` and
    ``fetch_period_average`` use the same budget + classification
    rules. Non-retriable failures bubble up unchanged for the caller
    to wrap in :class:`FxProviderError` with their own context.
    """
    attempts = _MAX_RETRIES + 1
    last_exc: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES and _is_retriable(exc):
                logger.warning(
                    "FX %s transient failure (attempt %d/%d): %s — retrying",
                    context,
                    attempt + 1,
                    attempts,
                    exc,
                )
                _sleep_for_retry(attempt)
                continue
            raise
    # Defensive — the loop above either returns or raises, but mypy
    # can't see that without help.
    assert last_exc is not None
    raise last_exc


class FxProviderError(Exception):
    """Raised when the upstream FX provider is unreachable or returns garbage.

    Caught by the metered invoicer and converted into a per-account skip
    (with the reason logged) rather than failing the whole bulk run.
    """


def fetch_spot(
    *,
    from_currency: str,
    to_currency: str,
    as_of: _dt.date,
    provider: Optional[str] = None,
) -> Decimal:
    """Return the FX rate for one pair on one date.

    Same-currency requests short-circuit to ``Decimal(1)``. Frankfurter
    returns the most recent business-day rate at-or-before ``as_of`` if
    that exact date isn't a publication day (weekends, holidays); we
    propagate the rate as-is since this matches ECB convention.

    Raises :class:`FxProviderError` on transport, parse, or upstream
    error response.
    """
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    if from_currency == to_currency:
        return Decimal(1)

    chosen_provider = (provider or DEFAULT_PROVIDER).lower()
    if chosen_provider != DEFAULT_PROVIDER:
        # Reserved for future fallback providers. Today only Frankfurter
        # is wired; failing loudly is safer than silently downgrading.
        raise FxProviderError(
            f"Unsupported FX provider: {chosen_provider!r}. "
            f"Only {DEFAULT_PROVIDER!r} is implemented.",
        )

    cache_key = (chosen_provider, from_currency, to_currency, as_of.isoformat())
    with _CACHE_LOCK:
        cached = _SPOT_CACHE.get(cache_key)
    if cached is not None:
        logger.debug("FX spot cache hit for %s", cache_key)
        return cached

    url = f"{FRANKFURTER_BASE_URL}/{as_of.isoformat()}"
    try:
        payload = _http_get_json(
            url,
            params={"base": from_currency, "symbols": to_currency},
            context=f"spot {from_currency}->{to_currency}@{as_of.isoformat()}",
        )
    except requests.RequestException as exc:
        raise FxProviderError(
            f"FX provider request failed for {from_currency}->{to_currency} "
            f"on {as_of.isoformat()}: {type(exc).__name__}: {exc}",
        ) from exc
    except ValueError as exc:
        raise FxProviderError(
            f"FX provider returned non-JSON for {from_currency}->{to_currency} "
            f"on {as_of.isoformat()}: {exc}",
        ) from exc

    rates = payload.get("rates", {}) if isinstance(payload, dict) else {}
    raw = rates.get(to_currency)
    if raw is None:
        raise FxProviderError(
            f"FX provider response missing {to_currency} for "
            f"{from_currency}->{to_currency} on {as_of.isoformat()}: {payload!r}",
        )
    try:
        rate = Decimal(str(raw))
    except (TypeError, ValueError) as exc:
        raise FxProviderError(
            f"Unparseable rate {raw!r} for {from_currency}->{to_currency} "
            f"on {as_of.isoformat()}",
        ) from exc
    if rate <= 0:
        raise FxProviderError(
            f"Non-positive rate {rate} for {from_currency}->{to_currency} "
            f"on {as_of.isoformat()}",
        )
    with _CACHE_LOCK:
        _SPOT_CACHE[cache_key] = rate
    return rate


def fetch_period_average(
    *,
    from_currency: str,
    to_currency: str,
    start: _dt.date,
    end: _dt.date,
    provider: Optional[str] = None,
) -> tuple[Decimal, list[_dt.date]]:
    """Return ``(average_rate, business_dates_used)`` for a period.

    Frankfurter's time-series endpoint returns one rate per ECB
    publication day in ``[start, end]`` (inclusive). We average them
    arithmetically. The returned date list is what gets pinned into
    ``Recharge.detail`` so a re-run can verify the same dates were used.

    Same-currency requests short-circuit to ``(Decimal(1), [])``.
    Raises :class:`FxProviderError` on any upstream issue, including
    "no business days in range" (a one-day weekend window with no
    holiday rollover would qualify).
    """
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    if from_currency == to_currency:
        return Decimal(1), []
    if end < start:
        raise ValueError(f"period end {end} precedes start {start}")

    chosen_provider = (provider or DEFAULT_PROVIDER).lower()
    if chosen_provider != DEFAULT_PROVIDER:
        raise FxProviderError(
            f"Unsupported FX provider: {chosen_provider!r}. "
            f"Only {DEFAULT_PROVIDER!r} is implemented.",
        )

    cache_key = (
        chosen_provider,
        from_currency,
        to_currency,
        start.isoformat(),
        end.isoformat(),
    )
    with _CACHE_LOCK:
        cached = _PERIOD_AVERAGE_CACHE.get(cache_key)
    if cached is not None:
        logger.debug("FX period-average cache hit for %s", cache_key)
        # Defensive copy of the date list so callers mutating the
        # returned value can't poison the cache.
        rate, dates = cached
        return rate, list(dates)

    url = f"{FRANKFURTER_BASE_URL}/{start.isoformat()}..{end.isoformat()}"
    try:
        payload = _http_get_json(
            url,
            params={"base": from_currency, "symbols": to_currency},
            context=(
                f"period {from_currency}->{to_currency} "
                f"{start.isoformat()}..{end.isoformat()}"
            ),
        )
    except requests.RequestException as exc:
        raise FxProviderError(
            f"FX provider request failed for {from_currency}->{to_currency} "
            f"period {start.isoformat()}..{end.isoformat()}: "
            f"{type(exc).__name__}: {exc}",
        ) from exc
    except ValueError as exc:
        raise FxProviderError(
            f"FX provider returned non-JSON for {from_currency}->{to_currency} "
            f"period {start.isoformat()}..{end.isoformat()}: {exc}",
        ) from exc

    series = payload.get("rates", {}) if isinstance(payload, dict) else {}
    if not isinstance(series, dict) or not series:
        raise FxProviderError(
            f"No rates returned for {from_currency}->{to_currency} "
            f"period {start.isoformat()}..{end.isoformat()}",
        )

    samples: list[tuple[_dt.date, Decimal]] = []
    for date_str, rates in sorted(series.items()):
        if not isinstance(rates, dict):
            continue
        raw = rates.get(to_currency)
        if raw is None:
            continue
        try:
            rate = Decimal(str(raw))
        except (TypeError, ValueError):
            continue
        if rate <= 0:
            continue
        try:
            d = _dt.date.fromisoformat(date_str)
        except ValueError:
            continue
        samples.append((d, rate))

    if not samples:
        raise FxProviderError(
            f"FX provider returned no usable samples for "
            f"{from_currency}->{to_currency} between "
            f"{start.isoformat()} and {end.isoformat()}",
        )

    total = sum((s[1] for s in samples), Decimal(0))
    average = (total / Decimal(len(samples))).quantize(Decimal("0.00000001"))
    dates = [s[0] for s in samples]
    with _CACHE_LOCK:
        _PERIOD_AVERAGE_CACHE[cache_key] = (average, list(dates))
    return average, dates
