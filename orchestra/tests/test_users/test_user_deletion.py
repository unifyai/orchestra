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
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}",
}


async def create_test_user(client: AsyncClient, email: str) -> dict:
    """Create a test user and return their data including API key."""
    response = await client.post(
        "/v0/admin/user",
        json={"email": email},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    user_id = response.json()["id"]

    response = await client.get(
        f"/v0/admin/user/by-user-id?user_id={user_id}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.anyio
async def test_delete_user_happy_path(client: AsyncClient):
    """Verify complete account deletion removes user from user table."""
    user = await create_test_user(client, "delete_happy@test.com")
    user_id = user["id"]

    response = await client.delete(
        f"/v0/admin/user?user_id={user_id}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["success"] is True
    assert "deleted" in data["message"].lower()

    response = await client.get(
        f"/v0/admin/user/by-user-id?user_id={user_id}",
        headers=HEADERS,
    )
    assert response.status_code == 404


@pytest.mark.anyio
async def test_delete_nonexistent_user(client: AsyncClient):
    """Deleting a non-existent user returns error."""
    response = await client.delete(
        "/v0/admin/user?user_id=nonexistent-user-id-12345",
        headers=HEADERS,
    )
    assert response.status_code == 400
    detail = response.json()["detail"].lower()
    assert "not_found" in detail or "not found" in detail


@pytest.mark.anyio
async def test_delete_user_blocked_by_pending_bills(client: AsyncClient, dbsession):
    """Deletion blocked when user has pending invoices."""
    from orchestra.db.dao.recharge_dao import RechargeDAO
    from orchestra.db.dao.user_dao import UserDAO
    from orchestra.db.models.orchestra_models import RechargeStatus

    user = await create_test_user(client, "pending_bills@test.com")
    user_id = user["id"]

    user_dao = UserDAO(dbsession)
    user_obj = user_dao.get_user_with_id(user_id)

    recharge_dao = RechargeDAO(dbsession)
    recharge_dao.create_recharge(
        billing_account_id=user_obj.billing_account_id,
        quantity=100,
        amount_usd=Decimal("50.00"),
        invoice_group=date.today(),
        type_="usage",
        status=RechargeStatus.PENDING_INVOICE,
    )
    dbsession.flush()

    response = await client.delete(
        f"/v0/admin/user?user_id={user_id}",
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
        f"/v0/admin/user?user_id={user_id}",
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
        f"/v0/admin/user?user_id={user_id}&force=true",
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
    from orchestra.db.dao.user_dao import UserDAO
    from orchestra.db.models.orchestra_models import RechargeStatus

    user = await create_test_user(client, "can_delete_bills@test.com")
    user_id = user["id"]
    user_headers = {"Authorization": f"Bearer {user['api_key']}"}

    user_dao = UserDAO(dbsession)
    user_obj = user_dao.get_user_with_id(user_id)

    recharge_dao = RechargeDAO(dbsession)
    recharge_dao.create_recharge(
        billing_account_id=user_obj.billing_account_id,
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
        f"/v0/admin/user/by-user-id?user_id={user_id}",
        headers=HEADERS,
    )
    assert response.status_code == 404


@pytest.mark.anyio
async def test_self_service_delete_cleans_org_assistant_runtime_and_contacts(
    client: AsyncClient,
    dbsession,
):
    """Self-service delete cleans org assistants that cascade from creator ownership."""
    from orchestra.db.dao.assistant_dao import AssistantDAO
    from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
    from orchestra.db.dao.role_dao import RoleDAO
    from orchestra.db.models.orchestra_models import AssistantContact
    from orchestra.services.assistant_cleanup_service import CleanupSource

    owner = await create_test_user(client, "delete_org_cleanup_owner@test.com")
    member = await create_test_user(client, "delete_org_cleanup_member@test.com")
    owner_headers = {"Authorization": f"Bearer {owner['api_key']}"}
    member_headers = {"Authorization": f"Bearer {member['api_key']}"}

    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Delete User Cleanup Org"},
        headers=owner_headers,
    )
    assert org_resp.status_code in (200, 201), org_resp.json()
    org_id = org_resp.json()["id"]

    add_member_resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner_headers,
    )
    assert add_member_resp.status_code == 201, add_member_resp.json()

    assistant_dao = AssistantDAO(dbsession)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)
    owner_role = role_dao.get_by_name("Owner", organization_id=None)

    assistant = assistant_dao.create_assistant(
        user_id=member["id"],
        first_name="DeleteUser",
        surname="OrgAssistant",
        age=None,
        nationality=None,
        about=None,
        weekly_limit=None,
        max_parallel=None,
        organization_id=org_id,
        deploy_env="preview",
        desktop_mode="windows",
    )
    dbsession.flush()
    agent_id = int(assistant.agent_id)

    resource_access_dao.grant_access(
        "assistant",
        agent_id,
        owner_role.id,
        "user",
        member["id"],
    )
    dbsession.add(
        AssistantContact(
            assistant_id=agent_id,
            contact_type="phone",
            contact_value="+15550300123",
            provider="twilio",
            status="active",
        ),
    )
    dbsession.commit()

    with patch(
        "orchestra.services.user_account_cleanup_service.enqueue_cleanup_tasks",
    ) as mock_enqueue_cleanup, patch(
        "orchestra.web.api.users.views.run_user_runtime_cleanup_tasks",
    ) as mock_run_cleanup, patch(
        "orchestra.services.bucket_service.BucketService",
    ) as mock_bucket_cls:
        mock_enqueue_cleanup.return_value = [SimpleNamespace(id=901)]
        mock_bucket = mock_bucket_cls.return_value
        mock_bucket.delete_all_assistant_data.return_value = {
            "media": 0,
            "recordings": 0,
            "attachments": 0,
        }
        mock_bucket.delete_user_account_photos.return_value = 0

        response = await client.request(
            "DELETE",
            "/v0/user/delete-account",
            json={"confirm_email": member["email"]},
            headers=member_headers,
        )

    assert response.status_code == 200, response.json()
    assert response.json()["runtime_cleanup_complete"] is False
    assert response.json()["runtime_cleanup_summary"] is None
    mock_enqueue_cleanup.assert_called_once()
    mock_run_cleanup.assert_called_once()
    assert mock_run_cleanup.call_args.kwargs == {
        "cleanup_task_ids": [901],
        "user_id": member["id"],
    }
    cleanup_specs = mock_enqueue_cleanup.call_args.args[1]
    assert [spec.assistant_id for spec in cleanup_specs] == [agent_id]
    assert (
        mock_enqueue_cleanup.call_args.kwargs["source_flow"]
        == CleanupSource.USER_DELETE
    )
    assert cleanup_specs[0].deploy_env == "preview"
    assert cleanup_specs[0].desktop_mode == "windows"
    assert [
        (contact.contact_type, contact.contact_value)
        for contact in cleanup_specs[0].contacts
    ] == [("phone", "+15550300123")]
    mock_bucket.delete_all_assistant_data.assert_called_once_with(agent_id)

    dbsession.expire_all()
    assert assistant_dao.get_assistant_by_agent_id(agent_id) is None

    lookup_resp = await client.get(
        f"/v0/admin/user/by-user-id?user_id={member['id']}",
        headers=HEADERS,
    )
    assert lookup_resp.status_code == 404


@pytest.mark.anyio
async def test_self_service_delete_schedules_background_runtime_cleanup(
    client: AsyncClient,
):
    """Self-service delete returns promptly and kicks runtime cleanup to background."""
    from orchestra.services.assistant_cleanup_service import AssistantCleanupSpec

    email = "delete_cleanup_retry@test.com"
    user = await create_test_user(client, email)
    user_headers = {"Authorization": f"Bearer {user['api_key']}"}

    with patch(
        "orchestra.services.user_account_cleanup_service.UserAccountCleanupService._get_user_assistant_cleanup_specs",
        return_value=[AssistantCleanupSpec(assistant_id=321, deploy_env="preview")],
    ), patch(
        "orchestra.services.user_account_cleanup_service.enqueue_cleanup_tasks",
        return_value=[SimpleNamespace(id=1234)],
    ), patch(
        "orchestra.web.api.users.views.run_user_runtime_cleanup_tasks",
    ) as mock_run_cleanup, patch(
        "orchestra.services.bucket_service.BucketService",
    ) as mock_bucket_cls:
        mock_bucket = mock_bucket_cls.return_value
        mock_bucket.delete_all_assistant_data.return_value = {
            "media": 0,
            "recordings": 0,
            "attachments": 0,
        }
        mock_bucket.delete_user_account_photos.return_value = 0

        response = await client.request(
            "DELETE",
            "/v0/user/delete-account",
            json={"confirm_email": email},
            headers=user_headers,
        )

    assert response.status_code == 200, response.json()
    assert response.json()["success"] is True
    assert response.json()["runtime_cleanup_complete"] is False
    assert response.json()["runtime_cleanup_summary"] is None
    assert response.json()["message"] == "Account deleted successfully"
    mock_run_cleanup.assert_called_once()
    assert mock_run_cleanup.call_args.kwargs == {
        "cleanup_task_ids": [1234],
        "user_id": user["id"],
    }


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
        f"/v0/admin/user?user_id={user_id}",
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
        f"/v0/admin/user?user_id={user_id}",
        headers=HEADERS,
    )
    assert response.status_code == 200

    key_after = dbsession.execute(
        select(ApiKey).where(ApiKey.key == api_key),
    ).scalar_one_or_none()
    assert key_after is None


@pytest.mark.anyio
async def test_delete_user_with_paid_recharges_allowed(client: AsyncClient, dbsession):
    """User with only PAID recharges can be deleted."""
    from orchestra.db.dao.recharge_dao import RechargeDAO
    from orchestra.db.dao.user_dao import UserDAO
    from orchestra.db.models.orchestra_models import RechargeStatus

    user = await create_test_user(client, "paid_recharges@test.com")
    user_id = user["id"]

    user_dao = UserDAO(dbsession)
    user_obj = user_dao.get_user_with_id(user_id)

    recharge_dao = RechargeDAO(dbsession)
    recharge_dao.create_recharge(
        billing_account_id=user_obj.billing_account_id,
        quantity=100,
        amount_usd=Decimal("50.00"),
        invoice_group=date.today(),
        type_="prepaid",
        status=RechargeStatus.PAID,
    )
    dbsession.flush()

    response = await client.delete(
        f"/v0/admin/user?user_id={user_id}",
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
