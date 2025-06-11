import pytest
from httpx import AsyncClient

from .test_log import HEADERS, _create_log, _delete_logs, _update_logs, fetch_logs


@pytest.mark.anyio
async def test_basic_commit_and_rollback(client: AsyncClient):
    """
    Tests the core project-level functionality:
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
    assert commit1_res.status_code == 200
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
    assert commit2_res.status_code == 200
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

    res = await _delete_logs(
        client,
        log_ids=[
            ([log_b_id], None),
        ],
        project_name=project_name,
        context=context_name,
    )
    assert res.status_code == 200, res.json()

    await _create_log(
        client,
        project_name,
        context={"name": context_name},
        entries={"log_key": "C"},
    )

    logs_v2_pre_commit = await fetch_logs(client, project_name, context=context_name)
    keys_pre_commit = {log["entries"]["log_key"] for log in logs_v2_pre_commit}
    assert keys_pre_commit == {"A", "C"}

    commit2_res = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Second commit"},
        headers=HEADERS,
    )
    assert commit2_res.status_code == 200

    # --- Rollback and Verify ---
    await client.post(
        f"/v0/project/{project_name}/rollback",
        json={"commit_hash": commit1_hash},
        headers=HEADERS,
    )

    logs_after_rollback = await fetch_logs(client, project_name, context=context_name)
    final_keys = {log["entries"]["log_key"] for log in logs_after_rollback}

    assert len(logs_after_rollback) == 2
    assert final_keys == {"A", "B"}


@pytest.mark.anyio
async def test_context_level_commit_and_rollback(client: AsyncClient):
    """
    Tests that committing and rolling back a single context works and
    does not affect other contexts.
    """
    project_name = "test_context_level_commit"
    context1_name = "context_to_commit"
    context2_name = "untouched_context"

    await client.post(
        "/v0/project",
        json={"name": project_name, "is_versioned": True},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context1_name, "is_versioned": True},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context2_name, "is_versioned": True},
        headers=HEADERS,
    )

    # --- Initial State ---
    await _create_log(
        client,
        project_name,
        context={"name": context1_name},
        entries={"val": 1},
    )
    await _create_log(
        client,
        project_name,
        context={"name": context2_name},
        entries={"val": 100},
    )

    # --- Commit only context1 ---
    commit_res = await client.post(
        f"/v0/project/{project_name}/contexts/{context1_name}/commit",
        json={"commit_message": "context 1 commit"},
        headers=HEADERS,
    )
    assert commit_res.status_code == 200
    commit_hash = commit_res.json()["commit_hash"]

    # --- Modify both contexts ---
    logs1 = await fetch_logs(client, project_name, context=context1_name)
    logs2 = await fetch_logs(client, project_name, context=context2_name)
    await _update_logs(
        client,
        [logs1[0]["id"]],
        {"val": 2},
        context={"name": context1_name},
        overwrite=True,
    )
    await _update_logs(
        client,
        [logs2[0]["id"]],
        {"val": 200},
        context={"name": context2_name},
        overwrite=True,
    )

    # --- Rollback only context1 ---
    rollback_res = await client.post(
        f"/v0/project/{project_name}/contexts/{context1_name}/rollback",
        json={"commit_hash": commit_hash},
        headers=HEADERS,
    )
    assert rollback_res.status_code == 200

    # --- Verify ---
    logs1_final = await fetch_logs(client, project_name, context=context1_name)
    logs2_final = await fetch_logs(client, project_name, context=context2_name)

    assert logs1_final[0]["entries"]["val"] == 1  # Rolled back
    assert logs2_final[0]["entries"]["val"] == 200  # Not rolled back


@pytest.mark.anyio
async def test_commit_history_endpoints(client: AsyncClient):
    """
    Tests the project and context commit history endpoints.
    """
    project_name = "test_history_project"
    context_name = "history_context"

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

    # 1. Project commit
    await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "project commit 1"},
        headers=HEADERS,
    )

    # 2. Context-only commit
    await client.post(
        f"/v0/project/{project_name}/contexts/{context_name}/commit",
        json={"commit_message": "context only commit"},
        headers=HEADERS,
    )

    # 3. Project commit again
    await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "project commit 2"},
        headers=HEADERS,
    )

    # --- Verify Project History ---
    proj_history_res = await client.get(
        f"/v0/project/{project_name}/commits",
        headers=HEADERS,
    )
    assert proj_history_res.status_code == 200
    proj_history = proj_history_res.json()
    assert len(proj_history) == 2
    assert proj_history[0]["commit_message"] == "project commit 2"
    assert proj_history[1]["commit_message"] == "project commit 1"

    # --- Verify Context History ---
    ctx_history_res = await client.get(
        f"/v0/project/{project_name}/contexts/{context_name}/commits",
        headers=HEADERS,
    )
    assert ctx_history_res.status_code == 200
    ctx_history = ctx_history_res.json()
    assert len(ctx_history) == 3
    # Check messages and types (order is descending by time)
    messages = [c["commit_message"] for c in ctx_history]
    types = [c["type"] for c in ctx_history]
    assert messages == ["project commit 2", "context only commit", "project commit 1"]
    assert types == ["project", "context", "project"]


@pytest.mark.anyio
async def test_project_rollback_ignores_context_commits(client: AsyncClient):
    """
    Tests that a project-level rollback correctly restores its state,
    ignoring any intermittent context-only commits.
    """
    project_name = "test_proj_rollback_isolation"
    context_name = "isolated_context"

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

    # --- Project Commit v1 (value: "A") ---
    await _create_log(
        client,
        project_name,
        context={"name": context_name},
        entries={"val": "A"},
    )
    proj_commit1_res = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "v1"},
        headers=HEADERS,
    )
    proj_commit1_hash = proj_commit1_res.json()["commit_hash"]

    # --- Context-only Commit (value: "B") ---
    logs_v1 = await fetch_logs(client, project_name, context=context_name)
    await _update_logs(
        client,
        [logs_v1[0]["id"]],
        {"val": "B"},
        context={"name": context_name},
        overwrite=True,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts/{context_name}/commit",
        json={"commit_message": "context only"},
        headers=HEADERS,
    )

    # --- Current state is now (value: "B") ---
    logs_v_context = await fetch_logs(client, project_name, context=context_name)
    assert logs_v_context[0]["entries"]["val"] == "B"

    # --- Rollback PROJECT to v1 ---
    await client.post(
        f"/v0/project/{project_name}/rollback",
        json={"commit_hash": proj_commit1_hash},
        headers=HEADERS,
    )

    # --- Verify state is "A", not "B" ---
    logs_final = await fetch_logs(client, project_name, context=context_name)
    assert logs_final[0]["entries"]["val"] == "A"
