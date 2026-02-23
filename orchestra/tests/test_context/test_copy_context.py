"""
Tests for the admin copy_context endpoint.

Tests cover:
- Basic same-project copy
- Cross-project copy
- Validation errors (missing source, existing target, same context)
- Field types and derived templates are copied
- Log event data integrity (deep copy — mutations don't propagate)
- Unique constraints are copied
- Batch processing (multiple batches)
- Derived log templates are preserved in the copy
- Embeddings are queued for the copy
- Mutating source after copy does NOT affect the target
"""

import json
import os

import pytest
from httpx import AsyncClient

from orchestra.tests.test_log import (
    HEADERS,
    _create_derived_entry,
    _create_log,
    _create_project,
    _update_logs,
)

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
admin_api_key = str(os.getenv("ORCHESTRA_ADMIN_KEY"))

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}

ADMIN_HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {admin_api_key}",
    "Content-Type": "application/json",
}


async def _get_user_id(client: AsyncClient) -> str:
    """Get the test user's ID."""
    resp = await client.get("/v0/credits", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _setup_source_project(
    client: AsyncClient,
    project_name: str,
    context_name: str,
    num_logs: int = 5,
):
    """Create a project with a context containing log entries."""
    resp = await _create_project(client, project_name)
    assert resp.status_code == 200, resp.json()

    entries = [
        {"score": i * 10, "label": f"item_{i}", "value": i * 1.5}
        for i in range(num_logs)
    ]

    resp = await _create_log(
        client,
        project_name,
        entries=entries,
        context={"name": context_name, "description": "Source context for copy test"},
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


# =============================================================================
# Basic Copy Tests
# =============================================================================


@pytest.mark.anyio
async def test_copy_context_same_project(client: AsyncClient):
    """Test copying a context within the same project."""
    user_id = await _get_user_id(client)
    project_name = "copy-test-same-proj"
    source_ctx = "source_ctx"
    target_ctx = "target_ctx"

    await _setup_source_project(client, project_name, source_ctx)

    resp = await client.post(
        "/v0/admin/copy_context",
        json={
            "source_user_id": user_id,
            "source_project_name": project_name,
            "source_context_name": source_ctx,
            "target_user_id": user_id,
            "target_project_name": project_name,
            "target_context_name": target_ctx,
            "copy_embeddings": False,
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.json()

    data = resp.json()
    assert "copied" in data["info"].lower() or "successfully" in data["info"].lower()

    details = data["details"]
    assert details["log_events_copied"] == 5
    assert details["field_types_copied"] > 0
    assert details["batches_processed"] >= 1


@pytest.mark.anyio
async def test_copy_context_cross_project(client: AsyncClient):
    """Test copying a context to a different project."""
    user_id = await _get_user_id(client)
    source_project = "copy-test-source-proj"
    target_project = "copy-test-target-proj"
    ctx_name = "my_context"

    await _setup_source_project(client, source_project, ctx_name)

    # Create target project
    resp = await _create_project(client, target_project)
    assert resp.status_code == 200, resp.json()

    resp = await client.post(
        "/v0/admin/copy_context",
        json={
            "source_user_id": user_id,
            "source_project_name": source_project,
            "source_context_name": ctx_name,
            "target_user_id": user_id,
            "target_project_name": target_project,
            "target_context_name": ctx_name,
            "copy_embeddings": False,
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["details"]["log_events_copied"] == 5


@pytest.mark.anyio
async def test_copy_context_deep_copy_independence(client: AsyncClient):
    """Verify deep copy: IDs differ, values match, and mutating source leaves target untouched."""
    user_id = await _get_user_id(client)
    project_name = "copy-test-independence"
    source_ctx = "original"
    target_ctx = "cloned"

    await _setup_source_project(client, project_name, source_ctx, num_logs=3)

    # Copy the context
    resp = await client.post(
        "/v0/admin/copy_context",
        json={
            "source_user_id": user_id,
            "source_project_name": project_name,
            "source_context_name": source_ctx,
            "target_user_id": user_id,
            "target_project_name": project_name,
            "target_context_name": target_ctx,
            "copy_embeddings": False,
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.json()

    # --- Fetch logs from both contexts ---
    source_logs_resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "context": source_ctx,
            "sorting": json.dumps({"id": "ascending"}),
        },
        headers=HEADERS,
    )
    assert source_logs_resp.status_code == 200
    source_logs = source_logs_resp.json()["logs"]

    target_logs_resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "context": target_ctx,
            "sorting": json.dumps({"id": "ascending"}),
        },
        headers=HEADERS,
    )
    assert target_logs_resp.status_code == 200
    target_logs = target_logs_resp.json()["logs"]

    assert len(source_logs) == len(target_logs) == 3

    # IDs must be different (deep copy, not reference)
    source_ids = [log["id"] for log in source_logs]
    target_ids = [log["id"] for log in target_logs]
    assert set(source_ids).isdisjoint(set(target_ids)), "Copied logs must have new IDs"

    # Values must match right after copy
    for s_log, t_log in zip(source_logs, target_logs):
        assert s_log["entries"]["score"] == t_log["entries"]["score"]
        assert s_log["entries"]["label"] == t_log["entries"]["label"]

    # Record target values BEFORE we mutate the source
    target_scores_before = [log["entries"]["score"] for log in target_logs]
    target_labels_before = [log["entries"]["label"] for log in target_logs]

    # --- Mutate the source: update the first log ---
    resp = await _update_logs(
        client,
        log_ids=[source_ids[0]],
        entries={"score": 9999, "label": "MUTATED"},
        overwrite=True,
    )
    assert resp.status_code == 200, resp.json()

    # --- Re-fetch target logs — they must be unchanged ---
    target_after_resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "context": target_ctx,
            "sorting": json.dumps({"id": "ascending"}),
        },
        headers=HEADERS,
    )
    assert target_after_resp.status_code == 200
    target_after = target_after_resp.json()["logs"]

    assert len(target_after) == 3
    target_scores_after = [log["entries"]["score"] for log in target_after]
    target_labels_after = [log["entries"]["label"] for log in target_after]

    assert (
        target_scores_after == target_scores_before
    ), "Target scores must not change when source is mutated"
    assert (
        target_labels_after == target_labels_before
    ), "Target labels must not change when source is mutated"


# =============================================================================
# Validation Error Tests
# =============================================================================


@pytest.mark.anyio
async def test_copy_context_source_not_found(client: AsyncClient):
    """Test 404 when source context doesn't exist."""
    user_id = await _get_user_id(client)
    project_name = "copy-test-404-source"

    resp = await _create_project(client, project_name)
    assert resp.status_code == 200

    resp = await client.post(
        "/v0/admin/copy_context",
        json={
            "source_user_id": user_id,
            "source_project_name": project_name,
            "source_context_name": "nonexistent",
            "target_user_id": user_id,
            "target_project_name": project_name,
            "target_context_name": "target",
            "copy_embeddings": False,
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 404
    assert "nonexistent" in resp.json()["detail"]


@pytest.mark.anyio
async def test_copy_context_target_already_exists(client: AsyncClient):
    """Test 409 when target context already exists."""
    user_id = await _get_user_id(client)
    project_name = "copy-test-409"
    ctx_a = "ctx_a"
    ctx_b = "ctx_b"

    await _setup_source_project(client, project_name, ctx_a)

    # Create the target context too
    resp = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": ctx_b},
        headers=HEADERS,
    )
    assert resp.status_code == 200

    resp = await client.post(
        "/v0/admin/copy_context",
        json={
            "source_user_id": user_id,
            "source_project_name": project_name,
            "source_context_name": ctx_a,
            "target_user_id": user_id,
            "target_project_name": project_name,
            "target_context_name": ctx_b,
            "copy_embeddings": False,
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


@pytest.mark.anyio
async def test_copy_context_same_source_and_target(client: AsyncClient):
    """Test 400 when source and target are identical."""
    user_id = await _get_user_id(client)
    project_name = "copy-test-same"
    ctx_name = "same_ctx"

    await _setup_source_project(client, project_name, ctx_name)

    resp = await client.post(
        "/v0/admin/copy_context",
        json={
            "source_user_id": user_id,
            "source_project_name": project_name,
            "source_context_name": ctx_name,
            "target_user_id": user_id,
            "target_project_name": project_name,
            "target_context_name": ctx_name,
            "copy_embeddings": False,
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400
    assert "identical" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_copy_context_source_project_not_found(client: AsyncClient):
    """Test 404 when source project doesn't exist."""
    user_id = await _get_user_id(client)

    resp = await client.post(
        "/v0/admin/copy_context",
        json={
            "source_user_id": user_id,
            "source_project_name": "nonexistent_project",
            "source_context_name": "any_ctx",
            "target_user_id": user_id,
            "target_project_name": "nonexistent_project",
            "target_context_name": "target",
            "copy_embeddings": False,
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_copy_context_requires_admin_auth(client: AsyncClient):
    """Test that the endpoint requires admin authentication."""
    resp = await client.post(
        "/v0/admin/copy_context",
        json={
            "source_user_id": "any",
            "source_project_name": "any",
            "source_context_name": "any",
            "target_user_id": "any",
            "target_project_name": "any",
            "target_context_name": "any",
        },
        headers=HEADERS,
    )
    # Should fail auth (either 401 or 403)
    assert resp.status_code in (401, 403)


# =============================================================================
# Batch Processing Tests
# =============================================================================


@pytest.mark.anyio
async def test_copy_context_small_batch_size(client: AsyncClient):
    """Test that batch_size parameter works (forces multiple batches)."""
    user_id = await _get_user_id(client)
    project_name = "copy-test-batched"
    source_ctx = "source"
    target_ctx = "target"

    # Create 10 logs
    await _setup_source_project(client, project_name, source_ctx, num_logs=10)

    # Copy with batch_size=3 to force multiple batches
    resp = await client.post(
        "/v0/admin/copy_context",
        json={
            "source_user_id": user_id,
            "source_project_name": project_name,
            "source_context_name": source_ctx,
            "target_user_id": user_id,
            "target_project_name": project_name,
            "target_context_name": target_ctx,
            "copy_embeddings": False,
            "batch_size": 3,
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.json()

    details = resp.json()["details"]
    assert details["log_events_copied"] == 10
    # With batch_size=3 and 10 logs: ceil(10/3) = 4 batches
    assert details["batches_processed"] == 4


# =============================================================================
# Empty Context Test
# =============================================================================


@pytest.mark.anyio
async def test_copy_empty_context(client: AsyncClient):
    """Test copying an empty context (no log events)."""
    user_id = await _get_user_id(client)
    project_name = "copy-test-empty"
    source_ctx = "empty_source"
    target_ctx = "empty_target"

    # Create project and empty context
    resp = await _create_project(client, project_name)
    assert resp.status_code == 200

    resp = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": source_ctx, "description": "Empty context"},
        headers=HEADERS,
    )
    assert resp.status_code == 200

    resp = await client.post(
        "/v0/admin/copy_context",
        json={
            "source_user_id": user_id,
            "source_project_name": project_name,
            "source_context_name": source_ctx,
            "target_user_id": user_id,
            "target_project_name": project_name,
            "target_context_name": target_ctx,
            "copy_embeddings": False,
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.json()

    details = resp.json()["details"]
    assert details["log_events_copied"] == 0
    assert details["embeddings_queued"] == 0


# =============================================================================
# Derived Log Template Tests
# =============================================================================


@pytest.mark.anyio
async def test_copy_context_with_derived_templates(client: AsyncClient, dbsession):
    """Verify that ActiveDerivedLog template rows are copied to the target context."""
    from sqlalchemy import select

    from orchestra.db.models.orchestra_models import ActiveDerivedLog, Context

    user_id = await _get_user_id(client)
    project_name = "copy-test-derived"
    source_ctx = "with_derived"
    target_ctx = "cloned_derived"

    create_resp = await _setup_source_project(
        client,
        project_name,
        source_ctx,
        num_logs=3,
    )
    log_ids = create_resp["log_event_ids"]

    # Create a derived entry — this inserts an ActiveDerivedLog template row
    resp = await _create_derived_entry(
        client,
        project_name,
        key="doubled_score",
        equation="{log:score} * 2",
        referenced_logs={"log": log_ids},
        context=source_ctx,
    )
    assert resp.status_code == 200, resp.json()

    # Look up the source context ID so we can query the template table
    source_ctx_row = dbsession.execute(
        select(Context).where(Context.name == source_ctx),
    ).scalar_one()

    # Verify the template exists on the source context
    source_template = dbsession.execute(
        select(ActiveDerivedLog).where(
            ActiveDerivedLog.context_id == source_ctx_row.id,
            ActiveDerivedLog.key == "doubled_score",
        ),
    ).scalar_one_or_none()
    assert (
        source_template is not None
    ), "ActiveDerivedLog template must exist on source context after derived entry creation"

    # Copy context
    resp = await client.post(
        "/v0/admin/copy_context",
        json={
            "source_user_id": user_id,
            "source_project_name": project_name,
            "source_context_name": source_ctx,
            "target_user_id": user_id,
            "target_project_name": project_name,
            "target_context_name": target_ctx,
            "copy_embeddings": False,
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.json()

    details = resp.json()["details"]
    assert (
        details["derived_templates_copied"] >= 1
    ), "At least one derived template should have been copied"
    assert details["log_events_copied"] == 3

    # Look up the target context and verify the template was copied
    target_ctx_row = dbsession.execute(
        select(Context).where(Context.name == target_ctx),
    ).scalar_one()

    target_template = dbsession.execute(
        select(ActiveDerivedLog).where(
            ActiveDerivedLog.context_id == target_ctx_row.id,
            ActiveDerivedLog.key == "doubled_score",
        ),
    ).scalar_one_or_none()
    assert (
        target_template is not None
    ), "ActiveDerivedLog template must exist on target context after copy"

    # Verify template fields match between source and target
    assert target_template.equation == source_template.equation
    assert target_template.inferred_type == source_template.inferred_type
    assert target_template.is_active == source_template.is_active
    assert target_template.referenced_keys == source_template.referenced_keys

    # Templates must be independent rows (different IDs and context IDs)
    assert target_template.id != source_template.id
    assert target_template.context_id != source_template.context_id


# =============================================================================
# Embedding Copy Tests
# =============================================================================


@pytest.mark.anyio
async def test_copy_context_with_embeddings(client: AsyncClient, dbsession):
    """Verify that embeddings are queued as vector_ready when copy_embeddings=True."""
    from sqlalchemy import select

    from orchestra.db.models.orchestra_models import Embedding, EmbeddingQueue

    user_id = await _get_user_id(client)
    project_name = "copy-test-embeddings"
    source_ctx = "emb_source"
    target_ctx = "emb_target"

    # Create project + log with text for embedding
    resp = await _create_project(client, project_name)
    assert resp.status_code == 200

    resp = await _create_log(
        client,
        project_name,
        entries={"description": "A test sentence for embedding generation"},
        context={"name": source_ctx, "description": "Source with embeddings"},
    )
    assert resp.status_code == 200, resp.json()
    log_id = resp.json()["log_event_ids"][0]

    # Create an embedding via the derived entry embed() path (sync)
    resp = await _create_derived_entry(
        client,
        project_name,
        key="desc_emb",
        equation="embed({log:description})",
        referenced_logs={"log": [log_id]},
        context=source_ctx,
    )
    assert resp.status_code == 200, resp.json()

    # Verify the source embedding actually exists
    source_emb = dbsession.execute(
        select(Embedding).where(
            Embedding.ref_id == log_id,
            Embedding.key == "desc_emb",
            Embedding.is_deleted.is_(False),
        ),
    ).scalar_one_or_none()
    assert source_emb is not None, "Source embedding must exist before copy"

    # Copy context WITH embeddings
    resp = await client.post(
        "/v0/admin/copy_context",
        json={
            "source_user_id": user_id,
            "source_project_name": project_name,
            "source_context_name": source_ctx,
            "target_user_id": user_id,
            "target_project_name": project_name,
            "target_context_name": target_ctx,
            "copy_embeddings": True,
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.json()

    details = resp.json()["details"]
    assert details["log_events_copied"] == 1
    assert (
        details["embeddings_queued"] >= 1
    ), "At least one embedding should have been queued"

    # Fetch target logs to get the new log ID
    target_logs_resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "context": target_ctx,
            "sorting": json.dumps({"id": "ascending"}),
        },
        headers=HEADERS,
    )
    assert target_logs_resp.status_code == 200
    target_logs = target_logs_resp.json()["logs"]
    assert len(target_logs) == 1

    new_log_id = target_logs[0]["id"]
    assert new_log_id != log_id, "Copied log must have a different ID"

    # Verify an EmbeddingQueue entry was created with vector_ready status
    queue_entry = dbsession.execute(
        select(EmbeddingQueue).where(
            EmbeddingQueue.ref_id == new_log_id,
            EmbeddingQueue.key == "desc_emb",
            EmbeddingQueue.status == "vector_ready",
        ),
    ).scalar_one_or_none()
    assert (
        queue_entry is not None
    ), "EmbeddingQueue entry should exist for copied log with status=vector_ready"
    assert (
        queue_entry.generated_vector is not None
    ), "Queued embedding should carry the pre-generated vector"
    assert (
        queue_entry.model == source_emb.model
    ), "Queued embedding model should match source"
