"""Shared httpx.AsyncClient singleton for outbound HTTP calls.

Provides a process-wide async client with connection pooling and
connection-level retries.  All outbound calls to Communication,
Adapters, and other services should use ``get_async_client()``
instead of creating a throwaway ``httpx.AsyncClient()`` per request.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def get_async_client() -> httpx.AsyncClient:
    """Return the shared ``httpx.AsyncClient``, creating it on first use.

    The client is configured with:

    * **Connection pooling** -- httpx defaults (100 total / 20 per host)
    * **Connection-level retries** -- 3 retries on TCP/TLS failures
    * **Default timeout** -- 20 s (callers can override per-request)
    """
    global _client
    if _client is None:
        transport = httpx.AsyncHTTPTransport(retries=3)
        _client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(20.0),
        )
        logger.info("Shared async HTTP client initialized")
    return _client


async def close_async_client() -> None:
    """Close the shared client (call during application shutdown)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
        logger.info("Shared async HTTP client closed")
