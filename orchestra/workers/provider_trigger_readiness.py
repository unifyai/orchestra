"""HTTP readiness surface for the provider-trigger worker."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

logger = logging.getLogger(__name__)

ReadinessEvaluator = Callable[[], tuple[bool, dict[str, object]]]


class _ReadinessHandler(BaseHTTPRequestHandler):
    evaluator: ReadinessEvaluator | None = None

    def do_GET(self) -> None:  # noqa: N802
        if self.path not in {"/", "/health", "/ready", "/readiness"}:
            self.send_response(404)
            self.end_headers()
            return

        evaluator = self.evaluator
        if evaluator is None:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b'{"ready": false, "reason": "evaluator_missing"}')
            return

        ready, payload = evaluator()
        body = json.dumps({"ready": ready, **payload}).encode("utf-8")
        self.send_response(200 if ready else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        logger.debug("provider-trigger readiness %s", format % args)


class ProviderTriggerWorkerReadinessServer:
    """Serve Cloud Run-compatible readiness checks for the trigger worker."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        evaluator: ReadinessEvaluator,
    ) -> None:
        self._evaluator = evaluator
        self._server = ThreadingHTTPServer((host, port), _ReadinessHandler)
        self._server.daemon_threads = True
        _ReadinessHandler.evaluator = evaluator
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="provider-trigger-readiness",
            daemon=True,
        )

    def start(self) -> None:
        """Start the readiness HTTP server in a background thread."""

        self._thread.start()
        logger.info(
            "provider-trigger readiness server listening on %s:%s",
            self._server.server_address[0],
            self._server.server_address[1],
        )

    def stop(self) -> None:
        """Stop the readiness HTTP server."""

        self._server.shutdown()
        self._thread.join(timeout=5)


def default_readiness_evaluator(
    *,
    last_cycle_completed_at: datetime | None,
    max_age_seconds: int,
    last_cycle_error: str | None = None,
) -> tuple[bool, dict[str, object]]:
    """Return readiness based on the last successful worker cycle."""

    if last_cycle_error:
        return False, {
            "reason": "last_cycle_failed",
            "error": last_cycle_error,
        }
    if last_cycle_completed_at is None:
        return False, {"reason": "no_completed_cycle"}

    now = datetime.now(timezone.utc)
    completed_at = last_cycle_completed_at
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)
    age_seconds = (now - completed_at).total_seconds()
    ready = age_seconds <= max_age_seconds
    return ready, {
        "reason": "healthy" if ready else "heartbeat_stale",
        "heartbeat_age_seconds": age_seconds,
        "max_age_seconds": max_age_seconds,
    }
