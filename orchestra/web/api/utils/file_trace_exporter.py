"""
File-based trace exporter for local development.

Exports OpenTelemetry spans to JSON files for debugging and analysis
when external trace collectors (Tempo, Jaeger) are not available.
"""

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

logger = logging.getLogger(__name__)


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


class FileSpanExporter(SpanExporter):
    """
    Exports spans to JSON files in a specified directory.

    Creates separate files for different trace types:
    - http_traces.jsonl: HTTP request traces
    - db_traces.jsonl: Database query traces
    - openai_traces.jsonl: OpenAI API call traces
    - other_traces.jsonl: All other traces
    """

    def __init__(self, trace_log_dir: str):
        self.trace_log_dir = Path(trace_log_dir)
        self.trace_log_dir.mkdir(parents=True, exist_ok=True)

        # File handles for different trace types
        self._files: dict[str, Optional[object]] = {}
        self._lock = threading.Lock()

        # Create subdirectories for organization
        (self.trace_log_dir / "traces").mkdir(exist_ok=True)

        logger.info(f"FileSpanExporter initialized at {self.trace_log_dir}")

    def _get_trace_type(self, span: ReadableSpan) -> str:
        """Determine the trace type based on span attributes."""
        name = span.name.lower() if span.name else ""
        attributes = span.attributes or {}

        # Check for OpenAI traces
        if "openai" in name or any(
            "openai" in str(k).lower() or "llm" in str(k).lower()
            for k in attributes.keys()
        ):
            return "openai"

        # Check for HTTP traces
        if any(
            k in attributes
            for k in ["http.method", "http.url", "http.route", "http.status_code"]
        ):
            return "http"

        # Check for database traces
        if any(
            k in attributes
            for k in ["db.system", "db.statement", "db.name", "db.operation"]
        ):
            return "db"

        return "other"

    def _get_file(self, trace_type: str):
        """Get or create file handle for a trace type."""
        if trace_type not in self._files or self._files[trace_type] is None:
            file_path = self.trace_log_dir / "traces" / f"{trace_type}_traces.jsonl"
            # Ensure directory exists (may have been deleted while server was running)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            self._files[trace_type] = open(file_path, "a", buffering=1)  # Line buffered
        return self._files[trace_type]

    def _write_span(self, trace_type: str, span_dict: dict) -> None:
        """Write a span to file, recovering from stale handles if needed."""
        try:
            file_handle = self._get_file(trace_type)
            json.dump(span_dict, file_handle, default=str)
            file_handle.write("\n")
        except OSError:
            # File handle is stale (directory may have been deleted and recreated)
            # Close and clear the handle, then retry once
            if trace_type in self._files:
                try:
                    self._files[trace_type].close()
                except Exception:
                    pass
                self._files[trace_type] = None
            # Retry with fresh handle
            file_handle = self._get_file(trace_type)
            json.dump(span_dict, file_handle, default=str)
            file_handle.write("\n")

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Export spans to appropriate JSON files."""
        try:
            with self._lock:
                for span in spans:
                    trace_type = self._get_trace_type(span)
                    span_dict = _span_to_dict(span)

                    # Add timestamp for easier searching
                    span_dict["_exported_at"] = datetime.utcnow().isoformat()

                    self._write_span(trace_type, span_dict)

            return SpanExportResult.SUCCESS
        except Exception as e:
            logger.error(f"Failed to export spans to file: {e}")
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        """Close all file handles."""
        with self._lock:
            for file_handle in self._files.values():
                if file_handle:
                    try:
                        file_handle.close()
                    except Exception:
                        pass
            self._files.clear()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Flush all file handles."""
        with self._lock:
            for file_handle in self._files.values():
                if file_handle:
                    try:
                        file_handle.flush()
                    except Exception:
                        pass
        return True
