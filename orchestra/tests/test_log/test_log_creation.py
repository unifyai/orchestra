import base64
import os

import cv2
import pytest
from httpx import AsyncClient

from . import HEADERS, _create_log, _create_project


@pytest.mark.anyio
async def test_create_logs(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    # Test single log creation
    response = await _create_log(client, project_name)
    assert response.status_code == 200, response.json()
    log_event_ids = response.json()["log_event_ids"]
    assert isinstance(log_event_ids, list) and len(log_event_ids) == 1
    assert isinstance(log_event_ids[0], int)

    # Test batch log creation with multiple entries
    batch_entries = [
        {"a/b/c/input": "Batch input 1", "a/b/c/numeric_input": 1.5},
        {"a/b/c/input": "Batch input 2", "a/b/c/numeric_input": 2.5},
        {"a/b/c/input": "Batch input 3", "a/b/c/numeric_input": 3.5},
    ]
    batch_params = [
        {"a/b/param1": "test"},
        {"a/b/param2": "test"},
        {"a/b/param3": "test"},
    ]
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": batch_params,
            "entries": batch_entries,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    log_event_ids = response.json()["log_event_ids"]
    assert isinstance(log_event_ids, list)
    assert len(log_event_ids) == 3
    assert all(isinstance(id, int) for id in log_event_ids)
    assert sorted(log_event_ids) == list(
        range(min(log_event_ids), max(log_event_ids) + 1),
    )


@pytest.mark.anyio
async def test_create_log_w_image(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    img_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
        "sample_datasets/img.png",
    )
    success, buffer = cv2.imencode(".png", cv2.imread(img_path))
    assert success
    img = base64.b64encode(buffer).decode("utf-8")

    # log image
    response = await _create_log(
        client,
        project_name,
        params={},
        entries={
            "img_raw": img,
            "img_url": "https://upload.wikimedia.org/wikipedia/commons/4/45/Eopsaltria_australis_-_Mogo_Campground.jpg",
        },
    )

    assert response.status_code == 200, response.json()
    assert isinstance(response.json()["log_event_ids"][0], int)

    # Verify field type
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    assert field_types_response.json()["img_raw"]["data_type"] == "image"
    assert field_types_response.json()["img_url"]["data_type"] == "image"
    assert field_types_response.json()["img_raw"]["field_type"] == "entry"
    assert field_types_response.json()["img_url"]["field_type"] == "entry"
    assert field_types_response.json()["img_raw"]["mutable"] == True
    assert field_types_response.json()["img_url"]["mutable"] == True
    assert field_types_response.json()["img_raw"]["artifacts"] == ""
    assert field_types_response.json()["img_url"]["artifacts"] == ""
    assert field_types_response.json()["img_raw"]["created_at"] is not None
    assert field_types_response.json()["img_url"]["created_at"] is not None


@pytest.mark.anyio
async def test_create_log_w_audio(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    # Use generic dummy bytes, as the content doesn't need to be a valid MP3 for this test.
    dummy_audio_bytes = b"dummy_mp3_data"
    audio_b64 = base64.b64encode(dummy_audio_bytes).decode("utf-8")

    # Log audio as both a raw base64 string and a URL.
    response = await _create_log(
        client,
        project_name,
        params={},
        entries={
            "user_recording": audio_b64,
            "sound_effect": "https://example.com/sounds/effect.mp3",
        },
    )

    assert response.status_code == 200, response.json()
    assert isinstance(response.json()["log_event_ids"][0], int)

    # Verify field types
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200, field_types_response.json()
    fields = field_types_response.json()

    # Check that both fields were correctly inferred as 'audio'
    assert fields["user_recording"]["data_type"] == "audio"
    assert fields["sound_effect"]["data_type"] == "audio"

    # Check other properties
    assert fields["user_recording"]["field_type"] == "entry"
    assert fields["user_recording"]["mutable"] is True
    assert fields["user_recording"]["created_at"] is not None

    assert fields["sound_effect"]["field_type"] == "entry"
    assert fields["sound_effect"]["mutable"] is True
    assert fields["sound_effect"]["created_at"] is not None


@pytest.mark.anyio
async def test_create_logs_autoincrement_version(client: AsyncClient):
    project_name = "non-matching-versions"
    _ = await _create_project(client, project_name)

    # This should work fine
    response = await client.post(
        "/v0/logs",
        json={"project": project_name, "params": {"p1": "test"}},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # same version and value
    response = await client.post(
        "/v0/logs",
        json={"project": project_name, "params": {"p1": "test"}},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # same version and different value -> autoincrement
    response = await client.post(
        "/v0/logs",
        json={"project": project_name, "params": {"p1": "test_v1"}},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()


@pytest.mark.anyio
async def test_create_logs_project_not_found(client: AsyncClient):
    project_name = "non_existent_project"

    response = await _create_log(client, project_name)

    assert response.status_code == 404, response.json()
    assert response.json() == {"detail": "Project not found."}
