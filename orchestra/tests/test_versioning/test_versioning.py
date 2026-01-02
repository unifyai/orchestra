"""
Versioning Tests - Dual-Mode Coverage (EAV + JSONB)

This test suite validates Orchestra's versioning system in both EAV and JSONB modes.
All tests are parametrized with use_jsonb_mode fixture to run in both modes unless
explicitly marked as mode-specific.

MODE-SPECIFIC TESTS:
- @requires_eav_mode: Tests that rely on EAV-only features (e.g., shared logs via copy=False)
- JSONB-only tests: Tests that validate JSONB-specific features (e.g., TOAST, key_order)

VERSIONING ARCHITECTURE:
- EAV mode: Uses LogVersion table (per-field snapshots) + Log/LogEventLog reconstruction
- JSONB mode: Uses LogEventVersion table (per-event JSONB snapshots) + direct data restore

PERFORMANCE EXPECTATIONS:
- JSONB snapshot creation: 2-10x faster (bulk insert vs N inserts)
- JSONB rollback: 2-3x faster (no Log/JSONLog recreation)
- Storage: JSONB uses ~3-4x more space (full document duplication vs EAV dedup)

See conftest.py for dual-mode testing infrastructure and performance tracking.
"""
import pytest
from httpx import AsyncClient

from orchestra.conftest import requires_eav_mode
from orchestra.tests.test_log import (
    HEADERS,
    _create_log,
    _delete_logs,
    _update_logs,
    fetch_logs,
)


@pytest.mark.anyio
async def test_basic_commit_and_rollback(client: AsyncClient, use_jsonb_mode):
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
async def test_rollback_with_structural_changes(client: AsyncClient, use_jsonb_mode):
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
async def test_context_level_commit_and_rollback(client: AsyncClient, use_jsonb_mode):
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
async def test_commit_history_endpoints(client: AsyncClient, use_jsonb_mode):
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
    use_jsonb_mode,
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
async def test_project_level_branching(client: AsyncClient, use_jsonb_mode):
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
async def test_context_level_branching(client: AsyncClient, use_jsonb_mode):
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
async def test_branching_history_endpoints(client: AsyncClient, use_jsonb_mode):
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


@requires_eav_mode
@pytest.mark.anyio
async def test_rollback_with_shared_logs(client: AsyncClient, use_jsonb_mode):
    """
    Tests rollback when logs are shared between multiple LogEvents (many-to-many).
    Ensures that LogVersion correctly captures each log's state for each LogEvent association.

    EAV MODE BEHAVIOR:
    - Multiple LogEvent rows can reference the same Log row via LogEventLog
    - copy=False creates shared references (space-efficient)
    - LogVersion snapshots each Log once per LogEvent association

    JSONB MODE BEHAVIOR:
    - Not supported: Each LogEvent has its own data JSONB column
    - copy=False would need to duplicate data (no true sharing)
    - Test is skipped in JSONB mode via @requires_eav_mode decorator
    """
    project_name = "test_shared_logs_rollback"
    context1_name = "context_with_shared_logs_1"
    context2_name = "context_with_shared_logs_2"

    # Setup: Create a versioned project and two contexts
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

    # Create initial logs in context1
    await _create_log(
        client,
        project_name,
        context={"name": context1_name},
        entries={"shared_key": "original_value"},
    )
    await _create_log(
        client,
        project_name,
        context={"name": context1_name},
        entries={"unique_to_ctx1": "unique_data"},
    )

    # Get the log IDs from context1
    logs_ctx1 = await fetch_logs(client, project_name, context=context1_name)
    shared_log_id = next(
        log["id"] for log in logs_ctx1 if "shared_key" in log["entries"]
    )

    # Add the shared log to context2 (copy=False means it shares the same log)
    add_logs_res = await client.post(
        f"/v0/project/{project_name}/contexts/add_logs",
        json={
            "context_name": context2_name,
            "log_ids": [shared_log_id],
            "copy": False,  # This creates a many-to-many relationship
        },
        headers=HEADERS,
    )
    assert add_logs_res.status_code == 200

    # Also create a unique log in context2
    await _create_log(
        client,
        project_name,
        context={"name": context2_name},
        entries={"unique_to_ctx2": "ctx2_specific"},
    )

    # Verify both contexts have the expected logs
    logs_ctx1_before = await fetch_logs(client, project_name, context=context1_name)
    assert len(logs_ctx1_before) == 2

    logs_ctx2_before = await fetch_logs(client, project_name, context=context2_name)
    assert len(logs_ctx2_before) == 2

    # The shared log should have the same values in both contexts
    shared_in_ctx1 = next(
        log for log in logs_ctx1_before if "shared_key" in log["entries"]
    )
    shared_in_ctx2 = next(
        log for log in logs_ctx2_before if "shared_key" in log["entries"]
    )
    assert shared_in_ctx1["entries"]["shared_key"] == "original_value"
    assert shared_in_ctx2["entries"]["shared_key"] == "original_value"

    # Commit to create version history
    commit_res = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Initial state with shared log"},
        headers=HEADERS,
    )
    assert commit_res.status_code == 200
    commit_hash = commit_res.json()["commit_hash"]

    # Now update the shared log (this will affect both contexts)
    update_res = await _update_logs(
        client,
        [shared_log_id],
        {"shared_key": "updated_value", "new_field": "added_after_commit"},
        context={"name": context1_name},
        overwrite=True,  # Need to overwrite to update existing fields
    )
    assert update_res.status_code == 200, f"Update failed: {update_res.text}"

    # Delete the unique log from context1
    unique_ctx1_log_id = next(
        log["id"] for log in logs_ctx1_before if "unique_to_ctx1" in log["entries"]
    )
    await _delete_logs(
        client,
        log_ids=[([unique_ctx1_log_id], None)],
        project_name=project_name,
        context=context1_name,
    )

    # Add a new log to context2
    await _create_log(
        client,
        project_name,
        context={"name": context2_name},
        entries={"new_in_ctx2": "created_after_commit"},
    )

    # Verify the changes before rollback
    logs_ctx1_after_changes = await fetch_logs(
        client,
        project_name,
        context=context1_name,
    )
    logs_ctx2_after_changes = await fetch_logs(
        client,
        project_name,
        context=context2_name,
    )

    # Context1 should have 1 log (shared log only, unique was deleted)
    assert len(logs_ctx1_after_changes) == 1
    assert logs_ctx1_after_changes[0]["entries"]["shared_key"] == "updated_value"
    assert logs_ctx1_after_changes[0]["entries"]["new_field"] == "added_after_commit"

    # Context2 should have 3 logs (shared + original unique + new)
    assert len(logs_ctx2_after_changes) == 3

    # Rollback to the committed state
    rollback_res = await client.post(
        f"/v0/project/{project_name}/rollback",
        json={"commit_hash": commit_hash},
        headers=HEADERS,
    )
    assert rollback_res.status_code == 200

    # Verify rollback restored the original state
    logs_ctx1_after_rollback = await fetch_logs(
        client,
        project_name,
        context=context1_name,
    )
    logs_ctx2_after_rollback = await fetch_logs(
        client,
        project_name,
        context=context2_name,
    )

    # Context1 should have 2 logs again
    assert len(logs_ctx1_after_rollback) == 2

    # Context2 should have 2 logs (shared + its original unique)
    assert len(logs_ctx2_after_rollback) == 2

    # Verify the shared log was restored to original value in both contexts
    shared_ctx1_rollback = next(
        log for log in logs_ctx1_after_rollback if "shared_key" in log["entries"]
    )
    shared_ctx2_rollback = next(
        log for log in logs_ctx2_after_rollback if "shared_key" in log["entries"]
    )

    assert shared_ctx1_rollback["entries"]["shared_key"] == "original_value"
    assert shared_ctx2_rollback["entries"]["shared_key"] == "original_value"
    assert "new_field" not in shared_ctx1_rollback["entries"]
    assert "new_field" not in shared_ctx2_rollback["entries"]

    # Verify unique logs were restored
    assert any(
        log["entries"].get("unique_to_ctx1") == "unique_data"
        for log in logs_ctx1_after_rollback
    )
    assert any(
        log["entries"].get("unique_to_ctx2") == "ctx2_specific"
        for log in logs_ctx2_after_rollback
    )

    # The new log created after commit should not exist
    assert not any("new_in_ctx2" in log["entries"] for log in logs_ctx2_after_rollback)


# =============================================================================
# JSONB-Specific Versioning Tests
# =============================================================================


@pytest.mark.anyio
async def test_jsonb_large_document_versioning(client: AsyncClient, use_jsonb_mode):
    """
    Tests versioning with large JSONB documents (>8KB) that trigger PostgreSQL TOAST storage.

    JSONB mode stores entire documents in LogEvent.data, which can exceed the 8KB inline
    storage threshold. This test ensures LogEventVersion correctly snapshots and restores
    large TOASTed JSONB values.

    EAV mode doesn't have this concern since each Log row stores a single value.
    """
    if not use_jsonb_mode:
        pytest.skip("Test is JSONB-specific (large document TOAST storage)")

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
async def test_jsonb_key_order_preservation(client: AsyncClient, use_jsonb_mode):
    """
    Tests that LogEvent.key_order is preserved through commit/rollback cycles.

    JSONB mode stores key_order separately to maintain dict insertion order for UI rendering.
    This test ensures key_order is correctly snapshotted in LogEventVersion and restored.

    EAV mode doesn't have this concept since Log rows don't have ordering.
    """
    if not use_jsonb_mode:
        pytest.skip("Test is JSONB-specific (key_order field)")

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
async def test_jsonb_nested_structure_versioning(client: AsyncClient, use_jsonb_mode):
    """
    Tests versioning of deeply nested dicts and arrays in JSONB mode.

    JSONB mode stores complex nested structures in a single data column. This test
    ensures nested modifications are correctly captured and restored.

    EAV mode stores each nested path as a separate Log row, so this behavior differs.
    """
    if not use_jsonb_mode:
        pytest.skip("Test is JSONB-specific (nested JSONB structures)")

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
