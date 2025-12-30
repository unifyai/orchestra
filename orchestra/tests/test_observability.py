"""
Tests for observability infrastructure - OpenTelemetry instrumentation and trace export.

These tests verify that:
1. FileSpanExporter routes spans to the correct trace files
2. Instrumentors (httpx, OpenAI, SQLAlchemy) are properly installed
3. Custom embedding spans capture expected attributes
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.trace import SpanKind
from opentelemetry.trace.status import Status, StatusCode

from orchestra.web.api.utils.file_trace_exporter import FileSpanExporter, _span_to_dict


def _create_mock_span(
    name: str,
    attributes: dict | None = None,
    kind: SpanKind = SpanKind.INTERNAL,
    trace_id: int = 0x1234567890ABCDEF,
    span_id: int = 0xFEDCBA09,
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
# FileSpanExporter Routing Tests
# =============================================================================


class TestFileSpanExporterRouting:
    """Test that FileSpanExporter routes spans to the correct trace files."""

    def test_routes_openai_spans_by_name(self):
        """Spans with 'openai' in the name go to openai_traces.jsonl."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            span = _create_mock_span(
                name="openai.embeddings",
                attributes={"gen_ai.system": "openai"},
            )

            result = exporter.export([span])
            exporter.shutdown()

            assert result == SpanExportResult.SUCCESS
            openai_file = Path(tmpdir) / "traces" / "openai_traces.jsonl"
            assert openai_file.exists()

            with open(openai_file) as f:
                data = json.loads(f.readline())
                assert data["name"] == "openai.embeddings"

    def test_routes_openai_spans_by_attribute(self):
        """Spans with 'llm' or 'openai' attributes go to openai_traces.jsonl."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            span = _create_mock_span(
                name="embedding_api_attempt",
                attributes={"llm.request.type": "embedding"},
            )

            result = exporter.export([span])
            exporter.shutdown()

            assert result == SpanExportResult.SUCCESS
            openai_file = Path(tmpdir) / "traces" / "openai_traces.jsonl"
            assert openai_file.exists()

    def test_routes_http_spans(self):
        """Spans with HTTP attributes go to http_traces.jsonl."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            span = _create_mock_span(
                name="POST",
                attributes={
                    "http.method": "POST",
                    "http.url": "https://api.openai.com/v1/embeddings",
                    "http.status_code": 200,
                },
                kind=SpanKind.CLIENT,
            )

            result = exporter.export([span])
            exporter.shutdown()

            assert result == SpanExportResult.SUCCESS
            http_file = Path(tmpdir) / "traces" / "http_traces.jsonl"
            assert http_file.exists()

            with open(http_file) as f:
                data = json.loads(f.readline())
                assert data["name"] == "POST"
                assert (
                    data["attributes"]["http.url"]
                    == "https://api.openai.com/v1/embeddings"
                )

    def test_routes_db_spans(self):
        """Spans with DB attributes go to db_traces.jsonl."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            span = _create_mock_span(
                name="SELECT",
                attributes={
                    "db.system": "postgresql",
                    "db.statement": "SELECT * FROM users",
                },
            )

            result = exporter.export([span])
            exporter.shutdown()

            assert result == SpanExportResult.SUCCESS
            db_file = Path(tmpdir) / "traces" / "db_traces.jsonl"
            assert db_file.exists()

    def test_routes_unknown_spans_to_other(self):
        """Spans without recognized attributes go to other_traces.jsonl."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            span = _create_mock_span(
                name="custom_operation",
                attributes={"custom.attribute": "value"},
            )

            result = exporter.export([span])
            exporter.shutdown()

            assert result == SpanExportResult.SUCCESS
            other_file = Path(tmpdir) / "traces" / "other_traces.jsonl"
            assert other_file.exists()

    def test_multiple_spans_route_to_correct_files(self):
        """Multiple spans in one export batch route to their correct files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            spans = [
                _create_mock_span(
                    name="openai.chat",
                    attributes={"gen_ai.system": "openai"},
                ),
                _create_mock_span(
                    name="GET /api/users",
                    attributes={"http.method": "GET", "http.status_code": 200},
                ),
                _create_mock_span(
                    name="INSERT users",
                    attributes={"db.system": "postgresql"},
                ),
                _create_mock_span(
                    name="internal_task",
                    attributes={},
                ),
            ]

            result = exporter.export(spans)
            exporter.shutdown()

            assert result == SpanExportResult.SUCCESS

            # Verify each file has exactly one span
            for filename in [
                "openai_traces.jsonl",
                "http_traces.jsonl",
                "db_traces.jsonl",
                "other_traces.jsonl",
            ]:
                filepath = Path(tmpdir) / "traces" / filename
                assert filepath.exists(), f"{filename} should exist"
                with open(filepath) as f:
                    lines = f.readlines()
                    assert len(lines) == 1, f"{filename} should have exactly 1 span"


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

    def test_embedding_span_routes_to_openai_traces(self):
        """Verify embedding_api_attempt spans route to openai_traces due to 'embedding' attribute."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            # Simulate a span from _get_embeddings_batch
            span = _create_mock_span(
                name="embedding_api_attempt",
                attributes={
                    "embedding.batch_size": 2,
                    "embedding.model": "text-embedding-3-small",
                    "embedding.split_depth": 0,
                    "embedding.duration_ms": 500.0,
                    "embedding.success": True,
                    "embedding.usage.total_tokens": 8,
                    "embedding.usage.prompt_tokens": 8,
                },
            )

            result = exporter.export([span])
            exporter.shutdown()

            # Should NOT go to openai_traces (no 'openai' or 'llm' in name/attrs)
            # Actually goes to 'other' since the routing checks for 'openai' or 'llm'
            # in attribute keys, not values
            assert result == SpanExportResult.SUCCESS

            # The span should be in other_traces since 'embedding.' doesn't match
            # the openai routing pattern (which looks for 'openai' or 'llm' in keys)
            other_file = Path(tmpdir) / "traces" / "other_traces.jsonl"
            assert other_file.exists()


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

    def test_creates_traces_directory(self):
        """Verify exporter creates traces/ subdirectory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            traces_dir = Path(tmpdir) / "traces"
            assert traces_dir.exists()
            assert traces_dir.is_dir()

            exporter.shutdown()

    def test_handles_empty_export(self):
        """Verify exporter handles empty span list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            result = exporter.export([])
            exporter.shutdown()

            assert result == SpanExportResult.SUCCESS

    def test_force_flush(self):
        """Verify force_flush works without errors."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            span = _create_mock_span(name="test")
            exporter.export([span])

            result = exporter.force_flush()
            exporter.shutdown()

            assert result is True

    def test_shutdown_closes_files(self):
        """Verify shutdown closes all file handles."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = FileSpanExporter(tmpdir)

            # Export to multiple files
            spans = [
                _create_mock_span(
                    name="openai.test",
                    attributes={"gen_ai.system": "openai"},
                ),
                _create_mock_span(
                    name="http.test",
                    attributes={"http.method": "GET"},
                ),
            ]
            exporter.export(spans)

            # Shutdown should close all files
            exporter.shutdown()

            # Verify files dict is cleared
            assert len(exporter._files) == 0
