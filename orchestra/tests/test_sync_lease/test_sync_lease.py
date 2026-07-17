"""Tests for exclusive sync lease acquire/release."""

import asyncio

import pytest
from httpx import AsyncClient

from orchestra.tests.test_log import HEADERS, _create_project


@pytest.mark.anyio
async def test_sync_lease_acquire_and_release(client: AsyncClient):
    project_name = "sync-lease-basic"
    await _create_project(client, project_name)

    acquire = await client.post(
        "/v0/sync_lease/acquire",
        json={
            "project": project_name,
            "lease_key": "Teams/11/Functions/Compositional:custom_sync",
            "holder": "writer-a",
            "ttl_seconds": 60,
        },
        headers=HEADERS,
    )
    assert acquire.status_code == 200, acquire.text
    body = acquire.json()
    assert body["acquired"] is True
    assert body["holder"] == "writer-a"
    assert body["expires_at"]

    conflict = await client.post(
        "/v0/sync_lease/acquire",
        json={
            "project": project_name,
            "lease_key": "Teams/11/Functions/Compositional:custom_sync",
            "holder": "writer-b",
            "ttl_seconds": 60,
        },
        headers=HEADERS,
    )
    assert conflict.status_code == 409, conflict.text

    renew = await client.post(
        "/v0/sync_lease/acquire",
        json={
            "project": project_name,
            "lease_key": "Teams/11/Functions/Compositional:custom_sync",
            "holder": "writer-a",
            "ttl_seconds": 60,
        },
        headers=HEADERS,
    )
    assert renew.status_code == 200, renew.text

    release = await client.post(
        "/v0/sync_lease/release",
        json={
            "project": project_name,
            "lease_key": "Teams/11/Functions/Compositional:custom_sync",
            "holder": "writer-a",
        },
        headers=HEADERS,
    )
    assert release.status_code == 200, release.text
    assert release.json()["released"] is True

    other = await client.post(
        "/v0/sync_lease/acquire",
        json={
            "project": project_name,
            "lease_key": "Teams/11/Functions/Compositional:custom_sync",
            "holder": "writer-b",
            "ttl_seconds": 60,
        },
        headers=HEADERS,
    )
    assert other.status_code == 200, other.text
    assert other.json()["holder"] == "writer-b"


@pytest.mark.anyio
async def test_sync_lease_serializes_concurrent_first_acquire(
    client_concurrent: AsyncClient,
):
    """Exactly one concurrent first acquire should win."""
    project_name = "sync-lease-race"
    await _create_project(client_concurrent, project_name)

    async def _acquire(holder: str):
        return await client_concurrent.post(
            "/v0/sync_lease/acquire",
            json={
                "project": project_name,
                "lease_key": "race-key",
                "holder": holder,
                "ttl_seconds": 120,
            },
            headers=HEADERS,
        )

    results = await asyncio.gather(
        _acquire("h1"),
        _acquire("h2"),
        _acquire("h3"),
        return_exceptions=True,
    )
    statuses = []
    for result in results:
        if isinstance(result, Exception):
            raise result
        statuses.append(result.status_code)

    assert statuses.count(200) == 1, statuses
    assert statuses.count(409) == 2, statuses
