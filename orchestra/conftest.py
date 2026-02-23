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
from orchestra.web.lifetime import flush_opentelemetry, setup_opentelemetry


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
    _engine: Engine,
) -> FastAPI:
    """
    Fixture for creating FastAPI app with OTel tracing enabled.

    Sets up db_engine on app state and configures OpenTelemetry instrumentation.
    When ORCHESTRA_LOG_DIR is set (e.g., in CI), traces are written to files.

    :return: fastapi app with mocked dependencies and OTel instrumentation.
    """
    application = get_app()
    application.dependency_overrides[get_db_session] = lambda: dbsession

    # Set up db_engine on app state (normally done in _setup_db during startup)
    # Required for SQLAlchemy instrumentation in setup_opentelemetry
    application.state.db_engine = _engine

    # Set up OTel tracing (idempotent - TracerProvider created once per process)
    # This enables trace capture during tests when ORCHESTRA_LOG_DIR is set
    setup_opentelemetry(application)

    return application


@pytest.fixture
def fastapi_app_concurrent(
    _engine: Engine,
    worker_id,
) -> Generator[FastAPI, None, None]:
    """
    FastAPI app fixture that uses INDEPENDENT sessions per request.

    Use this for tests that need true transaction isolation (e.g., advisory locks,
    concurrent insert tests). Each request gets its own session/connection.

    This fixture creates a SEPARATE engine with proper transaction management
    (not AUTOCOMMIT) to accurately reflect production behavior where:
    - session.commit() actually commits the transaction
    - pg_advisory_xact_lock is held until session.commit() is called
    - Advisory locks are released when the endpoint commits (not after)

    Note: Changes are NOT rolled back automatically - tests should use unique
    identifiers to avoid conflicts between test runs.

    :param _engine: database engine (used to get the database URL).
    :param worker_id: pytest-xdist worker ID for parallel test isolation.
    :yields: fastapi app with production-like session management.
    """
    from orchestra.settings import settings

    application = get_app()

    # Create a SEPARATE engine for concurrent tests with production-like settings
    # Production uses READ COMMITTED (PostgreSQL default) with proper transactions.
    url = str(settings.db_url)
    if worker_id:
        url = url.replace("orchestra_test", f"orchestra_test_{worker_id}")

    concurrent_engine = create_engine(
        url,
        isolation_level="READ COMMITTED",
        pool_size=50,
        max_overflow=50,
        pool_pre_ping=True,
    )

    # Create session factory matching production's pattern
    SessionFactory = sessionmaker(
        bind=concurrent_engine,
        expire_on_commit=False,
    )

    def get_independent_session() -> Generator[Session, None, None]:
        """
        Matches production's get_db_session pattern.

        Each call creates a NEW session from the factory. The session manages
        its own transaction, and session.commit() actually commits to the DB
        (releasing advisory locks at that point, just like production).
        """
        session: Session = SessionFactory()
        try:
            yield session
            session.commit()  # Same as production - commits and releases locks
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # Override to use production-like session management
    application.dependency_overrides[get_db_session] = get_independent_session

    # Set up db_engine on app state (normally done in _setup_db during startup)
    application.state.db_engine = concurrent_engine

    # Also set session factory for middleware that might need it
    application.state.db_session_factory = SessionFactory

    # Set up OTel tracing
    setup_opentelemetry(application)

    yield application

    # Cleanup: dispose the concurrent engine to release all connections
    concurrent_engine.dispose()


@pytest.fixture
async def client_concurrent(
    fastapi_app_concurrent: FastAPI,
    anyio_backend: Any,
    request: pytest.FixtureRequest,
) -> AsyncGenerator[AsyncClient, None]:
    """
    Client fixture for concurrent tests with independent sessions.

    Use this instead of `client` when testing advisory locks, concurrent inserts,
    or any scenario requiring true transaction isolation between requests.

    :param fastapi_app_concurrent: the application with independent sessions.
    :param request: pytest request object.
    :yields: async client for the app.
    """
    test_name = request.node.nodeid
    async with TestAwareAsyncClient(
        app=fastapi_app_concurrent,
        base_url="http://test",
        test_name=test_name,
    ) as ac:
        yield ac


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
    - Test name (from CURRENT_TEST_INFO global)
    - Status code

    Used for performance tracking in tests.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def request(self, method, url, **kwargs):
        # Capture timing
        start = time.monotonic()
        response = await super().request(method, url, **kwargs)
        duration = time.monotonic() - start

        # Get current test info from global (set by pytest_runtest_setup hook)
        test_name = CURRENT_TEST_INFO.get("name", "unknown")
        mode = CURRENT_TEST_INFO.get("mode", "unknown")

        # Build timing record
        path = str(url)
        TIMING_RECORDS.append(
            {
                "method": method,
                "path": f"{method} {path}",
                "duration": duration,
                "status_code": response.status_code,
                "test_name": test_name,
                "mode": mode,
                "args": (),  # Kept for backward compatibility
                "kwargs": kwargs,
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
# Test Results Tracking
# ============================================================================

# Global dictionary to track test results
TEST_RESULTS = {"passed": [], "failed": [], "skipped": []}


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """
    Hook to capture test name before each test runs.
    This allows timing records to include the test function name.
    """
    global CURRENT_TEST_INFO

    # Extract test function name (without module path and parameters)
    nodeid = item.nodeid
    # Get the function name part (after :: and before [)
    if "::" in nodeid:
        func_part = nodeid.split("::")[-1]
        test_name = func_part.split("[")[0] if "[" in func_part else func_part
    else:
        test_name = nodeid

    CURRENT_TEST_INFO = {"name": test_name}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """
    Capture test results.
    """
    outcome = yield
    report = outcome.get_result()

    # Only track test call phase (not setup/teardown)
    if report.when == "call":
        # Extract test name
        test_name = item.nodeid.split("[")[0] if "[" in item.nodeid else item.nodeid

        if report.passed:
            TEST_RESULTS["passed"].append(test_name)
        elif report.failed:
            TEST_RESULTS["failed"].append(
                {
                    "name": test_name,
                    "error": str(report.longrepr)[:200],  # Truncate long errors
                },
            )
        elif report.skipped:
            TEST_RESULTS["skipped"].append(test_name)


### SQL CAPTURE FOR TEST ANALYSIS ###


@pytest.fixture(autouse=True)
def sql_capture_context(request):
    """
    Auto-use fixture that sets up SQL capture context for each test.

    Captures test name and filter expression (if available) for SQL query analysis.
    Enable SQL capture by setting: SQL_CAPTURE_ENABLED=1

    Captured SQL queries are written to:
    orchestra/tests/test_log/captured_sql/sql_capture.jsonl

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
    along with the test name from CURRENT_TEST_INFO (set by pytest_runtest_setup).

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
    log_event_dao = LogEventDAO(session=session, context_dao=context_dao)

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

                # Bulk merge log entries into JSONB data
                log_event_dao.bulk_merge_data(entries)
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


@pytest.fixture(scope="session", autouse=True)
def flush_otel_traces():
    """
    Flush OTel traces at session end to ensure all spans are written to exporters.

    When ORCHESTRA_LOG_DIR is set (e.g., in CI), this ensures all trace files
    are written before the test process exits. Without this, traces in the
    BatchSpanProcessor buffer might be lost.
    """
    yield  # Allow tests to run

    # Flush any buffered traces to ensure they're written to files
    flush_opentelemetry(timeout_millis=10000)


def pytest_sessionfinish(session, exitstatus):
    """
    Generate test results and performance reports.

    Outputs:
    - perf_timings.json
    - test_results.json

    :param session: The pytest session object.
    :param exitstatus: The exit status of the session.
    """
    # ========================================================================
    # Test Results Report
    # ========================================================================

    # Calculate totals
    total = sum(len(TEST_RESULTS[k]) for k in ["passed", "failed", "skipped"])

    if total > 0:
        passed = len(TEST_RESULTS["passed"])
        failed = len(TEST_RESULTS["failed"])
        skipped = len(TEST_RESULTS["skipped"])
        pass_rate = f"{(passed/total*100):.1f}%" if total > 0 else "N/A"

        print("\n" + "=" * 80)
        print("TEST RESULTS SUMMARY")
        print("=" * 80)
        print(
            f"Total: {total}, Passed: {passed}, Failed: {failed}, Skipped: {skipped}, Pass Rate: {pass_rate}",
        )
        print("=" * 80 + "\n")

        results_json = {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "pass_rate": float(pass_rate.rstrip("%")) if pass_rate != "N/A" else None,
            "failed_tests": TEST_RESULTS["failed"],
        }

        with open("test_results.json", "w") as f:
            json.dump(results_json, f, indent=2)

    # ========================================================================
    # Performance Timing Reports
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
        test_groups = defaultdict(list)

        for record in TIMING_RECORDS:
            test_name = record.get("test_name", "unknown")
            if test_name != "unknown":
                test_groups[test_name].append(record)

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
                "p95_ms": (
                    float(np.percentile(filtered, 95))
                    if len(filtered) > 1
                    else float(filtered[0])
                ),
                "min_ms": float(np.min(filtered)),
                "max_ms": float(np.max(filtered)),
                "outliers_removed": outliers_removed,
            }

        # =====================================================================
        # 4. COMPUTE STATS FOR EACH TEST
        # =====================================================================
        test_stats = []

        for test_name, records in test_groups.items():
            stats_data = compute_stats(records)
            if stats_data:
                test_stats.append(
                    {
                        "test_name": test_name,
                        "stats": stats_data,
                    },
                )

        # Sort by avg_ms (slowest first)
        test_stats.sort(
            key=lambda x: x["stats"]["avg_ms"] if x["stats"] else 0,
            reverse=True,
        )

        # =====================================================================
        # 5. CONSOLE SUMMARY TABLE
        # =====================================================================
        if test_stats:
            print("\n" + "=" * 100)
            print("=== Performance Summary ===")
            print("=" * 100)

            # Header
            print(
                f"\n{'Test':<55} | {'Avg (ms)':>12} | {'P95 (ms)':>12} | {'Count':>10}",
            )
            print("-" * 100)

            for item in test_stats[:30]:  # Show top 30 slowest
                test_name = (
                    item["test_name"][:52] + "..."
                    if len(item["test_name"]) > 55
                    else item["test_name"]
                )
                avg_ms = f"{item['stats']['avg_ms']:.2f}"
                p95_ms = f"{item['stats']['p95_ms']:.2f}"
                count = str(item["stats"]["count"])

                print(
                    f"{test_name:<55} | {avg_ms:>12} | {p95_ms:>12} | {count:>10}",
                )

            print("-" * 100)
            print(f"  Total Tests with Timing: {len(test_stats)}")

        # Save performance report
        perf_report = {
            "test_stats": test_stats,
            "raw_records": TIMING_RECORDS,
        }

        with open("perf_results.json", "w") as f:
            json.dump(perf_report, f, indent=2, default=str)

        print("\n" + "=" * 100)
        print("Reports generated:")
        print("  - perf_timings.json")
        print("  - perf_results.json")
        print("=" * 100)

    except Exception as e:
        # Error handling: ensure basic output even if report generation fails
        print(f"\n⚠️ Error generating performance reports: {e}")
        traceback.print_exc()
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
def repairs_project(_engine_session_prod: Engine) -> Generator[str, None, None]:
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
    repairs_project: str,
) -> Generator[str, None, None]:
    """
    Session-scoped fixture that retrieves the context name for RepairsDemo data.

    :param _engine_session_prod: SQLAlchemy database engine connected to production DB.
    :param repairs_project: Project name for the RepairsAgent project.
    :yield: Context name (path) for the RepairsDemo context (used by the API)
    :raises ValueError: If context is not found in the database
    """
    from orchestra.db.models.orchestra_models import Context, Project

    SessionLocal = sessionmaker(bind=_engine_session_prod, expire_on_commit=False)
    session = SessionLocal()

    try:
        # Query project directly by name
        project = session.query(Project).filter(Project.name == repairs_project).first()

        if not project:
            raise ValueError(f"Project {repairs_project} not found")

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
    repairs_project: str,
    repairs_context: str,
) -> Generator[None, None, None]:
    """
    Session-scoped fixture that creates ~40k log events for RepairsDemo performance testing.

    Uses realistic RepairsDemo field values with 80% realistic patterns and
    20% randomized variations.

    :param _engine_session_prod: SQLAlchemy database engine connected to production DB.
    :param repairs_project: Project name for RepairsAgent.
    :param repairs_context: Context name (path) for the RepairsDemo context.
    :yield: Nothing (data is available in database during test session)
    """
    from sqlalchemy import delete

    from orchestra.db.dao.context_dao import ContextDAO
    from orchestra.db.dao.field_type_dao import FieldTypeDAO
    from orchestra.db.dao.log_event_dao import LogEventDAO
    from orchestra.db.models.orchestra_models import (
        Context,
        LogEvent,
        LogEventContext,
        Project,
    )

    SessionLocal = sessionmaker(bind=_engine_session_prod, expire_on_commit=False)
    session = SessionLocal()

    rng = random.Random(5678)

    # Track IDs for cleanup
    created_event_ids = []

    try:
        # Look up project ID from name
        project = session.query(Project).filter(Project.name == repairs_project).first()

        if not project:
            raise ValueError("Could not find project by name")

        project_id = project.id

        # Look up context ID from name
        context_obj = (
            session.query(Context)
            .filter(
                Context.project_id == project_id,
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
                WHERE project_id = :project_id AND id > 1000
                """,
            ),
            {"project_id": project_id},
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

        # Number of events to create (configurable via env var)
        num_events = int(os.getenv("ORCHESTRA_PERF_REPAIRS_COUNT", "40000"))
        batch_size = 500

        print(f"Creating {num_events} log events for RepairsDemo performance tests...")

        # Register field types for RepairsDemo fields
        print("Registering field types...")
        field_types_data = []
        rng_field_types = random.Random(5678)

        for field_name in REPAIRS_DEMO_FIELDS:
            field_types_data.append(
                {
                    "project_id": project_id,
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
                    "project_id": project_id,
                    "field_name": emb_field,
                    "value": [0.0] * 1536,  # Mock embedding
                    "context_id": context_id,
                    "mutable": True,
                    "field_category": "entry",
                    "field_type": "List[float]",
                },
            )

        field_type_dao.bulk_create_field_types(field_types_data)

        # Create log events with JSONB data
        print(f"Creating {num_events} log events...")

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
                        rng,
                    )

                # Add mock embeddings
                for emb_field in REPAIRS_DEMO_EMBEDDINGS:
                    data[emb_field] = [rng.gauss(0, 0.1) for _ in range(1536)]

                log_event = LogEvent(
                    project_id=project_id,
                    data=data,
                    created_at=ts,
                    updated_at=ts,
                )
                log_events.append(log_event)

            session.add_all(log_events)
            session.flush()

            # Get IDs and create context associations
            for log_event in log_events:
                created_event_ids.append(log_event.id)
                session.add(
                    LogEventContext(
                        log_event_id=log_event.id,
                        context_id=context_id,
                    ),
                )

            session.flush()

            if (batch_start + batch_count) % 5000 == 0:
                print(f"  Created {batch_start + batch_count}/{num_events} events")

        session.commit()
        print(
            f"RepairsDemo bulk data creation complete: {len(created_event_ids)} events",
        )

        yield

    finally:
        # Cleanup: Delete bulk-inserted data
        print("Cleaning up RepairsDemo bulk data...")

        try:
            if created_event_ids:
                # Delete in batches to avoid memory issues
                for i in range(0, len(created_event_ids), 1000):
                    batch_ids = created_event_ids[i : i + 1000]
                    session.execute(
                        delete(LogEvent).where(LogEvent.id.in_(batch_ids)),
                    )
                session.commit()
                print(f"  Cleaned up {len(created_event_ids)} events")

        except Exception as e:
            print(f"Warning: Failed to cleanup bulk data: {e}")
            session.rollback()

        session.close()
