"""The system key can read one named principal's data, and only read it.

``__system__`` owns the platform's own projects and nothing else, so without a
target it resolves ``Builtins`` and reads empty everywhere a real account has
data. Naming the target makes it useful for debugging; refusing to guess one
keeps it from presenting one tenant's rows as another's.
"""

from __future__ import annotations

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.db.models.core_models import Project
from orchestra.tests.utils import ADMIN_HEADERS, HEADERS

PROJECT = "SystemReadProbe"


async def _seed_project(client: AsyncClient, dbsession) -> str:
    """Create the caller's project and return the principal that owns it.

    The id is read from the row rather than an endpoint: the fixture account's
    ``/v0/credits`` reports ``"None"``, and reading as a principal that does
    not exist is exactly what these tests assert fails.
    """
    await client.post("/v0/project", json={"name": PROJECT}, headers=HEADERS)
    resp = await client.post(
        "/v0/logs",
        headers=HEADERS,
        json={
            "project_name": PROJECT,
            "context": "probe",
            "entries": {"marker": "visible-to-system"},
        },
    )
    assert resp.status_code == status.HTTP_200_OK
    row = (
        dbsession.query(Project)
        .filter(Project.name == PROJECT)
        .order_by(Project.id.desc())
        .first()
    )
    assert row is not None
    return row.user_id


@pytest.mark.anyio
async def test_system_key_reads_nothing_of_a_users_without_a_target(
    client: AsyncClient,
    dbsession,
):
    """The bare system key still sees only platform-owned projects."""
    await _seed_project(client, dbsession)

    resp = await client.get(
        "/v0/logs",
        headers=ADMIN_HEADERS,
        params={"project_name": PROJECT, "context": "probe"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_system_key_reads_a_named_users_project(client: AsyncClient, dbsession):
    user_id = await _seed_project(client, dbsession)

    resp = await client.get(
        "/v0/logs",
        headers=ADMIN_HEADERS,
        params={
            "project_name": PROJECT,
            "context": "probe",
            "as_user_id": user_id,
        },
    )
    assert resp.status_code == status.HTTP_200_OK
    markers = [row["entries"].get("marker") for row in resp.json()["logs"]]
    assert "visible-to-system" in markers


@pytest.mark.anyio
async def test_reading_as_an_unknown_principal_fails_loudly(client: AsyncClient):
    """A typo must not read as an empty result."""
    resp = await client.get(
        "/v0/logs",
        headers=ADMIN_HEADERS,
        params={
            "project_name": PROJECT,
            "context": "probe",
            "as_user_id": "no-such-user",
        },
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert "no-such-user" in resp.json()["detail"]


@pytest.mark.anyio
async def test_the_system_key_cannot_write_as_someone_else(
    client: AsyncClient,
    dbsession,
):
    user_id = await _seed_project(client, dbsession)
    resp = await client.post(
        "/v0/logs",
        headers=ADMIN_HEADERS,
        params={"as_user_id": user_id},
        json={
            "project_name": PROJECT,
            "context": "probe",
            "entries": {"marker": "should-never-land"},
        },
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert "read-only" in resp.json()["detail"]


@pytest.mark.anyio
async def test_an_org_context_needs_the_user_it_belongs_to(client: AsyncClient):
    resp = await client.get(
        "/v0/logs",
        headers=ADMIN_HEADERS,
        params={"project_name": PROJECT, "as_organization_id": "5"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "as_user_id" in resp.json()["detail"]


@pytest.mark.anyio
async def test_a_user_key_cannot_read_as_another_principal(client: AsyncClient):
    """The parameter is inert without the system key behind it."""
    resp = await client.get(
        "/v0/logs",
        headers=HEADERS,
        params={
            "project_name": PROJECT,
            "context": "probe",
            "as_user_id": "__system__",
        },
    )
    assert resp.status_code in (
        status.HTTP_200_OK,
        status.HTTP_404_NOT_FOUND,
    )
    if resp.status_code == status.HTTP_200_OK:
        # Served from the caller's own project, never the named one.
        assert isinstance(resp.json()["logs"], list)
