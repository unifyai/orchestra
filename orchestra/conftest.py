import json
import os
import random
import time
import warnings
from datetime import datetime, timedelta
from typing import Any, AsyncGenerator, Generator

import pytest
from fastapi import FastAPI
from google.cloud import storage
from httpx import AsyncClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

warnings.filterwarnings("ignore", category=UserWarning)

# Global list to store timing records
TIMING_RECORDS = []

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


@pytest.fixture
async def client(
    fastapi_app: FastAPI,
    anyio_backend: Any,
) -> AsyncGenerator[AsyncClient, None]:
    """
    Fixture that creates client for requesting server.

    :param fastapi_app: the application.
    :yield: client for the app.
    """
    async with AsyncClient(app=fastapi_app, base_url="http://test") as ac:
        yield ac


### PERF TESTS FIXTURES ###


@pytest.fixture
async def timed_client(
    client: AsyncClient,
) -> AsyncGenerator[AsyncClient, None]:
    """
    Fixture that wraps the client to record timing information for each request.

    :param client: The original AsyncClient.
    :yield: The wrapped client with timing instrumentation.
    """
    # Save original methods
    original_get = client.get
    original_post = client.post
    original_put = client.put
    original_delete = client.delete
    original_patch = client.patch
    original_head = client.head
    original_options = client.options

    # Wrap each method to record timing
    async def timed_get(*args, **kwargs):
        start = time.monotonic()
        response = await original_get(*args, **kwargs)
        duration = time.monotonic() - start
        path = args[0] if args else kwargs.get("url", "")
        TIMING_RECORDS.append(
            {
                "method": "GET",
                "path": f"GET {path}",
                "duration": duration,
                "status_code": response.status_code,
                "args": args,
                "kwargs": kwargs,
            },
        )
        return response

    async def timed_post(*args, **kwargs):
        start = time.monotonic()
        response = await original_post(*args, **kwargs)
        duration = time.monotonic() - start
        path = args[0] if args else kwargs.get("url", "")
        TIMING_RECORDS.append(
            {
                "method": "POST",
                "path": f"POST {path}",
                "duration": duration,
                "status_code": response.status_code,
                "args": args,
                "kwargs": kwargs,
            },
        )
        return response

    async def timed_put(*args, **kwargs):
        start = time.monotonic()
        response = await original_put(*args, **kwargs)
        duration = time.monotonic() - start
        path = args[0] if args else kwargs.get("url", "")
        TIMING_RECORDS.append(
            {
                "method": "PUT",
                "path": f"PUT {path}",
                "duration": duration,
                "status_code": response.status_code,
                "args": args,
                "kwargs": kwargs,
            },
        )
        return response

    async def timed_delete(*args, **kwargs):
        start = time.monotonic()
        response = await original_delete(*args, **kwargs)
        duration = time.monotonic() - start
        path = args[0] if args else kwargs.get("url", "")
        TIMING_RECORDS.append(
            {
                "method": "DELETE",
                "path": f"DELETE {path}",
                "duration": duration,
                "status_code": response.status_code,
                "args": args,
                "kwargs": kwargs,
            },
        )
        return response

    async def timed_patch(*args, **kwargs):
        start = time.monotonic()
        response = await original_patch(*args, **kwargs)
        duration = time.monotonic() - start
        path = args[0] if args else kwargs.get("url", "")
        TIMING_RECORDS.append(
            {
                "method": "PATCH",
                "path": f"PATCH {path}",
                "duration": duration,
                "status_code": response.status_code,
                "args": args,
                "kwargs": kwargs,
            },
        )
        return response

    async def timed_head(*args, **kwargs):
        start = time.monotonic()
        response = await original_head(*args, **kwargs)
        duration = time.monotonic() - start
        path = args[0] if args else kwargs.get("url", "")
        TIMING_RECORDS.append(
            {
                "method": "HEAD",
                "path": f"HEAD {path}",
                "duration": duration,
                "status_code": response.status_code,
                "args": args,
                "kwargs": kwargs,
            },
        )
        return response

    async def timed_options(*args, **kwargs):
        start = time.monotonic()
        response = await original_options(*args, **kwargs)
        duration = time.monotonic() - start
        path = args[0] if args else kwargs.get("url", "")
        TIMING_RECORDS.append(
            {
                "method": "OPTIONS",
                "path": f"OPTIONS {path}",
                "duration": duration,
                "status_code": response.status_code,
                "args": args,
                "kwargs": kwargs,
            },
        )
        return response

    # Replace methods with timed versions
    client.get = timed_get
    client.post = timed_post
    client.put = timed_put
    client.delete = timed_delete
    client.patch = timed_patch
    client.head = timed_head
    client.options = timed_options

    # Add records reference to client
    client.records = TIMING_RECORDS

    try:
        yield client
    finally:
        # Restore original methods
        client.get = original_get
        client.post = original_post
        client.put = original_put
        client.delete = original_delete
        client.patch = original_patch
        client.head = original_head
        client.options = original_options


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
    Hook that runs after the pytest session finishes.
    Writes timing records to a JSON file and logs them to stdout.

    :param session: The pytest session object.
    :param exitstatus: The exit status of the session.
    """
    if TIMING_RECORDS:
        # Calculate statistics
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

        # Calculate averages
        for path in stats:
            stats[path]["avg_duration"] = (
                stats[path]["total_duration"] / stats[path]["count"]
            )

        # Create output data
        output_data = {"records": TIMING_RECORDS, "statistics": stats}

        # Write to file
        with open("perf_timings.json", "w") as f:
            json.dump(output_data, f, indent=2)

        # Log to stdout
        print("\n=== Performance Test Results ===")
        for path, path_stats in stats.items():
            print(f"\nEndpoint: {path}")
            print(f"  Count: {path_stats['count']}")
            print(f"  Avg Duration: {path_stats['avg_duration']:.6f}s")
            print(f"  Min Duration: {path_stats['min_duration']:.6f}s")
            print(f"  Max Duration: {path_stats['max_duration']:.6f}s")
            print(f"  Status Codes: {path_stats['status_codes']}")

        print(f"\nDetailed results written to perf_timings.json")
