import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.tests.utils import ADMIN_HEADERS, create_test_user


@pytest.mark.anyio
async def test_admin_list_contacts_empty(client: AsyncClient):
    # When no contact logs exist, the endpoint should return an empty list
    resp = await client.get("/v0/admin/contacts", headers=ADMIN_HEADERS)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json() == []


@pytest.mark.anyio
async def test_admin_list_contacts_basic(client: AsyncClient):
    # 1) Create a test user and project
    user = await create_test_user(client, "contacts@example.com")
    project_name = "contacts_proj"
    create_proj_resp = await client.post(
        "/v0/project",
        json={"name": project_name, "is_versioned": False, "description": None},
        headers=user["headers"],
    )
    assert create_proj_resp.status_code == status.HTTP_200_OK

    # 2) Prepare contact entries
    contact1 = {
        "first_name": "John",
        "surname": "Doe",
        "email_address": "john.doe@example.com",
        "phone_number": "1234567890",
        "whatsapp_number": "0987654321",
        "description": "Test contact 1",
        "extra_field": "extra1",
    }
    contact2 = {
        "first_name": "Jane",
        "surname": "Smith",
        "email_address": "jane.smith@example.com",
        "phone_number": "1112223333",
        "whatsapp_number": "3332221111",
        "description": "Test contact 2",
        "another_field": "extra2",
    }

    # 3) Create logs under the "contacts" context
    payload = {
        "project": project_name,
        "context": "contacts",
        "params": {},
        "entries": [contact1, contact2],
    }
    create_logs_resp = await client.post(
        "/v0/logs",
        json=payload,
        headers=user["headers"],
    )
    assert create_logs_resp.status_code == status.HTTP_200_OK

    # 4) Retrieve all contacts (no filters)
    resp = await client.get("/v0/admin/contacts", headers=ADMIN_HEADERS)
    assert resp.status_code == status.HTTP_200_OK
    results = resp.json()
    assert isinstance(results, list) and len(results) == 2
    emails = {r["email_address"] for r in results}
    assert emails == {"john.doe@example.com", "jane.smith@example.com"}
    for r in results:
        assert r.get("user_id") == user["id"]
        assert "custom_fields" in r
        # Ensure core contact fields are present
        for field in ("first_name", "surname", "description"):
            assert field in r

    # 5) Filter by email_address
    resp2 = await client.get(
        f"/v0/admin/contacts?email_address={contact1['email_address']}",
        headers=ADMIN_HEADERS,
    )
    assert resp2.status_code == status.HTTP_200_OK
    filtered = resp2.json()
    assert len(filtered) == 1
    assert filtered[0]["email_address"] == contact1["email_address"]
