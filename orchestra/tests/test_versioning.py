import pytest
from httpx import AsyncClient

from .test_log import HEADERS, _create_log, _delete_logs, _update_logs, fetch_logs


@pytest.mark.anyio
async def test_basic_commit_and_rollback(client: AsyncClient):
    """
    Tests the core functionality:
    1. Create a log and commit (v1).
    2. Update the log and commit (v2).
    3. Rollback to v1 and verify the state.
    4. Rollback to v2 and verify the state.
    """
    project_name = "test_basic_rollback_project"
    context_name = "versioned_context"

    # Setup: Create a versioned project and context
    await client.post(
        "/v0/project",
        json={"name": project_name, "is_versioned": True},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name, "is_versioned": True},
        headers=HEADERS,
    )

    # --- Version 1 ---
    await _create_log(
        client,
        project_name,
        context={"name": context_name},
        entries={"value": "v1"},
    )
    commit1_res = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Initial commit"},
        headers=HEADERS,
    )
    commit1_hash = commit1_res.json()["commit_hash"]

    # --- Version 2 ---
    logs_v1 = await fetch_logs(client, project_name, context=context_name)
    await _update_logs(
        client,
        [logs_v1[0]["id"]],
        {"value": "v2"},
        context={"name": context_name},
        overwrite=True,
    )
    commit2_res = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Second commit"},
        headers=HEADERS,
    )
    commit2_hash = commit2_res.json()["commit_hash"]

    # --- Rollback and Verify ---
    await client.post(
        f"/v0/project/{project_name}/rollback",
        json={"commit_hash": commit1_hash},
        headers=HEADERS,
    )
    logs_after_rollback_1 = await fetch_logs(client, project_name, context=context_name)
    assert len(logs_after_rollback_1) == 1
    assert logs_after_rollback_1[0]["entries"]["value"] == "v1"

    await client.post(
        f"/v0/project/{project_name}/rollback",
        json={"commit_hash": commit2_hash},
        headers=HEADERS,
    )
    logs_after_rollback_2 = await fetch_logs(client, project_name, context=context_name)
    assert len(logs_after_rollback_2) == 1
    assert logs_after_rollback_2[0]["entries"]["value"] == "v2"


@pytest.mark.anyio
async def test_rollback_with_structural_changes(client: AsyncClient):
    """
    Tests that rollback correctly handles logs being added and removed.
    1. Create log_A and log_B, then commit.
    2. Delete log_B and add log_C, then commit.
    3. Rollback to the first state and verify that A and B exist, but C does not.
    """
    project_name = "test_structural_rollback"
    context_name = "context_with_changes"

    # Setup
    await client.post(
        "/v0/project",
        json={"name": project_name, "is_versioned": True},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name, "is_versioned": True},
        headers=HEADERS,
    )

    # --- Version 1: Two logs exist (A and B) ---
    await _create_log(
        client,
        project_name,
        context={"name": context_name},
        entries={"log_key": "A"},
    )
    await _create_log(
        client,
        project_name,
        context={"name": context_name},
        entries={"log_key": "B"},
    )

    commit1_res = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Initial commit"},
        headers=HEADERS,
    )
    assert commit1_res.status_code == 200, commit1_res.json()
    commit1_hash = commit1_res.json()["commit_hash"]

    # --- Version 2: Delete log B, add log C ---
    logs_v1 = await fetch_logs(client, project_name, context=context_name)
    log_b_id = next(log["id"] for log in logs_v1 if log["entries"]["log_key"] == "B")

    # Use the delete helper to remove log B
    res = await _delete_logs(
        client,
        log_ids=[
            ([log_b_id], None),
        ],
        project_name=project_name,
        context=context_name,
    )
    assert res.status_code == 200, res.json()

    # Add log C
    await _create_log(
        client,
        project_name,
        context={"name": context_name},
        entries={"log_key": "C"},
    )

    # Sanity check the state before second commit
    logs_v2_pre_commit = await fetch_logs(client, project_name, context=context_name)
    keys_pre_commit = {log["entries"]["log_key"] for log in logs_v2_pre_commit}
    assert keys_pre_commit == {"A", "C"}

    commit2_res = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Second commit"},
        headers=HEADERS,
    )

    # --- Rollback and Verify ---
    # Rollback to the first state where A and B existed
    await client.post(
        f"/v0/project/{project_name}/rollback",
        json={"commit_hash": commit1_hash},
        headers=HEADERS,
    )

    logs_after_rollback = await fetch_logs(client, project_name, context=context_name)
    final_keys = {log["entries"]["log_key"] for log in logs_after_rollback}

    assert len(logs_after_rollback) == 2
    assert final_keys == {"A", "B"}  # C is gone, B is restored.


@pytest.mark.anyio
async def test_rollback_with_multiple_contexts(client: AsyncClient):
    """
    Tests that a project rollback correctly affects all its versioned contexts,
    while leaving non-versioned contexts untouched.
    """
    project_name = "multi_context_project"
    v_context1_name = "v_context1"
    v_context2_name = "v_context2"
    nv_context_name = "non_versioned_context"

    # Setup
    await client.post(
        "/v0/project",
        json={"name": project_name, "is_versioned": True},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": v_context1_name, "is_versioned": True},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": v_context2_name, "is_versioned": True},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": nv_context_name, "is_versioned": False},
        headers=HEADERS,
    )

    # --- Version 1 State ---
    await _create_log(
        client,
        project_name,
        context={"name": v_context1_name},
        entries={"val": 1},
    )
    await _create_log(
        client,
        project_name,
        context={"name": v_context2_name},
        entries={"val": 1},
    )
    await _create_log(
        client,
        project_name,
        context={"name": nv_context_name},
        entries={"val": 1},
    )

    commit1_res = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Initial commit"},
        headers=HEADERS,
    )
    commit1_hash = commit1_res.json()["commit_hash"]

    # --- Modified State ---
    logs1 = await fetch_logs(client, project_name, context=v_context1_name)
    logs2 = await fetch_logs(client, project_name, context=v_context2_name)
    logs3 = await fetch_logs(client, project_name, context=nv_context_name)

    await _update_logs(
        client,
        [logs1[0]["id"]],
        {"val": 100},
        context={"name": v_context1_name},
        overwrite=True,
    )
    await _update_logs(
        client,
        [logs2[0]["id"]],
        {"val": 200},
        context={"name": v_context2_name},
        overwrite=True,
    )
    await _update_logs(
        client,
        [logs3[0]["id"]],
        {"val": 300},
        context={"name": nv_context_name},
        overwrite=True,
    )

    # --- Rollback and Verify ---
    await client.post(
        f"/v0/project/{project_name}/rollback",
        json={"commit_hash": commit1_hash},
        headers=HEADERS,
    )

    # Verify versioned contexts were rolled back
    logs1_final = await fetch_logs(client, project_name, context=v_context1_name)
    logs2_final = await fetch_logs(client, project_name, context=v_context2_name)
    assert logs1_final[0]["entries"]["val"] == 1
    assert logs2_final[0]["entries"]["val"] == 1

    # Verify non-versioned context was NOT rolled back
    logs3_final = await fetch_logs(client, project_name, context=nv_context_name)
    assert logs3_final[0]["entries"]["val"] == 300
