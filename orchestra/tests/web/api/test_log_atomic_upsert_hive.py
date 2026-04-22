"""Hive-awareness tests for the ``POST /v0/logs/atomic`` mirror bypass.

Per-body atomic upserts continue to mirror into the ``All/*`` archive context
when ``add_to_all_context=True``. Hive-scoped upserts (``Hives/{hive_id}/...``)
must skip that mirror entirely: Hive rows live in a single-tier tree with no
per-body aggregate counterpart, and materializing ``Hives/{hive_id}/All/...``
shells would only create garbage the Hive cascade has to clean up.

These tests exercise the real endpoint through the ``client`` fixture and
verify both the response payload and the underlying ``Context`` table state.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Context
from orchestra.tests.test_log import HEADERS, _create_project


@pytest.mark.anyio
async def test_hive_atomic_upsert_does_not_mirror_to_all_context(
    client: AsyncClient,
    dbsession: Session,
):
    """Hive writes skip the ``All/*`` mirror even when ``add_to_all_context=True``."""

    project_name = "hive-atomic-upsert-mirror-bypass"
    await _create_project(client, project_name)

    response = await client.post(
        "/v0/logs/atomic",
        json={
            "project": project_name,
            "context": "Hives/42/Knowledge",
            "unique_keys": {"_assistant_id": "str", "topic": "str"},
            "operation": "+1",
            "initial_data": {
                "_assistant_id": "789",
                "_user": "user1",
                "_assistant": "assistant1",
                "topic": "onboarding",
                "mentions": 0,
            },
            "add_to_all_context": True,
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["created"] is True
    assert data["new_value"] == 1.0
    assert data["mirrored_contexts"] is None

    mirror_names = [
        "Hives/42/All/Knowledge",
        "All/Knowledge",
        "user1/All/Knowledge",
        "user1/assistant1/All/Knowledge",
    ]
    offending = (
        dbsession.query(Context.name).filter(Context.name.in_(mirror_names)).all()
    )
    assert (
        offending == []
    ), f"Unexpected mirror contexts materialized for a Hive write: {offending}"


@pytest.mark.anyio
async def test_solo_atomic_upsert_still_mirrors_to_all_context(
    client: AsyncClient,
):
    """Per-body writes keep mirroring to ``{prefix}/All/{sub}`` exactly as before."""

    project_name = "solo-atomic-upsert-mirror-regression"
    await _create_project(client, project_name)

    response = await client.post(
        "/v0/logs/atomic",
        json={
            "project": project_name,
            "context": "user1/assistant1/Knowledge",
            "unique_keys": {"_assistant_id": "str", "topic": "str"},
            "operation": "+1",
            "initial_data": {
                "_assistant_id": "789",
                "_user": "user1",
                "_assistant": "assistant1",
                "topic": "onboarding",
                "mentions": 0,
            },
            "add_to_all_context": True,
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["created"] is True
    assert data["new_value"] == 1.0
    assert data["mirrored_contexts"] == ["All/Knowledge"]


@pytest.mark.anyio
async def test_hive_atomic_upsert_without_all_context_flag_writes_primary_only(
    client: AsyncClient,
    dbsession: Session,
):
    """Hive writes with ``add_to_all_context=False`` land in the primary context and nowhere else."""

    project_name = "hive-atomic-upsert-no-mirror-flag"
    await _create_project(client, project_name)

    response = await client.post(
        "/v0/logs/atomic",
        json={
            "project": project_name,
            "context": "Hives/99/Knowledge",
            "unique_keys": {"_assistant_id": "str", "topic": "str"},
            "operation": "+5",
            "initial_data": {
                "_assistant_id": "321",
                "topic": "followups",
                "mentions": 0,
            },
            "add_to_all_context": False,
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["created"] is True
    assert data["new_value"] == 5.0
    assert data["mirrored_contexts"] is None

    primary = (
        dbsession.query(Context).filter(Context.name == "Hives/99/Knowledge").first()
    )
    assert primary is not None, "Primary Hive context was not created"

    offending = (
        dbsession.query(Context.name)
        .filter(Context.name.in_(["Hives/99/All/Knowledge", "All/Knowledge"]))
        .all()
    )
    assert offending == []
