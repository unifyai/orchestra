import pytest
from httpx import AsyncClient

from orchestra.tests.utils import HEADERS


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
    }
    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
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
    }
    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_list_assistants_empty(client: AsyncClient):
    # `GET /v0/assistant` with no assistants -> 200 OK and empty list
    resp = await client.get("/v0/assistant", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == []


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
    data = list_resp.json()
    assert isinstance(data, list)
    assert len(data) == 2
    ids = {item["agent_id"] for item in data}
    assert {r1.json()["agent_id"], r2.json()["agent_id"]} == ids

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
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["agent_id"]
    new_limit = 45.5
    update_payload = {"weekly_limit": new_limit}
    patch = await client.patch(
        f"/v0/assistant/{aid}/config",
        json=update_payload,
        headers=HEADERS,
    )
    assert patch.status_code == 200
    updated = patch.json()
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
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["agent_id"]
    new_parallel = 7
    update_payload = {"max_parallel": new_parallel}
    patch = await client.patch(
        f"/v0/assistant/{aid}/config",
        json=update_payload,
        headers=HEADERS,
    )
    assert patch.status_code == 200
    updated = patch.json()
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
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["agent_id"]
    del_resp = await client.delete(f"/v0/assistant/{aid}", headers=HEADERS)
    assert del_resp.status_code == 200
    list_resp = await client.get("/v0/assistant", headers=HEADERS)
    assert all(item["agent_id"] != aid for item in list_resp.json())


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
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["agent_id"]
    new_about = "Updated bio with additional qualifications and expertise"
    update_payload = {"about": new_about}
    patch = await client.patch(
        f"/v0/assistant/{aid}/config",
        json=update_payload,
        headers=HEADERS,
    )
    assert patch.status_code == 200
    updated = patch.json()
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
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["agent_id"]
    new_phone = "+1-555-123-4567"
    update_payload = {"phone": new_phone}
    patch = await client.patch(
        f"/v0/assistant/{aid}/config",
        json=update_payload,
        headers=HEADERS,
    )
    assert patch.status_code == 200
    updated = patch.json()
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
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["agent_id"]
    new_email = "julia.garcia@example.com"
    update_payload = {"email": new_email}
    patch = await client.patch(
        f"/v0/assistant/{aid}/config",
        json=update_payload,
        headers=HEADERS,
    )
    assert patch.status_code == 200
    updated = patch.json()
    assert updated["email"] == new_email
    assert updated["phone"] is None
    assert updated["about"] == payload["about"]
    assert updated["weekly_limit"] == payload["weekly_limit"]


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
    }
    create = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    aid = create.json()["agent_id"]
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
    updated = patch.json()
    assert updated["about"] == update_payload["about"]
    assert updated["phone"] == update_payload["phone"]
    assert updated["email"] == update_payload["email"]
    assert updated["first_name"] == payload["first_name"]
    assert updated["region"] == payload["region"]
