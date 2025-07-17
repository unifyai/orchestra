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
        "context": "Contacts",
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


@pytest.mark.anyio
async def test_admin_list_contacts_across_contexts(client: AsyncClient):
    # Create a test user and project
    user = await create_test_user(client, "multi@example.com")
    project_name = "multi_proj"
    create_proj = await client.post(
        "/v0/project",
        json={"name": project_name, "is_versioned": False, "description": None},
        headers=user["headers"],
    )
    assert create_proj.status_code == status.HTTP_200_OK

    # Prepare two distinct contact entries
    contact1 = {
        "first_name": "Alice",
        "surname": "Wonder",
        "email_address": "alice@ex.com",
        "phone_number": "5550001",
        "whatsapp_number": "5550002",
        "description": "First",
    }
    contact2 = {
        "first_name": "Bob",
        "surname": "Builder",
        "email_address": "bob@ex.com",
        "phone_number": "5550003",
        "whatsapp_number": "5550004",
        "description": "Second",
    }

    # Create log in top-level Contacts context
    payload1 = {
        "project": project_name,
        "context": "Contacts",
        "params": {},
        "entries": [contact1],
    }
    resp1 = await client.post(
        "/v0/logs",
        json=payload1,
        headers=user["headers"],
    )
    assert resp1.status_code == status.HTTP_200_OK

    # Create log in nested Friend/Contacts context
    payload2 = {
        "project": project_name,
        "context": "Friend/Contacts",
        "params": {},
        "entries": [contact2],
    }
    resp2 = await client.post(
        "/v0/logs",
        json=payload2,
        headers=user["headers"],
    )
    assert resp2.status_code == status.HTTP_200_OK

    # Both entries should be returned by admin_list_contacts (case-sensitive match on 'Contacts')
    resp = await client.get("/v0/admin/contacts", headers=ADMIN_HEADERS)
    assert resp.status_code == status.HTTP_200_OK
    results = resp.json()
    emails = {r.get("email_address") for r in results}
    assert emails == {contact1["email_address"], contact2["email_address"]}
    assert len(results) == 2


@pytest.mark.anyio
async def test_admin_list_contacts_multiple_users(client: AsyncClient):
    # Create two users and separate projects
    user1 = await create_test_user(client, "user1@example.com")
    user2 = await create_test_user(client, "user2@example.com")
    project1 = "proj_user1"
    project2 = "proj_user2"
    resp_proj1 = await client.post(
        "/v0/project",
        json={"name": project1, "is_versioned": False, "description": None},
        headers=user1["headers"],
    )
    assert resp_proj1.status_code == status.HTTP_200_OK
    resp_proj2 = await client.post(
        "/v0/project",
        json={"name": project2, "is_versioned": False, "description": None},
        headers=user2["headers"],
    )
    assert resp_proj2.status_code == status.HTTP_200_OK

    # Prepare contacts for each user
    contact1 = {
        "first_name": "Charlie",
        "surname": "Delta",
        "email_address": "charlie@ex.com",
        "phone_number": "5551001",
        "whatsapp_number": "5551002",
        "description": "Third",
    }
    contact2 = {
        "first_name": "Echo",
        "surname": "Foxtrot",
        "email_address": "echo@ex.com",
        "phone_number": "5552001",
        "whatsapp_number": "5552002",
        "description": "Fourth",
    }

    # Create log for user1 in Contacts context
    payload1 = {
        "project": project1,
        "context": "Contacts",
        "params": {},
        "entries": [contact1],
    }
    resp1 = await client.post(
        "/v0/logs",
        json=payload1,
        headers=user1["headers"],
    )
    assert resp1.status_code == status.HTTP_200_OK

    # Create log for user2 in Contacts context
    payload2 = {
        "project": project2,
        "context": "Contacts",
        "params": {},
        "entries": [contact2],
    }
    resp2 = await client.post(
        "/v0/logs",
        json=payload2,
        headers=user2["headers"],
    )
    assert resp2.status_code == status.HTTP_200_OK

    # Admin should retrieve both contacts regardless of owner
    resp = await client.get("/v0/admin/contacts", headers=ADMIN_HEADERS)
    assert resp.status_code == status.HTTP_200_OK
    results = resp.json()
    emails = {r["email_address"] for r in results}
    assert emails == {contact1["email_address"], contact2["email_address"]}
    user_ids = {r["user_id"] for r in results}
    assert user_ids == {user1["id"], user2["id"]}
    assert len(results) == 2

    # Filter by whatsapp_number to return only contact1
    resp_filtered = await client.get(
        f"/v0/admin/contacts?whatsapp_number={contact1['whatsapp_number']}",
        headers=ADMIN_HEADERS,
    )
    assert resp_filtered.status_code == status.HTTP_200_OK
    filtered = resp_filtered.json()
    assert isinstance(filtered, list)
    assert len(filtered) == 1
    assert filtered[0]["whatsapp_number"] == contact1["whatsapp_number"]
