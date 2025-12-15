import base64
import io
import json
import os
import random
import time
import traceback
import warnings
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, AsyncGenerator, Generator

import numpy as np
import pytest

# Optional matplotlib for performance charts
try:
    import matplotlib

    matplotlib.use("Agg")  # Use non-interactive backend for headless rendering
    import matplotlib.pyplot as plt

    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False
    plt = None
from fastapi import FastAPI
from google.cloud import storage
from httpx import AsyncClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

warnings.filterwarnings("ignore", category=UserWarning)

# Global list to store timing records
TIMING_RECORDS = []

# Global to track current test info for timing records
CURRENT_TEST_INFO = {"name": None, "mode": None}

from orchestra.db.dependencies import get_db_session
from orchestra.db.utils import create_database, drop_database
from orchestra.settings import settings
from orchestra.web.application import get_app


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """
    Backend for anyio pytest plugin.

    :return: backend name.
    """
    return "asyncio"


@pytest.fixture(scope="function")
def _engine(worker_id) -> Generator[Engine, None, None]:
    """
    Create engine and databases.

    :yield: new engine.
    """
    from orchestra.db.meta import meta  # noqa: WPS433
    from orchestra.db.models import load_all_models  # noqa: WPS433

    load_all_models()

    create_database(worker_id)

    # set the gcp bucket url to the test bucket
    os.environ["ORCHESTRA_GCP_BUCKET_NAME"] = "test-log-images-bucket"

    url = str(settings.db_url)
    # If using xdist, the testing database (orchestra_test) needs to be
    # instantiated for every thread
    if worker_id:
        url = url.replace("orchestra_test", f"orchestra_test_{worker_id}")
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    meta.create_all(engine)
    with engine.begin() as conn:
        # Create the hamming_distance function for tests
        conn.execute(
            text(
                """
            CREATE OR REPLACE FUNCTION hamming_distance(a text, b text)
RETURNS integer
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $$
WITH ba AS (SELECT decode(a, 'hex') AS v),
     bb AS (SELECT decode(b, 'hex') AS v)
SELECT CASE
  WHEN octet_length(ba.v) = 8 AND octet_length(bb.v) = 8 THEN
       bit_count(((get_byte(ba.v,0) # get_byte(bb.v,0))::bit(8))) +
       bit_count(((get_byte(ba.v,1) # get_byte(bb.v,1))::bit(8))) +
       bit_count(((get_byte(ba.v,2) # get_byte(bb.v,2))::bit(8))) +
       bit_count(((get_byte(ba.v,3) # get_byte(bb.v,3))::bit(8))) +
       bit_count(((get_byte(ba.v,4) # get_byte(bb.v,4))::bit(8))) +
       bit_count(((get_byte(ba.v,5) # get_byte(bb.v,5))::bit(8))) +
       bit_count(((get_byte(ba.v,6) # get_byte(bb.v,6))::bit(8))) +
       bit_count(((get_byte(ba.v,7) # get_byte(bb.v,7))::bit(8)))
  ELSE 999
END
FROM ba, bb;
$$;


        """,
            ),
        )
        # Create safe temporal casting functions for tests
        conn.execute(
            text(
                """
            CREATE OR REPLACE FUNCTION safe_cast_to_timestamptz(input_text TEXT)
            RETURNS TIMESTAMP WITH TIME ZONE AS $$
            BEGIN
                RETURN input_text::TIMESTAMP WITH TIME ZONE;
            EXCEPTION
                WHEN OTHERS THEN
                    RETURN NULL;
            END;
            $$ LANGUAGE plpgsql IMMUTABLE;
            """,
            ),
        )
        conn.execute(
            text(
                """
            CREATE OR REPLACE FUNCTION safe_cast_to_time(input_text TEXT)
            RETURNS TIME AS $$
            BEGIN
                RETURN input_text::TIME;
            EXCEPTION
                WHEN OTHERS THEN
                    RETURN NULL;
            END;
            $$ LANGUAGE plpgsql IMMUTABLE;
            """,
            ),
        )
        conn.execute(
            text(
                """
            CREATE OR REPLACE FUNCTION safe_cast_to_date(input_text TEXT)
            RETURNS DATE AS $$
            BEGIN
                RETURN input_text::DATE;
            EXCEPTION
                WHEN OTHERS THEN
                    RETURN NULL;
            END;
            $$ LANGUAGE plpgsql IMMUTABLE;
            """,
            ),
        )
        conn.execute(
            text(
                """
            CREATE OR REPLACE FUNCTION safe_cast_to_interval(input_text TEXT)
            RETURNS INTERVAL AS $$
            BEGIN
                RETURN input_text::INTERVAL;
            EXCEPTION
                WHEN OTHERS THEN
                    RETURN NULL;
            END;
            $$ LANGUAGE plpgsql IMMUTABLE;
            """,
            ),
        )

        user_id = str(os.getenv("AUTH_ACCOUNT_USER_ID"))
        api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
        with open("orchestra/tests/seeding.sql") as file:
            conn.execute(text(file.read()), {"user_id": user_id, "api_key": api_key})

    try:
        import orchestra.web.lifetime as lifetime

        lifetime._engine = engine
        yield engine
    finally:
        engine.dispose()
        drop_database(worker_id)


@pytest.fixture
def dbsession(
    _engine: Engine,
    worker_id,
) -> Generator[Session, None, None]:
    """
    Get session to database.

    Fixture that returns a SQLAlchemy session with a SAVEPOINT, and the rollback to it
    after the test completes.

    :param _engine: current engine.
    :yields: session.
    """
    connection = _engine.connect()
    trans = connection.begin()

    session_maker = sessionmaker(connection, expire_on_commit=False)
    session = session_maker()

    try:
        yield session
    finally:
        session.close()
        connection.close()


@pytest.fixture
def fastapi_app(
    dbsession: Session,
) -> FastAPI:
    """
    Fixture for creating FastAPI app.

    :return: fastapi app with mocked dependencies.
    """
    application = get_app()
    application.dependency_overrides[get_db_session] = lambda: dbsession
    return application  # noqa: WPS331


class TestAwareAsyncClient(AsyncClient):
    """AsyncClient wrapper that injects test name into requests for SQL capture."""

    def __init__(self, *args, test_name: str = "unknown", **kwargs):
        super().__init__(*args, **kwargs)
        self._test_name = test_name

    async def request(self, method, url, **kwargs):
        # Inject test name header for SQL capture
        headers = kwargs.get("headers", {})
        if headers is None:
            headers = {}
        headers = dict(headers)  # Make mutable copy
        headers["X-Test-Name"] = self._test_name
        kwargs["headers"] = headers
        return await super().request(method, url, **kwargs)


class TimedAsyncClient(AsyncClient):
    """
    AsyncClient wrapper that records timing information for each request.

    Captures:
    - Request method and path
    - Response time (duration)
    - Test name and mode (from CURRENT_TEST_INFO global)
    - Status code

    Also injects X-Test-Name and X-Test-Mode headers for SQL capture.

    Used for performance comparison between EAV and JSONB storage modes.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def request(self, method, url, **kwargs):
        # Get current test info from global (set by pytest_runtest_setup hook)
        test_name = CURRENT_TEST_INFO.get("name", "unknown")
        mode = CURRENT_TEST_INFO.get("mode", "unknown")

        # Inject test name and mode headers for SQL capture
        headers = kwargs.get("headers", {})
        if headers is None:
            headers = {}
        headers = dict(headers)  # Make mutable copy
        headers["X-Test-Name"] = test_name
        headers["X-Test-Mode"] = mode or "unknown"
        kwargs["headers"] = headers

        # Capture timing
        start = time.monotonic()
        response = await super().request(method, url, **kwargs)
        duration = time.monotonic() - start

        # Build timing record - only store JSON-serializable data
        path = str(url)
        # Extract params if present (for debugging), converting to dict if needed
        params = kwargs.get("params", {})
        if hasattr(params, "items"):
            params = dict(params)
        else:
            params = {}

        TIMING_RECORDS.append(
            {
                "method": method,
                "path": f"{method} {path}",
                "duration": duration,
                "status_code": response.status_code,
                "test_name": test_name,
                "mode": mode,
                "params": params,  # Only store serializable params
            },
        )

        return response


@pytest.fixture
async def client(
    fastapi_app: FastAPI,
    anyio_backend: Any,
    request: pytest.FixtureRequest,
) -> AsyncGenerator[AsyncClient, None]:
    """
    Fixture that creates client for requesting server.

    :param fastapi_app: the application.
    :param request: pytest request object to get test name.
    :yield: client for the app.
    """
    test_name = request.node.nodeid
    async with TestAwareAsyncClient(
        app=fastapi_app,
        base_url="http://test",
        test_name=test_name,
    ) as ac:
        yield ac


# ============================================================================
# Storage Mode Testing Infrastructure
# ============================================================================
"""
Parametrized testing infrastructure for running tests across different storage modes.

Fixtures:
- use_jsonb_mode: Parametrized fixture that runs tests in both storage modes
- enable_jsonb_mode: Force JSONB storage mode for a single test
- enable_eav_mode: Force EAV storage mode for a single test

Decorators:
- @requires_eav_mode: Skip test when JSONB storage mode is active
- @skip_if_eav_mode: Skip test when EAV storage mode is active

Helper Functions:
- assert_mode_specific(): Different assertions per storage mode
- get_mode_specific_value(): Different expected values per storage mode

IMPORTANT: Do not change the fixture ids ("eav_mode", "jsonb_mode") in use_jsonb_mode
as the test result tracking relies on these identifiers.
"""


@pytest.fixture(params=[False, True], ids=["eav_mode", "jsonb_mode"])
def use_jsonb_mode(request, monkeypatch):
    """
    Parametrized fixture that runs tests in both storage modes for compatibility verification.

    Usage:
        @pytest.mark.anyio
        async def test_something(client, use_jsonb_mode):
            # Test runs in both modes
            pass
    """
    import orchestra.settings as settings_module

    monkeypatch.setattr(
        settings_module,
        "_use_jsonb_override",
        request.param,
        raising=False,
    )
    yield request.param


@pytest.fixture
def enable_jsonb_mode(monkeypatch):
    """
    Force JSONB storage mode for a single test.

    Usage:
        @pytest.mark.usefixtures("enable_jsonb_mode")
        def test_jsonb_only_feature(client):
            # Test runs only in JSONB mode
            pass
    """
    import orchestra.settings as settings_module

    monkeypatch.setattr(settings_module, "_use_jsonb_override", True, raising=False)
    yield True


@pytest.fixture
def enable_eav_mode(monkeypatch):
    """
    Force EAV storage mode for a single test.

    Usage:
        @pytest.mark.usefixtures("enable_eav_mode")
        def test_eav_only_feature(client):
            # Test runs only in EAV mode
            pass
    """
    import orchestra.settings as settings_module

    monkeypatch.setattr(settings_module, "_use_jsonb_override", False, raising=False)
    yield False


def requires_eav_mode(func):
    """
    Skip test when JSONB storage mode is active.

    Checks storage mode at execution time, compatible with parametrized fixtures.

    Usage:
        @requires_eav_mode
        @pytest.mark.anyio
        async def test_param_versioning(client):
            pass
    """
    import asyncio
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Check mode at execution time, not decoration time
        if settings.use_jsonb_queries:
            pytest.skip(
                "Test requires EAV mode (param versioning not supported in JSONB)",
            )
        return func(*args, **kwargs)

    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs):
        # Check mode at execution time, not decoration time
        if settings.use_jsonb_queries:
            pytest.skip(
                "Test requires EAV mode (param versioning not supported in JSONB)",
            )
        return await func(*args, **kwargs)

    # Return appropriate wrapper based on whether the function is async
    if asyncio.iscoroutinefunction(func):
        return async_wrapper
    return wrapper


# ============================================================================
# Test Results Tracking
# ============================================================================

# Global dictionary to track test results by mode
TEST_RESULTS_BY_MODE = {
    "eav_mode": {"passed": [], "failed": [], "skipped": []},
    "jsonb_mode": {"passed": [], "failed": [], "skipped": []},
}


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """
    Hook to capture test name and mode before each test runs.
    This allows timing records to include the test function name.
    """
    global CURRENT_TEST_INFO

    # Extract test function name (without module path and parameters)
    # e.g., "test_filter_closed_jobs" from "orchestra/tests/.../test_repairs_performance.py::test_filter_closed_jobs[eav]"
    nodeid = item.nodeid
    # Get the function name part (after :: and before [)
    if "::" in nodeid:
        func_part = nodeid.split("::")[-1]
        test_name = func_part.split("[")[0] if "[" in func_part else func_part
    else:
        test_name = nodeid

    # Determine mode from parametrization or nodeid
    mode = None
    if hasattr(item, "callspec") and hasattr(item.callspec, "params"):
        params = item.callspec.params
        # Check for 'mode' parameter (used in test_repairs_performance.py)
        if "mode" in params:
            mode = params["mode"]
        # Check for 'use_jsonb_mode' parameter (used in other tests)
        elif "use_jsonb_mode" in params:
            mode = "jsonb" if params["use_jsonb_mode"] else "eav"

    # Fallback: check nodeid for mode markers
    if mode is None:
        if "[eav]" in nodeid or "[eav-" in nodeid:
            mode = "eav"
        elif "[jsonb]" in nodeid or "[jsonb-" in nodeid:
            mode = "jsonb"

    CURRENT_TEST_INFO = {"name": test_name, "mode": mode}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """
    Capture test results and categorize by storage mode.

    Detects storage mode from test parametrization. Only parametrized tests are tracked in reports.
    """
    outcome = yield
    report = outcome.get_result()

    # Only track test call phase (not setup/teardown)
    if report.when == "call":
        mode = None

        # Strategy 1: Check item.callspec.params for use_jsonb_mode parameter
        # This is the most reliable method as it directly accesses the parametrized value
        if hasattr(item, "callspec") and hasattr(item.callspec, "params"):
            params = item.callspec.params
            if "use_jsonb_mode" in params:
                mode = "jsonb_mode" if params["use_jsonb_mode"] else "eav_mode"
            # Also check for 'mode' parameter (used in test_repairs_performance.py)
            elif "mode" in params:
                mode_val = params["mode"]
                if mode_val == "eav":
                    mode = "eav_mode"
                elif mode_val == "jsonb":
                    mode = "jsonb_mode"

        # Strategy 2: Fallback to nodeid string matching for compatibility
        # This handles cases where the parametrization ids might be in the nodeid
        if mode is None:
            if "[eav_mode]" in item.nodeid or "[eav]" in item.nodeid:
                mode = "eav_mode"
            elif "[jsonb_mode]" in item.nodeid or "[jsonb]" in item.nodeid:
                mode = "jsonb_mode"

        # Only track if we detected a parametrized test
        if mode:
            # Extract test name as module path + function name (up to first '[')
            # This ensures unique identification even for same-named functions in different modules
            # Example: "orchestra/tests/test_log/test_log_join.py::test_inner_join_logs"
            test_name = item.nodeid.split("[")[0] if "[" in item.nodeid else item.nodeid

            if report.passed:
                TEST_RESULTS_BY_MODE[mode]["passed"].append(test_name)
            elif report.failed:
                TEST_RESULTS_BY_MODE[mode]["failed"].append(
                    {
                        "name": test_name,
                        "error": str(report.longrepr)[:200],  # Truncate long errors
                    },
                )
            elif report.skipped:
                TEST_RESULTS_BY_MODE[mode]["skipped"].append(test_name)


# ============================================================================
# Helper Functions for Conditional Assertions
# ============================================================================


def assert_mode_specific(eav_condition, jsonb_condition, message=""):
    """
    Assert different conditions based on current storage mode.

    Args:
        eav_condition: Boolean condition to assert in EAV mode
        jsonb_condition: Boolean condition to assert in JSONB mode
        message: Optional assertion message

    Usage:
        assert_mode_specific(
            eav_condition=len(derived_log_rows) > 0,
            jsonb_condition=len(derived_log_rows) == 0,
            message="DerivedLog rows should only exist in EAV mode"
        )
    """
    if settings.use_jsonb_queries:
        assert jsonb_condition, f"[JSONB Mode] {message}"
    else:
        assert eav_condition, f"[EAV Mode] {message}"


def get_mode_specific_value(eav_value, jsonb_value):
    """
    Return different values based on storage mode.

    Args:
        eav_value: Value to return in EAV mode
        jsonb_value: Value to return in JSONB mode

    Returns:
        The appropriate value for the current storage mode

    Usage:
        expected_count = get_mode_specific_value(eav_value=10, jsonb_value=0)
    """
    return jsonb_value if settings.use_jsonb_queries else eav_value


def skip_if_eav_mode(reason="Feature only available in JSONB mode"):
    """
    Skip test when EAV storage mode is active.

    Usage:
        @skip_if_eav_mode("JSONB-specific optimization")
        def test_something():
            pass
    """
    return pytest.mark.skipif(not settings.use_jsonb_queries, reason=reason)


### SQL CAPTURE FOR TEST ANALYSIS ###


@pytest.fixture(autouse=True)
def sql_capture_context(request, use_jsonb_mode=None):
    """
    Auto-use fixture that sets up SQL capture context for each test.

    Captures test name, filter expression (if available), and storage mode for SQL query analysis.
    Enable SQL capture by setting: SQL_CAPTURE_ENABLED=1

    Captured SQL queries are written to:
    orchestra/tests/test_log/captured_sql/sql_capture_{mode}.jsonl

    Usage:
        SQL_CAPTURE_ENABLED=1 pytest orchestra/tests/test_log/test_log_filtering.py -v
    """
    try:
        from orchestra.tests.test_log.sql_capture import (
            clear_test_context,
            is_capture_enabled,
            set_test_context,
        )

        if not is_capture_enabled():
            yield
            return

        # Determine mode from fixture or test name
        mode = "unknown"
        if (
            hasattr(request, "fixturenames")
            and "use_jsonb_mode" in request.fixturenames
        ):
            try:
                jsonb_mode = request.getfixturevalue("use_jsonb_mode")
                mode = "jsonb" if jsonb_mode else "eav"
            except Exception:
                pass

        # Fallback: check node ID for mode markers
        if mode == "unknown":
            if (
                "[jsonb_mode]" in request.node.nodeid
                or "jsonb" in request.node.nodeid.lower()
            ):
                mode = "jsonb"
            elif (
                "[eav_mode]" in request.node.nodeid
                or "eav" in request.node.nodeid.lower()
            ):
                mode = "eav"
            elif "enable_jsonb_mode" in getattr(request, "fixturenames", []):
                mode = "jsonb"
            elif "enable_eav_mode" in getattr(request, "fixturenames", []):
                mode = "eav"

        # Try to extract filter expression from test parameters
        filter_expr = None
        if hasattr(request, "node") and hasattr(request.node, "callspec"):
            params = getattr(request.node.callspec, "params", {})
            # Common parameter names for filter expressions
            filter_expr = (
                params.get("expression")
                or params.get("filter_expr")
                or params.get("expr")
            )

        # Set context for SQL capture
        set_test_context(
            test_name=request.node.nodeid,
            filter_expr=filter_expr,
            mode=mode,
            extra={"test_function": request.node.name},
        )

        yield

        # Clear context after test
        clear_test_context()

    except ImportError:
        # sql_capture module not available
        yield


### PERF TESTS FIXTURES ###


@pytest.fixture
async def prod_client() -> AsyncGenerator[AsyncClient, None]:
    """
    Fixture that creates a client connected to the actual running local server.

    Used for performance tests that need to test against real production data.
    Requires the server to be running at localhost:8000.

    :yield: AsyncClient connected to localhost:8000
    """
    import httpx

    # Use a longer timeout for performance tests against real data
    timeout = httpx.Timeout(timeout=120.0)  # 2 minutes
    async with AsyncClient(base_url="http://localhost:8000", timeout=timeout) as ac:
        yield ac


@pytest.fixture
async def timed_client() -> AsyncGenerator[AsyncClient, None]:
    """
    Fixture that creates a timed client for performance testing.

    Uses TimedAsyncClient which automatically records timing for each request,
    along with test name and mode from CURRENT_TEST_INFO (set by pytest_runtest_setup).

    Used for performance tests that need real production data.
    Requires the server to be running at localhost:8000.

    :yield: TimedAsyncClient connected to localhost:8000 with timing instrumentation.
    """
    import httpx

    # Use a longer timeout for performance tests against real data
    timeout = httpx.Timeout(timeout=120.0)  # 2 minutes

    async with TimedAsyncClient(
        base_url="http://localhost:8000",
        timeout=timeout,
    ) as client:
        # Add records reference to client for backward compatibility
        client.records = TIMING_RECORDS
        yield client


def _make_value(key: str, id: int, offset: int, rng: random.Random):
    """
    Helper function to generate varied values for different field types.

    :param key: Field name
    :param id: Log event ID
    :param offset: Offset value for timestamp variation
    :param rng: Random number generator instance
    :return: Generated value appropriate for the field type
    """
    if key == "int_field":
        return rng.randint(0, 9999)
    elif key == "float_field":
        return rng.gauss(0, 100)
    elif key == "str_field":
        # Generate an 8-character slug
        chars = "abcdefghijklmnopqrstuvwxyz0123456789"
        return "".join(rng.choices(chars, k=8))
    elif key == "bool_field":
        return rng.choice([True, False])
    elif key == "list_field":
        return [1, 2, 3, id, rng.randint(0, 100)]
    elif key == "dict_field":
        return {"a": 1, "b": 2, "id": id, "r": rng.randint(0, 100)}
    elif key == "ts_field":
        # Spread timestamps over 30 days
        base_dt = datetime.fromisoformat("2023-01-01T00:00:00")
        new_dt = base_dt + timedelta(
            days=offset % 30,
            hours=rng.randint(0, 23),
            minutes=rng.randint(0, 59),
            seconds=rng.randint(0, 59),
        )
        return new_dt.isoformat()
    elif key == "category":
        return rng.choice(["alpha", "beta", "gamma", "delta"])
    elif key == "tags":
        tag_vocabulary = [
            "web",
            "mobile",
            "desktop",
            "cloud",
            "api",
            "database",
            "frontend",
            "backend",
            "security",
            "testing",
        ]
        num_tags = rng.randint(0, 3)
        return rng.sample(tag_vocabulary, num_tags)
    elif key == "long_text":
        # Generate Lorem ipsum text of random length between 1000-20000 chars
        lorem_chunks = [
            "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
            "Nullam auctor, nisl eget ultricies tincidunt, nisl nisl aliquam nisl.",
            "Vestibulum ante ipsum primis in faucibus orci luctus et ultrices posuere cubilia curae.",
            "Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.",
            "Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris.",
            "Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore.",
            "Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia.",
            "Nulla facilisi. Mauris sollicitudin, turpis in dictum scelerisque.",
            "Fusce varius, lectus non tincidunt dapibus, mauris dolor sagittis sapien.",
            "Praesent sagittis ipsum in dui sagittis, a commodo ante hendrerit.",
        ]

        length = rng.randint(1000, 20000)
        result = ""
        while len(result) < length:
            result += rng.choice(lorem_chunks) + " "

        return result[:length]
    return None


# RepairsDemo field definitions for performance testing
REPAIRS_DEMO_FIELDS = [
    "row_id",
    "ArrivedOnSite",
    "CompletedVisit",
    "JobTicketLinePlannedEndDate",
    "JobTicketLinePlannedStartDate",
    "SLADueDate",
    "FirstTimeFix",
    "FollowOn",
    "SecondTimeFix",
    "ThirdTimePlusFix",
    "FullAddress",
    "JobTicketReference",
    "WorksOrderRef",
    "OperativeName",
    "OperativeWhoCompletedJob",
    "PropertyReference",
    "SchemeName",
    "WorksOrderDescription",
    "WorksOrderPriorityDescription",
    "WorksOrderStatusDescription",
    "ActualCost",
    "TargetCost",
    "VarianceCost",
    "DaysToComplete",
    "HoursToComplete",
    "MinutesToComplete",
    "JobTicketLineStatus",
    "ContractorName",
    "TradeDescription",
    "SORDescription",
]

REPAIRS_DEMO_EMBEDDINGS = [
    "_WorksOrderDescription_emb",
    "_FullAddress_emb",
    "_OperativeName_emb",
    "_OperativeWhoCompletedJob_emb",
]

# Realistic data pools for RepairsDemo
REPAIRS_ADDRESSES = [
    "40 Harts Hill Way, Brierley Hill, Dudley, West Midlands, DY5 1JQ",
    "15 Cochrane Road, Dudley, West Midlands, DY2 0RE",
    "22 Oak Street, Birmingham, West Midlands, B1 2AA",
    "8 Maple Avenue, Wolverhampton, West Midlands, WV1 3BB",
    "55 High Street, Walsall, West Midlands, WS1 4CC",
    "101 Church Lane, Solihull, West Midlands, B91 5DD",
    "33 Park Road, Coventry, West Midlands, CV1 6EE",
    "77 Station Road, Stourbridge, West Midlands, DY8 7FF",
]

REPAIRS_OPERATIVE_NAMES = [
    "Adrian Hall",
    "John Smith",
    "David Wilson",
    "Michael Brown",
    "James Taylor",
    "Robert Davies",
    "William Evans",
    "Richard Thomas",
    "Christopher Johnson",
    "Daniel Roberts",
]

REPAIRS_SCHEMES = [
    "COCHRANE ROAD (GN)",
    "HARTS HILL WAY",
    "OAK STREET ESTATE",
    "MAPLE AVENUE BLOCK",
    "HIGH STREET TERRACE",
    "CHURCH LANE HOUSES",
    "PARK ROAD FLATS",
    "STATION ROAD COMPLEX",
]

REPAIRS_DESCRIPTIONS = [
    "Radiator loose - coming away from the wall",
    "Boiler not heating water properly",
    "Leaking tap in kitchen sink",
    "Blocked drain in bathroom",
    "Broken window handle - cannot close",
    "Front door lock mechanism faulty",
    "Electrical socket not working in living room",
    "Damp patch appearing on bedroom ceiling",
    "Guttering blocked causing overflow",
    "Fence panel blown down in storm",
    "Toilet cistern not filling correctly",
    "Smoke detector beeping intermittently",
]

REPAIRS_PRIORITIES = ["Routine", "Emergency", "Urgent"]
REPAIRS_STATUSES = ["Closed", "Open", "In Progress"]
REPAIRS_YES_NO = ["Yes", "No"]
REPAIRS_CONTRACTORS = [
    "ABC Repairs Ltd",
    "QuickFix Services",
    "HomeGuard Maintenance",
    "PropertyCare Solutions",
]
REPAIRS_TRADES = [
    "Plumbing",
    "Electrical",
    "Carpentry",
    "General Maintenance",
    "Roofing",
    "Glazing",
]
REPAIRS_SOR = [
    "Replace radiator bracket",
    "Service boiler",
    "Replace tap washer",
    "Clear blockage",
    "Replace window furniture",
    "Replace door lock",
]


def _make_repairs_value(key: str, id: int, rng: random.Random):
    """
    Generate realistic values for RepairsDemo fields.

    :param key: Field name from REPAIRS_DEMO_FIELDS
    :param id: Log event ID for deterministic variation
    :param rng: Random number generator instance
    :return: Generated value appropriate for the RepairsDemo field
    """
    # 80% realistic patterns, 20% randomized for variety
    use_realistic = rng.random() < 0.8

    if key == "row_id":
        return 40000 + id

    # DateTime fields - spread over July-Sept 2025 range
    datetime_fields = [
        "ArrivedOnSite",
        "CompletedVisit",
        "JobTicketLinePlannedEndDate",
        "JobTicketLinePlannedStartDate",
        "SLADueDate",
    ]
    if key in datetime_fields:
        base_dt = datetime.fromisoformat("2025-07-01T08:00:00")
        offset_days = rng.randint(0, 91)  # July to Sept (92 days)
        offset_hours = rng.randint(0, 10)  # Working hours 8am-6pm
        offset_mins = rng.randint(0, 59)
        new_dt = base_dt + timedelta(
            days=offset_days,
            hours=offset_hours,
            minutes=offset_mins,
        )
        return new_dt.isoformat()

    # Yes/No fields
    yes_no_fields = ["FirstTimeFix", "FollowOn", "SecondTimeFix", "ThirdTimePlusFix"]
    if key in yes_no_fields:
        if key == "FirstTimeFix":
            # 70% first time fix rate
            return "Yes" if rng.random() < 0.7 else "No"
        elif key == "FollowOn":
            # 20% follow on rate
            return "Yes" if rng.random() < 0.2 else "No"
        return rng.choice(REPAIRS_YES_NO)

    if key == "FullAddress":
        if use_realistic:
            return rng.choice(REPAIRS_ADDRESSES)
        # Generate random address
        num = rng.randint(1, 200)
        street = rng.choice(["High St", "Main Rd", "Park Ave", "Oak Lane"])
        return f"{num} {street}, Birmingham, B{rng.randint(1,99)} {rng.randint(1,9)}AA"

    if key == "JobTicketReference":
        return f"{4000000 + id}/{rng.randint(1, 5)}"

    if key == "WorksOrderRef":
        return f"WO-{2025}{rng.randint(10000, 99999)}"

    if key in ["OperativeName", "OperativeWhoCompletedJob"]:
        if use_realistic:
            return rng.choice(REPAIRS_OPERATIVE_NAMES)
        first = rng.choice(["Tom", "Sam", "Ben", "Max", "Joe"])
        last = rng.choice(["Lee", "King", "Hill", "Wood", "Clark"])
        return f"{first} {last}"

    if key == "PropertyReference":
        return f"PROP-{100000 + id}"

    if key == "SchemeName":
        if use_realistic:
            return rng.choice(REPAIRS_SCHEMES)
        return f"SCHEME-{rng.randint(1, 50)}"

    if key == "WorksOrderDescription":
        if use_realistic:
            return rng.choice(REPAIRS_DESCRIPTIONS)
        return f"Repair work required - ticket {id}"

    if key == "WorksOrderPriorityDescription":
        return rng.choice(REPAIRS_PRIORITIES)

    if key == "WorksOrderStatusDescription":
        return rng.choice(REPAIRS_STATUSES)

    # Cost fields
    if key == "ActualCost":
        return round(rng.uniform(50.0, 500.0), 2)

    if key == "TargetCost":
        return round(rng.uniform(40.0, 450.0), 2)

    if key == "VarianceCost":
        return round(rng.uniform(-50.0, 100.0), 2)

    # Duration fields
    if key == "DaysToComplete":
        return rng.randint(0, 14)

    if key == "HoursToComplete":
        return rng.randint(0, 23)

    if key == "MinutesToComplete":
        return rng.randint(0, 59)

    if key == "JobTicketLineStatus":
        return rng.choice(["Complete", "Pending", "Cancelled", "In Progress"])

    if key == "ContractorName":
        return rng.choice(REPAIRS_CONTRACTORS)

    if key == "TradeDescription":
        return rng.choice(REPAIRS_TRADES)

    if key == "SORDescription":
        return rng.choice(REPAIRS_SOR)

    # Default fallback
    return f"{key}_value_{id}"


async def toggle_jsonb_mode(
    client: AsyncClient,
    enabled: bool,
    headers: dict = None,
) -> dict:
    """
    Switch storage mode at runtime via debug endpoint.

    Useful for tests that need to verify behavior across storage modes.

    :param client: AsyncClient instance for making HTTP requests
    :param enabled: Storage mode flag
    :param headers: Optional headers dict (must include Authorization)
    :return: Response JSON from the debug endpoint

    Usage:
        from . import HEADERS
        await toggle_jsonb_mode(client, enabled=True, headers=HEADERS)
    """
    response = await client.post(
        "/v0/_debug/jsonb_mode",
        params={"enabled": enabled},
        headers=headers,
    )
    assert response.status_code == 200, f"Failed to toggle JSONB mode: {response.text}"
    return response.json()


@pytest.fixture(scope="session")
def _engine_session_prod() -> Generator[Engine, None, None]:
    """
    Create engine that connects directly to the production database.
    Used for performance tests that need access to existing production data
    (e.g., RepairsDemo projects).

    WARNING: This connects to the REAL database, not a test database.
    Do not use this for tests that modify data destructively.

    :yield: engine connected to production database.
    """
    from orchestra.db.models import load_all_models  # noqa: WPS433

    load_all_models()

    # Connect directly to the production database (orchestra) instead of test database
    # The test environment sets ORCHESTRA_DB_BASE=orchestra_test, but we need the
    # real database for performance tests that require existing production data
    url = str(settings.db_url).replace("orchestra_test", "orchestra")
    engine = create_engine(url, isolation_level="AUTOCOMMIT")

    yield engine

    engine.dispose()


@pytest.fixture(scope="session")
def _engine_session(worker_id) -> Generator[Engine, None, None]:
    """
    Create engine and databases for session-scoped fixtures.
    Same as _engine but with session scope, specifically for large_log_dataset.

    :yield: new engine.
    """
    from orchestra.db.meta import meta  # noqa: WPS433
    from orchestra.db.models import load_all_models  # noqa: WPS433

    load_all_models()

    create_database(worker_id)

    # set the gcp bucket url to the test bucket
    os.environ["ORCHESTRA_GCP_BUCKET_NAME"] = "test-log-images-bucket"

    url = str(settings.db_url)
    # If using xdist, the testing database (orchestra_test) needs to be
    # instantiated for every thread
    if worker_id:
        url = url.replace("orchestra_test", f"orchestra_test_{worker_id}")
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    meta.create_all(engine)
    with engine.begin() as conn:
        # Create the hamming_distance function for tests
        conn.execute(
            text(
                """
           CREATE OR REPLACE FUNCTION hamming_distance(a text, b text)
RETURNS integer
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $$
WITH ba AS (SELECT decode(a, 'hex') AS v),
     bb AS (SELECT decode(b, 'hex') AS v)
SELECT CASE
  WHEN octet_length(ba.v) = 8 AND octet_length(bb.v) = 8 THEN
       bit_count(((get_byte(ba.v,0) # get_byte(bb.v,0))::bit(8))) +
       bit_count(((get_byte(ba.v,1) # get_byte(bb.v,1))::bit(8))) +
       bit_count(((get_byte(ba.v,2) # get_byte(bb.v,2))::bit(8))) +
       bit_count(((get_byte(ba.v,3) # get_byte(bb.v,3))::bit(8))) +
       bit_count(((get_byte(ba.v,4) # get_byte(bb.v,4))::bit(8))) +
       bit_count(((get_byte(ba.v,5) # get_byte(bb.v,5))::bit(8))) +
       bit_count(((get_byte(ba.v,6) # get_byte(bb.v,6))::bit(8))) +
       bit_count(((get_byte(ba.v,7) # get_byte(bb.v,7))::bit(8)))
  ELSE 999
END
FROM ba, bb;
$$;


        """,
            ),
        )
        # Create safe temporal casting functions for tests
        conn.execute(
            text(
                """
            CREATE OR REPLACE FUNCTION safe_cast_to_timestamptz(input_text TEXT)
            RETURNS TIMESTAMP WITH TIME ZONE AS $$
            BEGIN
                RETURN input_text::TIMESTAMP WITH TIME ZONE;
            EXCEPTION
                WHEN OTHERS THEN
                    RETURN NULL;
            END;
            $$ LANGUAGE plpgsql IMMUTABLE;
            """,
            ),
        )
        conn.execute(
            text(
                """
            CREATE OR REPLACE FUNCTION safe_cast_to_time(input_text TEXT)
            RETURNS TIME AS $$
            BEGIN
                RETURN input_text::TIME;
            EXCEPTION
                WHEN OTHERS THEN
                    RETURN NULL;
            END;
            $$ LANGUAGE plpgsql IMMUTABLE;
            """,
            ),
        )
        conn.execute(
            text(
                """
            CREATE OR REPLACE FUNCTION safe_cast_to_date(input_text TEXT)
            RETURNS DATE AS $$
            BEGIN
                RETURN input_text::DATE;
            EXCEPTION
                WHEN OTHERS THEN
                    RETURN NULL;
            END;
            $$ LANGUAGE plpgsql IMMUTABLE;
            """,
            ),
        )
        conn.execute(
            text(
                """
            CREATE OR REPLACE FUNCTION safe_cast_to_interval(input_text TEXT)
            RETURNS INTERVAL AS $$
            BEGIN
                RETURN input_text::INTERVAL;
            EXCEPTION
                WHEN OTHERS THEN
                    RETURN NULL;
            END;
            $$ LANGUAGE plpgsql IMMUTABLE;
            """,
            ),
        )

        user_id = str(os.getenv("AUTH_ACCOUNT_USER_ID"))
        api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
        with open("orchestra/tests/seeding.sql") as file:
            conn.execute(text(file.read()), {"user_id": user_id, "api_key": api_key})

    try:
        yield engine
    finally:
        engine.dispose()
        drop_database(worker_id)


@pytest.fixture(scope="session")
def large_log_dataset(_engine_session: Engine):
    """
    Session-scoped fixture that creates a large dataset of logs for performance testing.
    :param _engine_session: SQLAlchemy database engine with session scope.
    """
    from orchestra.db.dao.context_dao import ContextDAO
    from orchestra.db.dao.field_type_dao import FieldTypeDAO
    from orchestra.db.dao.log_dao import LogDAO
    from orchestra.db.dao.log_event_dao import LogEventDAO
    from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
    from orchestra.db.dao.project_dao import ProjectDAO

    # Create a local session
    SessionLocal = sessionmaker(bind=_engine_session, expire_on_commit=False)
    session = SessionLocal()

    # Initialize DAOs
    organization_member_dao = OrganizationMemberDAO(session=session)
    context_dao = ContextDAO(session=session)
    project_dao = ProjectDAO(
        session=session,
        organization_member_dao=organization_member_dao,
        context_dao=context_dao,
    )
    field_type_dao = FieldTypeDAO(session=session)
    log_event_dao = LogEventDAO(session=session)
    log_dao = LogDAO(session=session, context_dao=context_dao)

    # Create deterministic random number generator
    rng = random.Random(1234)

    # Create or get project
    user_id = os.getenv("AUTH_ACCOUNT_USER_ID")
    project_name = "perf-project"

    try:
        # Check if project exists
        existing_projects = project_dao.filter(user_id=user_id, name=project_name)
        if existing_projects:
            print("Project already exists")
            return

        print("Creating project for performance tests...")
        # Create project
        project_dao.create(name=project_name, user_id=user_id)
        project_id = project_dao.filter(user_id=user_id, name=project_name)[0][0].id
        # Create contexts
        context_names = ["ctx_big"]
        context_ids = []

        for ctx_name in context_names:
            context_id = context_dao.get_or_create(
                project_id=project_id,
                name=ctx_name,
                description=f"Performance test context {ctx_name}",
                is_versioned=False,
                allow_duplicates=True,
            )
            context_ids.append(context_id)

        # Sample values for field types
        sample_values = {
            "int_field": 42,
            "float_field": 3.14,
            "str_field": "text",
            "bool_field": True,
            "list_field": [1, 2, 3],
            "dict_field": {"a": 1, "b": 2},
            "ts_field": "2023-01-01T00:00:00",
            "long_text": "x" * 20000,  # 20,000 character string
            "category": "alpha",
            "tags": ["web", "mobile"],
        }

        # Register field types for each context
        for context_id in context_ids:
            field_types_data = []
            for field_name, value in sample_values.items():
                field_types_data.append(
                    {
                        "project_id": project_id,
                        "field_name": field_name,
                        "value": value,
                        "context_id": context_id,
                        "mutable": True,
                        "field_category": "entry",
                    },
                )

            field_type_dao.bulk_create_field_types(field_types_data)

        # Create log events and entries for each context
        for i, context_id in enumerate(context_ids):
            # Create log events per context
            count = (
                int(os.getenv("ORCHESTRA_PERF_LOG_EVENTS_COUNT"))
                if i == 0
                else int(os.getenv("ORCHESTRA_PERF_LOG_EVENTS_COUNT")) // 2
            )
            log_event_ids = log_event_dao.bulk_create(
                project_id=project_id,
                count=count,
                context_id=context_id,
            )

            # Create log entries in batches of 500
            batch_size = 500
            for j in range(0, len(log_event_ids), batch_size):
                batch_ids = log_event_ids[j : j + batch_size]
                entries = []

                for log_event_id in batch_ids:
                    # Create entries with different data types
                    for field_name in sample_values.keys():
                        # Generate varied values using helper function
                        value = _make_value(field_name, log_event_id, j, rng)

                        entries.append(
                            {
                                "project_id": project_id,
                                "log_event_id": log_event_id,
                                "key": field_name,
                                "value": value,
                                "context_id": context_id,
                            },
                        )

                # Bulk create log entries
                log_dao.bulk_create(entries)
        yield

    finally:
        # Ensure we close the session in all cases
        session.close()


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_bucket():
    """
    Fixture to clean up all images in the test bucket after test session completes.
    This helps prevent accumulation of test images and associated costs.
    """
    yield  # Allow tests to run

    try:
        client = storage.Client()
        bucket = client.bucket("test-log-images-bucket")
        blobs = bucket.list_blobs()
        for blob in blobs:
            blob.delete()
    except Exception as e:
        print(f"Warning: Failed to cleanup test bucket: {str(e)}")


def pytest_sessionfinish(session, exitstatus):
    """
    Generate test results and performance comparison reports.

    Outputs:
    - perf_timings.json
    - repairs_perf_results.json
    - repairs_perf_report.html
    - dual_mode_test_results.json
    - dual_mode_test_results.csv

    :param session: The pytest session object.
    :param exitstatus: The exit status of the session.
    """
    # ========================================================================
    # Test Results Report
    # ========================================================================

    # Calculate totals first to determine if we have any tracked parametrized tests
    eav_total = sum(
        len(TEST_RESULTS_BY_MODE["eav_mode"][k])
        for k in ["passed", "failed", "skipped"]
    )
    jsonb_total = sum(
        len(TEST_RESULTS_BY_MODE["jsonb_mode"][k])
        for k in ["passed", "failed", "skipped"]
    )

    # Generate reports if we have any tracked tests (not just passed tests)
    # This ensures all-fail or all-skip scenarios are still reported
    if eav_total > 0 or jsonb_total > 0:
        print("\n" + "=" * 80)
        print("TEST RESULTS SUMMARY")
        print("=" * 80)

        # Calculate detailed statistics
        eav_passed = len(TEST_RESULTS_BY_MODE["eav_mode"]["passed"])
        jsonb_passed = len(TEST_RESULTS_BY_MODE["jsonb_mode"]["passed"])

        eav_failed = len(TEST_RESULTS_BY_MODE["eav_mode"]["failed"])
        jsonb_failed = len(TEST_RESULTS_BY_MODE["jsonb_mode"]["failed"])

        eav_skipped = len(TEST_RESULTS_BY_MODE["eav_mode"]["skipped"])
        jsonb_skipped = len(TEST_RESULTS_BY_MODE["jsonb_mode"]["skipped"])

        # Print summary table
        print(
            f"\n{'Mode':<15} {'Total':<10} {'Passed':<10} {'Failed':<10} {'Skipped':<10} {'Pass Rate':<10}",
        )
        print("-" * 80)

        # Handle division-by-zero when calculating pass rates
        eav_rate = f"{(eav_passed/eav_total*100):.1f}%" if eav_total > 0 else "N/A"
        jsonb_rate = (
            f"{(jsonb_passed/jsonb_total*100):.1f}%" if jsonb_total > 0 else "N/A"
        )

        print(
            f"{'EAV Mode':<15} {eav_total:<10} {eav_passed:<10} {eav_failed:<10} {eav_skipped:<10} {eav_rate:<10}",
        )
        print(
            f"{'JSONB Mode':<15} {jsonb_total:<10} {jsonb_passed:<10} {jsonb_failed:<10} {jsonb_skipped:<10} {jsonb_rate:<10}",
        )

        # Export to JSON - handle pass_rate calculation safely
        def safe_pass_rate(rate_str):
            """Convert pass rate string to float, handling N/A case."""
            if rate_str == "N/A":
                return None
            try:
                return float(rate_str.rstrip("%"))
            except (ValueError, AttributeError):
                return None

        results_json = {
            "eav_mode": {
                "total": eav_total,
                "passed": eav_passed,
                "failed": eav_failed,
                "skipped": eav_skipped,
                "pass_rate": safe_pass_rate(eav_rate),
                "failed_tests": TEST_RESULTS_BY_MODE["eav_mode"]["failed"],
            },
            "jsonb_mode": {
                "total": jsonb_total,
                "passed": jsonb_passed,
                "failed": jsonb_failed,
                "skipped": jsonb_skipped,
                "pass_rate": safe_pass_rate(jsonb_rate),
                "failed_tests": TEST_RESULTS_BY_MODE["jsonb_mode"]["failed"],
            },
        }

        with open("dual_mode_test_results.json", "w") as f:
            json.dump(results_json, f, indent=2)

        print(f"\n✓ Test results exported to: dual_mode_test_results.json")

        # Export to CSV
        with open("dual_mode_test_results.csv", "w") as f:
            f.write("Mode,Total,Passed,Failed,Skipped,Pass Rate\n")
            f.write(
                f"EAV,{eav_total},{eav_passed},{eav_failed},{eav_skipped},{eav_rate}\n",
            )
            f.write(
                f"JSONB,{jsonb_total},{jsonb_passed},{jsonb_failed},{jsonb_skipped},{jsonb_rate}\n",
            )

        print(f"✓ Test results exported to: dual_mode_test_results.csv")
        print("=" * 80 + "\n")
    else:
        # Log message when no parametrized tests were tracked
        print("\n[INFO] No parametrized tests were tracked. Reports not generated.\n")

    # ========================================================================
    # Performance Timing Reports (Existing Functionality)
    # ========================================================================

    if not TIMING_RECORDS:
        return

    try:
        # =====================================================================
        # 1. BACKWARD COMPATIBILITY: Write original perf_timings.json
        # =====================================================================
        stats = {}
        for record in TIMING_RECORDS:
            path = record["path"]
            if path not in stats:
                stats[path] = {
                    "count": 0,
                    "total_duration": 0,
                    "min_duration": float("inf"),
                    "max_duration": 0,
                    "status_codes": {},
                }

            stats[path]["count"] += 1
            stats[path]["total_duration"] += record["duration"]
            stats[path]["min_duration"] = min(
                stats[path]["min_duration"],
                record["duration"],
            )
            stats[path]["max_duration"] = max(
                stats[path]["max_duration"],
                record["duration"],
            )

            status_code = str(record["status_code"])
            if status_code not in stats[path]["status_codes"]:
                stats[path]["status_codes"][status_code] = 0
            stats[path]["status_codes"][status_code] += 1

        for path in stats:
            stats[path]["avg_duration"] = (
                stats[path]["total_duration"] / stats[path]["count"]
            )

        output_data = {"records": TIMING_RECORDS, "statistics": stats}
        with open("perf_timings.json", "w") as f:
            json.dump(output_data, f, indent=2)

        # =====================================================================
        # 2. PARSE AND GROUP TEST RECORDS BY TEST NAME
        # =====================================================================
        test_groups = defaultdict(lambda: {"eav": [], "jsonb": []})

        for record in TIMING_RECORDS:
            # Use the test_name and mode captured by TimedAsyncClient
            test_name = record.get("test_name", "unknown")
            mode = record.get("mode", "unknown")

            # Fallback: try to infer mode from project name in params (backward compat)
            if mode == "unknown" or mode is None:
                params = record.get("params", {})
                project = params.get("project", "") if isinstance(params, dict) else ""
                if "EAV" in str(project):
                    mode = "eav"
                elif "JSONB" in str(project):
                    mode = "jsonb"

            if mode in ("eav", "jsonb") and test_name != "unknown":
                test_groups[test_name][mode].append(record)

        # =====================================================================
        # 3. COMPUTE STATISTICS WITH OUTLIER DETECTION (3σ rule)
        # =====================================================================
        def compute_stats(records):
            """Compute robust statistics with outlier removal."""
            if not records:
                return None

            durations = np.array(
                [r["duration"] * 1000 for r in records],
            )  # Convert to ms

            if len(durations) < 3:
                # Not enough data for outlier detection
                filtered = durations
                outliers_removed = 0
            else:
                mean = np.mean(durations)
                std = np.std(durations)
                if std > 0:
                    mask = np.abs(durations - mean) <= 3 * std
                    filtered = durations[mask]
                    outliers_removed = len(durations) - len(filtered)
                else:
                    filtered = durations
                    outliers_removed = 0

            if len(filtered) == 0:
                filtered = durations  # Fallback to all data

            return {
                "count": len(records),
                "avg_ms": float(np.mean(filtered)),
                "median_ms": float(np.median(filtered)),
                "p95_ms": float(np.percentile(filtered, 95))
                if len(filtered) > 1
                else float(filtered[0]),
                "min_ms": float(np.min(filtered)),
                "max_ms": float(np.max(filtered)),
                "outliers_removed": outliers_removed,
            }

        # =====================================================================
        # 4. MATCH TEST PAIRS AND CALCULATE SPEEDUPS
        # =====================================================================
        test_pairs = []
        speedups = []

        for test_name, modes in test_groups.items():
            eav_stats = compute_stats(modes["eav"])
            jsonb_stats = compute_stats(modes["jsonb"])

            pair = {
                "test_name": test_name,
                "eav": eav_stats,
                "jsonb": jsonb_stats,
                "speedup": None,
                "time_saved_ms": None,
                "is_regression": None,
            }

            if eav_stats and jsonb_stats:
                if jsonb_stats["avg_ms"] > 0:
                    speedup = eav_stats["avg_ms"] / jsonb_stats["avg_ms"]
                    pair["speedup"] = speedup
                    pair["time_saved_ms"] = eav_stats["avg_ms"] - jsonb_stats["avg_ms"]
                    pair["is_regression"] = speedup < 1.0
                    speedups.append(speedup)
                else:
                    pair["speedup"] = float("inf")
                    pair["time_saved_ms"] = eav_stats["avg_ms"]
                    pair["is_regression"] = False

            test_pairs.append(pair)

        # Sort by speedup (highest first)
        test_pairs.sort(
            key=lambda x: x["speedup"] if x["speedup"] is not None else 0,
            reverse=True,
        )

        # =====================================================================
        # 5. COMPUTE AGGREGATE STATISTICS
        # =====================================================================
        valid_speedups = [
            p["speedup"]
            for p in test_pairs
            if p["speedup"] is not None and p["speedup"] != float("inf")
        ]
        time_savings = [
            p["time_saved_ms"] for p in test_pairs if p["time_saved_ms"] is not None
        ]

        aggregate_stats = {
            "num_tests": len(test_pairs),
            "num_paired_tests": len(valid_speedups),
            "num_regressions": sum(1 for p in test_pairs if p["is_regression"]),
            "mean_speedup": float(np.mean(valid_speedups)) if valid_speedups else None,
            "median_speedup": float(np.median(valid_speedups))
            if valid_speedups
            else None,
            "p95_speedup": float(np.percentile(valid_speedups, 95))
            if len(valid_speedups) > 1
            else (valid_speedups[0] if valid_speedups else None),
            "total_time_saved_ms": sum(time_savings) if time_savings else 0,
            "max_speedup": max(valid_speedups) if valid_speedups else None,
            "min_speedup": min(valid_speedups) if valid_speedups else None,
        }

        # =====================================================================
        # 6. CONSOLE SUMMARY TABLE
        # =====================================================================
        print("\n" + "=" * 100)
        print("=== Storage Mode Performance Comparison ===")
        print("=" * 100)

        # Header
        print(
            f"\n{'Test':<55} | {'EAV (ms)':>12} | {'JSONB (ms)':>12} | {'Speedup':>10} | {'Saved (ms)':>12}",
        )
        print("-" * 100)

        for pair in test_pairs:
            test_name = (
                pair["test_name"][:52] + "..."
                if len(pair["test_name"]) > 55
                else pair["test_name"]
            )
            eav_ms = f"{pair['eav']['avg_ms']:.2f}" if pair["eav"] else "N/A"
            jsonb_ms = f"{pair['jsonb']['avg_ms']:.2f}" if pair["jsonb"] else "N/A"

            if pair["speedup"] is not None and pair["speedup"] != float("inf"):
                speedup = f"{pair['speedup']:.2f}x"
                saved = f"{pair['time_saved_ms']:.2f}"
                # Mark regressions
                if pair["is_regression"]:
                    speedup = f"{speedup} ⚠️"
            else:
                speedup = "N/A"
                saved = "N/A"

            print(
                f"{test_name:<55} | {eav_ms:>12} | {jsonb_ms:>12} | {speedup:>10} | {saved:>12}",
            )

        # Footer with aggregate stats
        print("-" * 100)
        print("\nAggregate Statistics:")
        print(f"  Total Tests: {aggregate_stats['num_tests']}")
        print(
            f"  Paired Tests (both EAV & JSONB): {aggregate_stats['num_paired_tests']}",
        )
        if aggregate_stats["mean_speedup"]:
            print(f"  Mean Speedup: {aggregate_stats['mean_speedup']:.2f}x")
            print(f"  Median Speedup: {aggregate_stats['median_speedup']:.2f}x")
            print(f"  P95 Speedup: {aggregate_stats['p95_speedup']:.2f}x")
            print(f"  Max Speedup: {aggregate_stats['max_speedup']:.2f}x")
            print(f"  Min Speedup: {aggregate_stats['min_speedup']:.2f}x")
        print(f"  Total Time Saved: {aggregate_stats['total_time_saved_ms']:.2f} ms")
        print(f"  Regressions (JSONB slower): {aggregate_stats['num_regressions']}")

        # =====================================================================
        # 7. JSON REPORT GENERATION
        # =====================================================================
        json_report = {
            "test_pairs": test_pairs,
            "aggregate_stats": aggregate_stats,
            "raw_records": TIMING_RECORDS,
        }

        with open("repairs_perf_results.json", "w") as f:
            json.dump(json_report, f, indent=2, default=str)

        # =====================================================================
        # 8. CHART GENERATION
        # =====================================================================
        chart_images = {}

        # Only generate charts if we have paired test data and matplotlib is available
        paired_tests = [p for p in test_pairs if p["eav"] and p["jsonb"]]

        if paired_tests and HAS_MATPLOTLIB:
            # Chart 1: Horizontal double bar chart comparing EAV vs JSONB per test
            # Shows speedup annotations for easy comparison
            try:
                # Sort by speedup for better visualization
                sorted_paired = sorted(
                    paired_tests,
                    key=lambda x: x["speedup"] if x["speedup"] else 0,
                    reverse=True,
                )

                # Limit to top 30 tests for readability
                display_tests = sorted_paired[:30]
                num_tests = len(display_tests)

                # Calculate figure height based on number of tests
                fig_height = max(10, num_tests * 0.4)
                fig, ax = plt.subplots(figsize=(14, fig_height))

                test_names = [
                    p["test_name"][:50] + "..."
                    if len(p["test_name"]) > 50
                    else p["test_name"]
                    for p in display_tests
                ]
                eav_times = [p["eav"]["avg_ms"] for p in display_tests]
                jsonb_times = [p["jsonb"]["avg_ms"] for p in display_tests]
                speedups = [p["speedup"] for p in display_tests]

                y = np.arange(num_tests)
                height = 0.35

                # Create horizontal bars
                bars1 = ax.barh(
                    y - height / 2,
                    eav_times,
                    height,
                    label="EAV",
                    color="#e74c3c",
                    alpha=0.8,
                )
                bars2 = ax.barh(
                    y + height / 2,
                    jsonb_times,
                    height,
                    label="JSONB",
                    color="#27ae60",
                    alpha=0.8,
                )

                # Add speedup annotations at the end of each bar group
                for i, (eav_t, jsonb_t, speedup) in enumerate(
                    zip(eav_times, jsonb_times, speedups),
                ):
                    max_time = max(eav_t, jsonb_t)
                    if speedup and speedup != float("inf"):
                        color = "#27ae60" if speedup >= 1.0 else "#e74c3c"
                        label = f"{speedup:.1f}x"
                        ax.annotate(
                            label,
                            xy=(max_time, i),
                            xytext=(5, 0),
                            textcoords="offset points",
                            va="center",
                            fontsize=9,
                            fontweight="bold",
                            color=color,
                        )

                ax.set_ylabel("Test Name", fontsize=12)
                ax.set_xlabel("Duration (ms)", fontsize=12)
                ax.set_title(
                    "EAV vs JSONB Performance Comparison (Per Test)\n"
                    "Speedup shown at right (green = JSONB faster, red = EAV faster)",
                    fontsize=14,
                    fontweight="bold",
                )
                ax.set_yticks(y)
                ax.set_yticklabels(test_names, fontsize=8)
                ax.legend(loc="lower right")
                ax.grid(axis="x", alpha=0.3)

                # Use log scale if range is large
                all_times = eav_times + jsonb_times
                if max(all_times) / (min(all_times) + 0.001) > 100:
                    ax.set_xscale("log")

                # Invert y-axis so highest speedup is at top
                ax.invert_yaxis()

                plt.tight_layout()
                # Save to file FIRST, before closing
                plt.savefig("repairs_perf_bar_chart.png", dpi=100, bbox_inches="tight")
                # Then save to buffer for HTML embedding
                buf = io.BytesIO()
                plt.savefig(buf, format="png", dpi=100, bbox_inches="tight")
                buf.seek(0)
                chart_images["bar_chart"] = base64.b64encode(buf.read()).decode()
                plt.close(fig)
            except Exception as e:
                print(f"Warning: Failed to generate bar chart: {e}")

            # Chart 2: Histogram of speedup distribution
            try:
                fig, ax = plt.subplots(figsize=(10, 6))
                speedup_values = [
                    p["speedup"]
                    for p in paired_tests
                    if p["speedup"] and p["speedup"] != float("inf")
                ]

                if speedup_values:
                    ax.hist(
                        speedup_values,
                        bins=20,
                        color="#3498db",
                        alpha=0.7,
                        edgecolor="black",
                    )
                    ax.axvline(
                        x=1.0,
                        color="#e74c3c",
                        linestyle="--",
                        linewidth=2,
                        label="No Improvement (1.0x)",
                    )
                    ax.axvline(
                        x=np.mean(speedup_values),
                        color="#27ae60",
                        linestyle="-",
                        linewidth=2,
                        label=f"Mean ({np.mean(speedup_values):.2f}x)",
                    )

                    ax.set_xlabel("Speedup Factor (EAV time / JSONB time)", fontsize=12)
                    ax.set_ylabel("Number of Tests", fontsize=12)
                    ax.set_title(
                        "Distribution of Speedup Factors",
                        fontsize=14,
                        fontweight="bold",
                    )
                    ax.legend()
                    ax.grid(alpha=0.3)

                    plt.tight_layout()
                    # Save to file FIRST, before closing
                    plt.savefig(
                        "repairs_perf_speedup_hist.png",
                        dpi=100,
                        bbox_inches="tight",
                    )
                    # Then save to buffer for HTML embedding
                    buf = io.BytesIO()
                    plt.savefig(buf, format="png", dpi=100, bbox_inches="tight")
                    buf.seek(0)
                    chart_images["speedup_hist"] = base64.b64encode(buf.read()).decode()
                    plt.close(fig)
            except Exception as e:
                print(f"Warning: Failed to generate speedup histogram: {e}")

            # Chart 3: Waterfall chart showing cumulative time savings
            try:
                fig, ax = plt.subplots(figsize=(14, 8))
                sorted_pairs = sorted(
                    [p for p in paired_tests if p["time_saved_ms"] is not None],
                    key=lambda x: x["time_saved_ms"],
                    reverse=True,
                )[
                    :20
                ]  # Top 20

                if sorted_pairs:
                    test_names = [
                        p["test_name"][:35] + "..."
                        if len(p["test_name"]) > 35
                        else p["test_name"]
                        for p in sorted_pairs
                    ]
                    time_savings_vals = [p["time_saved_ms"] for p in sorted_pairs]
                    cumulative = np.cumsum(time_savings_vals)

                    colors = [
                        "#27ae60" if t > 0 else "#e74c3c" for t in time_savings_vals
                    ]
                    ax.bar(
                        range(len(test_names)),
                        time_savings_vals,
                        color=colors,
                        alpha=0.8,
                    )
                    ax.plot(
                        range(len(test_names)),
                        cumulative,
                        "o-",
                        color="#3498db",
                        linewidth=2,
                        label="Cumulative",
                    )

                    ax.set_xlabel("Test Name", fontsize=12)
                    ax.set_ylabel("Time Saved (ms)", fontsize=12)
                    ax.set_title(
                        "Time Savings by Test (Sorted by Impact)",
                        fontsize=14,
                        fontweight="bold",
                    )
                    ax.set_xticks(range(len(test_names)))
                    ax.set_xticklabels(test_names, rotation=45, ha="right", fontsize=8)
                    ax.legend()
                    ax.grid(axis="y", alpha=0.3)
                    ax.axhline(y=0, color="black", linestyle="-", linewidth=0.5)

                    plt.tight_layout()
                    # Save to file FIRST, before closing
                    plt.savefig(
                        "repairs_perf_waterfall.png",
                        dpi=100,
                        bbox_inches="tight",
                    )
                    # Then save to buffer for HTML embedding
                    buf = io.BytesIO()
                    plt.savefig(buf, format="png", dpi=100, bbox_inches="tight")
                    buf.seek(0)
                    chart_images["waterfall"] = base64.b64encode(buf.read()).decode()
                    plt.close(fig)
            except Exception as e:
                print(f"Warning: Failed to generate waterfall chart: {e}")

        # =====================================================================
        # 9. HTML REPORT GENERATION
        # =====================================================================
        html_rows = []
        for pair in test_pairs:
            test_name = pair["test_name"]
            eav_ms = f"{pair['eav']['avg_ms']:.2f}" if pair["eav"] else "N/A"
            jsonb_ms = f"{pair['jsonb']['avg_ms']:.2f}" if pair["jsonb"] else "N/A"

            if pair["speedup"] is not None and pair["speedup"] != float("inf"):
                speedup = f"{pair['speedup']:.2f}x"
                saved = f"{pair['time_saved_ms']:.2f}"
                row_class = (
                    "regression"
                    if pair["is_regression"]
                    else ("improvement" if pair["speedup"] > 1.5 else "")
                )
            else:
                speedup = "N/A"
                saved = "N/A"
                row_class = ""

            html_rows.append(
                f'<tr class="{row_class}"><td>{test_name}</td><td>{eav_ms}</td><td>{jsonb_ms}</td><td>{speedup}</td><td>{saved}</td></tr>',
            )

        # Build chart HTML
        chart_html = ""
        if "bar_chart" in chart_images:
            chart_html += f'<h2>Storage Mode Query Times</h2><img src="data:image/png;base64,{chart_images["bar_chart"]}" alt="Bar Chart">'
        if "speedup_hist" in chart_images:
            chart_html += f'<h2>Speedup Distribution</h2><img src="data:image/png;base64,{chart_images["speedup_hist"]}" alt="Speedup Histogram">'
        if "waterfall" in chart_images:
            chart_html += f'<h2>Time Savings Impact</h2><img src="data:image/png;base64,{chart_images["waterfall"]}" alt="Waterfall Chart">'

        html_template = f"""<!DOCTYPE html>
<html>
<head>
    <title>Storage Mode Performance Report</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 40px; background: #f5f5f5; }}
        .container {{ max-width: 1400px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        h1 {{ color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }}
        h2 {{ color: #34495e; margin-top: 30px; }}
        .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin: 20px 0; }}
        .stat-card {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; border-radius: 8px; text-align: center; }}
        .stat-card.green {{ background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); }}
        .stat-card.orange {{ background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); }}
        .stat-value {{ font-size: 2em; font-weight: bold; }}
        .stat-label {{ font-size: 0.9em; opacity: 0.9; }}
        table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
        th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #ddd; }}
        th {{ background: #3498db; color: white; position: sticky; top: 0; }}
        tr:hover {{ background: #f8f9fa; }}
        .regression {{ background: #ffebee !important; }}
        .improvement {{ background: #e8f5e9 !important; }}
        img {{ max-width: 100%; height: auto; margin: 20px 0; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        .timestamp {{ color: #7f8c8d; font-size: 0.9em; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🚀 Storage Mode Performance Report</h1>
        <p class="timestamp">Generated: {datetime.now().isoformat()}</p>

        <div class="summary">
            <div class="stat-card">
                <div class="stat-value">{aggregate_stats['num_paired_tests']}</div>
                <div class="stat-label">Paired Tests</div>
            </div>
            <div class="stat-card green">
                <div class="stat-value">{f"{aggregate_stats['mean_speedup']:.2f}x" if aggregate_stats['mean_speedup'] is not None else "N/A"}</div>
                <div class="stat-label">Mean Speedup</div>
            </div>
            <div class="stat-card green">
                <div class="stat-value">{f"{aggregate_stats['median_speedup']:.2f}x" if aggregate_stats['median_speedup'] is not None else "N/A"}</div>
                <div class="stat-label">Median Speedup</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{aggregate_stats['total_time_saved_ms']:.0f}ms</div>
                <div class="stat-label">Total Time Saved</div>
            </div>
            <div class="stat-card orange">
                <div class="stat-value">{aggregate_stats['num_regressions']}</div>
                <div class="stat-label">Regressions</div>
            </div>
        </div>

        {chart_html}

        <h2>Detailed Results</h2>
        <table>
            <thead>
                <tr>
                    <th>Test Name</th>
                    <th>EAV (ms)</th>
                    <th>JSONB (ms)</th>
                    <th>Speedup</th>
                    <th>Time Saved (ms)</th>
                </tr>
            </thead>
            <tbody>
                {''.join(html_rows)}
            </tbody>
        </table>

        <h2>Legend</h2>
        <ul>
            <li><span style="background:#e8f5e9;padding:2px 8px;">Green rows</span> = Significant improvement (>1.5x speedup)</li>
            <li><span style="background:#ffebee;padding:2px 8px;">Red rows</span> = Regression (JSONB slower than EAV)</li>
            <li>Speedup = EAV time / JSONB time (higher is better)</li>
        </ul>
    </div>
</body>
</html>"""

        with open("repairs_perf_report.html", "w") as f:
            f.write(html_template)

        # =====================================================================
        # 10. FINAL OUTPUT
        # =====================================================================
        print("\n" + "=" * 100)
        print("Reports generated:")
        print(f"  - perf_timings.json (backward compatible)")
        print(f"  - repairs_perf_results.json (detailed JSON)")
        print(f"  - repairs_perf_report.html (visual report)")
        if chart_images:
            print(f"  - repairs_perf_bar_chart.png")
            print(f"  - repairs_perf_speedup_hist.png")
            print(f"  - repairs_perf_waterfall.png")
        print("=" * 100)

    except Exception as e:
        # Error handling: ensure basic output even if report generation fails
        print(f"\n⚠️ Error generating performance reports: {e}")
        traceback.print_exc()
        # Still try to write basic JSON
        try:
            with open("perf_timings.json", "w") as f:
                json.dump({"records": TIMING_RECORDS, "error": str(e)}, f, indent=2)
            print("Basic timing data saved to perf_timings.json")
        except Exception:
            pass


### REPAIRS DEMO PERFORMANCE TEST FIXTURES ###

# Context path for RepairsDemo data
REPAIRS_CONTEXT_PATH = "Assistant/Files/Local/_Users_yushaar____tranet_repairs_MDH_Repairs_Data_July_-_Sept_25_-_DL_V1_xlsx/Tables/Raised_01-07-2025_to_30-09-2025"


@pytest.fixture(scope="session")
def repairs_eav_project(_engine_session_prod: Engine) -> Generator[str, None, None]:
    """
    Session-scoped fixture that retrieves the existing RepairsAgent_EAV project.

    :param _engine_session_prod: SQLAlchemy database engine connected to production DB.
    :yield: Project name for RepairsAgent_EAV (used by the API)
    :raises ValueError: If project is not found in the database
    """
    from orchestra.db.models.orchestra_models import Project

    SessionLocal = sessionmaker(bind=_engine_session_prod, expire_on_commit=False)
    session = SessionLocal()

    try:
        # Query project directly by name (doesn't require user_id)
        project = (
            session.query(Project).filter(Project.name == "RepairsAgent_EAV").first()
        )

        if not project:
            raise ValueError(
                "RepairsAgent_EAV project not found. Please ensure the project "
                "exists in the database before running performance tests.",
            )

        print(f"Found RepairsAgent_EAV project: {project.name} (ID: {project.id})")
        yield project.name

    finally:
        session.close()


@pytest.fixture(scope="session")
def repairs_jsonb_project(_engine_session_prod: Engine) -> Generator[str, None, None]:
    """
    Session-scoped fixture that retrieves the existing RepairsAgent_JSONB project.

    :param _engine_session_prod: SQLAlchemy database engine connected to production DB.
    :yield: Project name for RepairsAgent_JSONB (used by the API)
    :raises ValueError: If project is not found in the database
    """
    from orchestra.db.models.orchestra_models import Project

    SessionLocal = sessionmaker(bind=_engine_session_prod, expire_on_commit=False)
    session = SessionLocal()

    try:
        # Query project directly by name (doesn't require user_id)
        project = (
            session.query(Project).filter(Project.name == "RepairsAgent_JSONB").first()
        )

        if not project:
            raise ValueError(
                "RepairsAgent_JSONB project not found. Please ensure the project "
                "exists in the database before running performance tests.",
            )

        print(f"Found RepairsAgent_JSONB project: {project.name} (ID: {project.id})")
        yield project.name

    finally:
        session.close()


@pytest.fixture(scope="session")
def repairs_context(
    _engine_session_prod: Engine,
    repairs_eav_project: str,
) -> Generator[str, None, None]:
    """
    Session-scoped fixture that retrieves the context name for RepairsDemo data.

    :param _engine_session_prod: SQLAlchemy database engine connected to production DB.
    :param repairs_eav_project: Project name for the EAV project (used to verify context exists).
    :yield: Context name (path) for the RepairsDemo context (used by the API)
    :raises ValueError: If context is not found in the database
    """
    from orchestra.db.models.orchestra_models import Context, Project

    SessionLocal = sessionmaker(bind=_engine_session_prod, expire_on_commit=False)
    session = SessionLocal()

    try:
        # Query project directly by name
        project = (
            session.query(Project).filter(Project.name == repairs_eav_project).first()
        )

        if not project:
            raise ValueError(f"Project {repairs_eav_project} not found")

        # Query context by project_id and name
        context = (
            session.query(Context)
            .filter(
                Context.project_id == project.id,
                Context.name == REPAIRS_CONTEXT_PATH,
            )
            .first()
        )

        if not context:
            raise ValueError(
                f"RepairsDemo context not found at path: {REPAIRS_CONTEXT_PATH}. "
                "Please ensure the context exists in the database before running "
                "performance tests.",
            )

        print(f"Found RepairsDemo context: {context.name} (ID: {context.id})")
        # Yield the context name (path) since that's what the API expects
        yield context.name

    finally:
        session.close()


@pytest.fixture(scope="session")
def large_repairs_dataset(
    _engine_session_prod: Engine,
    repairs_eav_project: str,
    repairs_jsonb_project: str,
    repairs_context: str,
) -> Generator[None, None, None]:
    """
    Session-scoped fixture that creates ~40k log events for RepairsDemo performance testing.

    Creates bulk data for both EAV and JSONB projects to enable comparative performance
    testing. Uses realistic RepairsDemo field values with 80% realistic patterns and
    20% randomized variations.

    Note: This fixture does NOT toggle JSONB mode via the `/_debug/jsonb_mode` endpoint.
    It only creates raw database records. Tests are responsible for toggling JSONB mode
    as needed using the `toggle_jsonb_mode` helper function before executing queries.

    :param _engine_session_prod: SQLAlchemy database engine connected to production DB.
    :param repairs_eav_project: Project name for RepairsAgent_EAV.
    :param repairs_jsonb_project: Project name for RepairsAgent_JSONB.
    :param repairs_context: Context name (path) for the RepairsDemo context.
    :yield: Nothing (data is available in database during test session)
    """
    from sqlalchemy import delete

    from orchestra.db.dao.context_dao import ContextDAO
    from orchestra.db.dao.field_type_dao import FieldTypeDAO
    from orchestra.db.dao.log_dao import LogDAO
    from orchestra.db.dao.log_event_dao import LogEventDAO
    from orchestra.db.models.orchestra_models import (
        Context,
        LogEvent,
        LogEventContext,
        Project,
    )

    SessionLocal = sessionmaker(bind=_engine_session_prod, expire_on_commit=False)
    session = SessionLocal()

    # Create separate deterministic RNGs for EAV and JSONB data generation
    # to avoid cross-project coupling in generated data
    rng_eav = random.Random(5678)
    rng_jsonb = random.Random(5678)

    # Track IDs for cleanup
    created_eav_event_ids = []
    created_jsonb_event_ids = []

    try:
        # Look up project IDs from names for database operations

        eav_project = (
            session.query(Project).filter(Project.name == repairs_eav_project).first()
        )
        jsonb_project = (
            session.query(Project).filter(Project.name == repairs_jsonb_project).first()
        )

        if not eav_project or not jsonb_project:
            raise ValueError("Could not find EAV or JSONB project by name")

        eav_project_id = eav_project.id
        jsonb_project_id = jsonb_project.id

        # Look up context ID from name
        context_obj = (
            session.query(Context)
            .filter(
                Context.project_id == eav_project_id,
                Context.name == repairs_context,
            )
            .first()
        )
        if not context_obj:
            raise ValueError(f"Could not find context by name: {repairs_context}")
        context_id = context_obj.id

        # Check if bulk data already exists
        existing_count = session.execute(
            text(
                """
                SELECT COUNT(*) FROM log_event
                WHERE project_id IN (:eav_id, :jsonb_id) AND id > 1000
                """,
            ),
            {"eav_id": eav_project_id, "jsonb_id": jsonb_project_id},
        ).scalar()

        if existing_count and existing_count > 0:
            print(
                f"Bulk data already exists ({existing_count} events), skipping creation",
            )
            yield
            return

        # Initialize DAOs
        context_dao = ContextDAO(session=session)
        field_type_dao = FieldTypeDAO(session=session)
        log_event_dao = LogEventDAO(session=session)
        log_dao = LogDAO(session=session, context_dao=context_dao)

        # Number of events to create (configurable via env var)
        num_events = int(os.getenv("ORCHESTRA_PERF_REPAIRS_COUNT", "40000"))
        batch_size = 500

        print(f"Creating {num_events} log events for RepairsDemo performance tests...")

        # Register field types for RepairsDemo fields for BOTH projects
        # so that both EAV and JSONB projects have matching FieldType rows
        print("Registering field types for both EAV and JSONB projects...")
        field_types_data = []
        rng_field_types = random.Random(
            5678,
        )  # Separate RNG for field type sample values

        for pid in [eav_project_id, jsonb_project_id]:
            for field_name in REPAIRS_DEMO_FIELDS:
                field_types_data.append(
                    {
                        "project_id": pid,
                        "field_name": field_name,
                        "value": _make_repairs_value(field_name, 0, rng_field_types),
                        "context_id": context_id,
                        "mutable": True,
                        "field_category": "entry",
                    },
                )

            # Register embedding fields
            for emb_field in REPAIRS_DEMO_EMBEDDINGS:
                field_types_data.append(
                    {
                        "project_id": pid,
                        "field_name": emb_field,
                        "value": [0.0] * 1536,  # Mock embedding
                        "context_id": context_id,
                        "mutable": True,
                        "field_category": "entry",
                        "field_type": "List[float]",
                    },
                )

            # Reset RNG for consistent sample values across projects
            rng_field_types = random.Random(5678)

        field_type_dao.bulk_create_field_types(field_types_data)

        # === Create EAV project data ===
        print(f"Creating {num_events} log events for EAV project...")

        for batch_start in range(0, num_events, batch_size):
            batch_count = min(batch_size, num_events - batch_start)

            # Create log events
            log_event_ids = log_event_dao.bulk_create(
                project_id=eav_project_id,
                count=batch_count,
                context_id=context_id,
            )
            created_eav_event_ids.extend(log_event_ids)

            # Create log entries for each event
            entries = []
            for i, log_event_id in enumerate(log_event_ids):
                event_num = batch_start + i

                # Create entries for all 30 RepairsDemo fields
                for field_name in REPAIRS_DEMO_FIELDS:
                    value = _make_repairs_value(field_name, event_num, rng_eav)
                    entries.append(
                        {
                            "project_id": eav_project_id,
                            "log_event_id": log_event_id,
                            "key": field_name,
                            "value": value,
                            "context_id": context_id,
                        },
                    )

                # Create mock embeddings (1536-dim vectors with small random values)
                for emb_field in REPAIRS_DEMO_EMBEDDINGS:
                    mock_embedding = [rng_eav.gauss(0, 0.1) for _ in range(1536)]
                    entries.append(
                        {
                            "project_id": eav_project_id,
                            "log_event_id": log_event_id,
                            "key": emb_field,
                            "value": mock_embedding,
                            "context_id": context_id,
                            "explicit_types": {emb_field: {"type": "List[float]"}},
                        },
                    )

            # Bulk create log entries
            log_dao.bulk_create(entries)

            if (batch_start + batch_count) % 5000 == 0:
                print(f"  EAV: Created {batch_start + batch_count}/{num_events} events")

        print(f"  EAV: Completed {num_events} events")

        # === Create JSONB project data ===
        print(f"Creating {num_events} log events for JSONB project...")

        ts = datetime.now()

        for batch_start in range(0, num_events, batch_size):
            batch_count = min(batch_size, num_events - batch_start)

            # Create log events with JSONB data
            log_events = []
            for i in range(batch_count):
                event_num = batch_start + i

                # Build JSONB data dict
                data = {}
                for field_name in REPAIRS_DEMO_FIELDS:
                    data[field_name] = _make_repairs_value(
                        field_name,
                        event_num,
                        rng_jsonb,
                    )

                # Add mock embeddings
                for emb_field in REPAIRS_DEMO_EMBEDDINGS:
                    data[emb_field] = [rng_jsonb.gauss(0, 0.1) for _ in range(1536)]

                log_event = LogEvent(
                    project_id=jsonb_project_id,
                    data=data,
                    created_at=ts,
                    updated_at=ts,
                )
                log_events.append(log_event)

            session.add_all(log_events)
            session.flush()

            # Get IDs and create context associations
            for log_event in log_events:
                created_jsonb_event_ids.append(log_event.id)
                session.add(
                    LogEventContext(
                        log_event_id=log_event.id,
                        context_id=context_id,
                    ),
                )

            session.flush()

            if (batch_start + batch_count) % 5000 == 0:
                print(
                    f"  JSONB: Created {batch_start + batch_count}/{num_events} events",
                )

        session.commit()
        print(f"  JSONB: Completed {num_events} events")
        print(
            f"RepairsDemo bulk data creation complete: {len(created_eav_event_ids)} EAV + {len(created_jsonb_event_ids)} JSONB events",
        )

        yield

    finally:
        # Cleanup: Delete bulk-inserted data
        print("Cleaning up RepairsDemo bulk data...")

        try:
            if created_eav_event_ids:
                # Delete in batches to avoid memory issues
                for i in range(0, len(created_eav_event_ids), 1000):
                    batch_ids = created_eav_event_ids[i : i + 1000]
                    session.execute(
                        delete(LogEvent).where(LogEvent.id.in_(batch_ids)),
                    )
                session.commit()
                print(f"  Cleaned up {len(created_eav_event_ids)} EAV events")

            if created_jsonb_event_ids:
                for i in range(0, len(created_jsonb_event_ids), 1000):
                    batch_ids = created_jsonb_event_ids[i : i + 1000]
                    session.execute(
                        delete(LogEvent).where(LogEvent.id.in_(batch_ids)),
                    )
                session.commit()
                print(f"  Cleaned up {len(created_jsonb_event_ids)} JSONB events")

        except Exception as e:
            print(f"Warning: Failed to cleanup bulk data: {e}")
            session.rollback()

        session.close()
