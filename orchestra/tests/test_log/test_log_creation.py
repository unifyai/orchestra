import base64
import os

import cv2
import pytest
from httpx import AsyncClient

from . import HEADERS, _create_log, _create_project


@pytest.mark.anyio
async def test_create_logs(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    # Test single log creation
    response = await _create_log(client, project_name)
    assert response.status_code == 200, response.json()
    log_event_ids = response.json()["log_event_ids"]
    assert isinstance(log_event_ids, list) and len(log_event_ids) == 1
    assert isinstance(log_event_ids[0], int)

    # Test batch log creation with multiple entries
    batch_entries = [
        {"a/b/c/input": "Batch input 1", "a/b/c/numeric_input": 1.5},
        {"a/b/c/input": "Batch input 2", "a/b/c/numeric_input": 2.5},
        {"a/b/c/input": "Batch input 3", "a/b/c/numeric_input": 3.5},
    ]
    batch_params = [
        {"a/b/param1": "test"},
        {"a/b/param2": "test"},
        {"a/b/param3": "test"},
    ]
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": batch_params,
            "entries": batch_entries,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    log_event_ids = response.json()["log_event_ids"]
    assert isinstance(log_event_ids, list)
    assert len(log_event_ids) == 3
    assert all(isinstance(id, int) for id in log_event_ids)
    assert sorted(log_event_ids) == list(
        range(min(log_event_ids), max(log_event_ids) + 1),
    )

    # When no unique_keys/auto_counting are configured, auto_counting should be empty dict
    assert response.json()["auto_counting"] == {}


@pytest.mark.anyio
@pytest.mark.xdist_group(name="gcs_serial")
async def test_create_log_w_image(client: AsyncClient):
    """
    Note: Marked with xdist_group to run serially due to GCS eventual consistency issues.
    """
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    img_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
        "sample_datasets/img.png",
    )
    success, buffer = cv2.imencode(".png", cv2.imread(img_path))
    assert success
    img = base64.b64encode(buffer).decode("utf-8")

    # Phase 1: Implicit field creation (no explicit types) → data_type should be "Any"
    response_implicit = await _create_log(
        client,
        project_name,
        params={},
        entries={
            "img_raw_implicit": img,
            "img_url_implicit": "https://upload.wikimedia.org/wikipedia/commons/4/45/Eopsaltria_australis_-_Mogo_Campground.jpg",
        },
    )
    assert response_implicit.status_code == 200, response_implicit.json()

    fields_resp = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert fields_resp.status_code == 200
    fields = fields_resp.json()
    assert fields["img_raw_implicit"]["data_type"] == "Any"
    assert fields["img_url_implicit"]["data_type"] == "Any"

    # Phase 2: Explicit field creation via explicit_types → data_type should match explicit type
    response_explicit = await _create_log(
        client,
        project_name,
        params={},
        entries={
            "img_raw": img,
            "img_url": "https://upload.wikimedia.org/wikipedia/commons/4/45/Eopsaltria_australis_-_Mogo_Campground.jpg",
            "explicit_types": {
                "img_raw": {"type": "image"},
                "img_url": {"type": "str"},
            },
        },
    )
    assert response_explicit.status_code == 200, response_explicit.json()

    # Verify explicit types were respected
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    fields2 = field_types_response.json()
    assert fields2["img_raw"]["data_type"] == "image"
    assert fields2["img_url"]["data_type"] == "image"
    assert fields2["img_raw"]["field_type"] == "entry"
    assert fields2["img_url"]["field_type"] == "entry"
    assert fields2["img_raw"]["mutable"] == True
    assert fields2["img_url"]["mutable"] == True
    assert fields2["img_raw"]["artifacts"] == ""
    assert fields2["img_url"]["artifacts"] == ""
    assert fields2["img_raw"]["created_at"] is not None
    assert fields2["img_url"]["created_at"] is not None


@pytest.mark.anyio
async def test_create_log_w_audio(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    # Use generic dummy bytes, as the content doesn't need to be a valid MP3 for this test.
    dummy_audio_bytes = b"dummy_mp3_data"
    audio_b64 = base64.b64encode(dummy_audio_bytes).decode("utf-8")

    # Phase 1: Implicit creation → data_type should be "Any"
    resp_implicit = await _create_log(
        client,
        project_name,
        params={},
        entries={
            "user_recording_implicit": audio_b64,
            "sound_effect_implicit": "https://example.com/sounds/effect.mp3",
        },
    )
    assert resp_implicit.status_code == 200, resp_implicit.json()

    fields_resp1 = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert fields_resp1.status_code == 200, fields_resp1.json()
    fields1 = fields_resp1.json()
    assert fields1["user_recording_implicit"]["data_type"] == "Any"
    assert fields1["sound_effect_implicit"]["data_type"] == "Any"

    # Phase 2: Explicit creation via explicit_types → data_type should be "audio"
    resp_explicit = await _create_log(
        client,
        project_name,
        params={},
        entries={
            "user_recording": audio_b64,
            "sound_effect": "https://example.com/sounds/effect.mp3",
            "explicit_types": {
                "user_recording": {"type": "audio"},
                "sound_effect": {"type": "str"},
            },
        },
    )
    assert resp_explicit.status_code == 200, resp_explicit.json()

    # Verify field types
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200, field_types_response.json()
    fields = field_types_response.json()

    # Check that both fields match explicit 'audio'
    assert fields["user_recording"]["data_type"] == "audio"
    assert fields["sound_effect"]["data_type"] == "audio"

    # Check other properties
    assert fields["user_recording"]["field_type"] == "entry"
    assert fields["user_recording"]["mutable"] is True
    assert fields["user_recording"]["created_at"] is not None

    assert fields["sound_effect"]["field_type"] == "entry"
    assert fields["sound_effect"]["mutable"] is True
    assert fields["sound_effect"]["created_at"] is not None


@pytest.mark.anyio
async def test_create_logs_autoincrement_version(client: AsyncClient):
    project_name = "non-matching-versions"
    _ = await _create_project(client, project_name)

    # This should work fine
    response = await client.post(
        "/v0/logs",
        json={"project": project_name, "params": {"p1": "test"}},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # same version and value
    response = await client.post(
        "/v0/logs",
        json={"project": project_name, "params": {"p1": "test"}},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # same version and different value -> autoincrement
    response = await client.post(
        "/v0/logs",
        json={"project": project_name, "params": {"p1": "test_v1"}},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()


@pytest.mark.anyio
async def test_create_logs_returns_auto_counting_columns_and_values(
    client: AsyncClient,
):
    """Ensure response includes auto_counting dict with column names -> values."""
    project_name = "auto-counts-response-project"
    context_name = "auto-counts-response-context"

    # Create project and context: one unique key (message_id) and an independent auto-counting column (exchange_id)
    await _create_project(client, project_name)
    resp_ctx = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "unique_keys": {"message_id": "int"},
            "auto_counting": {"message_id": None, "exchange_id": None},
        },
        headers=HEADERS,
    )
    assert resp_ctx.status_code == 200, resp_ctx.json()

    # Create three logs; expect message_id in row_ids and both message_id and exchange_id in auto_counting
    res1 = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={"text": "a"},
    )
    assert res1.status_code == 200, res1.text
    body1 = res1.json()
    assert body1["row_ids"]["names"] == ["message_id"]
    assert body1["row_ids"]["ids"] == [[0]]
    # auto_counting should be a dict mapping column names to lists of values
    assert body1.get("auto_counting") is not None
    assert "message_id" in body1["auto_counting"]
    assert "exchange_id" in body1["auto_counting"]
    assert body1["auto_counting"]["message_id"] == [0]
    assert body1["auto_counting"]["exchange_id"] == [0]

    res2 = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={"text": "b"},
    )
    assert res2.status_code == 200, res2.text
    body2 = res2.json()
    assert body2["row_ids"]["ids"] == [[1]]
    assert body2["auto_counting"]["message_id"] == [1]
    assert body2["auto_counting"]["exchange_id"] == [1]

    res3 = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={"text": "c"},
    )
    assert res3.status_code == 200, res3.text
    body3 = res3.json()
    assert body3["row_ids"]["ids"] == [[2]]
    assert body3["auto_counting"]["message_id"] == [2]
    assert body3["auto_counting"]["exchange_id"] == [2]


@pytest.mark.anyio
async def test_create_logs_with_batch_auto_counting(client: AsyncClient):
    """Test batch log creation returns correct auto_counting values for multiple logs."""
    project_name = "batch-auto-count-project"
    context_name = "batch-context"

    await _create_project(client, project_name)
    resp_ctx = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "unique_keys": {"seq_id": "int"},
            "auto_counting": {"seq_id": None},
        },
        headers=HEADERS,
    )
    assert resp_ctx.status_code == 200, resp_ctx.json()

    # Create 5 logs in one batch
    batch_entries = [{"data": f"item_{i}"} for i in range(5)]
    res = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": context_name,
            "entries": batch_entries,
        },
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()

    # Verify log_event_ids
    assert len(body["log_event_ids"]) == 5
    assert all(isinstance(lid, int) for lid in body["log_event_ids"])

    # Verify row_ids for unique keys
    assert body["row_ids"]["names"] == ["seq_id"]
    assert body["row_ids"]["ids"] == [[0], [1], [2], [3], [4]]

    # Verify auto_counting values
    assert "seq_id" in body["auto_counting"]
    assert body["auto_counting"]["seq_id"] == [0, 1, 2, 3, 4]


@pytest.mark.anyio
async def test_create_logs_with_independent_auto_counting_columns(client: AsyncClient):
    """Test multiple independent auto-counting columns increment separately."""
    project_name = "independent-counters-project"
    context_name = "independent-context"

    await _create_project(client, project_name)
    resp_ctx = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "auto_counting": {"counter_a": None, "counter_b": None},
        },
        headers=HEADERS,
    )
    assert resp_ctx.status_code == 200, resp_ctx.json()

    # Create 3 logs
    for i in range(3):
        res = await client.post(
            "/v0/logs",
            json={
                "project": project_name,
                "context": context_name,
                "entries": {"value": f"log_{i}"},
            },
            headers=HEADERS,
        )
        assert res.status_code == 200, res.text
        body = res.json()

        # Both counters should increment independently from 0
        assert "counter_a" in body["auto_counting"]
        assert "counter_b" in body["auto_counting"]
        assert body["auto_counting"]["counter_a"] == [i]
        assert body["auto_counting"]["counter_b"] == [i]

        # No unique keys configured, so row_ids should be empty
        assert body["row_ids"]["names"] == []
        assert body["row_ids"]["ids"] == []


@pytest.mark.anyio
async def test_create_logs_with_hierarchical_auto_counting(client: AsyncClient):
    """Test hierarchical auto-counting where child counter depends on parent."""
    project_name = "hierarchical-counter-project"
    context_name = "hierarchical-context"

    await _create_project(client, project_name)
    resp_ctx = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "unique_keys": {"user_id": "int", "session_id": "int"},
            "auto_counting": {"user_id": None, "session_id": "user_id"},
        },
        headers=HEADERS,
    )
    assert resp_ctx.status_code == 200, resp_ctx.json()

    # First log: Let both user_id and session_id auto-generate
    res = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": context_name,
            "entries": {"action": "first_session_user_0"},
        },
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # Both should be auto-generated starting from 0
    assert body["auto_counting"]["user_id"] == [0]
    assert body["auto_counting"]["session_id"] == [0]
    user_0_id = body["auto_counting"]["user_id"][0]

    # Create more sessions for user 0 by providing user_id
    for session_num in range(1, 3):
        res = await client.post(
            "/v0/logs",
            json={
                "project": project_name,
                "context": context_name,
                "entries": {
                    "user_id": user_0_id,
                    "action": f"session_{session_num}_user_0",
                },
            },
            headers=HEADERS,
        )
        assert res.status_code == 200, res.text
        body = res.json()

        # user_id is provided, session_id auto-increments per user
        assert body["auto_counting"]["user_id"] == [user_0_id]
        assert body["auto_counting"]["session_id"] == [session_num]

    # Create first log for user 1 (let user_id auto-generate)
    res = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": context_name,
            "entries": {"action": "first_session_user_1"},
        },
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # user_id auto-increments to 1, session_id restarts from 0 for this new user
    assert body["auto_counting"]["user_id"] == [1]
    assert body["auto_counting"]["session_id"] == [0]
    user_1_id = body["auto_counting"]["user_id"][0]

    # Create another session for user 1
    res = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": context_name,
            "entries": {"user_id": user_1_id, "action": "session_1_user_1"},
        },
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["auto_counting"]["user_id"] == [user_1_id]
    assert body["auto_counting"]["session_id"] == [1]


@pytest.mark.anyio
async def test_create_logs_with_mixed_unique_and_auto_counting(client: AsyncClient):
    """Test context with both unique keys and separate auto-counting columns."""
    project_name = "mixed-config-project"
    context_name = "mixed-context"

    await _create_project(client, project_name)
    resp_ctx = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "unique_keys": {"task_id": "int"},
            "auto_counting": {"task_id": None, "attempt_id": None},
        },
        headers=HEADERS,
    )
    assert resp_ctx.status_code == 200, resp_ctx.json()

    # Create multiple logs
    for i in range(4):
        res = await client.post(
            "/v0/logs",
            json={
                "project": project_name,
                "context": context_name,
                "entries": {"status": f"status_{i}"},
            },
            headers=HEADERS,
        )
        assert res.status_code == 200, res.text
        body = res.json()

        # task_id is in unique_keys, should appear in row_ids
        assert body["row_ids"]["names"] == ["task_id"]
        assert body["row_ids"]["ids"] == [[i]]

        # Both task_id and attempt_id should appear in auto_counting
        assert set(body["auto_counting"].keys()) == {"task_id", "attempt_id"}
        assert body["auto_counting"]["task_id"] == [i]
        assert body["auto_counting"]["attempt_id"] == [i]


@pytest.mark.anyio
async def test_create_logs_with_user_provided_auto_counting_values(client: AsyncClient):
    """Test that explicitly provided auto-counting values are returned correctly."""
    project_name = "explicit-counter-project"
    context_name = "explicit-context"

    await _create_project(client, project_name)
    resp_ctx = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "unique_keys": {"record_id": "int"},
            "auto_counting": {"record_id": None},
        },
        headers=HEADERS,
    )
    assert resp_ctx.status_code == 200, resp_ctx.json()

    # Explicitly provide record_id values (skip 0, 1, 2 and provide 10, 20)
    res1 = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": context_name,
            "entries": {"record_id": 10, "data": "custom_10"},
        },
        headers=HEADERS,
    )
    assert res1.status_code == 200, res1.text
    body1 = res1.json()
    assert body1["auto_counting"]["record_id"] == [10]
    assert body1["row_ids"]["ids"] == [[10]]

    res2 = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": context_name,
            "entries": {"record_id": 20, "data": "custom_20"},
        },
        headers=HEADERS,
    )
    assert res2.status_code == 200, res2.text
    body2 = res2.json()
    assert body2["auto_counting"]["record_id"] == [20]
    assert body2["row_ids"]["ids"] == [[20]]


@pytest.mark.anyio
async def test_create_logs_with_composite_unique_keys(client: AsyncClient):
    """Test composite unique keys with auto-counting on one of them."""
    project_name = "composite-keys-project"
    context_name = "composite-context"

    await _create_project(client, project_name)
    resp_ctx = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "unique_keys": {"dept_id": "int", "emp_id": "int"},
            "auto_counting": {"emp_id": None},
        },
        headers=HEADERS,
    )
    assert resp_ctx.status_code == 200, resp_ctx.json()

    # Create employees in dept 100
    for emp_num in range(3):
        res = await client.post(
            "/v0/logs",
            json={
                "project": project_name,
                "context": context_name,
                "entries": {"dept_id": 100, "name": f"emp_{emp_num}"},
            },
            headers=HEADERS,
        )
        assert res.status_code == 200, res.text
        body = res.json()

        # Both dept_id and emp_id should be in row_ids
        assert body["row_ids"]["names"] == ["dept_id", "emp_id"]
        assert body["row_ids"]["ids"] == [[100, emp_num]]

        # Only emp_id is auto-counting
        assert body["auto_counting"] == {"emp_id": [emp_num]}


@pytest.mark.anyio
async def test_comprehensive_auto_counting_with_hierarchy_and_independent_counters(
    client: AsyncClient,
):
    """
    Comprehensive test covering:
    - Nested hierarchical auto-counting (department -> team -> employee)
    - Independent auto-counting columns (ticket_id, session_id)
    - No unique keys configured
    - Mix of auto-generated and user-provided values
    - Proper counter scoping and resets
    """
    project_name = "comprehensive-auto-counting-project"
    context_name = "comprehensive-context"

    await _create_project(client, project_name)
    resp_ctx = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "unique_keys": {
                "dept_id": "int",
                "team_id": "int",
                "emp_id": "int",
            },
            "auto_counting": {
                # Hierarchical: dept_id -> team_id -> emp_id
                "dept_id": None,  # Root counter
                "team_id": "dept_id",  # Scoped to dept_id
                "emp_id": "team_id",  # Scoped to team_id
                # Independent counters (not in unique_keys)
                "ticket_id": None,  # Independent counter
                "session_id": None,  # Another independent counter
            },
        },
        headers=HEADERS,
    )
    assert resp_ctx.status_code == 200, resp_ctx.json()

    # Test 1: Create first log - let all counters auto-generate to initialize them
    res = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": context_name,
            "entries": {"action": "initialize_counters"},
        },
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()

    # All counters auto-generate and initialize to 0
    assert body["auto_counting"]["dept_id"] == [0]
    assert body["auto_counting"]["team_id"] == [0]
    assert body["auto_counting"]["emp_id"] == [0]
    assert body["auto_counting"]["ticket_id"] == [0]
    assert body["auto_counting"]["session_id"] == [0]

    # unique_keys configured - row_ids should contain the hierarchical keys
    assert body["row_ids"]["names"] == ["dept_id", "team_id", "emp_id"]
    assert body["row_ids"]["ids"] == [[0, 0, 0]]

    # Capture the initialized values
    dept_0 = body["auto_counting"]["dept_id"][0]
    team_0 = body["auto_counting"]["team_id"][0]

    # Test 2: Add another employee to same team (now we can provide explicit values)
    res = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": context_name,
            "entries": {
                "dept_id": dept_0,
                "team_id": team_0,
                "action": "add_emp_1_to_team_0",
            },
        },
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["auto_counting"]["dept_id"] == [dept_0]
    assert body["auto_counting"]["team_id"] == [team_0]
    assert body["auto_counting"]["emp_id"] == [1]
    assert body["auto_counting"]["ticket_id"] == [1]
    assert body["auto_counting"]["session_id"] == [1]

    # Test 3: Add third employee to same team
    res = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": context_name,
            "entries": {
                "dept_id": dept_0,
                "team_id": team_0,
                "action": "add_emp_2_to_team_0",
            },
        },
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["auto_counting"]["emp_id"] == [2]
    assert body["auto_counting"]["ticket_id"] == [2]
    assert body["auto_counting"]["session_id"] == [2]

    # Test 4: Create new team in same department (providing only dept_id also creates emp_id=0)
    res = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": context_name,
            "entries": {"dept_id": dept_0, "action": "create_team_1_emp_0"},
        },
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["auto_counting"]["dept_id"] == [dept_0]
    assert body["auto_counting"]["team_id"] == [1]
    assert body["auto_counting"]["emp_id"] == [0]  # emp_id resets for new team
    assert body["auto_counting"]["ticket_id"] == [3]
    assert body["auto_counting"]["session_id"] == [3]

    team_1 = body["auto_counting"]["team_id"][0]

    # Test 5: Add second employee to team_1
    res = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": context_name,
            "entries": {
                "dept_id": dept_0,
                "team_id": team_1,
                "action": "add_emp_1_to_team_1",
            },
        },
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["auto_counting"]["emp_id"] == [1]
    assert body["auto_counting"]["ticket_id"] == [4]
    assert body["auto_counting"]["session_id"] == [4]

    # Test 6: Add third employee to team_1
    res = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": context_name,
            "entries": {
                "dept_id": dept_0,
                "team_id": team_1,
                "action": "add_emp_2_to_team_1",
            },
        },
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["auto_counting"]["emp_id"] == [2]
    assert body["auto_counting"]["ticket_id"] == [5]
    assert body["auto_counting"]["session_id"] == [5]

    # Test 7: Create entirely new department (all levels auto-generate)
    res = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": context_name,
            "entries": {"action": "create_dept_1_team_0_emp_0"},
        },
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["auto_counting"]["dept_id"] == [1]
    assert body["auto_counting"]["team_id"] == [0]  # Reset for new dept
    assert body["auto_counting"]["emp_id"] == [0]  # Reset for new team
    assert body["auto_counting"]["ticket_id"] == [6]
    assert body["auto_counting"]["session_id"] == [6]

    dept_1 = body["auto_counting"]["dept_id"][0]
    team_0_dept_1 = body["auto_counting"]["team_id"][0]

    # Test 8: Add second employee to dept_1, team_0
    res = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": context_name,
            "entries": {
                "dept_id": dept_1,
                "team_id": team_0_dept_1,
                "action": "add_emp_1_to_dept_1_team_0",
            },
        },
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["auto_counting"]["dept_id"] == [dept_1]
    assert body["auto_counting"]["team_id"] == [team_0_dept_1]
    assert body["auto_counting"]["emp_id"] == [1]
    assert body["auto_counting"]["ticket_id"] == [7]
    assert body["auto_counting"]["session_id"] == [7]

    # Test 9: Create new team in dept_1 (also creates emp_id=0)
    res = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": context_name,
            "entries": {"dept_id": dept_1, "action": "create_team_1_emp_0_dept_1"},
        },
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["auto_counting"]["dept_id"] == [dept_1]
    assert body["auto_counting"]["team_id"] == [1]
    assert body["auto_counting"]["emp_id"] == [0]  # Reset for new team
    assert body["auto_counting"]["ticket_id"] == [8]
    assert body["auto_counting"]["session_id"] == [8]
    team_1_dept_1 = body["auto_counting"]["team_id"][0]

    # Test 10: Batch with mixed hierarchy
    batch_entries = [
        {"dept_id": dept_1, "team_id": team_0_dept_1, "action": "batch_emp_2"},
        {"dept_id": dept_1, "team_id": team_0_dept_1, "action": "batch_emp_3"},
        {"dept_id": dept_1, "team_id": team_1_dept_1, "action": "batch_emp_1_team_1"},
    ]
    res = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": context_name,
            "entries": batch_entries,
        },
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()

    # Should have 3 log events
    assert len(body["log_event_ids"]) == 3

    # First two: emp_id increments within team_0, third: second emp in team_1
    assert body["auto_counting"]["dept_id"] == [dept_1, dept_1, dept_1]
    assert body["auto_counting"]["team_id"] == [
        team_0_dept_1,
        team_0_dept_1,
        team_1_dept_1,
    ]
    assert body["auto_counting"]["emp_id"] == [2, 3, 1]  # Third continues in team_1
    # Independent counters continue sequentially
    assert body["auto_counting"]["ticket_id"] == [9, 10, 11]
    assert body["auto_counting"]["session_id"] == [9, 10, 11]

    # Test 11: Verify we can provide explicit values for independent counters
    res = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": context_name,
            "entries": {
                "ticket_id": 999,
                "session_id": 888,
                "action": "explicit_independent_values",
            },
        },
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()

    # Hierarchical counters auto-generate (creates new dept/team/emp)
    assert body["auto_counting"]["dept_id"] == [2]
    assert body["auto_counting"]["team_id"] == [0]
    assert body["auto_counting"]["emp_id"] == [0]
    # Independent counters use provided values
    assert body["auto_counting"]["ticket_id"] == [999]
    assert body["auto_counting"]["session_id"] == [888]


@pytest.mark.anyio
async def test_create_logs_project_not_found(client: AsyncClient):
    project_name = "non_existent_project"

    response = await _create_log(client, project_name)

    assert response.status_code == 404, response.json()
    assert response.json() == {"detail": "Project not found."}


@pytest.mark.anyio
async def test_create_log_with_explicit_nested_list_type(client: AsyncClient):
    """Test creating a log with List[int] explicit type."""
    project_name = "test-nested-list-creation"
    _ = await _create_project(client, project_name)

    # Create a log with List[int] type
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "scores": [95, 87, 92, 88],
                "explicit_types": {
                    "scores": {"type": "List[int]", "mutable": True},
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Verify the field type is stored correctly
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert "scores" in field_types
    assert field_types["scores"]["data_type"] == "List[int]"
    assert field_types["scores"]["field_type"] == "entry"
    assert field_types["scores"]["mutable"] is True

    # Verify the log was created with the correct value
    logs_response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert logs_response.status_code == 200
    logs = logs_response.json()["logs"]
    assert len(logs) == 1
    assert logs[0]["id"] == log_id
    assert logs[0]["entries"]["scores"] == [95, 87, 92, 88]


@pytest.mark.anyio
async def test_create_log_with_explicit_nested_dict_type(client: AsyncClient):
    """Test creating a log with Dict[str, float] explicit type."""
    project_name = "test-nested-dict-creation"
    _ = await _create_project(client, project_name)

    # Create a log with Dict[str, float] type
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "metrics": {"accuracy": 0.95, "f1_score": 0.89, "recall": 0.92},
                "explicit_types": {
                    "metrics": {"type": "Dict[str, float]", "mutable": True},
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Verify the field type
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert "metrics" in field_types
    assert field_types["metrics"]["data_type"] == "Dict[str, float]"


@pytest.mark.anyio
async def test_create_log_explicit_type_overrides_field_name_inference(
    client: AsyncClient,
):
    """Test that explicit types override field name-based inference."""
    project_name = "test-override-name-inference"
    _ = await _create_project(client, project_name)

    # Create fields with names that would trigger inference, but with explicit types
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "recording_url": "https://example.com/file.mp3",
                "audio_file": "",
                "image_path": "path/to/photo.jpg",
                "explicit_types": {
                    "recording_url": {"type": "str", "mutable": True},
                    "audio_file": {"type": "str", "mutable": True},
                    "image_path": {"type": "str", "mutable": True},
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Verify all are stored as str, not audio/image
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert field_types["recording_url"]["data_type"] == "str"
    assert field_types["audio_file"]["data_type"] == "str"
    assert field_types["image_path"]["data_type"] == "str"


@pytest.mark.anyio
async def test_create_log_explicit_type_overrides_value_inference(
    client: AsyncClient,
):
    """Test that explicit types override value-based inference."""
    project_name = "test-override-value-inference"
    _ = await _create_project(client, project_name)

    # Create fields with values that would trigger inference
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "time_label": "12:30",  # Would be inferred as time
                "date_label": "12-30",  # Might be inferred as date
                "file_ext": ".mp3",  # Would be inferred as audio
                "explicit_types": {
                    "time_label": {"type": "str", "mutable": True},
                    "date_label": {"type": "str", "mutable": True},
                    "file_ext": {"type": "str", "mutable": True},
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Verify all are stored as str
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert field_types["time_label"]["data_type"] == "str"
    assert field_types["date_label"]["data_type"] == "str"
    assert field_types["file_ext"]["data_type"] == "str"


@pytest.mark.anyio
async def test_batch_create_logs_with_nested_types(client: AsyncClient):
    """Test batch creation with nested explicit types."""
    project_name = "test-batch-nested-creation"
    _ = await _create_project(client, project_name)

    # Create multiple logs with nested types
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": [
                {
                    "scores": [85, 90, 88],
                    "metrics": {"acc": 0.85, "prec": 0.90},
                    "explicit_types": {
                        "scores": {"type": "List[int]", "mutable": True},
                        "metrics": {"type": "Dict[str, float]", "mutable": True},
                    },
                },
                {
                    "scores": [92, 88, 95],
                    "metrics": {"acc": 0.92, "prec": 0.88},
                    "explicit_types": {
                        "scores": {"type": "List[int]", "mutable": True},
                        "metrics": {"type": "Dict[str, float]", "mutable": True},
                    },
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert len(response.json()["log_event_ids"]) == 2

    # Verify field types
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert field_types["scores"]["data_type"] == "List[int]"
    assert field_types["metrics"]["data_type"] == "Dict[str, float]"


# ================================================================================
# Comprehensive Type Tests - Base and Nested Types
# ================================================================================


@pytest.mark.anyio
async def test_create_field_then_log_with_matching_base_types(client: AsyncClient):
    """Test creating fields first, then logs with matching base types."""
    project_name = "test-field-first-base-types"
    _ = await _create_project(client, project_name)

    # Step 1: Create fields via POST /logs/fields
    response = await client.post(
        "/v0/logs/fields",
        json={
            "project": project_name,
            "fields": {
                "name": {"type": "str", "mutable": True},
                "age": {"type": "int", "mutable": True},
                "score": {"type": "float", "mutable": True},
                "active": {"type": "bool", "mutable": True},
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Step 2: Create log with matching types (implicit - no explicit_types)
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "name": "Alice",
                "age": 30,
                "score": 95.5,
                "active": True,
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Step 3: Verify log was created successfully
    logs_response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert logs_response.status_code == 200
    logs = logs_response.json()["logs"]
    assert len(logs) == 1
    assert logs[0]["entries"]["name"] == "Alice"
    assert logs[0]["entries"]["age"] == 30


@pytest.mark.anyio
async def test_create_field_then_log_with_mismatching_base_types(client: AsyncClient):
    """Test creating fields first, then logs with mismatching base types - should fail."""
    project_name = "test-field-mismatch-base"
    _ = await _create_project(client, project_name)

    # Step 1: Create fields
    response = await client.post(
        "/v0/logs/fields",
        json={
            "project": project_name,
            "fields": {
                "age": {"type": "int", "mutable": True},
                "score": {"type": "float", "mutable": True},
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Step 2: Try to create log with wrong types
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "age": "thirty",  # Wrong: should be int
                "score": "high",  # Wrong: should be float
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 400, response.json()  # Should fail


@pytest.mark.anyio
async def test_create_field_then_log_with_matching_nested_types(client: AsyncClient):
    """Test creating fields with nested types first, then logs with matching nested types."""
    project_name = "test-field-first-nested"
    _ = await _create_project(client, project_name)

    # Step 1: Create fields with nested types
    response = await client.post(
        "/v0/logs/fields",
        json={
            "project": project_name,
            "fields": {
                "scores": {"type": "List[int]", "mutable": True},
                "metrics": {"type": "Dict[str, float]", "mutable": True},
                "tags": {"type": "List[str]", "mutable": True},
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Step 2: Create log with matching nested types
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "scores": [95, 87, 92],
                "metrics": {"accuracy": 0.95, "precision": 0.89},
                "tags": ["ml", "experiment", "v1"],
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Verify
    logs_response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert logs_response.status_code == 200, logs_response.json()
    logs = logs_response.json()["logs"]
    assert len(logs) == 1
    assert logs[0]["entries"]["scores"] == [95, 87, 92]
    assert logs[0]["entries"]["metrics"]["accuracy"] == 0.95


@pytest.mark.anyio
async def test_create_field_then_log_with_mismatching_nested_types(client: AsyncClient):
    """Test nested type mismatch - should fail."""
    project_name = "test-nested-mismatch"
    _ = await _create_project(client, project_name)

    # Step 1: Create field with List[int]
    response = await client.post(
        "/v0/logs/fields",
        json={
            "project": project_name,
            "fields": {
                "scores": {"type": "List[int]", "mutable": True},
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Step 2: Try to create log with List[str] - should fail
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "scores": ["high", "medium", "low"],  # Wrong: should be ints
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 400, response.json()


@pytest.mark.anyio
async def test_implicit_then_explicit_nested_type_creation(client: AsyncClient):
    """Test creating log implicitly, then with explicit nested type."""
    project_name = "test-implicit-explicit-nested"
    _ = await _create_project(client, project_name)

    # Step 1: Create log implicitly (no explicit types) - gets type "Any"
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "data": [1, 2, 3],
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Verify it got type "Any"
    fields_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert fields_response.status_code == 200
    fields = fields_response.json()
    assert fields["data"]["data_type"] == "Any"

    # Step 2: Create another field with explicit nested type
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "scores": [95, 87, 92],
                "explicit_types": {
                    "scores": {"type": "List[int]", "mutable": True},
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Verify explicit type was set
    fields_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert fields_response.status_code == 200
    fields = fields_response.json()
    assert fields["scores"]["data_type"] == "List[int]"


@pytest.mark.anyio
async def test_heterogeneous_list_types(client: AsyncClient):
    """Test creating fields with heterogeneous list types."""
    project_name = "test-hetero-lists"
    _ = await _create_project(client, project_name)

    # Create field with heterogeneous list
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "mixed_data": [1, "text", 3.14, True],
                "explicit_types": {
                    "mixed_data": {
                        "type": "List[int, str, float, bool]",
                        "mutable": True,
                    },
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Verify field type
    fields_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert fields_response.status_code == 200, fields_response.json()
    fields = fields_response.json()
    assert "mixed_data" in fields
    # Type should be stored as provided
    assert fields["mixed_data"]["data_type"] == "List[int, str, float, bool]"


@pytest.mark.anyio
async def test_deeply_nested_types(client: AsyncClient):
    """Test creating fields with deeply nested types."""
    project_name = "test-deep-nested"
    _ = await _create_project(client, project_name)

    # Create field with deeply nested type
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "complex_data": {
                    "level1": {
                        "level2": [
                            {"name": "item1", "values": [1, 2, 3]},
                            {"name": "item2", "values": [4, 5, 6]},
                        ],
                    },
                },
                "explicit_types": {
                    "complex_data": {
                        "type": "Dict[str, Dict[str, List[dict]]]",
                        "mutable": True,
                    },
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Verify
    logs_response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert logs_response.status_code == 200, logs_response.json()
    logs = logs_response.json()["logs"]
    assert len(logs) == 1
    assert "level1" in logs[0]["entries"]["complex_data"]


@pytest.mark.anyio
async def test_create_with_pydantic_schema(client: AsyncClient):
    """Test creating field with Pydantic JSON schema."""
    pytest.importorskip("pydantic")
    from pydantic import BaseModel

    class Person(BaseModel):
        name: str
        age: int

    project_name = "test-pydantic-schema"
    _ = await _create_project(client, project_name)

    # Get Pydantic schema
    person_schema = Person.model_json_schema()

    # Create log with Pydantic schema
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "person": {"name": "Alice", "age": 30},
                "explicit_types": {
                    "person": {"type": person_schema, "mutable": True},
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Verify field was created with correct normalized type
    fields_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert fields_response.status_code == 200, fields_response.json()
    fields = fields_response.json()
    assert "person" in fields
    import json

    assert fields["person"]["data_type"] == json.dumps(person_schema)

    # Verify the stored log inferred type is a dict-like simple type
    logs_response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert logs_response.status_code == 200, logs_response.json()
    logs = logs_response.json()["logs"]
    assert len(logs) == 1
    # entries.person exists and is a dict
    assert isinstance(logs[0]["entries"]["person"], dict)


@pytest.mark.anyio
async def test_create_with_pydantic_schema_validation_failure(client: AsyncClient):
    """Test that invalid data against Pydantic schema fails."""
    pytest.importorskip("pydantic")
    from pydantic import BaseModel

    class Person(BaseModel):
        name: str
        age: int

    project_name = "test-pydantic-validation-fail"
    _ = await _create_project(client, project_name)

    person_schema = Person.model_json_schema()

    # Try to create log with invalid data
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "person": {"name": "Bob"},  # Missing required 'age'
                "explicit_types": {
                    "person": {"type": person_schema, "mutable": True},
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 400, response.json()  # Should fail validation


@pytest.mark.anyio
async def test_create_with_nested_pydantic_schema(client: AsyncClient):
    """Test creating field with nested Pydantic schema."""
    pytest.importorskip("pydantic")
    from typing import List as TypingList

    from pydantic import BaseModel

    class Item(BaseModel):
        name: str
        price: float

    class Order(BaseModel):
        order_id: str
        items: TypingList[Item]

    project_name = "test-nested-pydantic"
    _ = await _create_project(client, project_name)

    order_schema = Order.model_json_schema()

    # Create log with nested Pydantic schema
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "order": {
                    "order_id": "ORD-001",
                    "items": [
                        {"name": "Widget", "price": 9.99},
                        {"name": "Gadget", "price": 19.99},
                    ],
                },
                "explicit_types": {
                    "order": {"type": order_schema, "mutable": True},
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Verify
    logs_response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert logs_response.status_code == 200, logs_response.json()
    logs = logs_response.json()["logs"]
    assert len(logs) == 1
    assert logs[0]["entries"]["order"]["order_id"] == "ORD-001"
    assert len(logs[0]["entries"]["order"]["items"]) == 2

    # Verify field type normalization for nested schema
    fields_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert fields_response.status_code == 200, fields_response.json()
    fields = fields_response.json()
    import json

    assert fields["order"]["data_type"] == json.dumps(order_schema)
