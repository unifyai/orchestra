import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.tests.utils import ADMIN_HEADERS, HEADERS, create_test_user


@pytest.fixture(scope="function", autouse=True)
async def approve_default_user(client: AsyncClient):
    """Ensures the default test user for this module is approved for hiring."""
    # Get the user ID associated with the default HEADERS
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    user_id = credits_resp.json()["id"]

    # Approve the user
    approve_url = f"/v0/admin/auth-user/{user_id}/assistant-hiring-approval/approved"
    approve_resp = await client.put(approve_url, headers=ADMIN_HEADERS)
    assert (
        approve_resp.status_code == status.HTTP_200_OK
    ), f"Failed to approve default user {user_id}: {approve_resp.json()}"


def _get_sample_wav_bytes() -> bytes:
    sample_path = Path(__file__).parent / "sample_datasets" / "sample_recording.wav"
    return sample_path.read_bytes()


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
    ) as mock_wake_up, patch(
        "orchestra.web.api.assistant.views.reawaken_assistant",
    ) as mock_reawaken:

        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})

        yield mock_wake_up, mock_reawaken


@pytest.mark.anyio
@patch(
    "orchestra.db.dao.assistant_dao.send_unify_message",
    return_value={"status": "success"},
)
async def test_message_assistant_success(mock_send_message, client: AsyncClient):
    # TODO: Add test when the endpoint logic is updated to avoid
    # relying on the Transcripts to fetch the assistant response
    pass


@pytest.mark.anyio
async def test_create_assistant_unapproved_user_fails(client: AsyncClient):
    """Test that a user who is not approved cannot create an assistant."""
    unapproved_user = await create_test_user(
        client,
        "unapproved@example.com",
        hiring_approved=False,
    )
    payload = {
        "first_name": "Should",
        "surname": "Fail",
        "create_infra": False,
    }
    resp = await client.post(
        "/v0/assistant",
        json=payload,
        headers=unapproved_user["headers"],
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert (
        "You need to request approval first by going to console.unify.ai/assistants"
        in resp.json()["detail"]
    )


@pytest.mark.anyio
async def test_create_assistant_success(client: AsyncClient):
    # `POST /v0/assistant` with full payload -> 200 OK and returns created assistant
    payload = {
        "first_name": "Alice",
        "surname": "Smith",
        "age": 28,
        "weekly_limit": 15.5,
        "max_parallel": 3,
        "nationality": "United States",
        "profile_photo": "https://example.com/photos/alice.jpg",
        "about": "AI researcher specializing in natural language processing",
        "timezone": "America/New_York",
        "create_infra": False,
    }
    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert "info" in body
    data = body["info"]
    assert isinstance(data.get("agent_id"), str)
    assert data["first_name"] == payload["first_name"]
    assert data["surname"] == payload["surname"]
    assert data["age"] == payload["age"]
    assert data["timezone"] == payload["timezone"]
    assert isinstance(data["weekly_limit"], float)
    assert data["weekly_limit"] == payload["weekly_limit"]
    assert data["max_parallel"] == payload["max_parallel"]
    assert data["nationality"] == payload["nationality"]
    assert data["profile_photo"] == payload["profile_photo"]
    assert data["about"] == payload["about"]
    assert data["phone"] is None
    assert data["email"] is None
    assert isinstance(data.get("created_at"), str)
    assert "updated_at" in data


@pytest.mark.anyio
async def test_create_assistant_missing_field(client: AsyncClient):
    # `POST /v0/assistant` missing surname -> 200 OK (as it's optional)
    payload = {
        "first_name": "Bob",
        # surname omitted
        "age": 30,
        "weekly_limit": 10.0,
        "max_parallel": 2,
        "create_infra": False,
    }
    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_list_assistants_empty(client: AsyncClient):
    # `GET /v0/assistant` with no assistants -> 200 OK and empty list
    resp = await client.get("/v0/assistant", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"info": []}


@pytest.mark.anyio
async def test_list_assistants_after_create(client: AsyncClient):
    # Create two assistants then `GET /v0/assistant` -> list of two
    payload1 = {
        "first_name": "Carol",
        "surname": "Jones",
        "age": 22,
        "weekly_limit": 12.0,
        "max_parallel": 1,
        "nationality": "Germany",
        "profile_photo": "https://example.com/photos/carol.jpg",
        "about": "Data scientist with expertise in statistical modeling",
        "timezone": "Europe/Berlin",
        "create_infra": False,
    }
    payload2 = {
        "first_name": "Dave",
        "surname": "Lee",
        "age": 35,
        "weekly_limit": 20.0,
        "max_parallel": 5,
        "nationality": "China",
        "profile_photo": "https://example.com/photos/dave.jpg",
        "about": "Software engineer focused on distributed systems",
        "timezone": "Asia/Shanghai",
        "create_infra": False,
    }
    r1 = await client.post("/v0/assistant", json=payload1, headers=HEADERS)
    r2 = await client.post("/v0/assistant", json=payload2, headers=HEADERS)
    assert r1.status_code == 200 and r2.status_code == 200
    list_resp = await client.get("/v0/assistant", headers=HEADERS)
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert "info" in body
    data = body["info"]
    assert isinstance(data, list)
    assert len(data) == 2
    ids = {item["agent_id"] for item in data}
    assert {r1.json()["info"]["agent_id"], r2.json()["info"]["agent_id"]} == ids

    # Verify all assistants have the new fields
    for assistant in data:
        assert "nationality" in assistant
        assert "profile_photo" in assistant
        assert "about" in assistant
        assert "phone" in assistant
        assert "email" in assistant
        assert "timezone" in assistant
        # Default values for optional fields
        assert assistant["phone"] is None
        assert assistant["email"] is None


@pytest.mark.anyio
async def test_update_weekly_limit_only(client: AsyncClient):
    # Create assistant, then `PATCH /v0/assistant/{id}/config` weekly_limit only -> updated
    payload = {
        "first_name": "Eve",
        "surname": "Adams",
        "age": 40,
        "weekly_limit": 30.0,
        "max_parallel": 2,
        "nationality": "United States",
        "profile_photo": "https://example.com/photos/eve.jpg",
        "about": "Machine learning expert with focus on computer vision",
        "timezone": "America/Sao_Paulo",
        "create_infra": False,
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["info"]["agent_id"]
    new_limit = 45.5
    update_payload = {"weekly_limit": new_limit, "create_infra": False}
    patch = await client.patch(
        f"/v0/assistant/{aid}/config",
        json=update_payload,
        headers=HEADERS,
    )
    assert patch.status_code == 200
    updated = patch.json()["info"]
    assert updated["weekly_limit"] == new_limit
    assert updated["max_parallel"] == payload["max_parallel"]
    assert updated["first_name"] == payload["first_name"]
    assert updated["nationality"] == payload["nationality"]
    assert updated["profile_photo"] == payload["profile_photo"]
    assert updated["about"] == payload["about"]
    assert updated["timezone"] == payload["timezone"]
    assert updated["phone"] is None
    assert updated["email"] is None


@pytest.mark.anyio
async def test_update_max_parallel_only(client: AsyncClient):
    # Create assistant, then `PATCH /v0/assistant/{id}/config` max_parallel only -> updated
    payload = {
        "first_name": "Frank",
        "surname": "Miller",
        "age": 50,
        "weekly_limit": 25.0,
        "max_parallel": 4,
        "nationality": "Australia",
        "profile_photo": "https://example.com/photos/frank.jpg",
        "about": "Robotics engineer specializing in autonomous systems",
        "create_infra": False,
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["info"]["agent_id"]
    new_parallel = 7
    update_payload = {"max_parallel": new_parallel, "create_infra": False}
    patch = await client.patch(
        f"/v0/assistant/{aid}/config",
        json=update_payload,
        headers=HEADERS,
    )
    assert patch.status_code == 200
    updated = patch.json()["info"]
    assert updated["max_parallel"] == new_parallel
    assert updated["weekly_limit"] == payload["weekly_limit"]
    assert updated["surname"] == payload["surname"]
    assert updated["nationality"] == payload["nationality"]
    assert updated["profile_photo"] == payload["profile_photo"]
    assert updated["about"] == payload["about"]


@pytest.mark.anyio
async def test_update_timezone_only(client: AsyncClient):
    payload = {
        "first_name": "Timezone",
        "surname": "Tester",
        "age": 40,
        "weekly_limit": 30.0,
        "max_parallel": 2,
        "nationality": "United States",
        "about": "Testing timezone updates",
        "timezone": "America/Sao_Paulo",
        "create_infra": False,
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["info"]["agent_id"]
    new_timezone = "Europe/Lisbon"
    update_payload = {"timezone": new_timezone, "create_infra": False}
    patch = await client.patch(
        f"/v0/assistant/{aid}/config",
        json=update_payload,
        headers=HEADERS,
    )
    assert patch.status_code == 200
    updated = patch.json()["info"]
    assert updated["timezone"] == new_timezone
    assert updated["weekly_limit"] == payload["weekly_limit"]
    assert updated["first_name"] == payload["first_name"]
    assert updated["nationality"] == payload["nationality"]
    assert updated["about"] == payload["about"]


@pytest.mark.anyio
async def test_update_not_found(client: AsyncClient):
    # `PATCH /v0/assistant/9999/config` for non-existent -> 404 Not Found
    resp = await client.patch(
        "/v0/assistant/9999/config",
        json={"weekly_limit": 10},
        headers=HEADERS,
    )
    assert resp.status_code == 404
    assert resp.json().get("detail") == "Assistant not found."


@pytest.mark.anyio
async def test_delete_assistant_success(client: AsyncClient):
    # Create assistant, then `DELETE /v0/assistant/{id}` -> 200 OK and removed
    payload = {
        "first_name": "Grace",
        "surname": "Hopper",
        "age": 85,
        "weekly_limit": 50.0,
        "max_parallel": 1,
        "nationality": "United States",
        "profile_photo": "https://example.com/photos/grace.jpg",
        "about": "Computer scientist and pioneer in programming languages",
        "create_infra": False,
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["info"]["agent_id"]
    del_resp = await client.delete(f"/v0/assistant/{aid}", headers=HEADERS)
    assert del_resp.status_code == 200
    list_resp = await client.get("/v0/assistant", headers=HEADERS)
    assert all(item["agent_id"] != aid for item in list_resp.json()["info"])


@pytest.mark.anyio
async def test_delete_assistant_not_found(client: AsyncClient):
    # `DELETE /v0/assistant/9999` for non-existent -> 404 Not Found
    resp = await client.delete("/v0/assistant/9999", headers=HEADERS)
    assert resp.status_code == 404
    assert resp.json().get("detail") == "Assistant not found."


@pytest.mark.anyio
async def test_update_about_only(client: AsyncClient):
    # Create assistant, then `PATCH /v0/assistant/{id}/config` about only -> updated
    payload = {
        "first_name": "Hannah",
        "surname": "Kim",
        "age": 32,
        "weekly_limit": 35.0,
        "max_parallel": 3,
        "nationality": "China",
        "profile_photo": "https://example.com/photos/hannah.jpg",
        "about": "Original bio information",
        "create_infra": False,
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["info"]["agent_id"]
    new_about = "Updated bio with additional qualifications and expertise"
    update_payload = {"about": new_about, "create_infra": False}
    patch = await client.patch(
        f"/v0/assistant/{aid}/config",
        json=update_payload,
        headers=HEADERS,
    )
    assert patch.status_code == 200
    updated = patch.json()["info"]
    assert updated["about"] == new_about
    assert updated["first_name"] == payload["first_name"]
    assert updated["nationality"] == payload["nationality"]
    assert updated["phone"] is None
    assert updated["email"] is None


@pytest.mark.anyio
async def test_update_phone_only(client: AsyncClient):
    # Create assistant, then `PATCH /v0/assistant/{id}/config` phone only -> updated
    payload = {
        "first_name": "Ian",
        "surname": "Chen",
        "age": 45,
        "weekly_limit": 40.0,
        "max_parallel": 2,
        "nationality": "Germany",
        "profile_photo": "https://example.com/photos/ian.jpg",
        "about": "Cybersecurity expert with focus on network security",
        "create_infra": False,
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["info"]["agent_id"]
    new_phone = "+1-555-123-4567"
    update_payload = {"phone": new_phone, "create_infra": False}
    patch = await client.patch(
        f"/v0/assistant/{aid}/config",
        json=update_payload,
        headers=HEADERS,
    )
    assert patch.status_code == 200
    updated = patch.json()["info"]
    assert updated["phone"] == new_phone
    assert updated["email"] is None
    assert updated["about"] == payload["about"]
    assert updated["nationality"] == payload["nationality"]


@pytest.mark.anyio
async def test_update_email_only(client: AsyncClient):
    # Create assistant, then `PATCH /v0/assistant/{id}/config` email only -> updated
    payload = {
        "first_name": "Julia",
        "surname": "Garcia",
        "age": 38,
        "weekly_limit": 22.5,
        "max_parallel": 4,
        "nationality": "United States",
        "profile_photo": "https://example.com/photos/julia.jpg",
        "about": "Data engineer specializing in big data infrastructure",
        "create_infra": False,
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["info"]["agent_id"]
    new_email = "julia.garcia@example.com"
    update_payload = {"email": new_email, "create_infra": False}
    patch = await client.patch(
        f"/v0/assistant/{aid}/config",
        json=update_payload,
        headers=HEADERS,
    )
    assert patch.status_code == 200
    updated = patch.json()["info"]
    assert updated["email"] == new_email
    assert updated["phone"] is None
    assert updated["about"] == payload["about"]
    assert updated["weekly_limit"] == payload["weekly_limit"]


@pytest.mark.anyio
async def test_update_desktop_url_only(client: AsyncClient):
    # Create assistant, then `PATCH /v0/assistant/{id}/config` desktop_url only -> updated
    payload = {
        "first_name": "Desktop",
        "surname": "Updater",
        "age": 27,
        "weekly_limit": 12.0,
        "max_parallel": 2,
        "nationality": "Germany",
        "profile_photo": "https://example.com/photos/desktop.jpg",
        "about": "Testing desktop url update",
        "create_infra": False,
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert create.status_code == 200
    aid = create.json()["info"]["agent_id"]

    new_desktop_url = "https://app.example.com/assistants/desktop-updater"
    update_payload = {"desktop_url": new_desktop_url, "create_infra": False}
    patch = await client.patch(
        f"/v0/assistant/{aid}/config",
        json=update_payload,
        headers=HEADERS,
    )
    assert patch.status_code == 200
    updated = patch.json()["info"]
    assert updated["desktop_url"] == new_desktop_url


@pytest.mark.anyio
async def test_update_user_local_desktop_only(client: AsyncClient):
    # Create an assistant with some initial data, leaving user_local_desktop as default (None)
    payload = {
        "first_name": "Desktop",
        "surname": "Tester",
        "age": 31,
        "weekly_limit": 10.0,
        "max_parallel": 2,
        "nationality": "France",
        "about": "An assistant for testing desktop updates.",
        "create_infra": False,
    }
    create_resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert create_resp.status_code == 200
    created_data = create_resp.json()["info"]
    agent_id = created_data["agent_id"]
    assert created_data["user_local_desktop"] is None

    # Now, update only the user_local_desktop field
    new_desktop = "macos"
    update_payload = {"user_local_desktop": new_desktop, "create_infra": False}
    patch_resp = await client.patch(
        f"/v0/assistant/{agent_id}/config",
        json=update_payload,
        headers=HEADERS,
    )

    # Assert that the update was successful and only the intended field changed
    assert patch_resp.status_code == 200
    updated_data = patch_resp.json()["info"]
    assert updated_data["user_local_desktop"] == new_desktop
    assert updated_data["first_name"] == payload["first_name"]
    assert updated_data["nationality"] == payload["nationality"]
    assert updated_data["weekly_limit"] == payload["weekly_limit"]


@pytest.mark.anyio
async def test_update_multiple_fields(client: AsyncClient):
    # Create assistant, then `PATCH /v0/assistant/{id}/config` with multiple fields -> all updated
    payload = {
        "first_name": "Kevin",
        "surname": "Brown",
        "age": 29,
        "weekly_limit": 18.0,
        "max_parallel": 2,
        "nationality": "South Africa",
        "profile_photo": "https://example.com/photos/kevin.jpg",
        "about": "Original bio information",
        "timezone": "Africa/Nairobi",
        "create_infra": False,
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["info"]["agent_id"]
    update_payload = {
        "about": "Updated professional bio with new skills",
        "phone": "+1-555-987-6543",
        "email": "kevin.brown@example.com",
        "timezone": "UTC",
        "create_infra": False,
    }
    patch = await client.patch(
        f"/v0/assistant/{aid}/config",
        json=update_payload,
        headers=HEADERS,
    )
    assert patch.status_code == 200
    updated = patch.json()["info"]
    assert updated["about"] == update_payload["about"]
    assert updated["phone"] == update_payload["phone"]
    assert updated["email"] == update_payload["email"]
    assert updated["timezone"] == update_payload["timezone"]
    assert updated["first_name"] == payload["first_name"]
    assert updated["nationality"] == payload["nationality"]


@pytest.mark.anyio
async def test_assistant_recordings_audio_lifecycle(client: AsyncClient):
    # Create a new assistant
    payload = {
        "first_name": "Kevin",
        "surname": "Brown",
        "age": 29,
        "weekly_limit": 18.0,
        "max_parallel": 2,
        "nationality": "South Africa",
        "profile_photo": "https://example.com/photos/kevin.jpg",
        "about": "Original bio information",
        "create_infra": False,
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert create.status_code == 200
    assistant_info = create.json()["info"]
    agent_id = assistant_info["agent_id"]
    user_id = assistant_info["user_id"]

    # Read and encode sample WAV file
    raw_bytes = _get_sample_wav_bytes()
    b64_audio = base64.b64encode(raw_bytes).decode()

    # Upload raw recording
    record_payload = {
        "user_id": user_id,
        "assistant_id": agent_id,
        "conference_name": "test-conference-name",
        "recording_raw": b64_audio,
        "content_type": "audio/wav",
    }

    record_resp = await client.post(
        "/v0/admin/assistant/recordings",
        headers=ADMIN_HEADERS,
        json=record_payload,
    )
    assert record_resp.status_code == 200
    recording_info = record_resp.json()["info"]
    rec_id = recording_info["id"]
    assert isinstance(recording_info.get("url"), str) and recording_info.get(
        "url",
    ).startswith("http")

    # Verify recording is listed
    list_resp = await client.get(
        f"/v0/assistant/{agent_id}/recordings",
        headers=HEADERS,
    )
    assert list_resp.status_code == 200
    recordings = list_resp.json()["info"]
    assert isinstance(recordings, list) and len(recordings) == 1
    listed = recordings[0]
    assert listed["id"] == rec_id

    # Delete the recording
    delete_resp = await client.delete(
        f"/v0/assistant/{agent_id}/recordings/{rec_id}",
        headers=HEADERS,
    )
    assert delete_resp.status_code == 200

    # Confirm removal
    list_after_del = await client.get(
        f"/v0/assistant/{agent_id}/recordings",
        headers=HEADERS,
    )
    assert list_after_del.status_code == 200
    assert list_after_del.json()["info"] == []


@pytest.mark.anyio
async def test_admin_list_assistant_emails(client: AsyncClient):
    # Create two assistants
    payload1 = {
        "first_name": "Laura",
        "surname": "Wilson",
        "age": 33,
        "weekly_limit": 25.0,
        "max_parallel": 3,
        "nationality": "Germany",
        "profile_photo": "https://example.com/photos/laura.jpg",
        "about": "AI ethics researcher with focus on fairness in algorithms",
        "create_infra": False,
    }
    payload2 = {
        "first_name": "Michael",
        "surname": "Taylor",
        "age": 41,
        "weekly_limit": 30.0,
        "max_parallel": 4,
        "nationality": "United States",
        "profile_photo": "https://example.com/photos/michael.jpg",
        "about": "Cloud architecture specialist with expertise in distributed systems",
        "create_infra": False,
    }

    # Create the assistants
    resp1 = await client.post("/v0/assistant", json=payload1, headers=HEADERS)
    resp2 = await client.post("/v0/assistant", json=payload2, headers=HEADERS)
    assert resp1.status_code == 200 and resp2.status_code == 200

    # Get the assistant IDs
    aid1 = resp1.json()["info"]["agent_id"]
    aid2 = resp2.json()["info"]["agent_id"]

    # Set unique emails for each assistant
    email1 = "laura.wilson@example.com"
    email2 = "michael.taylor@example.com"

    # Update the assistants with emails
    update1 = await client.patch(
        f"/v0/assistant/{aid1}/config",
        json={"email": email1, "create_infra": False},
        headers=HEADERS,
    )
    update2 = await client.patch(
        f"/v0/assistant/{aid2}/config",
        json={"email": email2, "create_infra": False},
        headers=HEADERS,
    )
    assert update1.status_code == 200 and update2.status_code == 200

    # Test the admin endpoint for listing all assistant emails
    resp = await client.get("/v0/admin/assistant/emails", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    data = resp.json().get("info")
    assert isinstance(data, list)
    assert set(data) == {email1, email2}


@pytest.mark.anyio
async def test_search_assistants_by_phone(client: AsyncClient):
    # Create two assistants with distinct phone values, search by phone
    payload1 = {
        "first_name": "Paul",
        "surname": "Anderson",
        "age": 31,
        "weekly_limit": 15.0,
        "max_parallel": 2,
        "nationality": "Germany",
        "profile_photo": "https://example.com/photos/paul.jpg",
        "about": "Mobile app developer",
        "phone": "+15551112222",
        "create_infra": False,
    }
    payload2 = {
        "first_name": "Quinn",
        "surname": "Davis",
        "age": 26,
        "weekly_limit": 18.0,
        "max_parallel": 1,
        "nationality": "China",
        "profile_photo": "https://example.com/photos/quinn.jpg",
        "about": "UX designer",
        "phone": "+15553334444",
        "create_infra": False,
    }

    resp1 = await client.post("/v0/assistant", json=payload1, headers=HEADERS)
    resp2 = await client.post("/v0/assistant", json=payload2, headers=HEADERS)
    assert resp1.status_code == 200 and resp2.status_code == 200

    aid1 = resp1.json()["info"]["agent_id"]

    # Search by first assistant's phone
    search_resp = await client.get(
        f"/v0/assistant?phone={payload1['phone']}",
        headers=HEADERS,
    )
    assert search_resp.status_code == 200
    results = search_resp.json()["info"]
    assert len(results) == 1
    assert results[0]["agent_id"] == aid1
    assert results[0]["phone"] == payload1["phone"]


@pytest.mark.anyio
async def test_search_assistants_by_email(client: AsyncClient):
    # Create two assistants with distinct email values, search by email
    payload1 = {
        "first_name": "Rachel",
        "surname": "Martinez",
        "age": 29,
        "weekly_limit": 22.0,
        "max_parallel": 3,
        "nationality": "United States",
        "profile_photo": "https://example.com/photos/rachel.jpg",
        "about": "Backend developer",
        "email": "rachel.martinez@example.com",
        "create_infra": False,
    }
    payload2 = {
        "first_name": "Sam",
        "surname": "Johnson",
        "age": 37,
        "weekly_limit": 25.0,
        "max_parallel": 4,
        "nationality": "Australia",
        "profile_photo": "https://example.com/photos/sam.jpg",
        "about": "DevOps engineer",
        "email": "sam.johnson@example.com",
        "create_infra": False,
    }

    resp1 = await client.post("/v0/assistant", json=payload1, headers=HEADERS)
    resp2 = await client.post("/v0/assistant", json=payload2, headers=HEADERS)
    assert resp1.status_code == 200 and resp2.status_code == 200

    aid2 = resp2.json()["info"]["agent_id"]

    # Search by second assistant's email
    search_resp = await client.get(
        f"/v0/assistant?email={payload2['email']}",
        headers=HEADERS,
    )
    assert search_resp.status_code == 200
    results = search_resp.json()["info"]
    assert len(results) == 1
    assert results[0]["agent_id"] == aid2
    assert results[0]["email"] == payload2["email"]


@pytest.mark.anyio
async def test_admin_list_assistants_filter_phone(client: AsyncClient):
    # Create assistants with different phone values, filter by phone
    payload1 = {
        "first_name": "Phone",
        "surname": "Test1",
        "age": 28,
        "weekly_limit": 15.0,
        "max_parallel": 1,
        "nationality": "China",
        "profile_photo": "https://example.com/photos/phone1.jpg",
        "about": "Phone test assistant 1",
        "phone": "+15551111111",
        "create_infra": False,
    }
    payload2 = {
        "first_name": "Phone",
        "surname": "Test2",
        "age": 32,
        "weekly_limit": 18.0,
        "max_parallel": 2,
        "nationality": "Australia",
        "profile_photo": "https://example.com/photos/phone2.jpg",
        "about": "Phone test assistant 2",
        "phone": "+15552222222",
        "create_infra": False,
    }

    resp1 = await client.post("/v0/assistant", json=payload1, headers=HEADERS)
    resp2 = await client.post("/v0/assistant", json=payload2, headers=HEADERS)
    assert resp1.status_code == 200 and resp2.status_code == 200

    aid1 = resp1.json()["info"]["agent_id"]

    # Test admin endpoint with phone filter
    admin_resp = await client.get(
        f"/v0/admin/assistant?phone={payload1['phone']}",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    body = admin_resp.json()
    assert "info" in body
    results = body["info"]
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0]["agent_id"] == aid1
    assert results[0]["phone"] == payload1["phone"]


@pytest.mark.anyio
async def test_admin_list_assistants_filter_email(client: AsyncClient):
    # Create assistants with different email values, filter by email
    payload1 = {
        "first_name": "Email",
        "surname": "Test1",
        "age": 26,
        "weekly_limit": 12.0,
        "max_parallel": 1,
        "nationality": "United States",
        "profile_photo": "https://example.com/photos/email1.jpg",
        "about": "Email test assistant 1",
        "email": "email.test1@example.com",
        "create_infra": False,
    }
    payload2 = {
        "first_name": "Email",
        "surname": "Test2",
        "age": 40,
        "weekly_limit": 30.0,
        "max_parallel": 4,
        "nationality": "South Africa",
        "profile_photo": "https://example.com/photos/email2.jpg",
        "about": "Email test assistant 2",
        "email": "email.test2@example.com",
        "create_infra": False,
    }

    resp1 = await client.post("/v0/assistant", json=payload1, headers=HEADERS)
    resp2 = await client.post("/v0/assistant", json=payload2, headers=HEADERS)
    assert resp1.status_code == 200 and resp2.status_code == 200

    aid2 = resp2.json()["info"]["agent_id"]

    # Test admin endpoint with email filter
    admin_resp = await client.get(
        f"/v0/admin/assistant?email={payload2['email']}",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    body = admin_resp.json()
    assert "info" in body
    results = body["info"]
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0]["agent_id"] == aid2
    assert results[0]["email"] == payload2["email"]


@pytest.mark.anyio
async def test_admin_list_assistants_filter_agent_id(client: AsyncClient):
    # Create two assistants, filter by agent_id to return exactly one
    payload1 = {
        "first_name": "Agent",
        "surname": "One",
        "age": 21,
        "weekly_limit": 10.0,
        "max_parallel": 1,
        "nationality": "Test",
        "profile_photo": "https://example.com/a1.jpg",
        "about": "Assistant One",
        "create_infra": False,
    }
    payload2 = {
        "first_name": "Agent",
        "surname": "Two",
        "age": 22,
        "weekly_limit": 11.0,
        "max_parallel": 2,
        "nationality": "Test",
        "profile_photo": "https://example.com/a2.jpg",
        "about": "Assistant Two",
        "create_infra": False,
    }

    resp1 = await client.post("/v0/assistant", json=payload1, headers=HEADERS)
    resp2 = await client.post("/v0/assistant", json=payload2, headers=HEADERS)
    assert resp1.status_code == 200 and resp2.status_code == 200

    aid1 = resp1.json()["info"]["agent_id"]

    # Filter by the first agent's id
    admin_resp = await client.get(
        f"/v0/admin/assistant?agent_id={aid1}",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    body = admin_resp.json()
    assert "info" in body
    results = body["info"]
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0]["agent_id"] == aid1


@pytest.mark.anyio
async def test_admin_list_assistants_for_user(client: AsyncClient):
    # Create a second test user via create_test_user
    # (default HEADERS user will serve as user1)
    user2 = await create_test_user(client, "u2@test.com", hiring_approved=True)

    # Determine default user ID for HEADERS (user1)
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    assert credits_resp.status_code == 200
    user1_id = credits_resp.json()["id"]

    # Create assistant for user1
    payload1 = {
        "first_name": "UserOne",
        "surname": "Tester",
        "age": 30,
        "weekly_limit": 10.0,
        "max_parallel": 1,
        "nationality": "Testland",
        "profile_photo": "https://example.com/u1.jpg",
        "about": "Assistant for user1",
        "create_infra": False,
    }
    resp1 = await client.post(
        "/v0/assistant",
        json=payload1,
        headers=HEADERS,
    )
    assert resp1.status_code == 200
    aid1 = resp1.json()["info"]["agent_id"]

    # Do not create assistant for user2; expect no assistants for user2

    # Verify admin endpoint returns only user1's assistants
    res1 = await client.get(
        f"/v0/admin/assistant/user/{user1_id}",
        headers=ADMIN_HEADERS,
    )
    assert res1.status_code == 200
    info1 = res1.json()["info"]
    assert len(info1) == 1 and info1[0]["agent_id"] == aid1

    # Verify admin endpoint returns no assistants for user2
    res2 = await client.get(
        f"/v0/admin/assistant/user/{user2['id']}",
        headers=ADMIN_HEADERS,
    )
    assert res2.status_code == 200
    info2 = res2.json()["info"]
    assert isinstance(info2, list)
    assert len(info2) == 0


@pytest.mark.anyio
async def test_admin_update_assistant_whatsapp_number_and_user_whatsapp(
    client: AsyncClient,
):
    # Create two assistants with distinct phone, user_phone, and user_whatsapp_number for filtering
    initial_phone1 = "+15550000001"
    initial_phone2 = "+15550000002"

    payload1 = {
        "first_name": "Alice",
        "surname": "Example",
        "age": 25,
        "weekly_limit": 5.0,
        "max_parallel": 1,
        "nationality": "Testland",
        "profile_photo": "https://example.com/a1.jpg",
        "about": "First assistant",
        "phone": initial_phone1,
        "create_infra": False,
    }
    resp1 = await client.post(
        "/v0/assistant",
        json=payload1,
        headers=HEADERS,
    )
    assert resp1.status_code == 200
    aid1 = resp1.json()["info"]["agent_id"]

    payload2 = {
        "first_name": "Bob",
        "surname": "Example",
        "age": 28,
        "weekly_limit": 6.0,
        "max_parallel": 1,
        "nationality": "Testland",
        "profile_photo": "https://example.com/a2.jpg",
        "about": "Second assistant",
        "phone": initial_phone2,
        "create_infra": False,
    }
    resp2 = await client.post(
        "/v0/assistant",
        json=payload2,
        headers=HEADERS,
    )
    assert resp2.status_code == 200
    aid2 = resp2.json()["info"]["agent_id"]

    # Now test admin_update_assistant filtering
    new_assistant_whatsapp = "+15551234567"
    update_resp = await client.patch(
        f"/v0/admin/assistant?phone={initial_phone1}&new_assistant_whatsapp_number={new_assistant_whatsapp}",
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 200
    updated_info = update_resp.json()["info"]
    assert updated_info["agent_id"] == aid1
    assert updated_info["assistant_whatsapp_number"] == new_assistant_whatsapp

    # Verify via listing endpoint that only the updated assistant is returned by the new filters
    list_resp = await client.get(
        f"/v0/admin/assistant?phone={initial_phone1}&assistant_whatsapp_number={new_assistant_whatsapp}",
        headers=ADMIN_HEADERS,
    )
    assert list_resp.status_code == 200
    infos = list_resp.json()["info"]
    assert len(infos) == 1
    assert infos[0]["agent_id"] == aid1
    assert all(i["agent_id"] != aid2 for i in infos)


@pytest.mark.anyio
async def test_create_assistant_duplicate_name_fails(
    client: AsyncClient,
    dbsession,
):
    # `POST /v0/assistant` with a duplicate name for the same user should fail.
    payload = {
        "first_name": "David",
        "surname": "Miller",
        "age": 35,
        "weekly_limit": 20.0,
        "max_parallel": 2,
        "nationality": "United States",
        "about": "A test assistant.",
        "create_infra": False,
    }

    # First creation should succeed
    resp1 = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp1.status_code == 200, f"First assistant creation failed: {resp1.text}"

    # Second creation with the same name for the same user should fail
    resp2 = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp2.status_code == 409
    body = resp2.json()
    assert "detail" in body
    expected_error = f"An assistant with the name '{payload['first_name']} {payload['surname']}' already exists for this user."
    assert body["detail"] == expected_error

    # Verify that a different user CAN create an assistant with the same name
    user2 = await create_test_user(
        client,
        "user2-for-duplicate-test@example.com",
        hiring_approved=True,
    )
    user2_headers = user2["headers"]

    # Add credits to user2 so they can create an assistant
    from orchestra.db.dao.users_dao import UsersDAO
    from orchestra.settings import settings

    users_dao = UsersDAO(dbsession)
    users_dao.recharge_credit(user2["id"], settings.assistant_creation_cost)
    dbsession.commit()

    resp3 = await client.post("/v0/assistant", json=payload, headers=user2_headers)
    assert (
        resp3.status_code == 200
    ), f"Second user failed to create assistant with same name: {resp3.text}"
    data3 = resp3.json()["info"]
    assert data3["first_name"] == payload["first_name"]
    assert data3["surname"] == payload["surname"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid_timezone",
    ["PST", "UTC+1", "Germany/Fake_City", "gmt"],
)
async def test_create_assistant_with_invalid_timezone(
    client: AsyncClient,
    invalid_timezone: str,
):
    """Test that creating an assistant with an invalid timezone fails."""
    payload = {
        "first_name": "Timezone",
        "surname": "Fail",
        "timezone": invalid_timezone,
        "create_infra": False,
    }
    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    error_detail = resp.json()["detail"][0]
    assert "timezone" in error_detail["loc"]
    assert "not a valid IANA timezone" in error_detail["msg"]


# --- Assistant project creation and logging ---
@pytest.fixture
def pre_hire_chat_payload():
    """Provides a sample pre_hire_chat payload with the new simplified schema."""
    return {
        "pre_hire_chat": [
            {"role": "user", "msg": "Hello, are you available for an interview?"},
            {"role": "assistant", "msg": "Yes, I am. When would be a good time?"},
        ],
    }


@pytest.mark.anyio
async def test_create_assistant_creates_assistants_project(
    client: AsyncClient,
):
    # Call create_assistant for the first time
    payload = {
        "first_name": "Project",
        "surname": "Creator",
        "create_infra": False,
    }
    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == 200

    # Verify that the "Assistants" project now exists
    projects_resp = await client.get("/v0/projects", headers=HEADERS)
    assert projects_resp.status_code == 200
    projects = projects_resp.json()
    assert "Assistants" in projects


@pytest.mark.anyio
@patch("orchestra.web.api.assistant.views.log_pre_hire_chat")
async def test_create_assistant_with_pre_hire_chat_logs_correctly(
    mock_log_pre_hire_chat,
    client: AsyncClient,
    pre_hire_chat_payload,
):
    payload = {
        "first_name": "Chatty",
        "surname": "Cathy",
        "create_infra": False,
        **pre_hire_chat_payload,
    }

    create_resp = await client.post(
        "/v0/assistant",
        json=payload,
        headers=HEADERS,
    )
    assert (
        create_resp.status_code == 200
    ), f"Assistant creation failed: {create_resp.text}"

    assistant_id = create_resp.json()["info"]["agent_id"]

    # Verify that the webhook function was called with the correct arguments
    mock_log_pre_hire_chat.assert_called_once()
    call_args, call_kwargs = mock_log_pre_hire_chat.call_args
    assert call_kwargs["assistant_id"] == str(assistant_id)
    assert call_kwargs["messages"] == pre_hire_chat_payload["pre_hire_chat"]
    assert "is_staging" in call_kwargs


@pytest.mark.anyio
async def test_delete_assistant_deletes_contexts(
    client: AsyncClient,
):
    # Create an assistant
    payload = {
        "first_name": "Deletable",
        "surname": "Dan",
        "create_infra": False,
    }
    create_resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert create_resp.status_code == 200
    assistant_info = create_resp.json()["info"]
    assistant_id = assistant_info["agent_id"]

    # Manually create a project and context to simulate logs being present
    project_name = "Assistants"
    context_name = (
        f"{assistant_info['first_name']}{assistant_info['surname']}/Transcripts"
    )
    # The "Assistants" project is created automatically on first assistant creation
    log_payload = {
        "project": project_name,
        "context": context_name,
        "entries": [{"message": "test"}],
    }
    log_resp = await client.post("/v0/logs", json=log_payload, headers=HEADERS)
    assert log_resp.status_code == 200

    # Verify context and logs exist before deletion
    logs_before_delete = await client.get(
        f"/v0/logs?project={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert logs_before_delete.status_code == 200
    assert (
        logs_before_delete.json()["count"] > 0
    ), "Context was not created properly before test."

    # Delete the assistant
    delete_resp = await client.delete(
        f"/v0/assistant/{assistant_id}",
        headers=HEADERS,
    )
    assert delete_resp.status_code == 200, f"Delete failed: {delete_resp.text}"

    # Verify the context is now gone.
    # A successful deletion can result in either the context being empty (200 OK, count=0)
    # or the context itself being gone (404 Not Found). Both are valid success states.
    logs_after_delete = await client.get(
        f"/v0/logs?project={project_name}&context={context_name}",
        headers=HEADERS,
    )

    assert logs_after_delete.status_code in [
        200,
        404,
    ], f"Expected status 200 or 404, but got {logs_after_delete.status_code}. Response: {logs_after_delete.text}"

    if logs_after_delete.status_code == 200:
        assert (
            logs_after_delete.json()["count"] == 0
        ), f"Context still exists and is not empty. Found {logs_after_delete.json()['count']} logs."


@pytest.mark.anyio
async def test_delete_assistant_contact(client: AsyncClient):
    # Mock the infrastructure deletion calls to avoid external API calls during testing
    with patch(
        "orchestra.web.api.assistant.views.delete_phone_number",
    ) as mock_delete_phone, patch(
        "orchestra.web.api.assistant.views.delete_email",
    ) as mock_delete_email:

        # 1. Create a base assistant
        base_payload = {
            "first_name": "Contact",
            "surname": "Remover",
            "create_infra": False,
        }
        create_resp = await client.post(
            "/v0/assistant",
            json=base_payload,
            headers=HEADERS,
        )
        assert create_resp.status_code == 200
        assistant_id = create_resp.json()["info"]["agent_id"]

        # 2. Update the assistant to have all contact details for the test
        contact_payload = {
            "email": "contact.remover@example.com",
            "phone": "+15558675309",
            "user_phone": "+15558675310",  # user_phone can be different
            "user_whatsapp_number": "+15558675311",
            "create_infra": False,
        }
        update_resp = await client.patch(
            f"/v0/assistant/{assistant_id}/config",
            json=contact_payload,
            headers=HEADERS,
        )
        assert update_resp.status_code == 200

        # 3. Use an admin endpoint to set the assistant_whatsapp_number for a complete test case
        assistant_whatsapp_number = "+15551112222"
        admin_patch_resp = await client.patch(
            f"/v0/admin/assistant?phone={contact_payload['phone']}&new_assistant_whatsapp_number={assistant_whatsapp_number}",
            headers=ADMIN_HEADERS,
        )
        assert admin_patch_resp.status_code == 200
        assert (
            admin_patch_resp.json()["info"]["assistant_whatsapp_number"]
            == assistant_whatsapp_number
        )

        # 4. Delete Email contact
        delete_email_payload = {"contact_type": "email"}
        delete_email_resp = await client.request(
            "DELETE",
            f"/v0/assistant/{assistant_id}/contact",
            json=delete_email_payload,
            headers=HEADERS,
        )
        assert delete_email_resp.status_code == 200, delete_email_resp.text
        email_deleted_info = delete_email_resp.json()["info"]
        assert email_deleted_info["email"] is None
        assert (
            email_deleted_info["phone"] == contact_payload["phone"]
        )  # Should be unchanged
        mock_delete_email.assert_called_once_with(contact_payload["email"])

        # 5. Delete Phone contact
        delete_phone_payload = {"contact_type": "phone"}
        delete_phone_resp = await client.request(
            "DELETE",
            f"/v0/assistant/{assistant_id}/contact",
            json=delete_phone_payload,
            headers=HEADERS,
        )
        assert delete_phone_resp.status_code == 200, delete_phone_resp.text
        phone_deleted_info = delete_phone_resp.json()["info"]
        assert phone_deleted_info["phone"] is None
        assert phone_deleted_info["user_phone"] is None
        assert phone_deleted_info["email"] is None  # Should still be None
        assert (
            phone_deleted_info["assistant_whatsapp_number"] == assistant_whatsapp_number
        )  # Unchanged
        mock_delete_phone.assert_called_once_with(contact_payload["phone"])

        # 6. Delete WhatsApp contact
        delete_whatsapp_payload = {"contact_type": "whatsapp"}
        delete_whatsapp_resp = await client.request(
            "DELETE",
            f"/v0/assistant/{assistant_id}/contact",
            json=delete_whatsapp_payload,
            headers=HEADERS,
        )
        assert delete_whatsapp_resp.status_code == 200, delete_whatsapp_resp.text
        whatsapp_deleted_info = delete_whatsapp_resp.json()["info"]
        assert whatsapp_deleted_info["user_whatsapp_number"] is None
        assert whatsapp_deleted_info["assistant_whatsapp_number"] is None
        assert whatsapp_deleted_info["phone"] is None  # Should still be None

        # 7. Test invalid contact type
        delete_invalid_payload = {"contact_type": "carrier_pigeon"}
        delete_invalid_resp = await client.request(
            "DELETE",
            f"/v0/assistant/{assistant_id}/contact",
            json=delete_invalid_payload,
            headers=HEADERS,
        )
        assert delete_invalid_resp.status_code == 422  # Unprocessable Entity

        # 8. Test non-existent assistant
        delete_nonexistent_resp = await client.request(
            "DELETE",
            f"/v0/assistant/999999/contact",
            json=delete_email_payload,
            headers=HEADERS,
        )
        assert delete_nonexistent_resp.status_code == 404


@pytest.mark.anyio
@patch("orchestra.web.api.assistant.views.reawaken_assistant")
async def test_update_assistant_contact_info_reawakens(
    mock_reawaken,
    client: AsyncClient,
):
    # Create an assistant
    payload = {"first_name": "Reawaken", "surname": "Updater", "create_infra": False}
    create_resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert create_resp.status_code == 200
    assistant_id = create_resp.json()["info"]["agent_id"]

    # Update a contact field (phone) and expect reawaken to be called
    update_contact_payload = {"phone": "+15550001111", "create_infra": True}
    patch_contact_resp = await client.patch(
        f"/v0/assistant/{assistant_id}/config",
        json=update_contact_payload,
        headers=HEADERS,
    )
    assert patch_contact_resp.status_code == 200
    mock_reawaken.assert_called_once()
    # Use ANY from unittest.mock for the is_staging flag
    mock_reawaken.call_args[0][0] == str(assistant_id)

    # Reset the mock and update a non-contact field
    mock_reawaken.reset_mock()
    update_non_contact_payload = {"about": "new bio", "create_infra": True}
    patch_non_contact_resp = await client.patch(
        f"/v0/assistant/{assistant_id}/config",
        json=update_non_contact_payload,
        headers=HEADERS,
    )
    assert patch_non_contact_resp.status_code == 200
    mock_reawaken.assert_not_called()


@pytest.mark.anyio
async def test_update_assistant_with_invalid_timezone(client: AsyncClient):
    """Test that updating an assistant with an invalid timezone fails."""
    # 1. Create a valid assistant first
    payload = {
        "first_name": "Timezone",
        "surname": "UpdateFail",
        "create_infra": False,
    }
    create_resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert create_resp.status_code == 200
    assistant_id = create_resp.json()["info"]["agent_id"]

    # 2. Attempt to update with an invalid timezone
    update_payload = {"timezone": "America/Wrong_City", "create_infra": False}
    patch_resp = await client.patch(
        f"/v0/assistant/{assistant_id}/config",
        json=update_payload,
        headers=HEADERS,
    )

    # 3. Assert the request fails with a validation error
    assert patch_resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    error_detail = patch_resp.json()["detail"][0]
    assert "timezone" in error_detail["loc"]
    assert "not a valid IANA timezone" in error_detail["msg"]


@pytest.mark.anyio
@patch("orchestra.web.api.assistant.views.delete_phone_number")
@patch("orchestra.web.api.assistant.views.reawaken_assistant")
async def test_delete_assistant_contact_reawakens(
    mock_reawaken,
    mock_delete_phone,
    client: AsyncClient,
):
    # 1. Create an assistant
    payload = {"first_name": "Reawaken", "surname": "Deleter", "create_infra": False}
    create_resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert create_resp.status_code == 200
    assistant_id = create_resp.json()["info"]["agent_id"]

    # 2. Add a phone number to it so we can delete it
    # We set create_infra=False because we are mocking the reawaken call anyway
    update_payload = {"phone": "+15552223333", "create_infra": False}
    update_resp = await client.patch(
        f"/v0/assistant/{assistant_id}/config",
        json=update_payload,
        headers=HEADERS,
    )
    assert update_resp.status_code == 200
    mock_reawaken.assert_not_called()  # Should not be called when create_infra is false

    # 3. Delete the phone contact and verify reawaken is called
    delete_payload = {"contact_type": "phone"}
    delete_resp = await client.request(
        "DELETE",
        f"/v0/assistant/{assistant_id}/contact",
        json=delete_payload,
        headers=HEADERS,
    )
    assert delete_resp.status_code == 200
    mock_reawaken.assert_called_once()
    assert mock_reawaken.call_args[0][0] == str(assistant_id)
    # Also assert the mock for deleting the phone number was called
    mock_delete_phone.assert_called_once_with("+15552223333")


# ==== Voice Configuration Validation Tests ====


@pytest.mark.anyio
async def test_create_assistant_with_valid_voice_config(client: AsyncClient):
    """Tests creating an assistant with full and partial valid voice configs."""

    # Pre-register the voices that will be used in this test
    voice1_payload = {
        "voice_id": "voice123",
        "name": "Test Voice Full",
        "description": "A voice for testing.",
        "language": "en",
        "provider": "cartesia",
    }
    reg_resp1 = await client.post(
        "/v0/assistant/voice",
        json=voice1_payload,
        headers=HEADERS,
    )
    assert reg_resp1.status_code == 201, "Failed to register first test voice"

    voice2_payload = {
        "voice_id": "voice456",
        "name": "Test Voice Partial",
        "description": "Another voice for testing.",
        "language": "en",
        "provider": "elevenlabs",
    }
    reg_resp2 = await client.post(
        "/v0/assistant/voice",
        json=voice2_payload,
        headers=HEADERS,
    )
    assert reg_resp2.status_code == 201, "Failed to register second test voice"

    # Case 1: Full voice config
    payload_full = {
        "first_name": "Voice",
        "surname": "Full",
        "voice_id": "voice123",
        "voice_provider": "cartesia",
        "voice_mode": "sts",
        "create_infra": False,
    }
    resp_full = await client.post("/v0/assistant", json=payload_full, headers=HEADERS)
    assert resp_full.status_code == 200, resp_full.text
    data_full = resp_full.json()["info"]
    assert data_full["voice_id"] == "voice123"
    assert data_full["voice_provider"] == "cartesia"
    assert data_full["voice_mode"] == "sts"

    # Case 2: Partial voice config (mode should default to 'tts')
    payload_partial = {
        "first_name": "Voice",
        "surname": "Partial",
        "voice_id": "voice456",
        "voice_provider": "elevenlabs",
        # voice_mode is omitted
        "create_infra": False,
    }
    resp_partial = await client.post(
        "/v0/assistant",
        json=payload_partial,
        headers=HEADERS,
    )
    assert resp_partial.status_code == 200, resp_partial.text
    data_partial = resp_partial.json()["info"]
    assert data_partial["voice_id"] == "voice456"
    assert data_partial["voice_provider"] == "elevenlabs"
    assert data_partial["voice_mode"] == "tts"  # Check default


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid_payload",
    [
        {"first_name": "Invalid", "surname": "Voice1", "voice_id": "only_id"},
        {
            "first_name": "Invalid",
            "surname": "Voice2",
            "voice_provider": "only_provider",
        },
        {"first_name": "Invalid", "surname": "Voice3", "voice_mode": "tts"},
    ],
)
async def test_create_assistant_with_invalid_voice_config(
    client: AsyncClient,
    invalid_payload,
):
    """Tests creating an assistant with various invalid voice configs."""
    payload = {**invalid_payload, "create_infra": False}
    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == 422
    assert (
        "If providing voice information, both 'voice_id' and 'voice_provider' are required"
        in resp.text
    )


@pytest.mark.anyio
async def test_update_assistant_voice_config_valid_cases(client: AsyncClient):
    """Tests valid scenarios for updating an assistant's voice configuration."""
    # 1. Create a base assistant with no voice
    base_payload = {"first_name": "Voice", "surname": "Updater", "create_infra": False}
    create_resp = await client.post("/v0/assistant", json=base_payload, headers=HEADERS)
    assert create_resp.status_code == 200
    assistant_id = create_resp.json()["info"]["agent_id"]

    # Pre-register the voices that will be used for updates.
    voice_openai_payload = {
        "voice_id": "v_upd_1",
        "name": "Update Voice OpenAI",
        "description": "...",
        "language": "en",
        "provider": "openai",
    }
    reg1 = await client.post(
        "/v0/assistant/voice",
        json=voice_openai_payload,
        headers=HEADERS,
    )
    assert reg1.status_code == 201

    voice_cartesia_payload = {
        "voice_id": "v_upd_2",
        "name": "Update Voice Cartesia",
        "description": "...",
        "language": "en",
        "provider": "cartesia",
    }
    reg2 = await client.post(
        "/v0/assistant/voice",
        json=voice_cartesia_payload,
        headers=HEADERS,
    )
    assert reg2.status_code == 201

    # 2. Update to add full voice config
    update_full = {
        "voice_id": "v_upd_1",
        "voice_provider": "openai",
        "voice_mode": "sts",
    }
    patch1 = await client.patch(
        f"/v0/assistant/{assistant_id}/config",
        json=update_full,
        headers=HEADERS,
    )
    assert patch1.status_code == 200, patch1.text
    d1 = patch1.json()["info"]
    assert (
        d1["voice_id"] == "v_upd_1"
        and d1["voice_provider"] == "openai"
        and d1["voice_mode"] == "sts"
    )

    # 3. Update with partial config (mode defaults to 'tts')
    update_partial = {"voice_id": "v_upd_2", "voice_provider": "cartesia"}
    patch2 = await client.patch(
        f"/v0/assistant/{assistant_id}/config",
        json=update_partial,
        headers=HEADERS,
    )
    assert patch2.status_code == 200, patch2.text
    d2 = patch2.json()["info"]
    assert (
        d2["voice_id"] == "v_upd_2"
        and d2["voice_provider"] == "cartesia"
        and d2["voice_mode"] == "tts"
    )

    # 4. Clear voice config
    update_clear = {"voice_id": None}
    patch3 = await client.patch(
        f"/v0/assistant/{assistant_id}/config",
        json=update_clear,
        headers=HEADERS,
    )
    assert patch3.status_code == 200, patch3.text
    d3 = patch3.json()["info"]
    assert (
        d3["voice_id"] is None
        and d3["voice_provider"] is None
        and d3["voice_mode"] is None
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid_payload, error_msg",
    [
        (
            {"voice_id": "v_inv_1"},
            "both 'voice_id' and 'voice_provider' must be provided",
        ),
        (
            {"voice_provider": "cartesia"},
            "both 'voice_id' and 'voice_provider' must be provided",
        ),
        ({"voice_mode": "sts"}, "Cannot update 'voice_mode' alone"),
        (
            {"voice_id": "v_inv_2", "voice_provider": None},
            "'voice_provider' cannot be null",
        ),
    ],
)
async def test_update_assistant_with_invalid_voice_config(
    client: AsyncClient,
    invalid_payload,
    error_msg,
):
    """Tests updating an assistant with various invalid voice configs."""
    base_payload = {
        "first_name": "Invalid",
        "surname": "VoiceUpdate",
        "create_infra": False,
    }
    create_resp = await client.post("/v0/assistant", json=base_payload, headers=HEADERS)
    assert create_resp.status_code == 200
    assistant_id = create_resp.json()["info"]["agent_id"]

    resp = await client.patch(
        f"/v0/assistant/{assistant_id}/config",
        json=invalid_payload,
        headers=HEADERS,
    )
    assert resp.status_code == 422
    assert error_msg in resp.text
