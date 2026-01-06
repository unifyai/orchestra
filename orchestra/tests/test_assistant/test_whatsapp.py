from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status
from httpx import AsyncClient

from orchestra.tests.utils import ADMIN_HEADERS, HEADERS, create_test_user


@pytest.fixture(scope="function", autouse=True)
async def approve_default_user(client: AsyncClient):
    """Ensures the default test user for this module is approved for hiring."""
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    user_id = credits_resp.json()["id"]
    approve_url = f"/v0/admin/auth-user/{user_id}/assistant-hiring-approval/approved"
    approve_resp = await client.put(approve_url, headers=ADMIN_HEADERS)
    assert (
        approve_resp.status_code == status.HTTP_200_OK
    ), f"Failed to approve default user {user_id}: {approve_resp.json()}"


@pytest.fixture(autouse=True)
def mock_assistant_infra_calls(request):
    """
    Automatically mock assistant infrastructure webhooks for all tests.
    This prevents real network calls, making tests fast and reliable.
    """
    if "no_mock_infra" in request.keywords:
        yield
        return

    with patch(
        "orchestra.web.api.assistant.views.wake_up_assistant",
        new_callable=AsyncMock,
    ) as mock_wake_up, patch(
        "orchestra.web.api.assistant.views.reawaken_assistant",
        new_callable=AsyncMock,
    ) as mock_reawaken:

        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})

        yield mock_wake_up, mock_reawaken


# Helper to assign an available WhatsApp number not in use by any assistant for a given user
async def _assign_whatsapp_sender(
    client: AsyncClient,
    user_whatsapp_number: str,
    conflict_whatsapp_number: Optional[str] = None,
):
    resp = await client.get(
        f"/v0/admin/assistant?user_whatsapp_number={user_whatsapp_number}",
        headers=ADMIN_HEADERS,
    )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Failed to fetch assistants: {resp.text}",
        )
    data = resp.json()
    assigned_numbers = [
        a.get("assistant_whatsapp_number") for a in data.get("info", [])
    ]
    if conflict_whatsapp_number:
        assigned_numbers.append(conflict_whatsapp_number)
    # Manual list of all WhatsApp numbers for now
    # all_numbers = ["+18507877970", "+17343611691"]
    all_numbers = ["+5000000000", "+5000000001", "+5000000002", "+5000000003"]
    for num in all_numbers:
        if num not in assigned_numbers:
            return num
    raise HTTPException(status_code=400, detail="No available WhatsApp number found")


# Helper to detect conflict type for a target WhatsApp number
async def _get_conflict_whatsapp_number(
    client: AsyncClient,
    user_id: str,
    assistant_whatsapp_number: str,
    target_whatsapp_number: str,
):
    # Check for assistants matching both criteria
    resp = await client.get(
        f"/v0/admin/assistant?user_whatsapp_number={target_whatsapp_number}&assistant_whatsapp_number={assistant_whatsapp_number}",
        headers=ADMIN_HEADERS,
    )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Failed to fetch assistants: {resp.text}",
        )
    data = resp.json()
    if data.get("info"):
        return "both"
    # Check contacts for target number
    resp = await client.get(
        f"/v0/admin/contacts?whatsapp_number={target_whatsapp_number}",
        headers=ADMIN_HEADERS,
    )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Failed to fetch contacts: {resp.text}",
        )
    contacts = resp.json()
    user_ids = {c.get("user_id") for c in contacts}
    print(contacts)
    for uid in user_ids:
        if uid == user_id:
            continue
        resp2 = await client.get(
            f"/v0/admin/assistant/user/{uid}?assistant_whatsapp_number={assistant_whatsapp_number}",
            headers=ADMIN_HEADERS,
        )
        if resp2.status_code >= 400:
            raise HTTPException(
                status_code=resp2.status_code,
                detail=f"Failed to fetch assistants for user {uid}: {resp2.text}",
            )
        if resp2.json().get("info"):
            return "single"
    return "none"


# Tests
@pytest.mark.anyio
async def test_assistant_whatsapp_number_assignment_single_user(client: AsyncClient):
    # Create two assistants under the same user with distinct phone and user_phone
    user_phone = "+2000000001"
    assistant_payload1 = {
        "first_name": "Agent",
        "surname": "One",
        "age": 30,
        "weekly_limit": 10.0,
        "max_parallel": 2,
        "nationality": "Nationality1",
        "about": "First test assistant",
        "phone": "+1000000001",
        "user_phone": user_phone,
        "create_infra": False,
    }
    resp1 = await client.post(
        "/v0/assistant",
        json=assistant_payload1,
        headers=HEADERS,
    )
    assert resp1.status_code == status.HTTP_200_OK
    aid1 = resp1.json()["info"]["agent_id"]

    assistant_payload2 = {
        "first_name": "Agent",
        "surname": "Two",
        "age": 35,
        "weekly_limit": 15.0,
        "max_parallel": 3,
        "nationality": "Nationality2",
        "about": "Second test assistant",
        "phone": "+1000000002",
        "user_phone": user_phone,
        "create_infra": False,
    }
    resp2 = await client.post(
        "/v0/assistant",
        json=assistant_payload2,
        headers=HEADERS,
    )
    assert resp2.status_code == status.HTTP_200_OK
    aid2 = resp2.json()["info"]["agent_id"]

    # Patch both assistants to the same WhatsApp number via admin endpoint filtering by phone
    user_whatsapp = "+3000000001"
    assigned_whatsapp_numbers = []
    for payload, aid in [(assistant_payload1, aid1), (assistant_payload2, aid2)]:
        patch_resp = await client.patch(
            f"/v0/admin/assistant?phone={payload['phone']}&new_user_whatsapp_number={user_whatsapp}",
            headers=ADMIN_HEADERS,
        )
        assert patch_resp.status_code == status.HTTP_200_OK
        updated = patch_resp.json()["info"]
        assert updated["agent_id"] == aid
        assert updated["user_whatsapp_number"] == user_whatsapp

        ws_number = await _assign_whatsapp_sender(client, user_whatsapp)
        assigned_whatsapp_numbers.append(ws_number)
        patch_resp = await client.patch(
            f"/v0/admin/assistant?phone={payload['phone']}&new_assistant_whatsapp_number={ws_number}",
            headers=ADMIN_HEADERS,
        )
        assert patch_resp.status_code == status.HTTP_200_OK
        updated = patch_resp.json()["info"]
        assert updated["agent_id"] == aid
        assert updated["assistant_whatsapp_number"] == ws_number

    # Test assign whatsapp number with helper simulating comms endpoint
    assert assigned_whatsapp_numbers == ["+5000000000", "+5000000001"]


@pytest.mark.anyio
async def test_assistant_whatsapp_number_assignment_multiple_users(client: AsyncClient):
    # Create two assistants under the same user with distinct phone and user_phone
    assistant_payload1 = {
        "first_name": "Agent",
        "surname": "One",
        "age": 30,
        "weekly_limit": 10.0,
        "max_parallel": 2,
        "nationality": "Nationality1",
        "about": "First test assistant",
        "phone": "+1000000001",
        "user_phone": "+2000000001",
        "create_infra": False,
    }
    resp1 = await client.post(
        "/v0/assistant",
        json=assistant_payload1,
        headers=HEADERS,
    )
    assert resp1.status_code == status.HTTP_200_OK
    aid1 = resp1.json()["info"]["agent_id"]

    assistant_payload2 = {
        "first_name": "Agent",
        "surname": "Two",
        "age": 35,
        "weekly_limit": 15.0,
        "max_parallel": 3,
        "nationality": "Nationality2",
        "about": "Second test assistant",
        "phone": "+1000000002",
        "user_phone": "+2000000002",
        "create_infra": False,
    }
    resp2 = await client.post(
        "/v0/assistant",
        json=assistant_payload2,
        headers=HEADERS,
    )
    assert resp2.status_code == status.HTTP_200_OK
    aid2 = resp2.json()["info"]["agent_id"]

    # Patch both assistants to the same WhatsApp number via admin endpoint filtering by phone
    assigned_whatsapp_numbers = []
    for payload, aid, user_whatsapp in [
        (assistant_payload1, aid1, "+3000000001"),
        (assistant_payload2, aid2, "+3000000002"),
    ]:
        patch_resp = await client.patch(
            f"/v0/admin/assistant?phone={payload['phone']}&new_user_whatsapp_number={user_whatsapp}",
            headers=ADMIN_HEADERS,
        )
        assert patch_resp.status_code == status.HTTP_200_OK
        updated = patch_resp.json()["info"]
        assert updated["agent_id"] == aid
        assert updated["user_whatsapp_number"] == user_whatsapp

        ws_number = await _assign_whatsapp_sender(client, user_whatsapp)
        assigned_whatsapp_numbers.append(ws_number)
        patch_resp = await client.patch(
            f"/v0/admin/assistant?phone={payload['phone']}&new_assistant_whatsapp_number={ws_number}",
            headers=ADMIN_HEADERS,
        )
        assert patch_resp.status_code == status.HTTP_200_OK
        updated = patch_resp.json()["info"]
        assert updated["agent_id"] == aid
        assert updated["assistant_whatsapp_number"] == ws_number

    # Test assign whatsapp number with helper simulating comms endpoint
    assert assigned_whatsapp_numbers == ["+5000000000", "+5000000000"]


@pytest.mark.anyio
async def test_assistant_whatsapp_conflict_both(client: AsyncClient):
    # Create two assistants under the same user with distinct phone and user_phone
    assistant_payload1 = {
        "first_name": "Agent",
        "surname": "One",
        "age": 30,
        "weekly_limit": 10.0,
        "max_parallel": 2,
        "nationality": "Nationality1",
        "about": "First test assistant",
        "phone": "+1000000001",
        "user_phone": "+2000000001",
        "create_infra": False,
    }
    resp1 = await client.post(
        "/v0/assistant",
        json=assistant_payload1,
        headers=HEADERS,
    )
    assert resp1.status_code == status.HTTP_200_OK
    aid1 = resp1.json()["info"]["agent_id"]

    assistant_payload2 = {
        "first_name": "Agent",
        "surname": "Two",
        "age": 35,
        "weekly_limit": 15.0,
        "max_parallel": 3,
        "nationality": "Nationality2",
        "about": "Second test assistant",
        "phone": "+1000000002",
        "user_phone": "+2000000002",
        "create_infra": False,
    }
    resp2 = await client.post(
        "/v0/assistant",
        json=assistant_payload2,
        headers=HEADERS,
    )
    assert resp2.status_code == status.HTTP_200_OK
    aid2 = resp2.json()["info"]["agent_id"]

    # Patch both assistants to the same WhatsApp number via admin endpoint filtering by phone
    assigned_whatsapp_numbers = []
    for payload, aid, user_whatsapp in [
        (assistant_payload1, aid1, "+3000000001"),
        (assistant_payload2, aid2, "+3000000002"),
    ]:
        patch_resp = await client.patch(
            f"/v0/admin/assistant?phone={payload['phone']}&new_user_whatsapp_number={user_whatsapp}",
            headers=ADMIN_HEADERS,
        )
        assert patch_resp.status_code == status.HTTP_200_OK
        updated = patch_resp.json()["info"]
        assert updated["agent_id"] == aid
        assert updated["user_whatsapp_number"] == user_whatsapp

        ws_number = await _assign_whatsapp_sender(client, user_whatsapp)
        assigned_whatsapp_numbers.append(ws_number)
        patch_resp = await client.patch(
            f"/v0/admin/assistant?phone={payload['phone']}&new_assistant_whatsapp_number={ws_number}",
            headers=ADMIN_HEADERS,
        )
        assert patch_resp.status_code == status.HTTP_200_OK
        updated = patch_resp.json()["info"]
        assert updated["agent_id"] == aid
        assert updated["assistant_whatsapp_number"] == ws_number

    # Test assign whatsapp number with helper simulating comms endpoint
    assert assigned_whatsapp_numbers == ["+5000000000", "+5000000000"]

    # Check conflict
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    assert credits_resp.status_code == 200
    user_id = credits_resp.json()["id"]
    conflict = await _get_conflict_whatsapp_number(
        client,
        user_id,
        "+5000000000",
        "+3000000002",
    )
    assert conflict == "both"

    new_ws_number1 = await _assign_whatsapp_sender(client, "+3000000001")
    assert new_ws_number1 == "+5000000001"
    new_ws_number2 = await _assign_whatsapp_sender(
        client,
        "+3000000001",
        conflict_whatsapp_number=new_ws_number1,
    )
    assert new_ws_number2 == "+5000000002"


@pytest.mark.anyio
async def test_assistant_whatsapp_conflict_none_non_sharing_contact(
    client: AsyncClient,
):
    # Create a single assistant under the default test user
    user_phone = "+2000000001"
    assistant_payload = {
        "first_name": "Agent",
        "surname": "Conflict",
        "age": 30,
        "weekly_limit": 5.0,
        "max_parallel": 1,
        "nationality": "ConflictNationality",
        "about": "Testing conflict assistant",
        "phone": "+1000000001",
        "user_phone": user_phone,
        "create_infra": False,
    }
    resp = await client.post(
        "/v0/assistant",
        json=assistant_payload,
        headers=HEADERS,
    )
    assert resp.status_code == status.HTTP_200_OK
    aid = resp.json()["info"]["agent_id"]

    # Assign an available WhatsApp number and update assistant
    patch_resp = await client.patch(
        f"/v0/admin/assistant?phone={assistant_payload['phone']}&new_user_whatsapp_number={user_phone}",
        headers=ADMIN_HEADERS,
    )
    assert patch_resp.status_code == status.HTTP_200_OK
    updated = patch_resp.json()["info"]
    assert updated["agent_id"] == aid
    assert updated["user_whatsapp_number"] == user_phone

    assigned_ws = await _assign_whatsapp_sender(client, user_phone)
    assert assigned_ws == "+5000000000"

    # Assign the WhatsApp number to the assistant
    patch_resp = await client.patch(
        f"/v0/admin/assistant?phone={assistant_payload['phone']}&new_assistant_whatsapp_number={assigned_ws}",
        headers=ADMIN_HEADERS,
    )
    assert patch_resp.status_code == status.HTTP_200_OK
    updated = patch_resp.json()["info"]
    assert updated["agent_id"] == aid
    assert updated["assistant_whatsapp_number"] == assigned_ws

    # Create a project and log a single contact for this user
    project = "proj_conflict_test"
    proj_resp = await client.post(
        "/v0/project",
        json={"name": project, "is_versioned": False, "description": None},
        headers=HEADERS,
    )
    assert proj_resp.status_code == status.HTTP_200_OK

    contact_whatsapp = "+2000000002"
    contact = {
        "first_name": "Solo",
        "surname": "Contact",
        "email_address": "solo.contact@example.com",
        "phone_number": "9998887777",
        "whatsapp_number": contact_whatsapp,
        "description": "Single contact after assignment",
    }
    log_resp = await client.post(
        "/v0/logs",
        json={
            "project": project,
            "context": "Contacts",
            "params": {},
            "entries": [contact],
        },
        headers=HEADERS,
    )
    assert log_resp.status_code == status.HTTP_200_OK

    # Check conflict
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    assert credits_resp.status_code == 200
    user_id = credits_resp.json()["id"]
    conflict = await _get_conflict_whatsapp_number(
        client,
        user_id,
        assigned_ws,
        contact_whatsapp,
    )
    assert conflict == "none"


@pytest.mark.anyio
async def test_assistant_whatsapp_conflict_single_sharing_contact(client: AsyncClient):
    # Create a single assistant under the default test user
    user_phone = "+2000000001"
    assistant_payload = {
        "first_name": "Agent",
        "surname": "Conflict",
        "age": 30,
        "weekly_limit": 5.0,
        "max_parallel": 1,
        "nationality": "ConflictNationality",
        "about": "Testing conflict assistant",
        "phone": "+1000000001",
        "user_phone": user_phone,
        "create_infra": False,
    }
    resp = await client.post(
        "/v0/assistant",
        json=assistant_payload,
        headers=HEADERS,
    )
    assert resp.status_code == status.HTTP_200_OK
    aid = resp.json()["info"]["agent_id"]

    # Assign an available WhatsApp number and update assistant
    patch_resp = await client.patch(
        f"/v0/admin/assistant?phone={assistant_payload['phone']}&new_user_whatsapp_number={user_phone}",
        headers=ADMIN_HEADERS,
    )
    assert patch_resp.status_code == status.HTTP_200_OK
    updated = patch_resp.json()["info"]
    assert updated["agent_id"] == aid
    assert updated["user_whatsapp_number"] == user_phone

    assigned_ws = await _assign_whatsapp_sender(client, user_phone)
    assert assigned_ws == "+5000000000"

    patch_resp = await client.patch(
        f"/v0/admin/assistant?phone={assistant_payload['phone']}&new_assistant_whatsapp_number={assigned_ws}",
        headers=ADMIN_HEADERS,
    )
    assert patch_resp.status_code == status.HTTP_200_OK
    updated = patch_resp.json()["info"]
    assert updated["agent_id"] == aid
    assert updated["assistant_whatsapp_number"] == assigned_ws

    # Create a project and log a single contact for this user
    project = "proj_conflict_test"
    proj_resp = await client.post(
        "/v0/project",
        json={"name": project, "is_versioned": False, "description": None},
        headers=HEADERS,
    )
    assert proj_resp.status_code == status.HTTP_200_OK

    contact_whatsapp = "+2000000002"
    contact = {
        "first_name": "Solo",
        "surname": "Contact",
        "email_address": "solo.contact@example.com",
        "phone_number": "9998887777",
        "whatsapp_number": contact_whatsapp,
        "description": "Single contact after assignment",
    }
    log_resp = await client.post(
        "/v0/logs",
        json={
            "project": project,
            "context": "Contacts",
            "params": {},
            "entries": [contact],
        },
        headers=HEADERS,
    )
    assert log_resp.status_code == status.HTTP_200_OK

    # Check conflict
    user = await create_test_user(
        client,
        "whatsapp_conflict_none@example.com",
        hiring_approved=True,
    )
    user_id = user["id"]
    conflict = await _get_conflict_whatsapp_number(
        client,
        user_id,
        assigned_ws,
        contact_whatsapp,
    )
    assert conflict == "single"

    new_ws_number = await _assign_whatsapp_sender(
        client,
        user_phone,
        conflict_whatsapp_number=assigned_ws,
    )
    assert new_ws_number == "+5000000001"
