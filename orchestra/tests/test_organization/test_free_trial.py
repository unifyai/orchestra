"""Tests for organization free trial endpoints."""

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.tests.utils import ADMIN_HEADERS, create_test_user


@pytest.mark.anyio
async def test_enable_free_trial(client: AsyncClient):
    """Test enabling free trial for an organization."""
    user = await create_test_user(client, "ft_enable@example.com")
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Free Trial Enable Org"},
        headers=user["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_id = org_resp.json()["id"]

    resp = await client.put(
        f"/v0/admin/organization/{org_id}/free-trial",
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["free_trial"] is True
    assert data["organization_id"] == org_id


@pytest.mark.anyio
async def test_disable_free_trial(client: AsyncClient):
    """Test disabling free trial for an organization."""
    user = await create_test_user(client, "ft_disable@example.com")
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Free Trial Disable Org"},
        headers=user["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_id = org_resp.json()["id"]

    # Enable first
    await client.put(
        f"/v0/admin/organization/{org_id}/free-trial",
        headers=ADMIN_HEADERS,
    )

    # Disable
    resp = await client.delete(
        f"/v0/admin/organization/{org_id}/free-trial",
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["free_trial"] is False


@pytest.mark.anyio
async def test_free_trial_nonexistent_org(client: AsyncClient):
    """Test toggling free trial on a non-existent org returns 404."""
    resp = await client.put(
        "/v0/admin/organization/999999/free-trial",
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND

    resp = await client.delete(
        "/v0/admin/organization/999999/free-trial",
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_free_trial_in_org_response(client: AsyncClient):
    """Test that free_trial appears in the organization GET response."""
    user = await create_test_user(client, "ft_response@example.com")
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Free Trial Response Org"},
        headers=user["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_id = org_resp.json()["id"]

    # Default should be False
    get_resp = await client.get(
        f"/v0/organizations/{org_id}",
        headers=user["headers"],
    )
    assert get_resp.status_code == status.HTTP_200_OK
    assert get_resp.json()["free_trial"] is False

    # Enable free trial
    await client.put(
        f"/v0/admin/organization/{org_id}/free-trial",
        headers=ADMIN_HEADERS,
    )

    # Should now be True
    get_resp = await client.get(
        f"/v0/organizations/{org_id}",
        headers=user["headers"],
    )
    assert get_resp.status_code == status.HTTP_200_OK
    assert get_resp.json()["free_trial"] is True


@pytest.mark.anyio
async def test_free_trial_in_user_organizations(client: AsyncClient):
    """Test that free_trial flows through the user/by-email admin endpoint."""
    user = await create_test_user(client, "ft_user_orgs@example.com")
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Free Trial User Orgs Org"},
        headers=user["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_id = org_resp.json()["id"]

    # Enable free trial
    await client.put(
        f"/v0/admin/organization/{org_id}/free-trial",
        headers=ADMIN_HEADERS,
    )

    # Fetch user by email (this is what the Console calls)
    user_resp = await client.get(
        "/v0/admin/user/by-email",
        params={"email": "ft_user_orgs@example.com"},
        headers=ADMIN_HEADERS,
    )
    assert user_resp.status_code == status.HTTP_200_OK
    orgs = user_resp.json()["organizations"]
    matching = [o for o in orgs if o["id"] == org_id]
    assert len(matching) == 1
    assert matching[0]["free_trial"] is True
