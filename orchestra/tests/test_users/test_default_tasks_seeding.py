"""
Tests for DefaultTasksSeeder integration with user creation flows.

Verifies that the session.flush() change in DefaultTasksSeeder.seed() works
correctly in both:
1. create_user (OAuth sign-up path via POST /v0/admin/user)
2. create_user_after_verification (email sign-up path via POST /v0/admin/auth/create-user)

The seeder should create:
- A "Unity" project
- A "Unity" interface
- A "Tasks" tab
- A "Tasks" table tile
"""

import os
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.dao.auth_dao import AuthDAO, hash_code
from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.interface_dao import InterfaceDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.tab_dao import TabDAO
from orchestra.db.dao.tile_dao import TileDAO

ADMIN_HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}",
    "Content-Type": "application/json",
}

_EMAIL_PATCH_TARGET = "orchestra.web.api.utils.email.send_email_async"
_TURNSTILE_PATCH_TARGET = "orchestra.web.api.auth.views.verify_turnstile_token"


def _assert_default_tasks_seeded(session: Session, user_id: str):
    """
    Assert that DefaultTasksSeeder created all expected entities for the user.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

    # 1. Unity project exists
    project = project_dao.get_by_user_and_name(user_id=user_id, name="Unity")
    assert project is not None, "Unity project should have been created by seeder"

    # 2. Unity interface exists
    interface = interface_dao.get_by_project_and_name(
        project_id=project.id,
        name="Unity",
    )
    assert interface is not None, "Unity interface should have been created by seeder"

    # 3. Tasks tab exists
    tab = tab_dao.get_by_interface_and_name(
        interface_id=interface.id,
        name="Tasks",
    )
    assert tab is not None, "Tasks tab should have been created by seeder"

    # 4. Tasks table tile exists
    tile = tile_dao.get_by_tab_and_name(tab_id=tab.id, name="Tasks")
    assert tile is not None, "Tasks table tile should have been created by seeder"
    assert tile.type == "Table", "Tile type should be 'Table'"


# =============================================================================
# Test 1: create_user (OAuth / adapter sign-up path)
# =============================================================================


@pytest.mark.anyio
async def test_create_user_seeds_default_tasks(
    client: AsyncClient,
    dbsession: Session,
):
    """
    POST /v0/admin/user should seed default tasks (Unity project, interface,
    tab, tile) using session.flush() instead of session.commit().

    This is the path used by the NextAuth adapter for OAuth sign-ups.
    """
    email = "seed_oauth_user@example.com"

    response = await client.post(
        "/v0/admin/user",
        json={"email": email, "name": "Seed Test"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.json()

    user_id = response.json()["id"]

    # Verify all default task entities were created
    _assert_default_tasks_seeded(dbsession, user_id)


# =============================================================================
# Test 2: create_user_after_verification (email sign-up path)
# =============================================================================


@pytest.mark.anyio
async def test_create_user_after_verification_seeds_default_tasks(
    client: AsyncClient,
    dbsession: Session,
):
    """
    The email sign-up path (register → verify-code → create-user) should seed
    default tasks using session.flush() inside a savepoint, without conflicting
    with the outer session.commit().
    """
    email = "seed_email_user@example.com"
    password = "secureP@ss1"

    # Step 1: Register
    with (
        patch(_EMAIL_PATCH_TARGET, new_callable=AsyncMock) as mock_send,
        patch(_TURNSTILE_PATCH_TARGET, new_callable=AsyncMock) as mock_captcha,
    ):
        mock_send.return_value = True
        mock_captcha.return_value = True
        resp = await client.post(
            "/v0/admin/auth/register",
            json={
                "email": email,
                "password": password,
                "name": "Seed",
                "last_name": "EmailUser",
            },
            headers=ADMIN_HEADERS,
        )
    assert resp.status_code == 200, resp.json()

    # Step 2: Get the verification code from the DB and set a known code
    dao = AuthDAO(dbsession)
    entry = dao.get_pending_verification(email, "signup")
    assert entry is not None, f"No pending signup verification for {email}"
    code = "123456"
    entry.code_hash = hash_code(code)
    dbsession.flush()

    # Step 3: Verify code
    verify_resp = await client.post(
        "/v0/admin/auth/verify-code",
        json={"email": email, "code": code, "purpose": "signup"},
        headers=ADMIN_HEADERS,
    )
    assert verify_resp.status_code == 200, verify_resp.json()
    token = verify_resp.json()["token"]

    # Step 4: Create user (this calls create_user_after_verification internally)
    create_resp = await client.post(
        "/v0/admin/auth/create-user",
        json={"token": token},
        headers=ADMIN_HEADERS,
    )
    assert create_resp.status_code == 200, create_resp.json()

    user_id = create_resp.json()["id"]

    # Verify all default task entities were created
    _assert_default_tasks_seeded(dbsession, user_id)


# =============================================================================
# Test 3: Idempotency — seeder doesn't duplicate if called twice
# =============================================================================


@pytest.mark.anyio
async def test_default_tasks_seeder_is_idempotent(
    client: AsyncClient,
    dbsession: Session,
):
    """
    If seeder runs twice for the same user (e.g. retry logic), it should not
    create duplicate entities.
    """
    from orchestra.db.seeding.default_tasks_seeder import DefaultTasksSeeder

    email = "seed_idempotent@example.com"

    response = await client.post(
        "/v0/admin/user",
        json={"email": email, "name": "Idempotent Test"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200
    user_id = response.json()["id"]

    # Run seeder a second time — should not raise or create duplicates
    result = DefaultTasksSeeder.seed(dbsession, user_id=user_id)
    assert result is not None
    assert "project_id" in result

    # Verify there's still only one Unity project
    organization_member_dao = OrganizationMemberDAO(dbsession)
    context_dao = ContextDAO(dbsession)
    project_dao = ProjectDAO(dbsession, organization_member_dao, context_dao)
    projects = project_dao.filter(user_id=user_id, name="Unity")
    assert len(projects) == 1, "Seeder should not create duplicate Unity projects"
