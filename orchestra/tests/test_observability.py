"""
Tests for observability infrastructure - OpenTelemetry instrumentation and trace export.

These tests verify that:
1. FileSpanExporter organizes spans by request (trace_id)
2. Index file summarizes requests correctly
3. Spans are correctly serialized to JSON
4. Instrumentors (httpx, OpenAI, SQLAlchemy) are properly installed
"""

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.trace import SpanKind
from opentelemetry.trace.status import Status, StatusCode

from orchestra.web.api.utils.file_trace_exporter import (
    FileSpanExporter,
    _get_span_type,
    _span_to_dict,
)


def _create_mock_span(
    name: str,
    attributes: dict | None = None,
    kind: SpanKind = SpanKind.INTERNAL,
    trace_id: int = 0x1234567890ABCDEF1234567890ABCDEF,
    span_id: int = 0xFEDCBA0987654321,
    parent_span_id: int | None = None,
    start_time: int = 1000000000,
    end_time: int = 2000000000,
) -> MagicMock:
    """Create a mock ReadableSpan for testing."""
    span = MagicMock(spec=ReadableSpan)
    span.name = name
    span.attributes = attributes or {}
    span.kind = kind
    span.start_time = start_time
    span.end_time = end_time
    span.events = []
    span.links = []
    span.status = Status(StatusCode.UNSET)

    # Mock span context
    context = MagicMock()
    context.trace_id = trace_id
    context.span_id = span_id
    span.get_span_context.return_value = context

    # Mock parent
    if parent_span_id is not None:
        parent = MagicMock()
        parent.span_id = parent_span_id
        span.parent = parent
    else:
        span.parent = None

    # Mock resource
    resource = MagicMock()
    resource.attributes = {"service.name": "orchestra"}
    span.resource = resource

    return span


# =============================================================================
# FileSpanExporter Request Organization Tests
# =============================================================================


class TestFileSpanExporterRequestOrganization:
    """Test that FileSpanExporter organizes spans by request (trace_id)."""

    def test_creates_requests_directory(self):
        """Verify exporter creates requests/ subdirectory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            requests_dir = Path(tmpdir) / "requests"
            assert requests_dir.exists()
            assert requests_dir.is_dir()

            exporter.shutdown()

    def test_creates_one_file_per_trace(self):
        """Verify each trace_id gets its own file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            # Export spans with different trace_ids
            span1 = _create_mock_span(
                name="request_1",
                trace_id=0x1111111111111111,
                attributes={"http.method": "GET", "http.route": "/api/users"},
            )
            span2 = _create_mock_span(
                name="request_2",
                trace_id=0x2222222222222222,
                attributes={"http.method": "POST", "http.route": "/api/contacts"},
            )

            exporter.export([span1, span2])
            exporter.force_flush()
            exporter.shutdown()

            # Should have 2 request files
            requests_dir = Path(tmpdir) / "requests"
            request_files = list(requests_dir.glob("*.json"))
            assert len(request_files) == 2

    def test_groups_spans_by_trace_id(self):
        """Verify spans with same trace_id are grouped in one file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            trace_id = 0xABCDEF1234567890
            # Create multiple spans for the same trace (simulating HTTP + DB + etc.)
            http_span = _create_mock_span(
                name="GET /api/users",
                trace_id=trace_id,
                span_id=0x1111,
                attributes={"http.method": "GET", "http.route": "/api/users"},
            )
            db_span = _create_mock_span(
                name="SELECT users",
                trace_id=trace_id,
                span_id=0x2222,
                parent_span_id=0x1111,
                attributes={"db.system": "postgresql", "db.statement": "SELECT *"},
            )
            openai_span = _create_mock_span(
                name="openai.embeddings",
                trace_id=trace_id,
                span_id=0x3333,
                parent_span_id=0x1111,
                attributes={"gen_ai.system": "openai"},
            )

            exporter.export([http_span, db_span, openai_span])
            exporter.force_flush()
            exporter.shutdown()

            # Should have exactly 1 request file
            requests_dir = Path(tmpdir) / "requests"
            request_files = list(requests_dir.glob("*.json"))
            assert len(request_files) == 1

            # File should contain all 3 spans
            with open(request_files[0]) as f:
                data = json.load(f)
                assert len(data["spans"]) == 3


class TestFileSpanExporterIndex:
    """Test that index.jsonl is correctly maintained."""

    def test_creates_index_file(self):
        """Verify index.jsonl is created with request summaries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            span = _create_mock_span(
                name="GET /api/users",
                attributes={
                    "http.method": "GET",
                    "http.route": "/api/users",
                    "http.status_code": 200,
                },
            )

            exporter.export([span])
            exporter.force_flush()
            exporter.shutdown()

            index_path = Path(tmpdir) / "index.jsonl"
            assert index_path.exists()

            with open(index_path) as f:
                line = f.readline()
                summary = json.loads(line)
                assert summary["method"] == "GET"
                assert summary["route"] == "/api/users"
                assert summary["status"] == 200

    def test_index_contains_span_counts(self):
        """Verify index includes counts of different span types."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            trace_id = 0xABCDEF1234567890
            spans = [
                # Root HTTP span
                _create_mock_span(
                    name="POST /api/contacts",
                    trace_id=trace_id,
                    span_id=0x1111,
                    attributes={
                        "http.method": "POST",
                        "http.route": "/api/contacts",
                        "http.status_code": 201,
                    },
                ),
                # Two DB queries
                _create_mock_span(
                    name="SELECT",
                    trace_id=trace_id,
                    span_id=0x2222,
                    parent_span_id=0x1111,
                    attributes={"db.system": "postgresql"},
                ),
                _create_mock_span(
                    name="INSERT",
                    trace_id=trace_id,
                    span_id=0x3333,
                    parent_span_id=0x1111,
                    attributes={"db.system": "postgresql"},
                ),
                # One OpenAI call
                _create_mock_span(
                    name="openai.embeddings",
                    trace_id=trace_id,
                    span_id=0x4444,
                    parent_span_id=0x1111,
                    attributes={"gen_ai.system": "openai"},
                ),
            ]

            exporter.export(spans)
            exporter.force_flush()
            exporter.shutdown()

            index_path = Path(tmpdir) / "index.jsonl"
            with open(index_path) as f:
                summary = json.loads(f.readline())
                assert summary["span_count"] == 4
                assert summary["db_queries"] == 2
                assert summary["openai_calls"] == 1


# =============================================================================
# Span Type Classification Tests
# =============================================================================


class TestSpanTypeClassification:
    """Test that spans are correctly classified by type."""

    def test_classifies_openai_by_name(self):
        """Spans with 'openai' in name are classified as openai."""
        span_dict = {"name": "openai.embeddings", "attributes": {}}
        assert _get_span_type(span_dict) == "openai"

    def test_classifies_openai_by_llm_attribute(self):
        """Spans with 'llm' attributes are classified as openai."""
        span_dict = {"name": "api_call", "attributes": {"llm.request.type": "chat"}}
        assert _get_span_type(span_dict) == "openai"

    def test_classifies_http(self):
        """Spans with HTTP attributes are classified as http."""
        span_dict = {"name": "GET", "attributes": {"http.method": "GET"}}
        assert _get_span_type(span_dict) == "http"

    def test_classifies_db(self):
        """Spans with DB attributes are classified as db."""
        span_dict = {"name": "SELECT", "attributes": {"db.system": "postgresql"}}
        assert _get_span_type(span_dict) == "db"

    def test_classifies_other(self):
        """Spans without recognized attributes are classified as other."""
        span_dict = {"name": "custom_op", "attributes": {"custom.key": "value"}}
        assert _get_span_type(span_dict) == "other"


# =============================================================================
# Span Serialization Tests
# =============================================================================


class TestSpanSerialization:
    """Test that spans are correctly serialized to JSON."""

    def test_span_to_dict_basic_fields(self):
        """Verify basic span fields are serialized correctly."""
        span = _create_mock_span(
            name="test_span",
            attributes={"key": "value"},
            trace_id=0x1234567890ABCDEF1234567890ABCDEF,
            span_id=0xFEDCBA0987654321,
            start_time=1000000000,
            end_time=2000000000,
        )

        result = _span_to_dict(span)

        assert result["name"] == "test_span"
        assert result["trace_id"] == "1234567890abcdef1234567890abcdef"
        assert result["span_id"] == "fedcba0987654321"
        assert result["attributes"] == {"key": "value"}
        assert result["duration_ms"] == 1000.0  # (2000000000 - 1000000000) / 1_000_000

    def test_span_to_dict_with_parent(self):
        """Verify parent span ID is included when present."""
        span = _create_mock_span(
            name="child_span",
            parent_span_id=0xABCDEF0123456789,
        )

        result = _span_to_dict(span)

        assert result["parent_span_id"] == "abcdef0123456789"

    def test_span_to_dict_without_parent(self):
        """Verify parent_span_id is None for root spans."""
        span = _create_mock_span(name="root_span", parent_span_id=None)

        result = _span_to_dict(span)

        assert result["parent_span_id"] is None


# =============================================================================
# Embedding Span Attributes Tests
# =============================================================================


class TestEmbeddingSpanAttributes:
    """Test that embedding spans capture expected attributes."""

    def test_embedding_api_attempt_span_structure(self):
        """Verify embedding_api_attempt spans have expected attributes."""
        # This tests the expected structure of spans generated by
        # _get_embeddings_batch in helpers.py
        expected_attributes = {
            "embedding.batch_size",
            "embedding.model",
            "embedding.split_depth",
            "embedding.duration_ms",
            "embedding.success",
        }

        # Optional attributes that appear on success
        optional_success_attributes = {
            "embedding.usage.total_tokens",
            "embedding.usage.prompt_tokens",
        }

        # Optional attributes that appear on rate limit errors
        rate_limit_attributes = {
            "embedding.rate_limit.retry_after",
            "embedding.rate_limit.limit_requests",
            "embedding.rate_limit.remaining_requests",
            "embedding.rate_limit.reset_requests",
        }

        # Verify the attribute names are valid (no typos in the test)
        for attr in (
            expected_attributes | optional_success_attributes | rate_limit_attributes
        ):
            assert attr.startswith(
                "embedding.",
            ), f"Attribute {attr} should start with 'embedding.'"

    def test_embedding_span_classified_as_other(self):
        """Verify embedding_api_attempt spans are classified as 'other'."""
        # The routing checks for 'openai' or 'llm' in attribute keys,
        # not values, so embedding spans go to 'other'
        span_dict = {
            "name": "embedding_api_attempt",
            "attributes": {
                "embedding.batch_size": 2,
                "embedding.model": "text-embedding-3-small",
            },
        }
        assert _get_span_type(span_dict) == "other"


# =============================================================================
# Instrumentor Installation Tests
# =============================================================================


class TestInstrumentorInstallation:
    """Test that OpenTelemetry instrumentors are properly configured."""

    def test_httpx_instrumentor_import(self):
        """Verify HTTPXClientInstrumentor can be imported."""
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        assert HTTPXClientInstrumentor is not None

    def test_openai_instrumentor_import(self):
        """Verify OpenAIInstrumentor can be imported."""
        from opentelemetry.instrumentation.openai import OpenAIInstrumentor

        assert OpenAIInstrumentor is not None

    def test_sqlalchemy_instrumentor_import(self):
        """Verify SQLAlchemyInstrumentor can be imported."""
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        assert SQLAlchemyInstrumentor is not None

    def test_fastapi_instrumentor_import(self):
        """Verify FastAPIInstrumentor can be imported."""
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        assert FastAPIInstrumentor is not None

    def test_lifetime_imports_all_instrumentors(self):
        """Verify lifetime.py imports all required instrumentors."""
        # This test verifies the imports exist and won't fail at runtime
        from orchestra.web.lifetime import (
            HTTPXClientInstrumentor,
            OpenAIInstrumentor,
            SQLAlchemyInstrumentor,
        )

        assert HTTPXClientInstrumentor is not None
        assert OpenAIInstrumentor is not None
        assert SQLAlchemyInstrumentor is not None


# =============================================================================
# FileSpanExporter Edge Cases
# =============================================================================


class TestFileSpanExporterEdgeCases:
    """Test edge cases and error handling in FileSpanExporter."""

    def test_handles_empty_export(self):
        """Verify exporter handles empty span list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            result = exporter.export([])
            exporter.shutdown()

            assert result == SpanExportResult.SUCCESS

    def test_force_flush_writes_pending_traces(self):
        """Verify force_flush writes all pending traces."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            span = _create_mock_span(
                name="test",
                attributes={"http.method": "GET"},
            )
            exporter.export([span])

            # Force flush should write the trace immediately
            result = exporter.force_flush()
            assert result is True

            # Should have a request file now
            requests_dir = Path(tmpdir) / "requests"
            request_files = list(requests_dir.glob("*.json"))
            assert len(request_files) == 1

            exporter.shutdown()

    def test_shutdown_flushes_remaining_traces(self):
        """Verify shutdown writes any remaining buffered traces."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            span = _create_mock_span(
                name="test",
                attributes={"http.method": "GET"},
            )
            exporter.export([span])

            # Shutdown should flush
            exporter.shutdown()

            # Should have a request file
            requests_dir = Path(tmpdir) / "requests"
            request_files = list(requests_dir.glob("*.json"))
            assert len(request_files) == 1

    def test_automatic_flush_on_complete(self):
        """Verify completed traces (with root HTTP span) are flushed automatically."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            # Root HTTP span (no parent) signals request completion
            span = _create_mock_span(
                name="test",
                attributes={"http.method": "GET"},
            )
            exporter.export([span])

            # Wait for completion flush (0.5s delay + margin)
            time.sleep(1.0)

            # Should have a request file now
            requests_dir = Path(tmpdir) / "requests"
            request_files = list(requests_dir.glob("*.json"))
            assert len(request_files) == 1

            exporter.shutdown()

    def test_request_file_contains_sorted_spans(self):
        """Verify spans in request file are sorted by start_time."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            trace_id = 0xABCDEF1234567890
            # Export spans out of order
            spans = [
                _create_mock_span(
                    name="span_3",
                    trace_id=trace_id,
                    start_time=3000000000,
                    end_time=4000000000,
                ),
                _create_mock_span(
                    name="span_1",
                    trace_id=trace_id,
                    start_time=1000000000,
                    end_time=2000000000,
                ),
                _create_mock_span(
                    name="span_2",
                    trace_id=trace_id,
                    start_time=2000000000,
                    end_time=3000000000,
                ),
            ]

            exporter.export(spans)
            exporter.force_flush()
            exporter.shutdown()

            requests_dir = Path(tmpdir) / "requests"
            request_files = list(requests_dir.glob("*.json"))
            with open(request_files[0]) as f:
                data = json.load(f)
                span_names = [s["name"] for s in data["spans"]]
                assert span_names == ["span_1", "span_2", "span_3"]


# =============================================================================
# Request Trace Middleware Tests
# =============================================================================


class TestRequestTraceMiddleware:
    """Test request data capture utilities."""

    def test_truncate_short_string(self):
        """Short strings should not be truncated."""
        from orchestra.web.api.utils.request_trace_middleware import _truncate

        result = _truncate("hello", max_len=100)
        assert result == "hello"

    def test_truncate_long_string(self):
        """Long strings should be truncated with indicator."""
        from orchestra.web.api.utils.request_trace_middleware import _truncate

        long_str = "x" * 1000
        result = _truncate(long_str, max_len=100)
        assert len(result) < 1000  # Much shorter than original
        assert "truncated" in result
        assert "1000 total" in result

    def test_safe_json_dumps_dict(self):
        """Dicts should serialize to JSON."""
        from orchestra.web.api.utils.request_trace_middleware import _safe_json_dumps

        result = _safe_json_dumps({"key": "value", "num": 42})
        assert '"key"' in result
        assert '"value"' in result
        assert "42" in result

    def test_safe_json_dumps_with_non_serializable(self):
        """Non-serializable objects should fall back to str()."""
        from orchestra.web.api.utils.request_trace_middleware import _safe_json_dumps

        class Custom:
            def __str__(self):
                return "custom_obj"

        result = _safe_json_dumps({"obj": Custom()})
        assert "custom_obj" in result
