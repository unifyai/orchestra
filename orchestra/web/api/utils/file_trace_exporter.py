"""
File-based trace exporter for local development.

Exports OpenTelemetry spans to JSON files for debugging and analysis
when external trace collectors (Tempo, Jaeger) are not available.

Organization:
- index.jsonl: One line per HTTP request with summary info
- requests/: One JSON file per trace (containing all spans for that request)

This organization makes it easy for AI agents to:
1. Count requests: ls requests/ | wc -l
2. Find slow requests: grep duration_ms index.jsonl
3. Inspect one request: read a single small JSON file
"""

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

logger = logging.getLogger(__name__)

# Flush completed traces (those with root HTTP span) after this short delay
# to catch any straggler spans that arrive in the same batch
COMPLETED_TRACE_FLUSH_DELAY_SECONDS = 0.5

# Fallback timeout for traces without a root HTTP span (background jobs, etc.)
# or in case root span is somehow missed
ORPHAN_TRACE_FLUSH_TIMEOUT_SECONDS = 30.0


def _span_to_dict(span: ReadableSpan) -> dict:
    """Convert a ReadableSpan to a JSON-serializable dictionary."""
    context = span.get_span_context()

    # Convert attributes to serializable format
    attributes = {}
    if span.attributes:
        for key, value in span.attributes.items():
            # Handle various attribute types
            if hasattr(value, "tolist"):  # numpy arrays
                attributes[key] = value.tolist()
            elif isinstance(value, (list, tuple)):
                attributes[key] = list(value)
            else:
                attributes[key] = value

    # Convert events
    events = []
    if span.events:
        for event in span.events:
            event_dict = {
                "name": event.name,
                "timestamp": event.timestamp,
            }
            if event.attributes:
                event_dict["attributes"] = dict(event.attributes)
            events.append(event_dict)

    # Convert links
    links = []
    if span.links:
        for link in span.links:
            link_ctx = link.context
            link_dict = {
                "trace_id": f"{link_ctx.trace_id:032x}",
                "span_id": f"{link_ctx.span_id:016x}",
            }
            if link.attributes:
                link_dict["attributes"] = dict(link.attributes)
            links.append(link_dict)

    return {
        "trace_id": f"{context.trace_id:032x}",
        "span_id": f"{context.span_id:016x}",
        "parent_span_id": f"{span.parent.span_id:016x}" if span.parent else None,
        "name": span.name,
        "kind": span.kind.name if span.kind else None,
        "start_time": span.start_time,
        "end_time": span.end_time,
        "duration_ms": (span.end_time - span.start_time) / 1_000_000
        if span.end_time and span.start_time
        else None,
        "status": {
            "code": span.status.status_code.name if span.status else None,
            "description": span.status.description if span.status else None,
        },
        "attributes": attributes,
        "events": events,
        "links": links,
        "resource": {
            "attributes": dict(span.resource.attributes) if span.resource else {},
        },
    }


def _get_span_type(span_dict: dict) -> str:
    """Classify a span by type based on its attributes."""
    name = (span_dict.get("name") or "").lower()
    attributes = span_dict.get("attributes") or {}

    if "openai" in name or any(
        "openai" in str(k).lower() or "llm" in str(k).lower() for k in attributes.keys()
    ):
        return "openai"

    if any(
        k in attributes
        for k in ["http.method", "http.url", "http.route", "http.status_code"]
    ):
        return "http"

    if any(
        k in attributes
        for k in ["db.system", "db.statement", "db.name", "db.operation"]
    ):
        return "db"

    return "other"


def _is_root_http_span(span_dict: dict) -> bool:
    """Check if this is a root HTTP span (the main request handler)."""
    if _get_span_type(span_dict) != "http":
        return False
    # Root spans have no parent or their parent is from a different trace
    return span_dict.get("parent_span_id") is None


@dataclass
class TraceBuffer:
    """Buffer for collecting spans belonging to a single trace."""

    trace_id: str
    spans: list[dict] = field(default_factory=list)
    last_update: float = field(default_factory=time.monotonic)
    http_root_span: Optional[dict] = None
    # Time when root HTTP span was received (signals request completion)
    completed_at: Optional[float] = None

    def add_span(self, span_dict: dict) -> None:
        """Add a span to the buffer."""
        self.spans.append(span_dict)
        self.last_update = time.monotonic()

        # Track the root HTTP span for summary info
        # The root HTTP span arriving means the request is complete (response sent)
        if _is_root_http_span(span_dict):
            # Keep the one with the earliest start time (in case of duplicates)
            if self.http_root_span is None or (
                span_dict.get("start_time", 0)
                < self.http_root_span.get("start_time", float("inf"))
            ):
                self.http_root_span = span_dict
            # Mark as complete when we receive the root HTTP span
            if self.completed_at is None:
                self.completed_at = time.monotonic()

    def is_complete(self) -> bool:
        """Check if trace is complete (root HTTP span received)."""
        return self.completed_at is not None

    def _has_child_spans(self) -> bool:
        """Check if any spans have a parent (i.e., are children)."""
        return any(s.get("parent_span_id") is not None for s in self.spans)

    def is_ready_to_flush(self) -> bool:
        """Check if trace should be flushed.

        Flush if:
        1. Complete (has root HTTP span) and short delay passed (catch stragglers)
        2. OR orphaned (only root spans, no HTTP) and timeout passed (background jobs)

        NEVER timeout-flush traces with child spans - they're waiting for their
        root span which guarantees we capture the complete HTTP request.
        """
        now = time.monotonic()
        if self.completed_at is not None:
            # Complete trace: flush after short delay
            return (now - self.completed_at) > COMPLETED_TRACE_FLUSH_DELAY_SECONDS

        # If we have child spans, we're waiting for the root span to arrive.
        # NEVER timeout - only flush on shutdown. This guarantees 1 file = 1 request.
        if self._has_child_spans():
            return False

        # No children and no HTTP root = likely background job with only root spans.
        # Safe to timeout-flush these.
        return (now - self.last_update) > ORPHAN_TRACE_FLUSH_TIMEOUT_SECONDS

    def get_summary(self) -> dict:
        """Generate summary info for the index file."""
        # Count span types
        type_counts = {"http": 0, "db": 0, "openai": 0, "other": 0}
        for span in self.spans:
            type_counts[_get_span_type(span)] += 1

        # Extract info from root HTTP span
        root = self.http_root_span or {}
        attrs = root.get("attributes") or {}

        # Calculate request timing
        start_time = root.get("start_time")
        duration_ms = root.get("duration_ms")

        # Format timestamp for filename and display (include date since orchestra
        # server may run for extended periods spanning multiple test runs)
        if start_time:
            # start_time is in nanoseconds
            dt = datetime.fromtimestamp(start_time / 1e9, tz=timezone.utc)
            time_str = (
                dt.strftime("%Y-%m-%dT%H-%M-%S")
                + f".{int((start_time % 1e9) // 1e6):03d}"
            )
        else:
            now = datetime.now(timezone.utc)
            time_str = (
                now.strftime("%Y-%m-%dT%H-%M-%S") + f".{now.microsecond // 1000:03d}"
            )

        return {
            "time": time_str,
            "trace_id": self.trace_id,
            "method": attrs.get("http.method", ""),
            "route": attrs.get("http.route", attrs.get("http.target", "")),
            "status": attrs.get("http.status_code"),
            "duration_ms": round(duration_ms, 2) if duration_ms else None,
            "span_count": len(self.spans),
            "db_queries": type_counts["db"],
            "openai_calls": type_counts["openai"],
        }


class FileSpanExporter(SpanExporter):
    """
    Exports spans to JSON files organized by HTTP request.

    Creates:
    - index.jsonl: Summary of all requests (one line per request)
    - requests/: One JSON file per trace containing all spans

    This organization allows AI agents to quickly:
    - Count total requests (count files or index lines)
    - Find slow requests (grep index.jsonl)
    - Inspect individual requests (read one small file)
    """

    def __init__(self, trace_log_dir: str):
        self.trace_log_dir = Path(trace_log_dir)
        self.trace_log_dir.mkdir(parents=True, exist_ok=True)

        # Create subdirectories
        self.requests_dir = self.trace_log_dir / "requests"
        self.requests_dir.mkdir(exist_ok=True)

        # Index file for quick lookups
        self.index_path = self.trace_log_dir / "index.jsonl"

        # Buffers for collecting spans by trace_id
        self._trace_buffers: dict[str, TraceBuffer] = {}
        self._lock = threading.Lock()

        # Background thread for flushing stale traces
        self._shutdown_event = threading.Event()
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()

        logger.info(f"FileSpanExporter initialized at {self.trace_log_dir}")

    def _flush_loop(self) -> None:
        """Background loop to flush completed/stale trace buffers."""
        # Check frequently (100ms) to flush completed traces promptly
        while not self._shutdown_event.wait(timeout=0.1):
            self._flush_stale_traces()

    def _flush_stale_traces(self) -> None:
        """Flush traces that are ready (complete or orphaned timeout)."""
        ready_trace_ids = []

        with self._lock:
            for trace_id, buffer in self._trace_buffers.items():
                if buffer.is_ready_to_flush():
                    ready_trace_ids.append(trace_id)

        # Flush outside the lock to avoid blocking
        for trace_id in ready_trace_ids:
            self._flush_trace(trace_id)

    def _flush_trace(self, trace_id: str) -> None:
        """Write a trace's spans to disk and remove from buffer."""
        with self._lock:
            buffer = self._trace_buffers.pop(trace_id, None)

        if buffer is None or not buffer.spans:
            return

        try:
            # Sort spans by start_time for readability
            spans_sorted = sorted(
                buffer.spans,
                key=lambda s: s.get("start_time") or 0,
            )

            # Generate summary for index
            summary = buffer.get_summary()

            # Build filename: HH-MM-SS.mmm_METHOD_route_<trace_id_suffix>.json
            # Include method and route for easy identification at a glance
            trace_id_short = trace_id[-8:]  # Last 8 chars for uniqueness
            method = summary.get("method", "").upper() or "UNKNOWN"
            route = summary.get("route", "") or "unknown"
            # Sanitize route for filesystem: /v0/contacts/{id} -> contacts-id
            # Strip the /v0/ prefix as it's always present and redundant
            route_clean = route.strip("/")
            if route_clean.startswith("v0/"):
                route_clean = route_clean[3:]
            route_safe = route_clean.replace("/", "-").replace("{", "").replace("}", "")
            route_safe = route_safe[:40]  # Limit length to avoid overly long filenames
            filename = f"{summary['time']}_{method}_{route_safe}_{trace_id_short}.json"
            summary["file"] = f"requests/{filename}"

            # Ensure requests directory exists (may have been deleted)
            self.requests_dir.mkdir(parents=True, exist_ok=True)

            # Write the request file
            request_file = self.requests_dir / filename
            with request_file.open("w", encoding="utf-8") as f:
                json.dump(
                    {
                        "trace_id": trace_id,
                        "summary": summary,
                        "spans": spans_sorted,
                    },
                    f,
                    indent=2,
                    default=str,
                )

            # Append to index (with retry for stale handle)
            self._append_to_index(summary)

            logger.debug(
                f"Flushed trace {trace_id_short} with {len(spans_sorted)} spans",
            )

        except Exception as e:
            logger.error(f"Failed to flush trace {trace_id}: {e}")

    def _append_to_index(self, summary: dict) -> None:
        """Append a summary line to the index file."""
        try:
            # Ensure parent directory exists
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            with self.index_path.open("a", encoding="utf-8") as f:
                json.dump(summary, f, default=str)
                f.write("\n")
        except OSError:
            # Directory may have been deleted, try recreating
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            with self.index_path.open("a", encoding="utf-8") as f:
                json.dump(summary, f, default=str)
                f.write("\n")

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Buffer spans by trace_id for later flushing."""
        try:
            with self._lock:
                for span in spans:
                    span_dict = _span_to_dict(span)
                    span_dict["_exported_at"] = datetime.now(timezone.utc).isoformat()

                    trace_id = span_dict["trace_id"]

                    # Get or create buffer for this trace
                    if trace_id not in self._trace_buffers:
                        self._trace_buffers[trace_id] = TraceBuffer(trace_id=trace_id)

                    self._trace_buffers[trace_id].add_span(span_dict)

            return SpanExportResult.SUCCESS
        except Exception as e:
            logger.error(f"Failed to export spans: {e}")
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        """Flush all pending traces and stop the background thread."""
        # Signal the flush thread to stop
        self._shutdown_event.set()
        self._flush_thread.join(timeout=5.0)

        # Flush any remaining traces
        with self._lock:
            remaining_trace_ids = list(self._trace_buffers.keys())

        for trace_id in remaining_trace_ids:
            self._flush_trace(trace_id)

        logger.info("FileSpanExporter shutdown complete")

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Flush all pending traces immediately."""
        with self._lock:
            trace_ids = list(self._trace_buffers.keys())

        for trace_id in trace_ids:
            self._flush_trace(trace_id)

        return True
