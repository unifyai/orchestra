import base64
from pathlib import Path

import pytest
from httpx import AsyncClient

from orchestra.tests.utils import ADMIN_HEADERS, HEADERS
from orchestra.settings import settings


def _get_sample_wav_bytes() -> bytes:
    sample_path = Path(__file__).parent / "sample_datasets" / "sample_recording.wav"
    return sample_path.read_bytes()


@pytest.mark.anyio
async def test_create_assistant_success(client: AsyncClient, mocker):
    mocker.patch.object(settings, "is_staging", True)
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
async def test_create_assistant_missing_field(client: AsyncClient, mocker):
    mocker.patch.object(settings, "is_staging", True)
    # `POST /v0/assistant` missing surname -> 422 Unprocessable Entity
    payload = {
        "first_name": "Bob",
        # surname omitted
        "age": 30,
        "weekly_limit": 10.0,
        "max_parallel": 2,
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
async def test_list_assistants_after_create(client: AsyncClient, mocker):
    mocker.patch.object(settings, "is_staging", True)
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
async def test_update_weekly_limit_only(client: AsyncClient, mocker):
    mocker.patch.object(settings, "is_staging", True)
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
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["info"]["agent_id"]
    new_limit = 45.5
    update_payload = {"weekly_limit": new_limit}
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
async def test_update_max_parallel_only(client: AsyncClient, mocker):
    mocker.patch.object(settings, "is_staging", True)
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
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["info"]["agent_id"]
    new_parallel = 7
    update_payload = {"max_parallel": new_parallel}
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
async def test_delete_assistant_success(client: AsyncClient, mocker):
    mocker.patch.object(settings, "is_staging", True)
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
async def test_update_about_only(client: AsyncClient, mocker):
    mocker.patch.object(settings, "is_staging", True)
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
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["info"]["agent_id"]
    new_about = "Updated bio with additional qualifications and expertise"
    update_payload = {"about": new_about}
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
async def test_update_phone_only(client: AsyncClient, mocker):
    mocker.patch.object(settings, "is_staging", True)
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
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["info"]["agent_id"]
    new_phone = "+1-555-123-4567"
    update_payload = {"phone": new_phone}
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
async def test_update_email_only(client: AsyncClient, mocker):
    mocker.patch.object(settings, "is_staging", True)
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
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["info"]["agent_id"]
    new_email = "julia.garcia@example.com"
    update_payload = {"email": new_email}
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
async def test_update_multiple_fields(client: AsyncClient, mocker):
    mocker.patch.object(settings, "is_staging", True)
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
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["info"]["agent_id"]
    update_payload = {
        "about": "Updated professional bio with new skills",
        "phone": "+1-555-987-6543",
        "email": "kevin.brown@example.com",
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
async def test_assistant_recordings_audio_lifecycle(client: AsyncClient, mocker):
    mocker.patch.object(settings, "is_staging", True)
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
async def test_admin_list_assistant_emails(client: AsyncClient, mocker):
    mocker.patch.object(settings, "is_staging", True)
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
        json={"email": email1},
        headers=HEADERS,
    )
    update2 = await client.patch(
        f"/v0/assistant/{aid2}/config",
        json={"email": email2},
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
async def test_create_assistant_with_whatsapp_sid(client: AsyncClient, mocker):
    mocker.patch.object(settings, "is_staging", True)
    # Create assistant with whatsapp_sid -> verify it appears in create and list responses
    payload = {
        "first_name": "Nina",
        "surname": "Rodriguez",
        "age": 27,
        "weekly_limit": 20.0,
        "max_parallel": 2,
        "region": "South America",
        "profile_photo": "https://example.com/photos/nina.jpg",
        "about": "WhatsApp integration specialist",
        "whatsapp_sid": "WA1234567890abcdef",
    }
    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert "info" in body
    data = body["info"]
    assert data["whatsapp_sid"] == payload["whatsapp_sid"]
    assert data["first_name"] == payload["first_name"]
    assert data["phone"] is None
    assert data["email"] is None

    # Verify whatsapp_sid appears in list response
    list_resp = await client.get("/v0/assistant", headers=HEADERS)
    assert list_resp.status_code == 200
    assistants = list_resp.json()["info"]
    created_assistant = next(a for a in assistants if a["agent_id"] == data["agent_id"])
    assert created_assistant["whatsapp_sid"] == payload["whatsapp_sid"]


@pytest.mark.anyio
async def test_update_whatsapp_sid_only(client: AsyncClient, mocker):
    mocker.patch.object(settings, "is_staging", True)
    # Create assistant, then PATCH whatsapp_sid only -> updated
    payload = {
        "first_name": "Oscar",
        "surname": "Thompson",
        "age": 34,
        "weekly_limit": 28.0,
        "max_parallel": 3,
        "region": "North America",
        "profile_photo": "https://example.com/photos/oscar.jpg",
        "about": "Communication systems engineer",
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["info"]["agent_id"]
    new_whatsapp_sid = "WA9876543210fedcba"
    update_payload = {"whatsapp_sid": new_whatsapp_sid}
    patch = await client.patch(
        f"/v0/assistant/{aid}/config",
        json=update_payload,
        headers=HEADERS,
    )
    assert patch.status_code == 200
    updated = patch.json()["info"]
    assert updated["whatsapp_sid"] == new_whatsapp_sid
    assert updated["phone"] is None
    assert updated["email"] is None
    assert updated["first_name"] == payload["first_name"]
    assert updated["about"] == payload["about"]


@pytest.mark.anyio
async def test_search_assistants_by_phone(client: AsyncClient, mocker):
    mocker.patch.object(settings, "is_staging", True)
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
        "phone": "+1-555-111-2222",
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
        "phone": "+1-555-333-4444",
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
async def test_search_assistants_by_email(client: AsyncClient, mocker):
    mocker.patch.object(settings, "is_staging", True)
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
async def test_admin_list_assistants_filter_phone(client: AsyncClient, mocker):
    mocker.patch.object(settings, "is_staging", True)
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
        "phone": "+1-555-111-1111",
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
        "phone": "+1-555-222-2222",
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
async def test_admin_list_assistants_filter_email(client: AsyncClient, mocker):
    mocker.patch.object(settings, "is_staging", True)
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
