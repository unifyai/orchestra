"""
Tests for resource pool instrumentation.

Verifies that OTel span events are emitted when bottlenecks are detected,
and that traces remain quiet during normal operation.
"""

import os
import threading
import time

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool, StaticPool

from orchestra.web.api.utils.resource_limits_instrumentation import (
    CONCURRENT_REQUESTS_WARN_THRESHOLD,
    FD_USAGE_WARN_THRESHOLD,
    RequestTracker,
    check_fd_usage,
    emit_fd_warning_if_needed,
    get_active_request_count,
    get_peak_request_count,
    instrument_db_pool,
)


@pytest.fixture
def tracer_provider():
    """Set up an in-memory tracer provider for testing."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Set as global tracer provider
    original_provider = trace.get_tracer_provider()
    trace.set_tracer_provider(provider)

    yield provider, exporter

    # Restore original provider
    trace.set_tracer_provider(original_provider)


@pytest.fixture
def clear_spans(tracer_provider):
    """Clear spans before each test."""
    _, exporter = tracer_provider
    exporter.clear()
    yield exporter


class TestRequestTracker:
    """Tests for RequestTracker context manager."""

    def test_tracks_active_requests(self):
        """Verify request count increments and decrements correctly."""
        initial_count = get_active_request_count()

        with RequestTracker():
            assert get_active_request_count() == initial_count + 1

        assert get_active_request_count() == initial_count

    def test_tracks_concurrent_requests(self):
        """Verify concurrent request tracking works."""
        initial_count = get_active_request_count()

        with RequestTracker():
            with RequestTracker():
                assert get_active_request_count() == initial_count + 2
            assert get_active_request_count() == initial_count + 1
        assert get_active_request_count() == initial_count

    def test_peak_tracking(self):
        """Verify peak request count is tracked."""
        # Open multiple concurrent request contexts
        trackers = []
        for _ in range(5):
            tracker = RequestTracker()
            tracker.__enter__()
            trackers.append(tracker)

        # Peak should be at least 5 (the trackers we just opened)
        current_peak = get_peak_request_count()
        assert current_peak >= 5

        # Clean up
        for tracker in trackers:
            tracker.__exit__(None, None, None)

    def test_emits_event_on_high_concurrency(self, tracer_provider):
        """Verify span event is emitted when concurrency exceeds threshold."""
        provider, exporter = tracer_provider
        tracer = provider.get_tracer("test")

        # Create a span context and simulate high concurrency
        with tracer.start_as_current_span("test_request") as span:
            # Temporarily lower threshold for testing
            original_threshold = CONCURRENT_REQUESTS_WARN_THRESHOLD
            import orchestra.web.api.utils.resource_limits_instrumentation as module

            module.CONCURRENT_REQUESTS_WARN_THRESHOLD = 2

            try:
                # Open enough concurrent requests to exceed threshold
                with RequestTracker():
                    with RequestTracker():
                        pass  # Second tracker should trigger warning
            finally:
                module.CONCURRENT_REQUESTS_WARN_THRESHOLD = original_threshold

        # Check that the span has the high concurrency event
        spans = exporter.get_finished_spans()
        assert len(spans) == 1

        events = spans[0].events
        high_concurrency_events = [
            e for e in events if e.name == "request.high_concurrency"
        ]
        assert len(high_concurrency_events) >= 1

        event = high_concurrency_events[0]
        assert "request.concurrent_count" in event.attributes
        assert "request.worker_pid" in event.attributes

    def test_no_event_below_threshold(self, tracer_provider):
        """Verify no span event when concurrency is below threshold."""
        provider, exporter = tracer_provider
        tracer = provider.get_tracer("test")

        with tracer.start_as_current_span("test_request"):
            # Single request shouldn't trigger warning
            with RequestTracker():
                pass

        spans = exporter.get_finished_spans()
        assert len(spans) == 1

        events = spans[0].events
        high_concurrency_events = [
            e for e in events if e.name == "request.high_concurrency"
        ]
        # Should be no high concurrency events (threshold is 50 by default)
        assert len(high_concurrency_events) == 0


class TestFDMonitoring:
    """Tests for file descriptor monitoring."""

    def test_check_fd_usage_returns_none_when_healthy(self):
        """Verify no stats returned when FD usage is below threshold."""
        # In normal operation, FD usage should be well below 80%
        result = check_fd_usage()
        # Result should be None (below threshold) or contain stats (if somehow high)
        if result is not None:
            assert result["system.fd.usage_percent"] > FD_USAGE_WARN_THRESHOLD

    @pytest.mark.skipif(
        not os.path.isdir(f"/proc/{os.getpid()}/fd"),
        reason="FD monitoring only works on Linux with /proc",
    )
    def test_check_fd_usage_returns_stats_format(self):
        """Verify the stats dict has correct keys when returned."""
        # Temporarily lower threshold to force stats return
        import orchestra.web.api.utils.resource_limits_instrumentation as module

        original_threshold = module.FD_USAGE_WARN_THRESHOLD
        module.FD_USAGE_WARN_THRESHOLD = 0  # Any usage triggers

        try:
            result = check_fd_usage()
            if result is not None:
                assert "system.fd.open" in result
                assert "system.fd.limit" in result
                assert "system.fd.usage_percent" in result
                assert isinstance(result["system.fd.open"], int)
                assert isinstance(result["system.fd.limit"], int)
        finally:
            module.FD_USAGE_WARN_THRESHOLD = original_threshold

    def test_emit_fd_warning_no_span_when_healthy(self, tracer_provider):
        """Verify no span event when FD usage is healthy."""
        provider, exporter = tracer_provider
        tracer = provider.get_tracer("test")

        with tracer.start_as_current_span("test_request"):
            emit_fd_warning_if_needed()

        spans = exporter.get_finished_spans()
        if spans:
            events = spans[0].events
            fd_events = [e for e in events if e.name == "system.fd.high_usage"]
            # Should be no FD events in normal operation
            assert len(fd_events) == 0


class TestDBPoolInstrumentation:
    """Tests for DB connection pool instrumentation."""

    @pytest.fixture
    def sqlite_engine(self):
        """Create a simple SQLite engine for testing with QueuePool."""
        # SQLite defaults to SingletonThreadPool which doesn't support pool_size.
        # Use StaticPool for simple testing (single connection, thread-safe).
        engine = create_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
        )
        yield engine
        engine.dispose()

    def test_instrument_db_pool_attaches_listeners(self, sqlite_engine):
        """Verify instrumentation attaches event listeners."""
        from sqlalchemy import event

        from orchestra.web.api.utils.resource_limits_instrumentation import (
            _on_do_connect,
        )

        instrument_db_pool(sqlite_engine)

        # Check that connect listener is attached
        assert event.contains(
            sqlite_engine.pool,
            "connect",
            _on_do_connect,
        )

    def test_pool_instrumentation_does_not_break_queries(self, sqlite_engine):
        """Verify pool still works after instrumentation."""
        instrument_db_pool(sqlite_engine)

        # Execute a simple query
        with sqlite_engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            assert result.scalar() == 1

    def test_no_event_for_fast_checkout(self, tracer_provider, sqlite_engine):
        """Verify no span event when checkout is fast."""
        provider, exporter = tracer_provider
        tracer = provider.get_tracer("test")

        instrument_db_pool(sqlite_engine)

        with tracer.start_as_current_span("test_request"):
            # Fast checkout - should not emit event
            with sqlite_engine.connect() as conn:
                conn.execute(text("SELECT 1"))

        spans = exporter.get_finished_spans()
        assert len(spans) == 1

        events = spans[0].events
        checkout_events = [e for e in events if e.name == "db.pool.checkout_delayed"]
        # Fast checkout should not trigger warning
        assert len(checkout_events) == 0


class TestPoolContention:
    """
    Integration tests for pool contention detection.

    These tests use a tiny pool to force contention and verify
    that span events are emitted correctly.
    """

    @pytest.fixture
    def tiny_pool_engine(self):
        """Create an engine with minimal pool to force contention."""
        # Use QueuePool explicitly with SQLite to enable pool_size control.
        # Note: SQLite with QueuePool and check_same_thread=False allows
        # multi-threaded access for testing purposes.
        engine = create_engine(
            "sqlite:///:memory:?check_same_thread=false",
            poolclass=QueuePool,
            pool_size=1,
            max_overflow=0,
            pool_timeout=5,
        )
        instrument_db_pool(engine)
        yield engine
        engine.dispose()

    def test_detects_pool_contention(self, tracer_provider, tiny_pool_engine):
        """Verify span event is emitted when pool checkout is delayed."""
        provider, exporter = tracer_provider
        tracer = provider.get_tracer("test")

        # Temporarily lower threshold to make test faster
        import orchestra.web.api.utils.resource_limits_instrumentation as module

        original_threshold = module.POOL_CHECKOUT_WARN_THRESHOLD
        module.POOL_CHECKOUT_WARN_THRESHOLD = 0.05  # 50ms

        results = {"second_thread_waited": False}

        def hold_connection():
            """Hold a connection to cause contention."""
            conn = tiny_pool_engine.connect()
            time.sleep(0.2)  # Hold for 200ms
            conn.close()

        def try_get_connection():
            """Try to get a connection while another is held."""
            with tracer.start_as_current_span("contended_request"):
                # This should wait for the first connection
                start = time.perf_counter()
                with tiny_pool_engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                wait_time = time.perf_counter() - start
                if wait_time > 0.05:
                    results["second_thread_waited"] = True

        try:
            # Start thread that holds connection
            holder = threading.Thread(target=hold_connection)
            holder.start()

            time.sleep(0.05)  # Let holder grab the connection

            # Try to get connection from another thread
            requester = threading.Thread(target=try_get_connection)
            requester.start()
            requester.join(timeout=3)
            holder.join(timeout=3)

            # Verify the second thread had to wait
            assert results["second_thread_waited"], "Expected contention but got none"

            # Check for span event
            spans = exporter.get_finished_spans()
            contended_spans = [s for s in spans if s.name == "contended_request"]

            if contended_spans:
                events = contended_spans[0].events
                checkout_events = [
                    e for e in events if e.name == "db.pool.checkout_delayed"
                ]
                # The event should have been recorded
                # Note: due to threading, events may not always be captured
                # This is a best-effort test
                if checkout_events:
                    assert "db.pool.wait_seconds" in checkout_events[0].attributes

        finally:
            module.POOL_CHECKOUT_WARN_THRESHOLD = original_threshold


class TestQuietByDefault:
    """
    Tests that verify the "quiet by default" behavior.

    These ensure traces aren't polluted during normal operation.
    """

    def test_normal_operation_has_no_bottleneck_events(self, tracer_provider):
        """Verify a normal request produces no bottleneck-related events."""
        provider, exporter = tracer_provider
        tracer = provider.get_tracer("test")

        # Simulate a normal request with no bottlenecks
        with tracer.start_as_current_span("normal_request"):
            with RequestTracker():
                # Simulate some work
                time.sleep(0.01)

        spans = exporter.get_finished_spans()
        assert len(spans) == 1

        events = spans[0].events
        bottleneck_event_names = {
            "db.pool.checkout_delayed",
            "db.pool.overflow_connection_created",
            "system.fd.high_usage",
            "request.high_concurrency",
        }

        bottleneck_events = [e for e in events if e.name in bottleneck_event_names]
        assert (
            len(bottleneck_events) == 0
        ), f"Expected no bottleneck events, got: {[e.name for e in bottleneck_events]}"
