"""
Tests for the embed() function's async_embeddings parameter.

Tests verify that:
1. Default behavior (no kwarg) generates embeddings synchronously
2. async_embeddings=False generates embeddings synchronously
3. async_embeddings=True queues embeddings for background processing
4. Combined with other args (model, dimensions) works correctly
5. Invalid async_embeddings value raises an error
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from . import _create_derived_entry, _create_log, _create_project


@pytest.mark.anyio
async def test_embed_default_sync_behavior(
    client: AsyncClient,
    dbsession,
):
    """
    Test that embed() without async_embeddings kwarg generates embeddings synchronously.

    Default behavior should be sync (async_embeddings=False).
    Embeddings should be available in the Embedding table immediately.
    """
    from orchestra.db.models.orchestra_models import Embedding, EmbeddingQueue

    project_name = "test_embed_default_sync"
    await _create_project(client, project_name, user=1)

    # Create a log with text content
    response = await _create_log(
        client,
        project_name,
        entries={"text_field": "Hello world, this is a test for embeddings"},
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Create derived entry with embed() - no async_embeddings kwarg (default = sync)
    key = "text_embed"
    equation = "embed({log:text_field})"
    referenced_logs = {"log": [log_id]}
    response = await _create_derived_entry(
        client,
        project_name,
        key,
        equation,
        referenced_logs,
    )
    assert response.status_code == 200

    # Verify embedding was created synchronously (should exist in Embedding table)
    embedding_result = dbsession.execute(
        select(Embedding).where(
            Embedding.ref_id == log_id,
            Embedding.key == "text_field",
            Embedding.is_deleted == False,  # noqa: E712
        ),
    ).scalar_one_or_none()

    assert embedding_result is not None, "Embedding should exist (sync generation)"
    assert embedding_result.vector is not None, "Embedding vector should be populated"

    # Verify nothing was added to the queue
    queue_result = dbsession.execute(
        select(EmbeddingQueue).where(EmbeddingQueue.ref_id == log_id),
    ).scalar_one_or_none()

    assert queue_result is None, "No queue entry should exist for sync embedding"


@pytest.mark.anyio
async def test_embed_explicit_sync(client: AsyncClient, dbsession):
    """
    Test that embed(..., async_embeddings=False) generates embeddings synchronously.
    """
    from orchestra.db.models.orchestra_models import Embedding, EmbeddingQueue

    project_name = "test_embed_explicit_sync"
    await _create_project(client, project_name, user=1)

    # Create a log with text content
    response = await _create_log(
        client,
        project_name,
        entries={"text_field": "Explicit sync test for embeddings"},
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Create derived entry with explicit async_embeddings=False
    key = "text_embed"
    equation = "embed({log:text_field}, async_embeddings=False)"
    referenced_logs = {"log": [log_id]}
    response = await _create_derived_entry(
        client,
        project_name,
        key,
        equation,
        referenced_logs,
    )
    assert response.status_code == 200

    # Verify embedding was created synchronously
    embedding_result = dbsession.execute(
        select(Embedding).where(
            Embedding.ref_id == log_id,
            Embedding.key == "text_field",
            Embedding.is_deleted == False,  # noqa: E712
        ),
    ).scalar_one_or_none()

    assert embedding_result is not None, "Embedding should exist (explicit sync)"
    assert embedding_result.vector is not None

    # Verify nothing was added to the queue
    queue_result = dbsession.execute(
        select(EmbeddingQueue).where(EmbeddingQueue.ref_id == log_id),
    ).scalar_one_or_none()

    assert queue_result is None, "No queue entry should exist for sync embedding"


@pytest.mark.anyio
async def test_embed_async_queues_for_background(
    client: AsyncClient,
    dbsession,
):
    """
    Test that embed(..., async_embeddings=True) queues embeddings for background processing.

    Embedding should NOT be in Embedding table immediately.
    Entry should be added to EmbeddingQueue table.
    """
    from orchestra.db.models.orchestra_models import Embedding, EmbeddingQueue

    project_name = "test_embed_async"
    await _create_project(client, project_name, user=1)

    # Create a log with text content
    response = await _create_log(
        client,
        project_name,
        entries={"text_field": "Async test for embeddings"},
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Create derived entry with async_embeddings=True
    key = "text_embed"
    equation = "embed({log:text_field}, async_embeddings=True)"
    referenced_logs = {"log": [log_id]}
    response = await _create_derived_entry(
        client,
        project_name,
        key,
        equation,
        referenced_logs,
    )
    assert response.status_code == 200

    # Verify embedding was NOT created synchronously (should be queued)
    embedding_result = dbsession.execute(
        select(Embedding).where(
            Embedding.ref_id == log_id,
            Embedding.key == "text_field",
            Embedding.is_deleted == False,  # noqa: E712
        ),
    ).scalar_one_or_none()

    assert embedding_result is None, "Embedding should NOT exist yet (async mode)"

    # Verify entry was added to the queue
    queue_result = dbsession.execute(
        select(EmbeddingQueue).where(
            EmbeddingQueue.ref_id == log_id,
            EmbeddingQueue.key == "text_field",
        ),
    ).scalar_one_or_none()

    assert queue_result is not None, "Queue entry should exist for async embedding"
    assert queue_result.status == "pending", "Queue entry should be pending"
    assert queue_result.text == "Async test for embeddings"


@pytest.mark.anyio
async def test_embed_async_with_model_arg(
    client: AsyncClient,
    dbsession,
):
    """
    Test that embed() with model argument and async_embeddings=True works correctly.
    """
    from orchestra.db.models.orchestra_models import EmbeddingQueue

    project_name = "test_embed_async_model"
    await _create_project(client, project_name, user=1)

    # Create a log with text content
    response = await _create_log(
        client,
        project_name,
        entries={"text_field": "Test with model argument"},
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Create derived entry with model arg and async_embeddings=True
    key = "text_embed"
    equation = (
        'embed({log:text_field}, "text-embedding-3-small", async_embeddings=True)'
    )
    referenced_logs = {"log": [log_id]}
    response = await _create_derived_entry(
        client,
        project_name,
        key,
        equation,
        referenced_logs,
    )
    assert response.status_code == 200

    # Verify entry was added to the queue with correct model
    queue_result = dbsession.execute(
        select(EmbeddingQueue).where(
            EmbeddingQueue.ref_id == log_id,
            EmbeddingQueue.key == "text_field",
        ),
    ).scalar_one_or_none()

    assert queue_result is not None
    assert queue_result.model == "text-embedding-3-small"
    assert queue_result.status == "pending"


@pytest.mark.anyio
async def test_embed_sync_with_model_and_dimensions(
    client: AsyncClient,
    dbsession,
):
    """
    Test that embed() with model, dimensions, and async_embeddings=False works correctly.
    """
    from orchestra.db.models.orchestra_models import Embedding

    project_name = "test_embed_sync_full"
    await _create_project(client, project_name, user=1)

    # Create a log with text content
    response = await _create_log(
        client,
        project_name,
        entries={"text_field": "Full args test for embeddings"},
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Create derived entry with all args
    # Note: text-embedding-3-small requires 1536 dimensions per database constraint
    key = "text_embed"
    equation = 'embed({log:text_field}, "text-embedding-3-small", 1536, async_embeddings=False)'
    referenced_logs = {"log": [log_id]}
    response = await _create_derived_entry(
        client,
        project_name,
        key,
        equation,
        referenced_logs,
    )
    assert response.status_code == 200

    # Verify embedding was created synchronously
    embedding_result = dbsession.execute(
        select(Embedding).where(
            Embedding.ref_id == log_id,
            Embedding.key == "text_field",
            Embedding.is_deleted == False,  # noqa: E712
        ),
    ).scalar_one_or_none()

    assert embedding_result is not None
    assert embedding_result.model == "text-embedding-3-small"


@pytest.mark.anyio
async def test_embed_invalid_async_embeddings_value(
    client: AsyncClient,
):
    """
    Test that embed(..., async_embeddings="invalid") raises an error.

    The async_embeddings parameter must be a boolean literal.
    """
    project_name = "test_embed_invalid_async"
    await _create_project(client, project_name, user=1)

    # Create a log with text content
    response = await _create_log(
        client,
        project_name,
        entries={"text_field": "Invalid async_embeddings test"},
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Create derived entry with invalid async_embeddings value
    key = "text_embed"
    equation = 'embed({log:text_field}, async_embeddings="not_a_bool")'
    referenced_logs = {"log": [log_id]}
    response = await _create_derived_entry(
        client,
        project_name,
        key,
        equation,
        referenced_logs,
    )

    # Should fail with an error (invalid async_embeddings value)
    # The error gets wrapped by the derived log creation handler
    assert response.status_code in (400, 500), response.text
