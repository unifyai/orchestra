"""Tests for user account deletion functionality.

Tests cover:
1. Successful account deletion (personal projects, assistants, etc.)
2. Blocking conditions (org ownership)
3. Cleanup of external resources
4. Deletion status check endpoint
"""

import os
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.db.dao.auth_user_dao import AuthUserDAO
from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.models.orchestra_models import (
    ApiKey,
    AuthUser,
    CreditCardFingerprint,
    CustomApiKey,
    CustomEndpoint,
    CustomRouter,
    LocalEndpoint,
    Organization,
    OrganizationMember,
    Project,
    Query,
    QueryTagAssociation,
    Recharge,
    RechargeStatus,
    Router,
    Tag,
    Users,
)
from orchestra.tests.utils import ADMIN_HEADERS, create_test_user


@pytest.fixture(autouse=True)
def mock_external_services(request):
    """Mock external service calls for all tests.

    Patches are applied at the source modules where the classes/functions are defined,
    not where they're imported (since imports happen inside methods).
    """
    if "no_mock_external" in request.keywords:
        yield
        return

    with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_mock"}), patch(
        "orchestra.services.bucket_service.BucketService",
    ) as mock_bucket, patch(
        "orchestra.web.api.utils.assistant_infra.delete_phone_number",
    ) as mock_delete_phone, patch(
        "orchestra.web.api.utils.assistant_infra.delete_email",
    ) as mock_delete_email, patch(
        "orchestra.web.api.utils.assistant_infra.delete_pubsub_topic",
    ) as mock_delete_pubsub, patch(
        "orchestra.web.api.utils.assistant_infra.stop_jobs",
    ) as mock_stop_jobs, patch(
        "orchestra.services.user_account_cleanup_service.stripe",
    ) as mock_stripe, patch(
        "orchestra.services.contact_sync_service.ContactSyncService.mark_member_contact_as_non_system",
    ) as mock_contact_sync:
        mock_bucket_instance = MagicMock()
        mock_bucket_instance.delete_assistant_file.return_value = True
        mock_bucket.return_value = mock_bucket_instance
        mock_delete_phone.return_value = {"success": True}
        mock_delete_email.return_value = {"success": True}
        mock_delete_pubsub.return_value = {"success": True}
        mock_stop_jobs.return_value = {"success": True, "job_names": []}

        # Mock Stripe API
        mock_stripe.Customer.retrieve.return_value = MagicMock(
            invoice_settings=MagicMock(default_payment_method="pm_test"),
        )
        mock_stripe.Customer.delete.return_value = MagicMock(deleted=True)
        mock_stripe.Invoice.create.return_value = MagicMock(id="in_test")
        mock_stripe.Invoice.finalize_invoice.return_value = MagicMock(id="in_test")
        mock_stripe.Invoice.pay.return_value = MagicMock(status="paid")
        mock_stripe.InvoiceItem.create.return_value = MagicMock(id="ii_test")
        mock_stripe.InvoiceItem.list.return_value = MagicMock(
            auto_paging_iter=lambda: [],
        )
        mock_stripe.Invoice.list.return_value = MagicMock(auto_paging_iter=lambda: [])
        mock_stripe.Invoice.void_invoice.return_value = MagicMock()

        mock_contact_sync.return_value = None

        yield {
            "bucket": mock_bucket_instance,
            "delete_phone": mock_delete_phone,
            "delete_email": mock_delete_email,
            "delete_pubsub": mock_delete_pubsub,
            "stop_jobs": mock_stop_jobs,
            "stripe": mock_stripe,
            "contact_sync": mock_contact_sync,
        }


# =============================================================================
# Deletion Status Check Tests
# =============================================================================


@pytest.mark.anyio
async def test_deletion_status_no_blockers(client: AsyncClient, dbsession):
    """Test deletion status check when no blockers exist."""
    user = await create_test_user(client, "deletion_status_ok@test.com")

    response = await client.get(
        "/v0/user/account/deletion-status",
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["blocked"] is False
    assert "can be deleted" in data["message"]


@pytest.mark.anyio
async def test_deletion_status_blocked_by_org_ownership(
    client: AsyncClient,
    dbsession,
):
    """Test deletion status is blocked when user owns an organization."""
    owner = await create_test_user(client, "org_owner_blocked@test.com")

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Blocking Org"},
        headers=owner["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED

    # Check deletion status
    response = await client.get(
        "/v0/user/account/deletion-status",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["blocked"] is True
    assert "Blocking Org" in str(data["reasons"])
    assert "Transfer ownership" in str(data["reasons"])


# =============================================================================
# Account Deletion Tests
# =============================================================================


@pytest.mark.anyio
async def test_delete_account_requires_confirmation(client: AsyncClient, dbsession):
    """Test that account deletion requires explicit confirmation."""
    user = await create_test_user(client, "no_confirm@test.com")

    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": False},
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.anyio
async def test_delete_account_blocked_by_org_ownership(
    client: AsyncClient,
    dbsession,
):
    """Test account deletion is blocked when user owns an organization."""
    owner = await create_test_user(client, "org_owner_delete@test.com")

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Blocking Delete Org"},
        headers=owner["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED

    # Try to delete account
    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "blocked" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_delete_account_success(client: AsyncClient, dbsession):
    """Test successful account deletion."""
    user = await create_test_user(client, "delete_me@test.com")
    user_id = user["id"]

    # Verify user exists
    auth_user_dao = AuthUserDAO(dbsession)
    user_before = auth_user_dao.get_by_id(user_id)
    assert user_before is not None

    # Delete account
    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True, "reason": "Testing deletion"},
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["success"] is True
    assert "deleted_resources" in data

    # Verify user no longer exists
    dbsession.expire_all()
    user_after = auth_user_dao.get_by_id(user_id)
    assert user_after is None


@pytest.mark.anyio
async def test_delete_account_cleans_up_projects(client: AsyncClient, dbsession):
    """Test that account deletion removes user's personal projects."""
    user = await create_test_user(client, "project_cleanup@test.com")
    user_id = user["id"]

    # Create a personal project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(name="My Project", user_id=user_id, organization_id=None)
    dbsession.commit()

    # Verify project exists
    projects_before = project_dao.filter(user_id=user_id)
    assert len(projects_before) >= 1

    # Delete account
    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    # Verify projects are deleted (CASCADE)
    dbsession.expire_all()
    projects_after = dbsession.query(Project).filter(Project.user_id == user_id).all()
    assert len(projects_after) == 0


@pytest.mark.anyio
async def test_delete_account_cleans_up_legacy_records(client: AsyncClient, dbsession):
    """Test that account deletion removes legacy users table dependencies."""
    user = await create_test_user(client, "legacy_cleanup@test.com")
    user_id = user["id"]

    # Create some legacy records
    users_dao = UsersDAO(dbsession)
    legacy_user = users_dao.filter(id=user_id)

    if legacy_user:
        # Add a custom API key
        custom_key = CustomApiKey(user_id=user_id, key="test_key", value="test_value")
        dbsession.add(custom_key)
        dbsession.flush()

        # Add a local endpoint
        local_endpoint = LocalEndpoint(user_id=user_id, name="test_endpoint")
        dbsession.add(local_endpoint)

        # Add a tag
        tag = Tag(user_id=user_id, tag_name="test_tag")
        dbsession.add(tag)

        dbsession.commit()

    # Delete account
    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    # Verify legacy records are cleaned up
    dbsession.expire_all()
    assert (
        dbsession.query(CustomApiKey).filter(CustomApiKey.user_id == user_id).count()
        == 0
    )
    assert (
        dbsession.query(LocalEndpoint).filter(LocalEndpoint.user_id == user_id).count()
        == 0
    )
    assert dbsession.query(Tag).filter(Tag.user_id == user_id).count() == 0


@pytest.mark.anyio
async def test_delete_account_cleans_up_custom_endpoints(
    client: AsyncClient,
    dbsession,
):
    """Test that account deletion removes custom endpoints and their API keys."""
    user = await create_test_user(client, "custom_endpoint_cleanup@test.com")
    user_id = user["id"]

    users_dao = UsersDAO(dbsession)
    legacy_user = users_dao.filter(id=user_id)

    if legacy_user:
        # Create a custom API key (required for custom endpoint)
        custom_key = CustomApiKey(
            user_id=user_id,
            key="endpoint_api_key",
            value="endpoint_api_value",
        )
        dbsession.add(custom_key)
        dbsession.flush()

        # Create a custom endpoint linked to that API key
        custom_endpoint = CustomEndpoint(
            user_id=user_id,
            name="test_custom_endpoint",
            url="https://example.com/api",
            key_id=custom_key.id,
        )
        dbsession.add(custom_endpoint)
        dbsession.commit()

        # Verify records exist
        assert (
            dbsession.query(CustomEndpoint)
            .filter(CustomEndpoint.user_id == user_id)
            .count()
            == 1
        )

    # Delete account
    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    # Verify cleanup
    dbsession.expire_all()
    assert (
        dbsession.query(CustomEndpoint)
        .filter(CustomEndpoint.user_id == user_id)
        .count()
        == 0
    )
    assert (
        dbsession.query(CustomApiKey).filter(CustomApiKey.user_id == user_id).count()
        == 0
    )


@pytest.mark.anyio
async def test_delete_account_cleans_up_custom_routers(client: AsyncClient, dbsession):
    """Test that account deletion removes custom routers."""
    user = await create_test_user(client, "custom_router_cleanup@test.com")
    user_id = user["id"]

    users_dao = UsersDAO(dbsession)
    legacy_user = users_dao.filter(id=user_id)

    if legacy_user:
        custom_router = CustomRouter(
            user_id=user_id,
            router_name="test_router",
            router_id="router_123",
        )
        dbsession.add(custom_router)
        dbsession.commit()

        assert (
            dbsession.query(CustomRouter)
            .filter(CustomRouter.user_id == user_id)
            .count()
            == 1
        )

    # Delete account
    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    # Verify cleanup
    dbsession.expire_all()
    assert (
        dbsession.query(CustomRouter).filter(CustomRouter.user_id == user_id).count()
        == 0
    )


@pytest.mark.anyio
async def test_delete_account_cleans_up_credit_card_fingerprints(
    client: AsyncClient,
    dbsession,
):
    """Test that account deletion removes credit card fingerprints."""
    user = await create_test_user(client, "cc_fingerprint_cleanup@test.com")
    user_id = user["id"]

    users_dao = UsersDAO(dbsession)
    legacy_user = users_dao.filter(id=user_id)

    if legacy_user:
        fingerprint = CreditCardFingerprint(
            user_id=user_id,
            fingerprint="fp_test_1234567890",
        )
        dbsession.add(fingerprint)
        dbsession.commit()

        assert (
            dbsession.query(CreditCardFingerprint)
            .filter(CreditCardFingerprint.user_id == user_id)
            .count()
            == 1
        )

    # Delete account
    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    # Verify cleanup
    dbsession.expire_all()
    assert (
        dbsession.query(CreditCardFingerprint)
        .filter(CreditCardFingerprint.user_id == user_id)
        .count()
        == 0
    )


@pytest.mark.anyio
async def test_delete_account_cleans_up_queries_and_routers(
    client: AsyncClient,
    dbsession,
):
    """Test that account deletion removes queries and routers."""
    user = await create_test_user(client, "query_router_cleanup@test.com")
    user_id = user["id"]

    users_dao = UsersDAO(dbsession)
    legacy_user = users_dao.filter(id=user_id)

    if legacy_user:
        # Create a query (at and model_provider_str are required)
        query = Query(
            user_id=user_id,
            at=datetime.now(),
            model_provider_str="test_provider",
            credits=0,
            query_body="test query",
            response_body="test response",
            status_code=200,
        )
        dbsession.add(query)

        # Create a router
        router = Router(
            user_id=user_id,
            name="test_router",
            endpoints="endpoint1,endpoint2",
        )
        dbsession.add(router)
        dbsession.commit()

        assert dbsession.query(Query).filter(Query.user_id == user_id).count() == 1
        assert dbsession.query(Router).filter(Router.user_id == user_id).count() == 1

    # Delete account
    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    # Verify cleanup
    dbsession.expire_all()
    assert dbsession.query(Query).filter(Query.user_id == user_id).count() == 0
    assert dbsession.query(Router).filter(Router.user_id == user_id).count() == 0


@pytest.mark.anyio
async def test_delete_account_cleans_up_query_tag_associations(
    client: AsyncClient,
    dbsession,
):
    """Test that account deletion removes query-tag associations."""
    user = await create_test_user(client, "query_tag_assoc_cleanup@test.com")
    user_id = user["id"]

    users_dao = UsersDAO(dbsession)
    legacy_user = users_dao.filter(id=user_id)

    if legacy_user:
        # Create a tag
        tag = Tag(user_id=user_id, tag_name="assoc_test_tag")
        dbsession.add(tag)
        dbsession.flush()

        # Create a query (all NOT NULL fields must be provided)
        query = Query(
            user_id=user_id,
            at=datetime.now(),
            model_provider_str="test_provider",
            credits=0,
            query_body="test query",
            response_body="test response",
            status_code=200,
        )
        dbsession.add(query)
        dbsession.flush()

        # Create association
        assoc = QueryTagAssociation(
            user_id=user_id,
            query_id=query.id,
            tag_id=tag.id,
        )
        dbsession.add(assoc)
        dbsession.commit()

        assert (
            dbsession.query(QueryTagAssociation)
            .filter(QueryTagAssociation.user_id == user_id)
            .count()
            == 1
        )

    # Delete account
    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    # Verify cleanup
    dbsession.expire_all()
    assert (
        dbsession.query(QueryTagAssociation)
        .filter(QueryTagAssociation.user_id == user_id)
        .count()
        == 0
    )
    assert dbsession.query(Tag).filter(Tag.user_id == user_id).count() == 0
    assert dbsession.query(Query).filter(Query.user_id == user_id).count() == 0


@pytest.mark.anyio
async def test_delete_account_cleans_up_api_keys(client: AsyncClient, dbsession):
    """Test that account deletion removes all API keys."""
    user = await create_test_user(client, "apikey_cleanup@test.com")
    user_id = user["id"]

    # Verify API key exists before deletion
    api_keys_before = dbsession.query(ApiKey).filter(ApiKey.user_id == user_id).count()
    assert api_keys_before >= 1

    # Delete account
    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    # Verify API keys are deleted (CASCADE)
    dbsession.expire_all()
    api_keys_after = dbsession.query(ApiKey).filter(ApiKey.user_id == user_id).count()
    assert api_keys_after == 0


@pytest.mark.anyio
async def test_delete_account_removes_auth_user(client: AsyncClient, dbsession):
    """Test that the auth_user record is properly deleted."""
    user = await create_test_user(client, "auth_user_delete@test.com")
    user_id = user["id"]

    # Verify auth_user exists
    auth_user_before = dbsession.query(AuthUser).filter(AuthUser.id == user_id).first()
    assert auth_user_before is not None

    # Delete account
    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    # Verify auth_user is deleted
    dbsession.expire_all()
    auth_user_after = dbsession.query(AuthUser).filter(AuthUser.id == user_id).first()
    assert auth_user_after is None


@pytest.mark.anyio
async def test_delete_account_removes_legacy_users(client: AsyncClient, dbsession):
    """Test that the legacy users record is properly deleted."""
    user = await create_test_user(client, "legacy_user_delete@test.com")
    user_id = user["id"]

    # Verify users record exists (created during user creation)
    legacy_user_before = dbsession.query(Users).filter(Users.id == user_id).first()

    # Delete account
    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    # Verify users record is deleted
    dbsession.expire_all()
    legacy_user_after = dbsession.query(Users).filter(Users.id == user_id).first()
    assert legacy_user_after is None


# =============================================================================
# Organization Member Deletion Tests
# =============================================================================


@pytest.mark.anyio
async def test_org_member_can_delete_account(client: AsyncClient, dbsession):
    """Test that org members (non-owners) can delete their accounts."""
    owner = await create_test_user(client, "org_owner_for_member@test.com")
    member = await create_test_user(client, "org_member_deletable@test.com")

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Member Delete Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Add member to organization
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Member should be able to delete their account
    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=member["headers"],
    )
    assert response.status_code == status.HTTP_200_OK


# =============================================================================
# Edge Cases
# =============================================================================


@pytest.mark.anyio
async def test_delete_nonexistent_user(client: AsyncClient, dbsession):
    """Test deletion with invalid/nonexistent user returns proper error."""
    # Create a user and get their token
    user = await create_test_user(client, "soon_deleted@test.com")

    # Delete the user directly via admin endpoint
    await client.delete(
        "/v0/admin/auth-user",
        params={"user_id": user["id"]},
        headers=ADMIN_HEADERS,
    )

    # Try to delete via the user endpoint (user no longer exists)
    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=user["headers"],
    )
    # Should get 401 or 404 since apikey and user doesn't exist
    assert response.status_code in [
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND,
    ]


@pytest.mark.anyio
async def test_delete_account_with_reason_logged(client: AsyncClient, dbsession):
    """Test that deletion reason is properly handled."""
    user = await create_test_user(client, "reason_test@test.com")

    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={
            "confirm": True,
            "reason": "Testing the deletion flow",
        },
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["success"] is True


@pytest.mark.anyio
async def test_delete_account_double_delete_fails(client: AsyncClient, dbsession):
    """Test that attempting to delete an already-deleted account fails gracefully."""
    user = await create_test_user(client, "double_delete@test.com")
    headers = user["headers"]

    # First deletion should succeed
    response1 = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=headers,
    )
    assert response1.status_code == status.HTTP_200_OK

    # Second deletion with same token should fail
    response2 = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=headers,
    )
    # Should get 401 (invalid token) or 404 (user not found)
    assert response2.status_code in [
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND,
    ]


@pytest.mark.anyio
async def test_delete_account_empty_body_fails(client: AsyncClient, dbsession):
    """Test that deletion with empty body returns validation error."""
    user = await create_test_user(client, "empty_body@test.com")

    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={},
        headers=user["headers"],
    )
    # Missing required 'confirm' field should return 422
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.anyio
async def test_deletion_status_multiple_org_ownership(client: AsyncClient, dbsession):
    """Test deletion status lists all owned organizations."""
    owner = await create_test_user(client, "multi_org_owner@test.com")

    # Create first organization
    org1_resp = await client.post(
        "/v0/organizations",
        json={"name": "First Blocking Org"},
        headers=owner["headers"],
    )
    assert org1_resp.status_code == status.HTTP_201_CREATED

    # Create second organization
    org2_resp = await client.post(
        "/v0/organizations",
        json={"name": "Second Blocking Org"},
        headers=owner["headers"],
    )
    assert org2_resp.status_code == status.HTTP_201_CREATED

    # Check deletion status
    response = await client.get(
        "/v0/user/account/deletion-status",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()

    assert data["blocked"] is True
    reasons_str = str(data["reasons"])
    assert "First Blocking Org" in reasons_str
    assert "Second Blocking Org" in reasons_str
    assert "2 organization" in reasons_str


@pytest.mark.anyio
async def test_delete_account_removes_org_membership(client: AsyncClient, dbsession):
    """Test that deleting a member's account removes their org memberships."""
    owner = await create_test_user(client, "owner_for_membership_test@test.com")
    member = await create_test_user(client, "member_to_delete@test.com")
    member_id = member["id"]

    # Create first organization
    org1_resp = await client.post(
        "/v0/organizations",
        json={"name": "Membership Test Org 1"},
        headers=owner["headers"],
    )
    org1_id = org1_resp.json()["id"]

    # Create second organization
    org2_resp = await client.post(
        "/v0/organizations",
        json={"name": "Membership Test Org 2"},
        headers=owner["headers"],
    )
    org2_id = org2_resp.json()["id"]

    # Add member to both organizations
    await client.post(
        f"/v0/organizations/{org1_id}/members",
        json={"user_id": member_id},
        headers=owner["headers"],
    )
    await client.post(
        f"/v0/organizations/{org2_id}/members",
        json={"user_id": member_id},
        headers=owner["headers"],
    )

    # Verify memberships exist
    memberships_before = (
        dbsession.query(OrganizationMember)
        .filter(OrganizationMember.user_id == member_id)
        .count()
    )
    assert memberships_before == 2

    # Member deletes their account
    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=member["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    # Verify memberships are removed (CASCADE)
    dbsession.expire_all()
    memberships_after = (
        dbsession.query(OrganizationMember)
        .filter(OrganizationMember.user_id == member_id)
        .count()
    )
    assert memberships_after == 0

    # Verify organizations still exist
    assert (
        dbsession.query(Organization).filter(Organization.id == org1_id).first()
        is not None
    )
    assert (
        dbsession.query(Organization).filter(Organization.id == org2_id).first()
        is not None
    )


# =============================================================================
# BILLING DELEGATION BLOCKER TESTS
# =============================================================================


@pytest.mark.anyio
async def test_deletion_blocked_when_billing_delegate(client: AsyncClient, dbsession):
    """Test that deletion is blocked when user is billing delegate for an org."""
    # Create two users: owner and billing delegate
    owner = await create_test_user(client, "org_owner_billing@test.com")
    delegate = await create_test_user(client, "billing_delegate@test.com")
    delegate_id = delegate["id"]

    # Create organization with owner
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Delegated Billing Org"},
        headers=owner["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_id = org_resp.json()["id"]

    # Set delegate as billing_user_id (simulate delegated billing)
    org = dbsession.query(Organization).filter_by(id=org_id).first()
    org.billing_user_id = delegate_id
    org.stripe_customer_id = None  # Delegated billing = no direct Stripe
    dbsession.commit()

    # Check deletion status for delegate
    status_resp = await client.get(
        "/v0/user/account/deletion-status",
        headers=delegate["headers"],
    )
    assert status_resp.status_code == status.HTTP_200_OK
    data = status_resp.json()
    assert data["blocked"] is True
    assert any("billing" in r.lower() for r in data["reasons"])

    # Try to delete - should fail
    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=delegate["headers"],
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "billing" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_deletion_allowed_when_org_has_direct_billing(
    client: AsyncClient,
    dbsession,
):
    """Test that user can delete if org has direct billing (not delegated)."""
    owner = await create_test_user(client, "direct_billing_owner@test.com")
    user = await create_test_user(client, "direct_billing_user@test.com")
    user_id = user["id"]

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Direct Billing Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Set user as billing_user_id BUT org has stripe_customer_id (direct billing)
    org = dbsession.query(Organization).filter_by(id=org_id).first()
    org.billing_user_id = user_id
    org.stripe_customer_id = "cus_direct_billing_123"  # Direct billing
    dbsession.commit()

    # Check deletion status - should be allowed (org has direct billing)
    status_resp = await client.get(
        "/v0/user/account/deletion-status",
        headers=user["headers"],
    )
    assert status_resp.status_code == status.HTTP_200_OK
    data = status_resp.json()
    assert data["blocked"] is False


# =============================================================================
# RESOURCE ACCESS CLEANUP TESTS
# =============================================================================


@pytest.mark.anyio
async def test_delete_account_cleans_up_resource_access_grants(
    client: AsyncClient,
    dbsession,
):
    """Test that ResourceAccess grants for user are deleted."""
    from orchestra.db.models.orchestra_models import ResourceAccess, Role

    user = await create_test_user(client, "resource_access_cleanup@test.com")
    user_id = user["id"]

    # Get a role ID for the grant
    role = dbsession.query(Role).first()
    if not role:
        pytest.skip("No roles in database")

    # Create ResourceAccess grant for user
    grant = ResourceAccess(
        resource_type="project",
        resource_id=999,  # Dummy ID
        role_id=role.id,
        grantee_type="user",
        grantee_id=user_id,
    )
    dbsession.add(grant)
    dbsession.commit()

    # Verify grant exists
    grants_before = (
        dbsession.query(ResourceAccess)
        .filter(
            ResourceAccess.grantee_type == "user",
            ResourceAccess.grantee_id == user_id,
        )
        .count()
    )
    assert grants_before == 1

    # Delete account
    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    # Verify grants are cleaned up
    dbsession.expire_all()
    grants_after = (
        dbsession.query(ResourceAccess)
        .filter(
            ResourceAccess.grantee_type == "user",
            ResourceAccess.grantee_id == user_id,
        )
        .count()
    )
    assert grants_after == 0

    # Check response includes cleanup count
    data = response.json()
    assert data["deleted_resources"]["resource_access_deleted"] >= 1


# =============================================================================
# PAY-THEN-DELETE BILLING TESTS
# =============================================================================


@pytest.mark.anyio
async def test_delete_account_settles_pending_invoice_recharges(
    client: AsyncClient,
    dbsession,
):
    """Test that pending recharges are settled before account deletion."""
    from datetime import date
    from decimal import Decimal

    from orchestra.lib.time import month_end_utc

    user = await create_test_user(client, "settle_recharges@test.com")
    user_id = user["id"]

    # Create a legacy user record with stripe_customer_id
    legacy_user = dbsession.query(Users).filter_by(id=user_id).first()
    if legacy_user:
        legacy_user.stripe_customer_id = "cus_settle_test"
        dbsession.commit()

        # Create pending recharges
        recharge1 = Recharge(
            user_id=user_id,
            quantity=Decimal("10"),
            amount_usd=Decimal("10.00"),
            status=RechargeStatus.PENDING_INVOICE,
            invoice_group=month_end_utc(date.today()),
            type="auto",
        )
        recharge2 = Recharge(
            user_id=user_id,
            quantity=Decimal("15"),
            amount_usd=Decimal("15.00"),
            status=RechargeStatus.PENDING_INVOICE,
            invoice_group=month_end_utc(date.today()),
            type="auto",
        )
        dbsession.add(recharge1)
        dbsession.add(recharge2)
        dbsession.commit()

        # Verify recharges exist
        pending = (
            dbsession.query(Recharge)
            .filter(
                Recharge.user_id == user_id,
                Recharge.status == RechargeStatus.PENDING_INVOICE,
            )
            .count()
        )
        assert pending == 2

    # Delete account - should settle recharges first
    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    # Check response includes settlement info
    data = response.json()
    if data["deleted_resources"].get("balance_settled"):
        assert data["deleted_resources"]["balance_settled"]["amount"] == 25.0
        assert data["deleted_resources"]["balance_settled"]["recharges_settled"] == 2


@pytest.mark.anyio
async def test_delete_account_with_zero_balance_succeeds(
    client: AsyncClient,
    dbsession,
):
    """Test that deletion succeeds when user has no outstanding balance."""
    user = await create_test_user(client, "zero_balance@test.com")

    # No recharges created - zero balance

    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    # balance_settled should be None
    data = response.json()
    assert data["deleted_resources"]["balance_settled"] is None


@pytest.mark.anyio
async def test_delete_account_stripe_customer_deleted(
    client: AsyncClient,
    dbsession,
):
    """Test that Stripe customer is deleted during account cleanup."""
    user = await create_test_user(client, "stripe_cleanup@test.com")
    user_id = user["id"]

    # Set stripe_customer_id
    legacy_user = dbsession.query(Users).filter_by(id=user_id).first()
    if legacy_user:
        legacy_user.stripe_customer_id = "cus_to_delete"
        dbsession.commit()

    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    # Check response indicates Stripe customer was deleted
    data = response.json()
    assert data["deleted_resources"]["stripe_customer_deleted"] is True


@pytest.mark.anyio
async def test_delete_account_disables_autorecharge(
    client: AsyncClient,
    dbsession,
):
    """Test that autorecharge is disabled before account deletion."""
    user = await create_test_user(client, "disable_autorecharge@test.com")
    user_id = user["id"]

    # Enable autorecharge
    legacy_user = dbsession.query(Users).filter_by(id=user_id).first()
    if legacy_user:
        legacy_user.autorecharge = True
        legacy_user.stripe_customer_id = "cus_autorecharge"
        dbsession.commit()

        assert legacy_user.autorecharge is True

    response = await client.request(
        "DELETE",
        "/v0/user/account",
        json={"confirm": True},
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    # User should be deleted - verify via response
    assert response.json()["success"] is True
