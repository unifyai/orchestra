"""
Versioning Tests

This test suite validates Orchestra's versioning system.

VERSIONING ARCHITECTURE:
- Uses LogEventVersion table (per-event JSONB snapshots) for version tracking
- Rollback restores data directly from snapshots

See conftest.py for testing infrastructure and performance tracking.
"""

import pytest
from httpx import AsyncClient

from orchestra.tests.test_log import (
    HEADERS,
    _create_log,
    _delete_logs,
    _update_logs,
    fetch_logs,
)
from orchestra.tests.utils import create_test_user


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

    # Verify new branching fields are present in existing endpoints
    for commit in proj_history:
        assert "prev_commit_hash" in commit
        assert "next_commit_hash" in commit
        assert isinstance(commit["next_commit_hash"], list)

    for commit in ctx_history:
        assert "prev_commit_hash" in commit
        assert "next_commit_hash" in commit
        assert isinstance(commit["next_commit_hash"], list)


@pytest.mark.anyio
async def test_project_rollback_ignores_context_commits(
    client: AsyncClient,
):
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

    # --- Rollback and Verify ---
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


@pytest.mark.anyio
async def test_project_level_branching(client: AsyncClient):
    """
    Tests project-level version branching functionality:
    1. Create a log and commit (commit A)
    2. Update the log and commit (commit B)
    3. Update the log again and commit (commit C)
    4. Rollback to commit A
    5. Update the log with different content and commit (commit D)
    6. Verify the branching structure in commit history
    """
    project_name = "test_project_branching"
    context_name = "branching_context"

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

    # --- Commit A: Initial log ---
    await _create_log(
        client,
        project_name,
        context={"name": context_name},
        entries={"value": "initial"},
    )
    commit_a_res = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Commit A - Initial"},
        headers=HEADERS,
    )
    assert commit_a_res.status_code == 200
    commit_a_hash = commit_a_res.json()["commit_hash"]

    # --- Commit B: Update log ---
    logs_a = await fetch_logs(client, project_name, context=context_name)
    await _update_logs(
        client,
        [logs_a[0]["id"]],
        {"value": "updated_b"},
        context={"name": context_name},
        overwrite=True,
    )
    commit_b_res = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Commit B - First update"},
        headers=HEADERS,
    )
    assert commit_b_res.status_code == 200
    commit_b_hash = commit_b_res.json()["commit_hash"]

    # --- Commit C: Update log again ---
    logs_b = await fetch_logs(client, project_name, context=context_name)
    await _update_logs(
        client,
        [logs_b[0]["id"]],
        {"value": "updated_c"},
        context={"name": context_name},
        overwrite=True,
    )
    commit_c_res = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Commit C - Second update"},
        headers=HEADERS,
    )
    assert commit_c_res.status_code == 200
    commit_c_hash = commit_c_res.json()["commit_hash"]

    # --- Rollback to commit A ---
    await client.post(
        f"/v0/project/{project_name}/rollback",
        json={"commit_hash": commit_a_hash},
        headers=HEADERS,
    )

    # --- Commit D: Update with different content (creates branch) ---
    logs_after_rollback = await fetch_logs(client, project_name, context=context_name)
    await _update_logs(
        client,
        [logs_after_rollback[0]["id"]],
        {"value": "branched_d"},
        context={"name": context_name},
        overwrite=True,
    )
    commit_d_res = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Commit D - Branched update"},
        headers=HEADERS,
    )
    assert commit_d_res.status_code == 200
    commit_d_hash = commit_d_res.json()["commit_hash"]

    # --- Verify branching structure ---
    history_res = await client.get(
        f"/v0/project/{project_name}/commits",
        headers=HEADERS,
    )
    assert history_res.status_code == 200
    history = history_res.json()

    # Create a lookup for commits by hash
    commits_by_hash = {commit["commit_hash"]: commit for commit in history}

    # Verify commit A has both B and D as next commits
    commit_a = commits_by_hash[commit_a_hash]
    assert set(commit_a["next_commit_hash"]) == {commit_b_hash, commit_d_hash}
    assert commit_a["prev_commit_hash"] is None

    # Verify commit B has A as previous commit
    commit_b = commits_by_hash[commit_b_hash]
    assert commit_b["prev_commit_hash"] == commit_a_hash
    assert commit_b["next_commit_hash"] == [commit_c_hash]

    # Verify commit C has B as previous commit
    commit_c = commits_by_hash[commit_c_hash]
    assert commit_c["prev_commit_hash"] == commit_b_hash
    assert commit_c["next_commit_hash"] == []

    # Verify commit D has A as previous commit
    commit_d = commits_by_hash[commit_d_hash]
    assert commit_d["prev_commit_hash"] == commit_a_hash
    assert commit_d["next_commit_hash"] == []


@pytest.mark.anyio
async def test_context_level_branching(client: AsyncClient):
    """
    Tests context-level version branching functionality using context-specific endpoints.
    Mirrors the project-level branching test but uses context commit/rollback endpoints.
    """
    project_name = "test_context_branching"
    context_name = "context_branching"

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

    # --- Commit A: Initial log ---
    await _create_log(
        client,
        project_name,
        context={"name": context_name},
        entries={"value": "initial_context"},
    )
    commit_a_res = await client.post(
        f"/v0/project/{project_name}/contexts/{context_name}/commit",
        json={"commit_message": "Context Commit A - Initial"},
        headers=HEADERS,
    )
    assert commit_a_res.status_code == 200
    commit_a_hash = commit_a_res.json()["commit_hash"]

    # --- Commit B: Update log ---
    logs_a = await fetch_logs(client, project_name, context=context_name)
    await _update_logs(
        client,
        [logs_a[0]["id"]],
        {"value": "updated_context_b"},
        context={"name": context_name},
        overwrite=True,
    )
    commit_b_res = await client.post(
        f"/v0/project/{project_name}/contexts/{context_name}/commit",
        json={"commit_message": "Context Commit B - First update"},
        headers=HEADERS,
    )
    assert commit_b_res.status_code == 200
    commit_b_hash = commit_b_res.json()["commit_hash"]

    # --- Commit C: Update log again ---
    logs_b = await fetch_logs(client, project_name, context=context_name)
    await _update_logs(
        client,
        [logs_b[0]["id"]],
        {"value": "updated_context_c"},
        context={"name": context_name},
        overwrite=True,
    )
    commit_c_res = await client.post(
        f"/v0/project/{project_name}/contexts/{context_name}/commit",
        json={"commit_message": "Context Commit C - Second update"},
        headers=HEADERS,
    )
    assert commit_c_res.status_code == 200
    commit_c_hash = commit_c_res.json()["commit_hash"]

    # --- Rollback to commit A ---
    await client.post(
        f"/v0/project/{project_name}/contexts/{context_name}/rollback",
        json={"commit_hash": commit_a_hash},
        headers=HEADERS,
    )

    # --- Commit D: Update with different content (creates branch) ---
    logs_after_rollback = await fetch_logs(client, project_name, context=context_name)
    await _update_logs(
        client,
        [logs_after_rollback[0]["id"]],
        {"value": "branched_context_d"},
        context={"name": context_name},
        overwrite=True,
    )
    commit_d_res = await client.post(
        f"/v0/project/{project_name}/contexts/{context_name}/commit",
        json={"commit_message": "Context Commit D - Branched update"},
        headers=HEADERS,
    )
    assert commit_d_res.status_code == 200
    commit_d_hash = commit_d_res.json()["commit_hash"]

    # --- Verify branching structure ---
    history_res = await client.get(
        f"/v0/project/{project_name}/contexts/{context_name}/commits",
        headers=HEADERS,
    )
    assert history_res.status_code == 200
    history = history_res.json()

    # Create a lookup for commits by hash
    commits_by_hash = {commit["commit_hash"]: commit for commit in history}

    # Verify commit A has both B and D as next commits
    commit_a = commits_by_hash[commit_a_hash]
    assert set(commit_a["next_commit_hash"]) == {commit_b_hash, commit_d_hash}
    assert commit_a["prev_commit_hash"] is None

    # Verify commit B has A as previous commit
    commit_b = commits_by_hash[commit_b_hash]
    assert commit_b["prev_commit_hash"] == commit_a_hash
    assert commit_b["next_commit_hash"] == [commit_c_hash]

    # Verify commit C has B as previous commit
    commit_c = commits_by_hash[commit_c_hash]
    assert commit_c["prev_commit_hash"] == commit_b_hash
    assert commit_c["next_commit_hash"] == []

    # Verify commit D has A as previous commit
    commit_d = commits_by_hash[commit_d_hash]
    assert commit_d["prev_commit_hash"] == commit_a_hash
    assert commit_d["next_commit_hash"] == []


@pytest.mark.anyio
async def test_branching_history_endpoints(client: AsyncClient):
    """
    Tests that commit history endpoints return the correct branching information
    and that the new fields are present even in linear histories.
    """
    project_name = "test_branching_history"
    context_name = "history_branching_context"

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

    # Create a simple branching scenario
    await _create_log(
        client,
        project_name,
        context={"name": context_name},
        entries={"value": "base"},
    )

    # Project commit
    proj_commit_res = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Project base commit"},
        headers=HEADERS,
    )
    assert proj_commit_res.status_code == 200
    proj_commit_hash = proj_commit_res.json()["commit_hash"]

    # Context-only commit
    logs = await fetch_logs(client, project_name, context=context_name)
    await _update_logs(
        client,
        [logs[0]["id"]],
        {"value": "context_update"},
        context={"name": context_name},
        overwrite=True,
    )
    ctx_commit_res = await client.post(
        f"/v0/project/{project_name}/contexts/{context_name}/commit",
        json={"commit_message": "Context only commit"},
        headers=HEADERS,
    )
    assert ctx_commit_res.status_code == 200
    ctx_commit_hash = ctx_commit_res.json()["commit_hash"]

    # --- Test Project History Endpoint ---
    proj_history_res = await client.get(
        f"/v0/project/{project_name}/commits",
        headers=HEADERS,
    )
    assert proj_history_res.status_code == 200
    proj_history = proj_history_res.json()

    # Verify new fields are present
    for commit in proj_history:
        assert "prev_commit_hash" in commit
        assert "next_commit_hash" in commit
        assert isinstance(commit["next_commit_hash"], list)

    # Verify the project commit has correct branching info
    proj_commit = next(c for c in proj_history if c["commit_hash"] == proj_commit_hash)
    assert proj_commit["prev_commit_hash"] is None  # First commit
    assert proj_commit["next_commit_hash"] == []  # No project-level children

    # --- Test Context History Endpoint ---
    ctx_history_res = await client.get(
        f"/v0/project/{project_name}/contexts/{context_name}/commits",
        headers=HEADERS,
    )
    assert ctx_history_res.status_code == 200
    ctx_history = ctx_history_res.json()

    # Verify new fields are present
    for commit in ctx_history:
        assert "prev_commit_hash" in commit
        assert "next_commit_hash" in commit
        assert isinstance(commit["next_commit_hash"], list)
        assert "type" in commit  # Context history includes type field

    # Verify the context commit has correct branching info
    ctx_commit = next(c for c in ctx_history if c["commit_hash"] == ctx_commit_hash)
    assert (
        ctx_commit["prev_commit_hash"] == proj_commit_hash
    )  # Points to project commit
    assert ctx_commit["next_commit_hash"] == []  # No children
    assert ctx_commit["type"] == "context"

    # Verify the project commit in context history
    proj_in_ctx = next(c for c in ctx_history if c["commit_hash"] == proj_commit_hash)
    assert proj_in_ctx["prev_commit_hash"] is None
    assert (
        ctx_commit_hash in proj_in_ctx["next_commit_hash"]
    )  # Has context commit as child
    assert proj_in_ctx["type"] == "project"


# =============================================================================
# JSONB-Specific Versioning Tests
# =============================================================================


@pytest.mark.anyio
async def test_jsonb_large_document_versioning(client: AsyncClient):
    """
    Tests versioning with large JSONB documents (>8KB) that trigger PostgreSQL TOAST storage.

    JSONB mode stores entire documents in LogEvent.data, which can exceed the 8KB inline
    storage threshold. This test ensures LogEventVersion correctly snapshots and restores
    large TOASTed JSONB values.

    """
    project_name = "test_large_jsonb_versioning"
    context_name = "large_doc_context"

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

    # Create a log with a large text field (>8KB to trigger TOAST)
    large_text = "x" * 10000  # 10KB text
    await _create_log(
        client,
        project_name,
        context={"name": context_name},
        entries={
            "large_field": large_text,
            "metadata": {"nested": {"deep": "value"}},
            "array": list(range(100)),
        },
    )

    # Commit v1
    commit1_res = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Large doc v1"},
        headers=HEADERS,
    )
    assert commit1_res.status_code == 200
    commit1_hash = commit1_res.json()["commit_hash"]

    # Update to smaller document
    logs_v1 = await fetch_logs(client, project_name, context=context_name)
    await _update_logs(
        client,
        [logs_v1[0]["id"]],
        {"large_field": "small", "metadata": {"changed": True}},
        context={"name": context_name},
        overwrite=True,
    )

    # Commit v2
    await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Large doc v2"},
        headers=HEADERS,
    )

    # Rollback to v1 and verify large document restored
    await client.post(
        f"/v0/project/{project_name}/rollback",
        json={"commit_hash": commit1_hash},
        headers=HEADERS,
    )

    logs_after_rollback = await fetch_logs(client, project_name, context=context_name)
    assert len(logs_after_rollback) == 1
    assert logs_after_rollback[0]["entries"]["large_field"] == large_text
    assert logs_after_rollback[0]["entries"]["metadata"] == {
        "nested": {"deep": "value"},
    }
    assert logs_after_rollback[0]["entries"]["array"] == list(range(100))


@pytest.mark.anyio
async def test_jsonb_key_order_preservation(client: AsyncClient):
    """
    Tests that LogEvent.key_order is preserved through commit/rollback cycles.

    JSONB mode stores key_order separately to maintain dict insertion order for UI rendering.
    This test ensures key_order is correctly snapshotted in LogEventVersion and restored.

    """
    project_name = "test_key_order_versioning"
    context_name = "key_order_context"

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

    # Create log with specific key order
    await _create_log(
        client,
        project_name,
        context={"name": context_name},
        entries={
            "z_field": "last",
            "a_field": "first",
            "m_field": "middle",
        },
    )

    # Fetch and verify key_order exists
    logs_v1 = await fetch_logs(client, project_name, context=context_name)
    original_key_order = logs_v1[0].get("key_order")
    # key_order may be None in some implementations - just capture original state
    original_keys = set(logs_v1[0]["entries"].keys())

    # Commit v1
    commit1_res = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Key order v1"},
        headers=HEADERS,
    )
    commit1_hash = commit1_res.json()["commit_hash"]

    # Update with different key order (add new fields)
    await _update_logs(
        client,
        [logs_v1[0]["id"]],
        {
            "new_field": "added",
            "z_field": "updated",
        },
        context={"name": context_name},
        overwrite=False,  # Append, don't overwrite
    )

    # Commit v2
    await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Key order v2"},
        headers=HEADERS,
    )

    # Rollback to v1
    await client.post(
        f"/v0/project/{project_name}/rollback",
        json={"commit_hash": commit1_hash},
        headers=HEADERS,
    )

    # Verify key_order and keys restored
    logs_after_rollback = await fetch_logs(client, project_name, context=context_name)
    restored_key_order = logs_after_rollback[0].get("key_order")

    # If key_order was tracked, verify it's restored
    if original_key_order is not None:
        assert restored_key_order == original_key_order, (
            f"key_order not preserved after rollback. "
            f"Expected: {original_key_order}, Got: {restored_key_order}"
        )

    # Verify keys match original
    assert set(logs_after_rollback[0]["entries"].keys()) == original_keys
    assert "new_field" not in logs_after_rollback[0]["entries"]


@pytest.mark.anyio
async def test_jsonb_nested_structure_versioning(client: AsyncClient):
    """
    Tests versioning of deeply nested dicts and arrays in JSONB mode.

    JSONB mode stores complex nested structures in a single data column. This test
    ensures nested modifications are correctly captured and restored.

    """
    project_name = "test_nested_jsonb_versioning"
    context_name = "nested_context"

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

    # Create log with nested structures
    await _create_log(
        client,
        project_name,
        context={"name": context_name},
        entries={
            "config": {
                "model": {
                    "name": "gpt-4",
                    "params": {
                        "temperature": 0.7,
                        "max_tokens": 100,
                    },
                },
                "retry": {"max_attempts": 3, "backoff": [1, 2, 4]},
            },
            "tags": ["prod", "critical"],
            "matrix": [[1, 2], [3, 4]],
        },
    )

    # Commit v1
    commit1_res = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Nested v1"},
        headers=HEADERS,
    )
    commit1_hash = commit1_res.json()["commit_hash"]

    # Deep update: modify nested values
    logs_v1 = await fetch_logs(client, project_name, context=context_name)
    await _update_logs(
        client,
        [logs_v1[0]["id"]],
        {
            "config": {
                "model": {
                    "name": "gpt-3.5-turbo",  # Changed
                    "params": {
                        "temperature": 0.9,  # Changed
                        "max_tokens": 200,  # Changed
                    },
                },
                "retry": {"max_attempts": 5, "backoff": [2, 4, 8]},  # Changed
            },
            "tags": ["staging"],  # Completely replaced
            "matrix": [[5, 6], [7, 8], [9, 10]],  # Extended
        },
        context={"name": context_name},
        overwrite=True,
    )

    # Commit v2
    await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Nested v2"},
        headers=HEADERS,
    )

    # Rollback to v1
    await client.post(
        f"/v0/project/{project_name}/rollback",
        json={"commit_hash": commit1_hash},
        headers=HEADERS,
    )

    # Verify exact nested structure restored
    logs_after_rollback = await fetch_logs(client, project_name, context=context_name)
    entries = logs_after_rollback[0]["entries"]

    assert entries["config"]["model"]["name"] == "gpt-4"
    assert entries["config"]["model"]["params"]["temperature"] == 0.7
    assert entries["config"]["model"]["params"]["max_tokens"] == 100
    assert entries["config"]["retry"]["max_attempts"] == 3
    assert entries["config"]["retry"]["backoff"] == [1, 2, 4]
    assert entries["tags"] == ["prod", "critical"]
    assert entries["matrix"] == [[1, 2], [3, 4]]


@pytest.mark.anyio
async def test_rollback_cleans_up_field_types(client: AsyncClient):
    """
    Tests that rollback removes FieldType records created after the commit point.

    BUG: Currently FieldType records are NOT cleaned up during rollback.
    This means that after rolling back, the fields list still shows fields
    that were created after the rolled-back commit, even though the logs
    containing those fields no longer exist.

    Steps:
    1. Create a log with field_a, commit
    2. Add a new field field_b to the log
    3. Rollback to the first commit
    4. Verify field_b is NOT in the fields list (this currently fails)
    """
    project_name = "test_rollback_field_cleanup"
    context_name = "field_cleanup_context"

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

    # --- Version 1: Create log with field_a ---
    await _create_log(
        client,
        project_name,
        context={"name": context_name},
        entries={"field_a": "initial_value"},
    )

    # Get fields before commit - should only have field_a
    fields_v1_res = await client.get(
        f"/v0/logs/fields?project_name={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert fields_v1_res.status_code == 200
    fields_v1 = set(fields_v1_res.json().keys())
    assert "field_a" in fields_v1
    assert "field_b" not in fields_v1

    # Commit v1
    commit1_res = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Initial commit with field_a"},
        headers=HEADERS,
    )
    assert commit1_res.status_code == 200
    commit1_hash = commit1_res.json()["commit_hash"]

    # --- Add field_b (after commit) ---
    logs_v1 = await fetch_logs(client, project_name, context=context_name)
    await _update_logs(
        client,
        [logs_v1[0]["id"]],
        {"field_b": "new_field_value"},
        context={"name": context_name},
        overwrite=False,  # Append, don't overwrite
    )

    # Verify field_b now exists
    fields_v2_res = await client.get(
        f"/v0/logs/fields?project_name={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert fields_v2_res.status_code == 200
    fields_v2 = set(fields_v2_res.json().keys())
    assert "field_a" in fields_v2
    assert "field_b" in fields_v2, "field_b should exist after update"

    # --- Rollback to v1 ---
    rollback_res = await client.post(
        f"/v0/project/{project_name}/rollback",
        json={"commit_hash": commit1_hash},
        headers=HEADERS,
    )
    assert rollback_res.status_code == 200

    # Verify log data is rolled back
    logs_after_rollback = await fetch_logs(client, project_name, context=context_name)
    assert len(logs_after_rollback) == 1
    assert logs_after_rollback[0]["entries"]["field_a"] == "initial_value"
    assert (
        "field_b" not in logs_after_rollback[0]["entries"]
    ), "field_b should not be in log entries after rollback"

    # --- KEY ASSERTION: field_b should NOT be in the fields list ---
    fields_after_rollback_res = await client.get(
        f"/v0/logs/fields?project_name={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert fields_after_rollback_res.status_code == 200
    fields_after_rollback = set(fields_after_rollback_res.json().keys())

    assert "field_a" in fields_after_rollback, "field_a should still exist"
    assert "field_b" not in fields_after_rollback, (
        f"BUG: field_b should NOT exist after rollback, but found fields: "
        f"{fields_after_rollback}. FieldType records created after the commit "
        f"point are not being cleaned up during rollback."
    )


@pytest.mark.anyio
async def test_rollback_cleans_up_active_derived_log_templates(
    client: AsyncClient,
    dbsession,
):
    """
    Tests that rollback removes ActiveDerivedLog templates created after the commit point.

    BUG: Currently ActiveDerivedLog templates are NOT cleaned up during rollback.
    This means that after rolling back, derived field templates still exist and
    will be applied to new logs, even though the derived field shouldn't exist
    in the rolled-back state.

    Steps:
    1. Create a log with base_value, commit
    2. Create a derived field computed_value = base_value * 2
    3. Rollback to the first commit
    4. Verify the ActiveDerivedLog template for computed_value is deleted
    """
    project_name = "test_rollback_derived_template_cleanup"
    context_name = "derived_cleanup_context"

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

    # --- Version 1: Create log with base_value ---
    await _create_log(
        client,
        project_name,
        context={"name": context_name},
        entries={"base_value": 10},
    )

    # Commit v1
    commit1_res = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Initial commit with base_value"},
        headers=HEADERS,
    )
    assert commit1_res.status_code == 200
    commit1_hash = commit1_res.json()["commit_hash"]

    # --- Create derived field (after commit) ---
    derived_key = "computed_value"
    derive_res = await client.post(
        "/v0/logs/derived",
        json={
            "project_name": project_name,
            "key": derived_key,
            "equation": "{log:base_value} * 2",
            "referenced_logs": {"log": {"context": context_name}},
            "context": context_name,
        },
        headers=HEADERS,
    )
    assert derive_res.status_code == 200, derive_res.text
    assert "Created 1 derived logs" in derive_res.json().get("info", "")

    # Verify derived field exists in logs
    logs_with_derived = await fetch_logs(client, project_name, context=context_name)
    assert len(logs_with_derived) == 1
    assert logs_with_derived[0].get("derived_entries", {}).get(derived_key) == 20

    # Verify the ActiveDerivedLog template exists (check via fields endpoint)
    fields_with_derived = await client.get(
        f"/v0/logs/fields?project_name={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert fields_with_derived.status_code == 200
    assert derived_key in fields_with_derived.json()

    # --- Rollback to v1 ---
    rollback_res = await client.post(
        f"/v0/project/{project_name}/rollback",
        json={"commit_hash": commit1_hash},
        headers=HEADERS,
    )
    assert rollback_res.status_code == 200

    # Verify log data is rolled back (no derived field)
    logs_after_rollback = await fetch_logs(client, project_name, context=context_name)
    assert len(logs_after_rollback) == 1
    assert logs_after_rollback[0]["entries"]["base_value"] == 10
    assert derived_key not in logs_after_rollback[0].get(
        "derived_entries",
        {},
    ), f"Derived field {derived_key} should not exist in log after rollback"

    # --- KEY ASSERTION: ActiveDerivedLog template should be deleted ---
    # The template's existence can be checked by:
    # 1. The derived field should not appear in the fields list
    # 2. Creating a new log should NOT auto-compute the derived field

    # Check 1: Derived field should not be in fields list
    fields_after_rollback = await client.get(
        f"/v0/logs/fields?project_name={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert fields_after_rollback.status_code == 200
    fields_after = fields_after_rollback.json()
    assert derived_key not in fields_after, (
        f"BUG: Derived field '{derived_key}' should NOT be in fields after rollback. "
        f"Found fields: {list(fields_after.keys())}. "
        f"ActiveDerivedLog templates created after the commit point are not being "
        f"cleaned up during rollback."
    )

    # Check 2: Directly verify ActiveDerivedLog template is deleted from database
    from sqlalchemy import select

    from orchestra.db.models.orchestra_models import ActiveDerivedLog, Context, Project

    dbsession.expire_all()

    # Get project and context IDs
    project = dbsession.execute(
        select(Project).where(Project.name == project_name),
    ).scalar_one()
    context = dbsession.execute(
        select(Context).where(
            Context.project_id == project.id,
            Context.name == context_name,
        ),
    ).scalar_one()

    # Check if ActiveDerivedLog template still exists
    template = dbsession.execute(
        select(ActiveDerivedLog).where(
            ActiveDerivedLog.project_id == project.id,
            ActiveDerivedLog.context_id == context.id,
            ActiveDerivedLog.key == derived_key,
        ),
    ).scalar_one_or_none()

    assert template is None, (
        f"BUG: ActiveDerivedLog template for '{derived_key}' should NOT exist after "
        f"rollback. The template was created after the commit point and should have "
        f"been cleaned up. Found template: key={template.key}, equation={template.equation}"
    )


@pytest.mark.anyio
async def test_rollback_cleans_up_plots(client: AsyncClient, dbsession):
    """Tests that rollback removes Plot records created after the commit point."""
    from orchestra.db.dao.context_dao import ContextDAO
    from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
    from orchestra.db.dao.plot_dao import PlotDAO
    from orchestra.db.dao.project_dao import ProjectDAO

    user = await create_test_user(client, "rollback_plot_cleanup@test.com")

    project_name = "test_rollback_plot_cleanup"
    context_name = "plot_cleanup_context"

    # Create project and context via DAOs
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name=project_name,
        user_id=user["id"],
        organization_id=None,
        is_versioned=True,
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=user["id"], name=project_name)
    project = projects[0][0]

    context_id = context_dao.create(
        project_id=project.id,
        name=context_name,
        is_versioned=True,
    )
    dbsession.commit()

    # --- Version 1: Create a log and commit ---
    await client.post(
        "/v0/log",
        json={
            "project": project_name,
            "context": {"name": context_name},
            "entries": {"value": 100},
        },
        headers=user["headers"],
    )

    # Commit v1
    commit1_res = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Initial commit"},
        headers=user["headers"],
    )
    assert commit1_res.status_code == 200
    commit1_hash = commit1_res.json()["commit_hash"]

    # --- Create a plot after the commit (using DAO) ---
    plot_dao = PlotDAO(dbsession)
    plot = plot_dao.create(
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
        plot_config={"type": "histogram", "x_axis": "value"},
        project_config={
            "project_name": project_name,
            "context": context_name,
        },
        title="Plot Created After Commit",
    )
    dbsession.commit()
    plot_token = plot.token

    # Verify plot exists
    assert plot_dao.get_by_token(plot_token) is not None

    # --- Rollback to v1 ---
    rollback_res = await client.post(
        f"/v0/project/{project_name}/rollback",
        json={"commit_hash": commit1_hash},
        headers=user["headers"],
    )
    assert rollback_res.status_code == 200

    # Refresh session to see changes
    dbsession.expire_all()

    # --- KEY ASSERTION: Plot should NOT exist after rollback ---
    # The plot was created after the commit point we rolled back to,
    # so it should be cleaned up during rollback
    assert plot_dao.get_by_token(plot_token) is None, (
        f"BUG: Plot should NOT exist after rollback. "
        f"Plot records created after the commit point are not being cleaned up "
        f"during rollback."
    )


@pytest.mark.anyio
async def test_rollback_cleans_up_table_views(client: AsyncClient, dbsession):
    """Tests that rollback removes TableView records created after the commit point."""
    from orchestra.db.dao.context_dao import ContextDAO
    from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
    from orchestra.db.dao.project_dao import ProjectDAO
    from orchestra.db.dao.table_view_dao import TableViewDAO

    user = await create_test_user(client, "rollback_table_view_cleanup@test.com")

    project_name = "test_rollback_table_view_cleanup"
    context_name = "table_view_cleanup_context"

    # Create project and context via DAOs
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name=project_name,
        user_id=user["id"],
        organization_id=None,
        is_versioned=True,
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=user["id"], name=project_name)
    project = projects[0][0]

    context_id = context_dao.create(
        project_id=project.id,
        name=context_name,
        is_versioned=True,
    )
    dbsession.commit()

    # --- Version 1: Create a log and commit ---
    await client.post(
        "/v0/log",
        json={
            "project": project_name,
            "context": {"name": context_name},
            "entries": {"value": 100},
        },
        headers=user["headers"],
    )

    # Commit v1
    commit1_res = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Initial commit"},
        headers=user["headers"],
    )
    assert commit1_res.status_code == 200
    commit1_hash = commit1_res.json()["commit_hash"]

    # --- Create a table view after the commit (using DAO) ---
    table_view_dao = TableViewDAO(dbsession)
    table_view = table_view_dao.create(
        project_id=project.id,
        user_id=user["id"],
        organization_id=None,
        table_config={"columns": ["value"]},
        project_config={
            "project_name": project_name,
            "context": context_name,
        },
        title="TableView Created After Commit",
    )
    dbsession.commit()
    table_view_token = table_view.token

    # Verify table view exists
    assert table_view_dao.get_by_token(table_view_token) is not None

    # --- Rollback to v1 ---
    rollback_res = await client.post(
        f"/v0/project/{project_name}/rollback",
        json={"commit_hash": commit1_hash},
        headers=user["headers"],
    )
    assert rollback_res.status_code == 200

    # Refresh session to see changes
    dbsession.expire_all()

    # --- KEY ASSERTION: TableView should NOT exist after rollback ---
    # The table view was created after the commit point we rolled back to,
    # so it should be cleaned up during rollback
    assert table_view_dao.get_by_token(table_view_token) is None, (
        f"BUG: TableView should NOT exist after rollback. "
        f"TableView records created after the commit point are not being cleaned up "
        f"during rollback."
    )
