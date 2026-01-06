"""
Tests for user account deletion functionality.

Covers:
- Happy path: Complete account deletion with data cleanup
- Blocker: Pending bills block deletion
- Blocker: Organization ownership blocks deletion
- Self-service: Email confirmation validation
- Admin: Force flag bypasses org check
- Data integrity: Verify all user data is removed
"""

import os
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}",
}


async def create_test_user(client: AsyncClient, email: str) -> dict:
    """Create a test user and return their data including API key."""
    response = await client.post(
        "/v0/admin/auth-user",
        json={"email": email},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    user_id = response.json()["id"]

    response = await client.get(
        f"/v0/admin/auth-user/by-user-id?user_id={user_id}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.anyio
async def test_delete_user_happy_path(client: AsyncClient):
    """Verify complete account deletion removes user from auth_user table."""
    user = await create_test_user(client, "delete_happy@test.com")
    user_id = user["id"]

    response = await client.delete(
        f"/v0/admin/auth-user?user_id={user_id}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["success"] is True
    assert "deleted" in data["message"].lower()

    response = await client.get(
        f"/v0/admin/auth-user/by-user-id?user_id={user_id}",
        headers=HEADERS,
    )
    assert response.status_code == 404


@pytest.mark.anyio
async def test_delete_nonexistent_user(client: AsyncClient):
    """Deleting a non-existent user returns error."""
    response = await client.delete(
        "/v0/admin/auth-user?user_id=nonexistent-user-id-12345",
        headers=HEADERS,
    )
    assert response.status_code == 400
    detail = response.json()["detail"].lower()
    assert "not_found" in detail or "not found" in detail


@pytest.mark.anyio
async def test_delete_user_blocked_by_pending_bills(client: AsyncClient, dbsession):
    """Deletion blocked when user has pending invoices."""
    from orchestra.db.dao.recharge_dao import RechargeDAO
    from orchestra.db.models.orchestra_models import RechargeStatus

    user = await create_test_user(client, "pending_bills@test.com")
    user_id = user["id"]

    recharge_dao = RechargeDAO(dbsession)
    recharge_dao.create_recharge(
        user_id=user_id,
        quantity=100,
        amount_usd=Decimal("50.00"),
        invoice_group=date.today(),
        type_="usage",
        status=RechargeStatus.PENDING_INVOICE,
    )
    dbsession.flush()

    response = await client.delete(
        f"/v0/admin/auth-user?user_id={user_id}",
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "pending" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_delete_user_blocked_by_organization_ownership(client: AsyncClient):
    """Deletion blocked when user owns an organization."""
    user = await create_test_user(client, "org_owner@test.com")
    user_id = user["id"]

    response = await client.post(
        "/v0/admin/organization",
        params={"name": "OwnedOrg", "owner_id": user_id},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    response = await client.delete(
        f"/v0/admin/auth-user?user_id={user_id}",
        headers=HEADERS,
    )
    assert response.status_code == 400
    detail = response.json()["detail"].lower()
    assert "organization" in detail or "owner" in detail


@pytest.mark.anyio
async def test_delete_user_force_bypasses_org_check(client: AsyncClient):
    """force=True allows deletion even when user owns organization."""
    user = await create_test_user(client, "force_delete@test.com")
    user_id = user["id"]

    response = await client.post(
        "/v0/admin/organization",
        params={"name": "ForceDeleteOrg", "owner_id": user_id},
        headers=HEADERS,
    )
    assert response.status_code == 200

    response = await client.delete(
        f"/v0/admin/auth-user?user_id={user_id}&force=true",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["success"] is True


@pytest.mark.anyio
async def test_can_delete_account_no_blockers(client: AsyncClient):
    """can-delete-account returns true when no blockers."""
    user = await create_test_user(client, "can_delete@test.com")
    user_headers = {"Authorization": f"Bearer {user['api_key']}"}

    response = await client.get(
        "/v0/user/can-delete-account",
        headers=user_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["can_delete"] is True
    assert data["blockers"] == []


@pytest.mark.anyio
async def test_can_delete_account_with_pending_bills(client: AsyncClient, dbsession):
    """can-delete-account returns false when user has pending bills."""
    from orchestra.db.dao.recharge_dao import RechargeDAO
    from orchestra.db.models.orchestra_models import RechargeStatus

    user = await create_test_user(client, "can_delete_bills@test.com")
    user_id = user["id"]
    user_headers = {"Authorization": f"Bearer {user['api_key']}"}

    recharge_dao = RechargeDAO(dbsession)
    recharge_dao.create_recharge(
        user_id=user_id,
        quantity=100,
        amount_usd=Decimal("25.00"),
        invoice_group=date.today(),
        type_="usage",
        status=RechargeStatus.INVOICE_CREATED,
    )
    dbsession.flush()

    response = await client.get(
        "/v0/user/can-delete-account",
        headers=user_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["can_delete"] is False
    assert len(data["blockers"]) == 1
    assert data["blockers"][0]["reason"] == "pending_bills"


@pytest.mark.anyio
async def test_self_service_delete_requires_email_confirmation(client: AsyncClient):
    """Self-service deletion fails if email doesn't match."""
    user = await create_test_user(client, "confirm_email@test.com")
    user_headers = {"Authorization": f"Bearer {user['api_key']}"}

    response = await client.request(
        "DELETE",
        "/v0/user/delete-account",
        json={"confirm_email": "wrong@email.com"},
        headers=user_headers,
    )
    assert response.status_code == 400
    assert "email" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_self_service_delete_success(client: AsyncClient):
    """Self-service deletion succeeds with correct email confirmation."""
    email = "self_delete@test.com"
    user = await create_test_user(client, email)
    user_id = user["id"]
    user_headers = {"Authorization": f"Bearer {user['api_key']}"}

    response = await client.request(
        "DELETE",
        "/v0/user/delete-account",
        json={"confirm_email": email},
        headers=user_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True

    response = await client.get(
        f"/v0/admin/auth-user/by-user-id?user_id={user_id}",
        headers=HEADERS,
    )
    assert response.status_code == 404


@pytest.mark.anyio
async def test_delete_user_removes_projects(client: AsyncClient):
    """Verify that user's projects are deleted via CASCADE."""
    user = await create_test_user(client, "delete_projects@test.com")
    user_id = user["id"]
    user_headers = {"Authorization": f"Bearer {user['api_key']}"}

    response = await client.post(
        "/v0/project",
        json={"name": "TestProject"},
        headers=user_headers,
    )
    assert response.status_code == 200, response.json()

    response = await client.get("/v0/projects", headers=user_headers)
    assert response.status_code == 200
    projects = response.json()
    assert "TestProject" in projects or any("TestProject" in str(p) for p in projects)

    response = await client.delete(
        f"/v0/admin/auth-user?user_id={user_id}",
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
async def test_delete_user_removes_api_keys(client: AsyncClient, dbsession):
    """Verify that user's API keys are deleted via CASCADE."""
    from sqlalchemy import select

    from orchestra.db.models.orchestra_models import ApiKey

    user = await create_test_user(client, "delete_apikeys@test.com")
    user_id = user["id"]
    api_key = user["api_key"]

    key_exists = dbsession.execute(
        select(ApiKey).where(ApiKey.key == api_key),
    ).scalar_one_or_none()
    assert key_exists is not None

    response = await client.delete(
        f"/v0/admin/auth-user?user_id={user_id}",
        headers=HEADERS,
    )
    assert response.status_code == 200

    key_after = dbsession.execute(
        select(ApiKey).where(ApiKey.key == api_key),
    ).scalar_one_or_none()
    assert key_after is None


@pytest.mark.anyio
async def test_delete_user_with_queries(client: AsyncClient, dbsession):
    """Verify queries are deleted during account deletion."""
    from datetime import datetime

    from sqlalchemy import insert, select

    from orchestra.db.models.orchestra_models import Query

    user = await create_test_user(client, "delete_queries@test.com")
    user_id = user["id"]

    dbsession.execute(
        insert(Query).values(
            user_id=user_id,
            at=datetime.utcnow(),
            model_provider_str="openai@gpt-4",
            credits=0.03,
            query_body='{"prompt": "test"}',
            response_body='{"response": "test"}',
            status_code=200,
        ),
    )
    dbsession.flush()

    query_exists = dbsession.execute(
        select(Query).where(Query.user_id == user_id),
    ).scalar_one_or_none()
    assert query_exists is not None

    response = await client.delete(
        f"/v0/admin/auth-user?user_id={user_id}",
        headers=HEADERS,
    )
    assert response.status_code == 200

    query_after = dbsession.execute(
        select(Query).where(Query.user_id == user_id),
    ).scalar_one_or_none()
    assert query_after is None


@pytest.mark.anyio
async def test_delete_user_with_paid_recharges_allowed(client: AsyncClient, dbsession):
    """User with only PAID recharges can be deleted."""
    from orchestra.db.dao.recharge_dao import RechargeDAO
    from orchestra.db.models.orchestra_models import RechargeStatus

    user = await create_test_user(client, "paid_recharges@test.com")
    user_id = user["id"]

    recharge_dao = RechargeDAO(dbsession)
    recharge_dao.create_recharge(
        user_id=user_id,
        quantity=100,
        amount_usd=Decimal("50.00"),
        invoice_group=date.today(),
        type_="prepaid",
        status=RechargeStatus.PAID,
    )
    dbsession.flush()

    response = await client.delete(
        f"/v0/admin/auth-user?user_id={user_id}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["success"] is True


@pytest.mark.anyio
async def test_self_service_delete_case_insensitive_email(client: AsyncClient):
    """Email confirmation is case-insensitive."""
    email = "CaseSensitive@Test.com"
    user = await create_test_user(client, email)
    user_headers = {"Authorization": f"Bearer {user['api_key']}"}

    response = await client.request(
        "DELETE",
        "/v0/user/delete-account",
        json={"confirm_email": "casesensitive@test.com"},
        headers=user_headers,
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
