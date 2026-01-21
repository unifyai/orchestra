"""Tests for atomic field update operations."""

import asyncio

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
