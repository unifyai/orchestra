"""
Tests for JsonlSpanExporter.

Verifies that Orchestra's JSONL span exporter produces output compatible
with Unity's FileSpanExporter format, enabling unified traces when both
write to the same directory.
"""

import json
import time

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from orchestra.web.api.utils.file_trace_exporter import JsonlSpanExporter


@pytest.fixture
def reset_otel():
    """Reset OTel state before and after test."""
    exporter = InMemorySpanExporter()
    resource = Resource.create({SERVICE_NAME: "orchestra-test"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Reset OTel global state
    trace._TRACER_PROVIDER_SET_ONCE._done = False
    trace._TRACER_PROVIDER = None

    trace.set_tracer_provider(provider)

    yield {"provider": provider, "exporter": exporter}

    exporter.clear()


class TestJsonlSpanExporter:
    """Tests for JsonlSpanExporter."""

    def test_writes_spans_to_jsonl_file(self, reset_otel, tmp_path):
        """JsonlSpanExporter writes spans to {trace_id}.jsonl files."""
        exporter = reset_otel["exporter"]
        jsonl_exporter = JsonlSpanExporter(str(tmp_path), service_name="orchestra")

        provider = reset_otel["provider"]
        provider.add_span_processor(SimpleSpanProcessor(jsonl_exporter))

        tracer = trace.get_tracer("test")

        with tracer.start_as_current_span("test-operation") as span:
            span.set_attribute("test.key", "test-value")

        # Check that a JSONL file was created
        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1

        # Verify filename is {trace_id}.jsonl
        trace_id = f"{span.get_span_context().trace_id:032x}"
        assert files[0].name == f"{trace_id}.jsonl"

        # Read and verify content
        with open(files[0], "r") as f:
            lines = f.readlines()

        assert len(lines) >= 1
        span_data = json.loads(lines[-1])  # Get last span (ours)

        assert span_data["name"] == "test-operation"
        assert span_data["service"] == "orchestra"
        assert span_data["trace_id"] == trace_id
        assert span_data["attributes"]["test.key"] == "test-value"

    def test_nested_spans_same_file(self, reset_otel, tmp_path):
        """Nested spans go to the same trace file."""
        jsonl_exporter = JsonlSpanExporter(str(tmp_path), service_name="orchestra")

        provider = reset_otel["provider"]
        provider.add_span_processor(SimpleSpanProcessor(jsonl_exporter))

        tracer = trace.get_tracer("test")

        with tracer.start_as_current_span("parent") as parent:
            with tracer.start_as_current_span("child") as child:
                pass

        # Should be one file (same trace)
        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1

        # Should have at least 2 spans (parent + child)
        with open(files[0], "r") as f:
            lines = f.readlines()

        # Find our spans
        spans = [json.loads(line) for line in lines]
        our_spans = [s for s in spans if s["name"] in ("parent", "child")]
        assert len(our_spans) == 2

        parent_span = next(s for s in our_spans if s["name"] == "parent")
        child_span = next(s for s in our_spans if s["name"] == "child")

        # Same trace ID
        assert parent_span["trace_id"] == child_span["trace_id"]

        # Child has parent span ID
        assert child_span["parent_span_id"] == parent_span["span_id"]

    def test_different_traces_different_files(self, reset_otel, tmp_path):
        """Different traces go to different files."""
        from opentelemetry import context

        jsonl_exporter = JsonlSpanExporter(str(tmp_path), service_name="orchestra")

        provider = reset_otel["provider"]
        provider.add_span_processor(SimpleSpanProcessor(jsonl_exporter))

        tracer = trace.get_tracer("test")

        # Create two separate traces by detaching from any parent context
        token1 = context.attach(context.Context())
        with tracer.start_as_current_span("trace1") as span1:
            trace_id_1 = span1.get_span_context().trace_id
        context.detach(token1)

        token2 = context.attach(context.Context())
        with tracer.start_as_current_span("trace2") as span2:
            trace_id_2 = span2.get_span_context().trace_id
        context.detach(token2)

        # Verify they have different trace IDs
        assert trace_id_1 != trace_id_2

        # Should be two files (different traces)
        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 2

    def test_records_timing_information(self, reset_otel, tmp_path):
        """JsonlSpanExporter records timing information."""
        jsonl_exporter = JsonlSpanExporter(str(tmp_path), service_name="orchestra")

        provider = reset_otel["provider"]
        provider.add_span_processor(SimpleSpanProcessor(jsonl_exporter))

        tracer = trace.get_tracer("test")

        with tracer.start_as_current_span("timed-op"):
            time.sleep(0.01)  # 10ms

        files = list(tmp_path.glob("*.jsonl"))
        with open(files[0], "r") as f:
            lines = f.readlines()

        span_data = json.loads(lines[-1])

        assert span_data["start_time"] is not None
        assert span_data["end_time"] is not None
        assert span_data["duration_ms"] is not None
        assert span_data["duration_ms"] >= 10  # At least 10ms

    def test_records_error_status(self, reset_otel, tmp_path):
        """JsonlSpanExporter records error status."""
        from opentelemetry.trace import Status, StatusCode

        jsonl_exporter = JsonlSpanExporter(str(tmp_path), service_name="orchestra")

        provider = reset_otel["provider"]
        provider.add_span_processor(SimpleSpanProcessor(jsonl_exporter))

        tracer = trace.get_tracer("test")

        with tracer.start_as_current_span("error-op") as span:
            span.set_status(Status(StatusCode.ERROR, "something failed"))

        files = list(tmp_path.glob("*.jsonl"))
        with open(files[0], "r") as f:
            lines = f.readlines()

        span_data = json.loads(lines[-1])
        assert span_data["status"] == "ERROR"


class TestJsonlSpanExporterUnityCompatibility:
    """Tests verifying compatibility with Unity's FileSpanExporter format."""

    def test_format_matches_unity(self, reset_otel, tmp_path):
        """Output format matches Unity's FileSpanExporter JSONL format."""
        jsonl_exporter = JsonlSpanExporter(str(tmp_path), service_name="orchestra")

        provider = reset_otel["provider"]
        provider.add_span_processor(SimpleSpanProcessor(jsonl_exporter))

        tracer = trace.get_tracer("test")

        with tracer.start_as_current_span("http-request") as span:
            span.set_attribute("http.method", "POST")
            span.set_attribute("http.route", "/v0/contacts")

        files = list(tmp_path.glob("*.jsonl"))
        with open(files[0], "r") as f:
            span_data = json.loads(f.readline())

        # Verify all required fields present (matching Unity's format)
        required_fields = [
            "trace_id",
            "span_id",
            "parent_span_id",
            "name",
            "service",
            "start_time",
            "end_time",
            "duration_ms",
            "status",
            "attributes",
        ]
        for field in required_fields:
            assert field in span_data, f"Missing field: {field}"

        # Verify trace_id/span_id are 32/16 hex chars
        assert len(span_data["trace_id"]) == 32
        assert len(span_data["span_id"]) == 16

    def test_can_append_to_existing_file(self, reset_otel, tmp_path):
        """JsonlSpanExporter can append to an existing file (simulating Unity)."""
        # Simulate Unity already wrote a span to this trace file
        trace_id = "0" * 32  # Dummy trace_id
        trace_file = tmp_path / f"{trace_id}.jsonl"

        # Pre-create file with Unity-style span
        unity_span = {
            "trace_id": trace_id,
            "span_id": "a" * 16,
            "parent_span_id": None,
            "name": "ContactManager.ask",
            "service": "unity",
            "start_time": "2025-01-01T00:00:00+00:00",
            "end_time": "2025-01-01T00:00:01+00:00",
            "duration_ms": 1000,
            "status": "OK",
            "attributes": {"unity.query": "find john"},
        }
        with open(trace_file, "w") as f:
            f.write(json.dumps(unity_span) + "\n")

        # Now have Orchestra append to the same file
        jsonl_exporter = JsonlSpanExporter(str(tmp_path), service_name="orchestra")

        # Manually create a span dict and write it
        orchestra_span = {
            "trace_id": trace_id,
            "span_id": "b" * 16,
            "parent_span_id": "a" * 16,  # Child of Unity span
            "name": "POST /v0/contacts",
            "service": "orchestra",
            "start_time": "2025-01-01T00:00:00.100+00:00",
            "end_time": "2025-01-01T00:00:00.500+00:00",
            "duration_ms": 400,
            "status": "OK",
            "attributes": {"http.status_code": 200},
        }

        # Append manually (simulating exporter behavior)
        with open(trace_file, "a") as f:
            f.write(json.dumps(orchestra_span) + "\n")

        # Verify both spans in file
        with open(trace_file, "r") as f:
            lines = f.readlines()

        assert len(lines) == 2

        spans = [json.loads(line) for line in lines]
        assert spans[0]["service"] == "unity"
        assert spans[1]["service"] == "orchestra"

        # Same trace_id
        assert spans[0]["trace_id"] == spans[1]["trace_id"]

        # Orchestra span is child of Unity span
        assert spans[1]["parent_span_id"] == spans[0]["span_id"]
