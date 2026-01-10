"""
Inactivity-based automatic shutdown for local development.

When ORCHESTRA_INACTIVITY_TIMEOUT_SECONDS is set, the server will automatically
shut down after that many seconds without receiving any API requests. This prevents
the server from quietly consuming CPU resources when forgotten.
"""

import asyncio
import logging
import os
import signal
import time
from typing import Optional

from orchestra.settings import settings

logger = logging.getLogger(__name__)

# Monotonic timestamp of last API activity
_last_activity_time: float = time.monotonic()

# Reference to the monitor task for cleanup
_monitor_task: Optional[asyncio.Task] = None


def record_activity() -> None:
    """
    Record that API activity occurred.

    Call this from middleware when a real API request is processed.
    """
    global _last_activity_time
    _last_activity_time = time.monotonic()


def get_seconds_since_activity() -> float:
    """Return seconds elapsed since last recorded activity."""
    return time.monotonic() - _last_activity_time


async def _inactivity_monitor_loop(timeout_seconds: int) -> None:
    """
    Background loop that checks for inactivity and triggers shutdown.

    Checks every 30 seconds (or timeout/4, whichever is smaller) whether
    the inactivity threshold has been exceeded.
    """
    check_interval = min(30, timeout_seconds / 4)
    logger.info(
        f"Inactivity monitor started: will shut down after {timeout_seconds}s "
        f"of no API requests (checking every {check_interval:.0f}s)",
    )

    while True:
        await asyncio.sleep(check_interval)

        elapsed = get_seconds_since_activity()
        if elapsed >= timeout_seconds:
            logger.warning(
                f"No API requests for {elapsed:.0f}s (threshold: {timeout_seconds}s). "
                "Initiating shutdown due to inactivity.",
            )
            # Send SIGTERM to trigger graceful uvicorn shutdown
            os.kill(os.getpid(), signal.SIGTERM)
            return


def start_inactivity_monitor() -> None:
    """
    Start the inactivity monitor background task if configured.

    Does nothing if ORCHESTRA_INACTIVITY_TIMEOUT_SECONDS is not set.
    Safe to call multiple times; only starts one monitor.
    """
    global _monitor_task

    timeout = settings.inactivity_timeout_seconds
    if timeout is None:
        return

    if _monitor_task is not None and not _monitor_task.done():
        logger.debug("Inactivity monitor already running")
        return

    loop = asyncio.get_event_loop()
    _monitor_task = loop.create_task(_inactivity_monitor_loop(timeout))


def stop_inactivity_monitor() -> None:
    """Cancel the inactivity monitor task if running."""
    global _monitor_task

    if _monitor_task is not None and not _monitor_task.done():
        _monitor_task.cancel()
        _monitor_task = None
