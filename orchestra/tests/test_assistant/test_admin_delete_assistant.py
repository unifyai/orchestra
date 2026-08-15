"""Tests for the admin mount of the assistant delete route.

The admin route exists so operational cleanup and abuse response do not have
to borrow an owner's key. These tests pin the two properties that make that
safe: it reaches assistants the caller does not own, and it still refuses the
structural deletions the owner route refuses.
"""

from __future__ import annotations

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Assistant, User
from orchestra.tests.utils import ADMIN_HEADERS, create_test_user


async def _create_owned_assistant(client: AsyncClient, email: str) -> tuple[dict, int]:
    """Create a user with one assistant; return the user and the assistant id."""
    owner = await create_test_user(client, email)
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Admin",
            "surname": "Delete",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200, create_resp.json()
    return owner, int(create_resp.json()["info"]["agent_id"])


@pytest.mark.anyio
async def test_admin_delete_removes_another_users_assistant(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Admin deletes an assistant it does not own; the owner route cannot."""
    await _create_owned_assistant(client, "admin_delete_owner@test.com")
    owner, agent_id = await _create_owned_assistant(
        client,
        "admin_delete_target@test.com",
    )
    stranger = await create_test_user(client, "admin_delete_stranger@test.com")

    # The owner-scoped route is not a cross-owner path, so the admin mount is
    # the only way to reach someone else's assistant.
    stranger_resp = await client.delete(
        f"/v0/assistant/{agent_id}",
        headers=stranger["headers"],
    )
    assert stranger_resp.status_code == status.HTTP_404_NOT_FOUND
    dbsession.expire_all()
    assert dbsession.get(Assistant, agent_id) is not None

    admin_resp = await client.delete(
        f"/v0/admin/assistant/{agent_id}?reason=operational+cleanup",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == status.HTTP_200_OK, admin_resp.json()

    dbsession.expire_all()
    assert dbsession.get(Assistant, agent_id) is None


@pytest.mark.anyio
async def test_admin_delete_requires_reason(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """A blank or absent reason is refused and the assistant survives."""
    _, agent_id = await _create_owned_assistant(
        client,
        "admin_delete_no_reason@test.com",
    )

    for query in ("", "?reason=", "?reason=%20%20"):
        resp = await client.delete(
            f"/v0/admin/assistant/{agent_id}{query}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.json()

    dbsession.expire_all()
    assert dbsession.get(Assistant, agent_id) is not None


@pytest.mark.anyio
async def test_admin_delete_refuses_coordinator(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Coordinators are structural; admin gets no override for them."""
    # Built directly rather than through signup: ``is_coordinator`` is
    # insert-only, and a signed-up user already holds the one Coordinator its
    # workspace uniqueness constraint allows.
    owner = User(
        id="admin-delete-coordinator-user",
        email="admin_delete_coordinator@test.com",
    )
    dbsession.add(owner)
    dbsession.flush()
    coordinator = Assistant(
        user_id=owner.id,
        first_name="Coordinator",
        surname="Structural",
        is_coordinator=True,
    )
    dbsession.add(coordinator)
    dbsession.commit()
    agent_id = coordinator.agent_id

    resp = await client.delete(
        f"/v0/admin/assistant/{agent_id}?reason=offboarding",
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_409_CONFLICT
    assert resp.json()["detail"] == "cannot_delete_coordinator"

    dbsession.expire_all()
    assert dbsession.get(Assistant, agent_id) is not None


@pytest.mark.anyio
async def test_admin_delete_unknown_assistant_is_404(client: AsyncClient) -> None:
    """An unknown id is a 404. The admin router sets no principal on
    ``request.state``, so the not-found path must not read one back off it."""
    resp = await client.delete(
        "/v0/admin/assistant/99999999?reason=cleanup",
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
