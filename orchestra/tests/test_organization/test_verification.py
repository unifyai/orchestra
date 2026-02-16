"""
Tests for organization verification endpoints.

Verifying organizations grants them higher rate limits.
"""

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.tests.utils import ADMIN_HEADERS, create_test_user


@pytest.mark.anyio
async def test_verify_organization(client: AsyncClient):
    """Test verifying an organization."""
    # Create a user and organization
    user = await create_test_user(client, "verify_test@example.com")
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Verification Test Org"},
        headers=user["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_id = org_resp.json()["id"]

    # Verify the organization
    verify_resp = await client.put(
        f"/v0/admin/organization/{org_id}/verify",
        headers=ADMIN_HEADERS,
    )
    assert verify_resp.status_code == status.HTTP_200_OK
    data = verify_resp.json()
    assert data["verified"] is True
    assert data["verified_at"] is not None
    assert "verified successfully" in data["message"]


@pytest.mark.anyio
async def test_verify_already_verified_organization(client: AsyncClient):
    """Test verifying an already verified organization."""
    user = await create_test_user(client, "already_verified@example.com")
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Already Verified Org"},
        headers=user["headers"],
    )
    org_id = org_resp.json()["id"]

    # Verify first time
    await client.put(
        f"/v0/admin/organization/{org_id}/verify",
        headers=ADMIN_HEADERS,
    )

    # Verify again
    verify_resp = await client.put(
        f"/v0/admin/organization/{org_id}/verify",
        headers=ADMIN_HEADERS,
    )
    assert verify_resp.status_code == status.HTTP_200_OK
    data = verify_resp.json()
    assert data["verified"] is True
    assert "already verified" in data["message"]


@pytest.mark.anyio
async def test_unverify_organization(client: AsyncClient):
    """Test removing verification from an organization."""
    user = await create_test_user(client, "unverify_test@example.com")
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Unverify Test Org"},
        headers=user["headers"],
    )
    org_id = org_resp.json()["id"]

    # Verify first
    await client.put(
        f"/v0/admin/organization/{org_id}/verify",
        headers=ADMIN_HEADERS,
    )

    # Unverify
    unverify_resp = await client.delete(
        f"/v0/admin/organization/{org_id}/verify",
        headers=ADMIN_HEADERS,
    )
    assert unverify_resp.status_code == status.HTTP_200_OK
    data = unverify_resp.json()
    assert data["verified"] is False
    assert data["verified_at"] is None
    assert "verification removed" in data["message"]


@pytest.mark.anyio
async def test_unverify_not_verified_organization(client: AsyncClient):
    """Test unverifying an organization that isn't verified."""
    user = await create_test_user(client, "not_verified@example.com")
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Not Verified Org"},
        headers=user["headers"],
    )
    org_id = org_resp.json()["id"]

    # Try to unverify without verifying first
    unverify_resp = await client.delete(
        f"/v0/admin/organization/{org_id}/verify",
        headers=ADMIN_HEADERS,
    )
    assert unverify_resp.status_code == status.HTTP_200_OK
    data = unverify_resp.json()
    assert data["verified"] is False
    assert "not verified" in data["message"]


@pytest.mark.anyio
async def test_get_verification_status(client: AsyncClient):
    """Test getting organization verification status."""
    user = await create_test_user(client, "get_status@example.com")
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Get Status Org"},
        headers=user["headers"],
    )
    org_id = org_resp.json()["id"]

    # Get status before verification
    status_resp = await client.get(
        f"/v0/admin/organization/{org_id}/verification",
        headers=ADMIN_HEADERS,
    )
    assert status_resp.status_code == status.HTTP_200_OK
    data = status_resp.json()
    assert data["verified"] is False
    assert data["verified_at"] is None

    # Verify
    await client.put(
        f"/v0/admin/organization/{org_id}/verify",
        headers=ADMIN_HEADERS,
    )

    # Get status after verification
    status_resp = await client.get(
        f"/v0/admin/organization/{org_id}/verification",
        headers=ADMIN_HEADERS,
    )
    assert status_resp.status_code == status.HTTP_200_OK
    data = status_resp.json()
    assert data["verified"] is True
    assert data["verified_at"] is not None


@pytest.mark.anyio
async def test_verify_nonexistent_organization(client: AsyncClient):
    """Test verifying a non-existent organization returns 404."""
    verify_resp = await client.put(
        "/v0/admin/organization/999999/verify",
        headers=ADMIN_HEADERS,
    )
    assert verify_resp.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_get_verification_nonexistent_organization(client: AsyncClient):
    """Test getting verification status of non-existent organization returns 404."""
    status_resp = await client.get(
        "/v0/admin/organization/999999/verification",
        headers=ADMIN_HEADERS,
    )
    assert status_resp.status_code == status.HTTP_404_NOT_FOUND
