"""Tests for atomic field update operations."""

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from httpx import AsyncClient

from . import HEADERS, _create_log, _create_project, _get_log


@pytest.mark.anyio
async def test_atomic_increment(client: AsyncClient):
    """Test basic atomic increment operation."""
    project_name = "atomic-increment-test"
    await _create_project(client, project_name)

    # Create a log with a counter field
    response = await _create_log(
        client,
        project_name,
        entries={"counter": 10},
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Apply atomic increment
    response = await client.patch(
        f"/v0/logs/{log_id}/fields/counter/atomic",
        json={"operation": "+5"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["new_value"] == 15.0


@pytest.mark.anyio
async def test_atomic_decrement(client: AsyncClient):
    """Test atomic decrement operation."""
    project_name = "atomic-decrement-test"
    await _create_project(client, project_name)

    response = await _create_log(
        client,
        project_name,
        entries={"counter": 100},
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    response = await client.patch(
        f"/v0/logs/{log_id}/fields/counter/atomic",
        json={"operation": "-30"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["new_value"] == 70.0


@pytest.mark.anyio
async def test_atomic_multiply(client: AsyncClient):
    """Test atomic multiplication operation."""
    project_name = "atomic-multiply-test"
    await _create_project(client, project_name)

    response = await _create_log(
        client,
        project_name,
        entries={"value": 7},
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    response = await client.patch(
        f"/v0/logs/{log_id}/fields/value/atomic",
        json={"operation": "*3"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["new_value"] == 21.0


@pytest.mark.anyio
async def test_atomic_divide(client: AsyncClient):
    """Test atomic division operation."""
    project_name = "atomic-divide-test"
    await _create_project(client, project_name)

    response = await _create_log(
        client,
        project_name,
        entries={"value": 100},
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    response = await client.patch(
        f"/v0/logs/{log_id}/fields/value/atomic",
        json={"operation": "/4"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["new_value"] == 25.0


@pytest.mark.anyio
async def test_atomic_on_null_field(client: AsyncClient):
    """Test atomic operation on a field that doesn't exist (should treat as 0)."""
    project_name = "atomic-null-field-test"
    await _create_project(client, project_name)

    # Create a log without the counter field
    response = await _create_log(
        client,
        project_name,
        entries={"other_field": "value"},
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Increment a non-existent field (should start from 0)
    response = await client.patch(
        f"/v0/logs/{log_id}/fields/counter/atomic",
        json={"operation": "+5"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["new_value"] == 5.0


@pytest.mark.anyio
async def test_atomic_with_float_operand(client: AsyncClient):
    """Test atomic operation with decimal operand."""
    project_name = "atomic-float-test"
    await _create_project(client, project_name)

    response = await _create_log(
        client,
        project_name,
        entries={"score": 10},
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    response = await client.patch(
        f"/v0/logs/{log_id}/fields/score/atomic",
        json={"operation": "*1.5"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["new_value"] == 15.0


@pytest.mark.anyio
async def test_atomic_invalid_operation_format(client: AsyncClient):
    """Test that invalid operation formats are rejected."""
    project_name = "atomic-invalid-test"
    await _create_project(client, project_name)

    response = await _create_log(
        client,
        project_name,
        entries={"counter": 10},
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Test various invalid formats
    invalid_operations = [
        "5",  # Missing operator
        "++5",  # Double operator
        "+",  # Missing operand
        "add5",  # Invalid operator
        "+5+3",  # Multiple operations
        "x+1",  # Variable reference
    ]

    for op in invalid_operations:
        response = await client.patch(
            f"/v0/logs/{log_id}/fields/counter/atomic",
            json={"operation": op},
            headers=HEADERS,
        )
        assert (
            response.status_code == 400
        ), f"Expected 400 for operation '{op}', got {response.status_code}"


@pytest.mark.anyio
async def test_atomic_division_by_zero(client: AsyncClient):
    """Test that division by zero is rejected."""
    project_name = "atomic-div-zero-test"
    await _create_project(client, project_name)

    response = await _create_log(
        client,
        project_name,
        entries={"counter": 10},
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    response = await client.patch(
        f"/v0/logs/{log_id}/fields/counter/atomic",
        json={"operation": "/0"},
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "zero" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_atomic_nonexistent_log(client: AsyncClient):
    """Test atomic operation on a log that doesn't exist."""
    response = await client.patch(
        "/v0/logs/999999999/fields/counter/atomic",
        json={"operation": "+1"},
        headers=HEADERS,
    )
    assert response.status_code == 404


@pytest.mark.anyio
async def test_atomic_concurrent_updates(client: AsyncClient):
    """
    Test that concurrent atomic updates are correctly serialized.

    This is the core test case: if we fire N concurrent +1 requests,
    the final value should be exactly initial_value + N.
    """
    project_name = "atomic-concurrent-test"
    await _create_project(client, project_name)

    initial_value = 0
    response = await _create_log(
        client,
        project_name,
        entries={"counter": initial_value},
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Fire N concurrent increment requests
    num_concurrent = 20

    async def do_increment():
        return await client.patch(
            f"/v0/logs/{log_id}/fields/counter/atomic",
            json={"operation": "+1"},
            headers=HEADERS,
        )

    # Execute all increments concurrently
    tasks = [do_increment() for _ in range(num_concurrent)]
    responses = await asyncio.gather(*tasks)

    # All should succeed
    for i, resp in enumerate(responses):
        assert resp.status_code == 200, f"Request {i} failed: {resp.json()}"

    # Verify the final value is exactly initial_value + num_concurrent
    get_response = await _get_log(client, project_name, log_id)
    assert get_response.status_code == 200
    final_value = get_response.json()["logs"][0]["entries"]["counter"]
    assert final_value == initial_value + num_concurrent, (
        f"Expected {initial_value + num_concurrent}, got {final_value}. "
        "This indicates a race condition in atomic updates."
    )


@pytest.mark.anyio
async def test_atomic_multiple_sequential_operations(client: AsyncClient):
    """Test multiple sequential atomic operations on the same field."""
    project_name = "atomic-sequential-test"
    await _create_project(client, project_name)

    response = await _create_log(
        client,
        project_name,
        entries={"value": 10},
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Apply a series of operations: 10 + 5 = 15, 15 * 2 = 30, 30 - 10 = 20, 20 / 4 = 5
    operations = ["+5", "*2", "-10", "/4"]
    expected_values = [15.0, 30.0, 20.0, 5.0]

    for op, expected in zip(operations, expected_values):
        response = await client.patch(
            f"/v0/logs/{log_id}/fields/value/atomic",
            json={"operation": op},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        assert (
            response.json()["new_value"] == expected
        ), f"After operation {op}, expected {expected}, got {response.json()['new_value']}"


# ===========================================================================
# Atomic Upsert Tests (POST /logs/atomic)
# ===========================================================================


@pytest.mark.anyio
async def test_atomic_upsert_create_new_log(client: AsyncClient):
    """Test atomic upsert creates a new log when none exists."""
    project_name = "atomic-upsert-create-test"
    await _create_project(client, project_name)

    response = await client.post(
        "/v0/logs/atomic",
        json={
            "project": project_name,
            "context": "JohnDoe/AdaLovelace/Spending/Monthly",
            "unique_keys": {"_assistant_id": "str", "month": "str"},
            "operation": "+5.50",
            "initial_data": {
                "_assistant_id": "123",
                "month": "2026-01",
                "_org_id": 456,
                "cumulative_spend": 0,  # Field to increment
            },
            "add_to_all_context": False,
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["created"] is True
    assert data["new_value"] == 5.50
    assert data["log_id"] > 0


@pytest.mark.anyio
async def test_atomic_upsert_update_existing_log(client: AsyncClient):
    """Test atomic upsert updates an existing log."""
    project_name = "atomic-upsert-update-test"
    await _create_project(client, project_name)

    # First call creates the log
    response1 = await client.post(
        "/v0/logs/atomic",
        json={
            "project": project_name,
            "context": "User/Assistant/Spending/Monthly",
            "unique_keys": {"_assistant_id": "str", "month": "str"},
            "operation": "+10.00",
            "initial_data": {
                "_assistant_id": "456",
                "month": "2026-02",
                "cumulative_spend": 0,
            },
            "add_to_all_context": False,
        },
        headers=HEADERS,
    )
    assert response1.status_code == 200, response1.json()
    data1 = response1.json()
    assert data1["created"] is True
    assert data1["new_value"] == 10.00
    log_id = data1["log_id"]

    # Second call updates the same log
    response2 = await client.post(
        "/v0/logs/atomic",
        json={
            "project": project_name,
            "context": "User/Assistant/Spending/Monthly",
            "unique_keys": {"_assistant_id": "str", "month": "str"},
            "operation": "+5.50",
            "initial_data": {
                "_assistant_id": "456",
                "month": "2026-02",
                "cumulative_spend": 0,
            },
            "add_to_all_context": False,
        },
        headers=HEADERS,
    )
    assert response2.status_code == 200, response2.json()
    data2 = response2.json()
    assert data2["created"] is False
    assert data2["new_value"] == 15.50
    assert data2["log_id"] == log_id


@pytest.mark.anyio
async def test_atomic_upsert_with_archive_context(client: AsyncClient):
    """Test atomic upsert mirrors to archive context when add_to_all_context=true."""
    project_name = "atomic-upsert-archive-test"
    await _create_project(client, project_name)

    response = await client.post(
        "/v0/logs/atomic",
        json={
            "project": project_name,
            "context": "JohnDoe/AdaLovelace/Spending/Monthly",
            "unique_keys": {"_assistant_id": "str", "month": "str"},
            "operation": "+25.00",
            "initial_data": {
                "_assistant_id": "789",
                "_user": "JohnDoe",
                "_assistant": "AdaLovelace",
                "month": "2026-03",
                "cumulative_spend": 0,
            },
            "add_to_all_context": True,
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["created"] is True
    assert data["new_value"] == 25.00
    assert "All/Spending/Monthly" in data["mirrored_contexts"]


@pytest.mark.anyio
async def test_atomic_upsert_invalid_operation(client: AsyncClient):
    """Test atomic upsert rejects invalid operation format."""
    project_name = "atomic-upsert-invalid-test"
    await _create_project(client, project_name)

    response = await client.post(
        "/v0/logs/atomic",
        json={
            "project": project_name,
            "context": "Test/Spending/Monthly",
            "unique_keys": {"_assistant_id": "str", "month": "str"},
            "operation": "invalid",  # Invalid format
            "initial_data": {
                "_assistant_id": "123",
                "month": "2026-01",
                "cumulative_spend": 0,
            },
            "add_to_all_context": False,
        },
        headers=HEADERS,
    )

    assert response.status_code == 400, response.json()
    assert "Invalid operation format" in response.json()["detail"]


@pytest.mark.anyio
async def test_atomic_upsert_missing_unique_key(client: AsyncClient):
    """Test atomic upsert rejects missing unique key in initial_data."""
    project_name = "atomic-upsert-missing-key-test"
    await _create_project(client, project_name)

    response = await client.post(
        "/v0/logs/atomic",
        json={
            "project": project_name,
            "context": "Test/Spending/Monthly",
            "unique_keys": {"_assistant_id": "str", "month": "str"},
            "operation": "+5.00",
            "initial_data": {
                "_assistant_id": "123",
                # "month" is missing
                "cumulative_spend": 0,
            },
            "add_to_all_context": False,
        },
        headers=HEADERS,
    )

    assert response.status_code == 400, response.json()
    assert "Missing unique key" in response.json()["detail"]


@pytest.mark.anyio
async def test_atomic_upsert_division_by_zero(client: AsyncClient):
    """Test atomic upsert rejects division by zero."""
    project_name = "atomic-upsert-div-zero-test"
    await _create_project(client, project_name)

    response = await client.post(
        "/v0/logs/atomic",
        json={
            "project": project_name,
            "context": "Test/Spending/Monthly",
            "unique_keys": {"_assistant_id": "str", "month": "str"},
            "operation": "/0",
            "initial_data": {
                "_assistant_id": "123",
                "month": "2026-01",
                "cumulative_spend": 0,
            },
            "add_to_all_context": False,
        },
        headers=HEADERS,
    )

    assert response.status_code == 400, response.json()
    assert "Division by zero" in response.json()["detail"]


@pytest.mark.anyio
async def test_atomic_upsert_missing_required_fields(client: AsyncClient):
    """Test atomic upsert rejects request missing required upsert fields."""
    response = await client.post(
        "/v0/logs/atomic",
        json={
            "operation": "+5.00",
            # Missing project, context, unique_keys, initial_data
        },
        headers=HEADERS,
    )

    assert response.status_code == 400, response.json()
    assert "Upsert mode requires" in response.json()["detail"]


@pytest.mark.anyio
async def test_atomic_upsert_concurrent_updates(client: AsyncClient):
    """
    Test that concurrent atomic updates are correctly serialized.

    This tests the core atomicity - if we fire N concurrent +1 requests
    on the same log, the final value should be exactly N.
    """
    project_name = "atomic-upsert-concurrent-test"
    await _create_project(client, project_name)

    # First, create the log entry
    response = await client.post(
        "/v0/logs/atomic",
        json={
            "project": project_name,
            "context": "ConcurrentTest/Spending/Monthly",
            "unique_keys": {"_assistant_id": "str", "month": "str"},
            "operation": "+0.00",  # Initialize to 0
            "initial_data": {
                "_assistant_id": "concurrent-test",
                "month": "2026-01",
                "cumulative_spend": 0,
            },
            "add_to_all_context": False,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["created"] is True

    # Now fire concurrent updates
    num_concurrent = 10

    async def do_upsert():
        return await client.post(
            "/v0/logs/atomic",
            json={
                "project": project_name,
                "context": "ConcurrentTest/Spending/Monthly",
                "unique_keys": {"_assistant_id": "str", "month": "str"},
                "operation": "+1.00",
                "initial_data": {
                    "_assistant_id": "concurrent-test",
                    "month": "2026-01",
                    "cumulative_spend": 0,
                },
                "add_to_all_context": False,
            },
            headers=HEADERS,
        )

    # Execute all upserts concurrently
    tasks = [do_upsert() for _ in range(num_concurrent)]
    responses = await asyncio.gather(*tasks)

    # All should succeed and be updates (not creates)
    for i, resp in enumerate(responses):
        assert resp.status_code == 200, f"Request {i} failed: {resp.json()}"
        assert (
            resp.json()["created"] is False
        ), f"Request {i} created instead of updated"

    # Final value should be exactly num_concurrent (each adds 1)
    final_values = [r.json()["new_value"] for r in responses]
    max_value = max(final_values)
    assert max_value == float(num_concurrent), (
        f"Expected final value {num_concurrent}, got {max_value}. "
        "This indicates a race condition in atomic updates."
    )


def test_atomic_upsert_concurrent_first_inserts_threaded(fastapi_app_concurrent):
    """
    Test that concurrent FIRST inserts are correctly serialized using threads.

    This is the critical race condition test: if N concurrent requests all
    try to create the same log (none exists yet), exactly ONE log should
    be created and the final value should be N * increment_amount.

    Uses ThreadPoolExecutor for TRUE parallelism at the OS level, ensuring
    advisory locks are properly tested across concurrent database connections.
    """
    import uuid

    # Use unique project and assistant ID to avoid conflicts between test runs
    unique_suffix = uuid.uuid4().hex[:8]
    project_name = f"atomic-concurrent-create-{unique_suffix}"
    unique_assistant_id = f"concurrent-create-{unique_suffix}"

    num_concurrent = 15
    increment_amount = 2.50

    def make_request_in_thread(app, method, url, json_data):
        """Run async request in its own event loop (for thread isolation)."""

        async def _make_request():
            async with AsyncClient(app=app, base_url="http://test") as client:
                if method == "post":
                    return await client.post(url, json=json_data, headers=HEADERS)
                return await client.get(url, headers=HEADERS)

        # Create new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(_make_request())
        finally:
            loop.close()

    # Create project first (sequential) using the same helper as threaded requests
    response = make_request_in_thread(
        fastapi_app_concurrent,
        "post",
        "/v0/project",
        {"name": project_name},
    )
    assert response.status_code == 200, f"Project creation failed: {response.json()}"

    def do_upsert(thread_id: int):
        """Execute upsert in a thread with its own event loop."""
        resp = make_request_in_thread(
            fastapi_app_concurrent,
            "post",
            "/v0/logs/atomic",
            {
                "project": project_name,
                "context": "ConcurrentCreate/Spending/Monthly",
                "unique_keys": {"_assistant_id": "str", "month": "str"},
                "operation": f"+{increment_amount}",
                "initial_data": {
                    "_assistant_id": unique_assistant_id,
                    "month": "2026-01",
                    "cumulative_spend": 0,
                },
                "add_to_all_context": False,
            },
        )
        return thread_id, resp.status_code, resp.json()

    # Fire ALL requests concurrently using threads for TRUE parallelism
    results = []
    with ThreadPoolExecutor(max_workers=num_concurrent) as executor:
        futures = [executor.submit(do_upsert, i) for i in range(num_concurrent)]
        for future in as_completed(futures):
            results.append(future.result())

    # All requests should succeed
    for thread_id, status_code, data in results:
        assert status_code == 200, f"Request {thread_id} failed: {data}"

    # Extract results
    created_count = sum(1 for _, _, data in results if data["created"] is True)
    log_ids = set(data["log_id"] for _, _, data in results)
    final_values = [data["new_value"] for _, _, data in results]
    max_value = max(final_values)
    expected_value = num_concurrent * increment_amount

    # CRITICAL ASSERTIONS:
    # 1. Exactly ONE log should have been created (advisory lock working)
    assert created_count == 1, (
        f"Expected exactly 1 log to be created, but {created_count} were created. "
        "This indicates the advisory lock is not working correctly."
    )

    # 2. All requests should reference the same log
    assert len(log_ids) == 1, (
        f"Expected all requests to use the same log, but got {len(log_ids)} different log IDs: {log_ids}. "
        "This indicates duplicate logs were created."
    )

    # 3. Final value should be exactly num_concurrent * increment_amount
    assert max_value == expected_value, (
        f"Expected final value {expected_value}, got {max_value}. "
        "This indicates a race condition - some increments were lost."
    )


@pytest.mark.anyio
async def test_atomic_upsert_concurrent_different_keys(client: AsyncClient):
    """
    Test that concurrent upserts with DIFFERENT unique keys don't interfere.

    Each request targets a different month, so each should create its own log.
    """
    project_name = "atomic-upsert-concurrent-diff-keys-test"
    await _create_project(client, project_name)

    num_concurrent = 5

    async def do_upsert(month_num: int):
        return await client.post(
            "/v0/logs/atomic",
            json={
                "project": project_name,
                "context": "DiffKeys/Spending/Monthly",
                "unique_keys": {"_assistant_id": "str", "month": "str"},
                "operation": "+10.00",
                "initial_data": {
                    "_assistant_id": "same-assistant",
                    "month": f"2026-{month_num:02d}",  # Different month for each
                    "cumulative_spend": 0,
                },
                "add_to_all_context": False,
            },
            headers=HEADERS,
        )

    # Fire requests for different months concurrently
    tasks = [do_upsert(i + 1) for i in range(num_concurrent)]
    responses = await asyncio.gather(*tasks)

    # All should succeed
    for i, resp in enumerate(responses):
        assert resp.status_code == 200, f"Request {i} failed: {resp.json()}"

    # All should be creates (different keys = different logs)
    created_count = sum(1 for r in responses if r.json()["created"] is True)
    assert created_count == num_concurrent, (
        f"Expected {num_concurrent} logs to be created, but only {created_count} were. "
        "Different unique keys should create different logs."
    )

    # All log_ids should be different
    log_ids = [r.json()["log_id"] for r in responses]
    assert (
        len(set(log_ids)) == num_concurrent
    ), "Expected all log IDs to be different for different unique keys."


def test_atomic_upsert_concurrent_high_contention_threaded(fastapi_app_concurrent):
    """
    High contention test: many concurrent requests to the same log using threads.

    Tests that the system handles high contention correctly without:
    - Deadlocks
    - Lost updates
    - Duplicate logs

    Uses ThreadPoolExecutor for TRUE parallelism at the OS level.
    """
    import uuid

    # Use unique project and ID to avoid conflicts between test runs
    unique_suffix = uuid.uuid4().hex[:8]
    project_name = f"atomic-high-contention-{unique_suffix}"
    unique_id = f"high-contention-{unique_suffix}"

    num_concurrent = 30  # High contention
    increment_amount = 1.00

    def make_request_in_thread(app, method, url, json_data):
        """Run async request in its own event loop (for thread isolation)."""

        async def _make_request():
            async with AsyncClient(app=app, base_url="http://test") as client:
                if method == "post":
                    return await client.post(url, json=json_data, headers=HEADERS)
                return await client.get(url, headers=HEADERS)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(_make_request())
        finally:
            loop.close()

    # Create project first (sequential) using the same helper as threaded requests
    response = make_request_in_thread(
        fastapi_app_concurrent,
        "post",
        "/v0/project",
        {"name": project_name},
    )
    assert response.status_code == 200, f"Project creation failed: {response.json()}"

    def do_upsert(thread_id: int):
        resp = make_request_in_thread(
            fastapi_app_concurrent,
            "post",
            "/v0/logs/atomic",
            {
                "project": project_name,
                "context": "HighContention/Spending/Monthly",
                "unique_keys": {"_assistant_id": "str", "month": "str"},
                "operation": f"+{increment_amount}",
                "initial_data": {
                    "_assistant_id": unique_id,
                    "month": "2026-06",
                    "cumulative_spend": 0,
                },
                "add_to_all_context": False,
            },
        )
        return thread_id, resp.status_code, resp.json()

    # Fire all requests at once using threads
    results = []
    with ThreadPoolExecutor(max_workers=num_concurrent) as executor:
        futures = [executor.submit(do_upsert, i) for i in range(num_concurrent)]
        for future in as_completed(futures):
            results.append(future.result())

    # All should succeed (no deadlocks or failures)
    success_count = sum(1 for _, status, _ in results if status == 200)
    assert success_count == num_concurrent, (
        f"Only {success_count}/{num_concurrent} requests succeeded. "
        "High contention may be causing failures."
    )

    # Exactly one create (advisory lock ensures serialization)
    created_count = sum(1 for _, _, data in results if data["created"] is True)
    assert created_count == 1, f"Expected 1 create, got {created_count}"

    # Final value should be correct (all increments applied)
    max_value = max(data["new_value"] for _, _, data in results)
    expected = num_concurrent * increment_amount
    assert (
        max_value == expected
    ), f"Expected {expected}, got {max_value}. Lost {expected - max_value} in updates."
