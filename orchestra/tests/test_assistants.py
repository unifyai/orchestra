import base64
import datetime
from pathlib import Path

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
        "region": "North America",
        "profile_photo": "https://example.com/photos/alice.jpg",
        "about": "AI researcher specializing in natural language processing",
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
    assert isinstance(data["weekly_limit"], float)
    assert data["weekly_limit"] == payload["weekly_limit"]
    assert data["max_parallel"] == payload["max_parallel"]
    assert data["region"] == payload["region"]
    assert data["profile_photo"] == payload["profile_photo"]
    assert data["about"] == payload["about"]
    assert data["phone"] is None
    assert data["email"] is None
    assert isinstance(data.get("created_at"), str)
    assert "updated_at" in data


@pytest.mark.anyio
async def test_create_assistant_missing_field(client: AsyncClient):
    # `POST /v0/assistant` missing surname -> 422 Unprocessable Entity
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
        "region": "Europe",
        "profile_photo": "https://example.com/photos/carol.jpg",
        "about": "Data scientist with expertise in statistical modeling",
        "create_infra": False,
    }
    payload2 = {
        "first_name": "Dave",
        "surname": "Lee",
        "age": 35,
        "weekly_limit": 20.0,
        "max_parallel": 5,
        "region": "Asia",
        "profile_photo": "https://example.com/photos/dave.jpg",
        "about": "Software engineer focused on distributed systems",
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
        assert "region" in assistant
        assert "profile_photo" in assistant
        assert "about" in assistant
        assert "phone" in assistant
        assert "email" in assistant
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
        "region": "South America",
        "profile_photo": "https://example.com/photos/eve.jpg",
        "about": "Machine learning expert with focus on computer vision",
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
    assert updated["region"] == payload["region"]
    assert updated["profile_photo"] == payload["profile_photo"]
    assert updated["about"] == payload["about"]
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
        "region": "Australia",
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
    assert updated["region"] == payload["region"]
    assert updated["profile_photo"] == payload["profile_photo"]
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
        "region": "North America",
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
        "region": "Asia",
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
    assert updated["region"] == payload["region"]
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
        "region": "Europe",
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
    assert updated["region"] == payload["region"]


@pytest.mark.anyio
async def test_update_email_only(client: AsyncClient):
    # Create assistant, then `PATCH /v0/assistant/{id}/config` email only -> updated
    payload = {
        "first_name": "Julia",
        "surname": "Garcia",
        "age": 38,
        "weekly_limit": 22.5,
        "max_parallel": 4,
        "region": "South America",
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
        "region": "Europe",
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
        "region": "Digital Ocean",
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
    assert updated_data["region"] == payload["region"]
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
        "region": "Africa",
        "profile_photo": "https://example.com/photos/kevin.jpg",
        "about": "Original bio information",
        "create_infra": False,
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["info"]["agent_id"]
    update_payload = {
        "about": "Updated professional bio with new skills",
        "phone": "+1-555-987-6543",
        "email": "kevin.brown@example.com",
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
    assert updated["first_name"] == payload["first_name"]
    assert updated["region"] == payload["region"]


@pytest.mark.anyio
async def test_assistant_recordings_audio_lifecycle(client: AsyncClient):
    # Create a new assistant
    payload = {
        "first_name": "Kevin",
        "surname": "Brown",
        "age": 29,
        "weekly_limit": 18.0,
        "max_parallel": 2,
        "region": "Africa",
        "profile_photo": "https://example.com/photos/kevin.jpg",
        "about": "Original bio information",
        "create_infra": False,
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert create.status_code == 200
    assistant_info = create.json()["info"]
    agent_id = assistant_info["agent_id"]

    # Read and encode sample WAV file
    raw_bytes = _get_sample_wav_bytes()
    b64_audio = base64.b64encode(raw_bytes).decode()

    # Upload raw recording
    record_payload = {"recording_raw": b64_audio, "content_type": "audio/wav"}
    record_resp = await client.post(
        f"/v0/assistant/{agent_id}/recordings",
        headers=HEADERS,
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
        "region": "Europe",
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
        "region": "North America",
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
        "region": "Europe",
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
        "region": "Asia",
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
        "region": "North America",
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
        "region": "Australia",
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
        "region": "Asia",
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
        "region": "Australia",
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
        "region": "South America",
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
        "region": "Africa",
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
        "region": "Test",
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
        "region": "Test",
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
        "region": "Testland",
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
        "region": "Testland",
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
        "region": "Testland",
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
async def test_create_assistant_duplicate_name_fails(client: AsyncClient):
    # `POST /v0/assistant` with a duplicate name for the same user should fail.
    payload = {
        "first_name": "David",
        "surname": "Miller",
        "age": 35,
        "weekly_limit": 20.0,
        "max_parallel": 2,
        "region": "North America",
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
    resp3 = await client.post("/v0/assistant", json=payload, headers=user2_headers)
    assert (
        resp3.status_code == 200
    ), f"Second user failed to create assistant with same name: {resp3.text}"
    data3 = resp3.json()["info"]
    assert data3["first_name"] == payload["first_name"]
    assert data3["surname"] == payload["surname"]


# --- Assistant project creation and logging ---
@pytest.fixture
def pre_hire_chat_payload():
    """Provides a sample pre_hire_chat payload."""
    return {
        "pre_hire_chat": [
            {
                "message_id": 1,
                "medium": "unify_chat",
                "sender_id": 1,
                "receiver_ids": [0],
                "timestamp": datetime.datetime.now(
                    datetime.timezone.utc,
                ).isoformat(),
                "content": "Hello, are you available for an interview?",
                "exchange_id": 101,
            },
            {
                "message_id": 2,
                "medium": "unify_chat",
                "sender_id": 0,
                "receiver_ids": [1],
                "timestamp": datetime.datetime.now(
                    datetime.timezone.utc,
                ).isoformat(),
                "content": "Yes, I am. When would be a good time?",
                "exchange_id": 101,
            },
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
async def test_create_assistant_with_pre_hire_chat_logs_correctly(
    client: AsyncClient,
    pre_hire_chat_payload,
):
    payload = {
        "first_name": "Chatty",
        "surname": "Cathy",
        "age": 30,
        "weekly_limit": 10,
        "max_parallel": 1,
        "create_infra": False,
        **pre_hire_chat_payload,
    }

    # Create the assistant
    create_resp = await client.post(
        "/v0/assistant",
        json=payload,
        headers=HEADERS,
    )
    assert (
        create_resp.status_code == 200
    ), f"Assistant creation failed: {create_resp.text}"

    # Verify the logs were created
    context_name = "ChattyCathy/Transcripts"
    logs_resp = await client.get(
        f"/v0/logs?project=Assistants&context={context_name}",
        headers=HEADERS,
    )
    assert logs_resp.status_code == 200, f"Failed to get logs: {logs_resp.text}"
    logs_data = logs_resp.json()

    assert logs_data["count"] == 2, f"Expected 2 logs, but found {logs_data['count']}."
    assert (
        len(logs_data["logs"]) == 2
    ), f"Expected 2 log objects, but found {len(logs_data['logs'])}."

    returned_logs = logs_data["logs"]
    original_messages = pre_hire_chat_payload["pre_hire_chat"]

    # Create a dictionary of returned logs keyed by their content for easy lookup
    returned_logs_map = {
        log["entries"]["content"]: log["entries"] for log in returned_logs
    }

    # Loop through the original messages and check if each one exists in the returned logs
    for original_msg in original_messages:
        content = original_msg["content"]

        assert (
            content in returned_logs_map
        ), f"Message content '{content}' not found in returned logs."

        returned_entry = returned_logs_map[content]

        # Assert that all fields match
        assert (
            returned_entry["message_id"] == original_msg["message_id"]
        ), f"Message ID mismatch for content '{content}'. Expected {original_msg['message_id']}, got {returned_entry['message_id']}"
        assert (
            returned_entry["sender_id"] == original_msg["sender_id"]
        ), f"Sender ID mismatch for content '{content}'. Expected {original_msg['sender_id']}, got {returned_entry['sender_id']}"
        assert (
            returned_entry["receiver_ids"] == original_msg["receiver_ids"]
        ), f"Receiver IDs mismatch for content '{content}'. Expected {original_msg['receiver_ids']}, got {returned_entry['receiver_ids']}"
        assert (
            returned_entry["exchange_id"] == original_msg["exchange_id"]
        ), f"Exchange ID mismatch for content '{content}'. Expected {original_msg['exchange_id']}, got {returned_entry['exchange_id']}"


@pytest.mark.anyio
async def test_delete_assistant_deletes_contexts(
    client: AsyncClient,
    pre_hire_chat_payload,
):
    # Create an assistant with pre_hire_chat to ensure context is created
    payload = {
        "first_name": "Deletable",
        "surname": "Dan",
        "create_infra": False,
        **pre_hire_chat_payload,
    }
    create_resp = await client.post(
        "/v0/assistant",
        json=payload,
        headers=HEADERS,
    )
    assert create_resp.status_code == 200
    assistant_id = create_resp.json()["info"]["agent_id"]

    # Verify context and logs exist before deletion
    context_name = "DeletableDan/Transcripts"
    logs_before_delete = await client.get(
        f"/v0/logs?project=Assistants&context={context_name}",
        headers=HEADERS,
    )
    assert logs_before_delete.status_code == 200
    assert (
        logs_before_delete.json()["count"] > 0
    ), "Context was created but no logs were found."

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
        f"/v0/logs?project=Assistants&context={context_name}",
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
        "orchestra.web.api.assistant.views.delete_phone_number"
    ) as mock_delete_phone, patch(
        "orchestra.web.api.assistant.views.delete_email"
    ) as mock_delete_email:

        # 1. Create a base assistant
        base_payload = {
            "first_name": "Contact",
            "surname": "Remover",
            "create_infra": False,
        }
        create_resp = await client.post(
            "/v0/assistant", json=base_payload, headers=HEADERS
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
        delete_email_resp = await client.delete(
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
        delete_phone_resp = await client.delete(
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
        delete_whatsapp_resp = await client.delete(
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
        delete_invalid_resp = await client.delete(
            f"/v0/assistant/{assistant_id}/contact",
            json=delete_invalid_payload,
            headers=HEADERS,
        )
        assert delete_invalid_resp.status_code == 422  # Unprocessable Entity

        # 8. Test non-existent assistant
        delete_nonexistent_resp = await client.delete(
            f"/v0/assistant/999999/contact",
            json=delete_email_payload,
            headers=HEADERS,
        )
        assert delete_nonexistent_resp.status_code == 404
