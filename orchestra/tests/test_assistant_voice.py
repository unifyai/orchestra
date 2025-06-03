from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

from orchestra.services.cartesia_service import CartesiaAPIError
from orchestra.services.cartesia_service import (
    CartesiaService as OriginalCartesiaService,
)
from orchestra.tests.test_assistants import _get_sample_wav_bytes
from orchestra.tests.utils import HEADERS


@pytest.fixture(
    autouse=True,
)
def mock_cartesia_service_factory(
    fastapi_app,
):
    """
    Provides a mock CartesiaService instance and overrides the dependency for FastAPI.
    Yields the mock instance for tests to customize.
    Also patches send_pubsub_msg from where it's called by the middleware.
    """

    cartesia_mock_instance = MagicMock(spec=OriginalCartesiaService)

    cartesia_mock_instance.clone_voice = MagicMock(
        return_value={
            "id": "mock-cloned-cartesia-id",
            "name": "Mock Cloned Voice",
            "description": "Description of mock cloned voice",
            "gender": "female",
            "language": "en",
        },
    )
    cartesia_mock_instance.localize_voice = MagicMock(
        return_value={
            "id": "mock-localized-cartesia-id",
            "name": "Mock Localized Voice",
            "description": "Description of mock localized voice",
            "gender": "male",
            "language": "es",
        },
    )
    cartesia_mock_instance.delete_voice = MagicMock(return_value={"status": "success"})

    # Patch send_pubsub_msg where it's looked up by the middleware's log_production_traffic function.
    # The log_production_traffic function is in the same module as ProductionTrafficMiddleware,
    # and it calls send_pubsub_msg directly.
    with patch(
        "orchestra.web.api.utils.production_traffic_middleware.send_pubsub_msg",
    ) as mock_send_pubsub:

        fastapi_app.dependency_overrides[
            OriginalCartesiaService
        ] = lambda: cartesia_mock_instance
        yield cartesia_mock_instance  # Yield the CartesiaService mock
        fastapi_app.dependency_overrides.pop(OriginalCartesiaService, None)


async def get_user_id_from_request_state(
    client: AsyncClient,
    path: str = "/v0/assistant/voice",
) -> str:
    return "test-user-id-default"


# --- Test Cases ---


@pytest.mark.anyio
async def test_register_preset_voice(
    client: AsyncClient,
    dbsession,
    mock_cartesia_service_factory,
):
    user_id = await get_user_id_from_request_state(client)
    payload = {
        "voice_id": "cartesia-preset-echo",
        "name": "Echo (Preset)",
        "description": "A standard preset voice.",
        "gender": "female",
        "language": "en",
        "is_preset": True,
    }
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post("/v0/assistant/voice", json=payload, headers=HEADERS)

    assert resp.status_code == 201
    data = resp.json()["info"]
    assert data["voice_id"] == payload["voice_id"]
    assert data["name"] == payload["name"]
    assert data["is_preset"] is True

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.delete(
            f"/v0/assistant/voice/{payload['voice_id']}",
            headers=HEADERS,
        )


@pytest.mark.anyio
async def test_register_non_preset_voice(
    client: AsyncClient,
    dbsession,
    mock_cartesia_service_factory,
):
    user_id = await get_user_id_from_request_state(client)
    payload = {
        "voice_id": "user-owned-cartesia-voice-123",
        "name": "My Existing Cartesia Voice",
        "description": "A voice I already have in Cartesia.",
        "gender": "male",
        "language": "fr",
        "is_preset": False,
    }
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post("/v0/assistant/voice", json=payload, headers=HEADERS)

    assert resp.status_code == 201
    data = resp.json()["info"]
    assert data["voice_id"] == payload["voice_id"]
    assert data["is_preset"] is False

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.delete(
            f"/v0/assistant/voice/{payload['voice_id']}",
            headers=HEADERS,
        )


@pytest.mark.anyio
async def test_register_voice_already_exists_in_db(
    client: AsyncClient,
    dbsession,
    mock_cartesia_service_factory,
):
    user_id = await get_user_id_from_request_state(client)
    payload = {
        "voice_id": "db-conflict-voice",
        "name": "DB Conflict",
        "description": "...",
        "gender": "f",
        "language": "en",
        "is_preset": False,
    }

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp1 = await client.post("/v0/assistant/voice", json=payload, headers=HEADERS)
        assert resp1.status_code == 201
        resp2 = await client.post("/v0/assistant/voice", json=payload, headers=HEADERS)

    assert resp2.status_code == 409
    assert "already exists" in resp2.json()["detail"].lower()

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.delete(
            f"/v0/assistant/voice/{payload['voice_id']}",
            headers=HEADERS,
        )


@pytest.mark.anyio
async def test_list_voices_scenario(
    client: AsyncClient,
    dbsession,
    mock_cartesia_service_factory,
):
    user_id = await get_user_id_from_request_state(client)

    custom_voice_payload = {
        "voice_id": "user-custom-list",
        "name": "My Listed Custom",
        "description": "D1",
        "gender": "f",
        "language": "en",
        "is_preset": False,
    }
    user_registered_preset_payload = {
        "voice_id": "preset-listed-by-user",
        "name": "My Registered Preset",
        "description": "D2",
        "gender": "m",
        "language": "es",
        "is_preset": True,
    }
    other_user_id = "other-user-for-global-preset"
    global_preset_payload = {
        "voice_id": "global-preset-for-list",
        "name": "Global Preset EN",
        "description": "D3",
        "gender": "f",
        "language": "en",
        "is_preset": True,
    }

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.post(
            "/v0/assistant/voice",
            json=custom_voice_payload,
            headers=HEADERS,
        )
        await client.post(
            "/v0/assistant/voice",
            json=user_registered_preset_payload,
            headers=HEADERS,
        )

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = other_user_id
        await client.post(
            "/v0/assistant/voice",
            json=global_preset_payload,
            headers=HEADERS,
        )

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.get("/v0/assistant/voice", headers=HEADERS)

    assert resp.status_code == 200
    listed_voices = resp.json()["info"]
    listed_voice_ids = {v["voice_id"] for v in listed_voices}

    assert custom_voice_payload["voice_id"] in listed_voice_ids
    assert user_registered_preset_payload["voice_id"] in listed_voice_ids
    assert global_preset_payload["voice_id"] in listed_voice_ids
    assert len(listed_voices) == 3

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.delete(
            f"/v0/assistant/voice/{custom_voice_payload['voice_id']}",
            headers=HEADERS,
        )
        await client.delete(
            f"/v0/assistant/voice/{user_registered_preset_payload['voice_id']}",
            headers=HEADERS,
        )
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = other_user_id
        await client.delete(
            f"/v0/assistant/voice/{global_preset_payload['voice_id']}",
            headers=HEADERS,
        )


@pytest.mark.anyio
async def test_clone_voice(
    client: AsyncClient,
    dbsession,
    mock_cartesia_service_factory: MagicMock,
):
    user_id = await get_user_id_from_request_state(client)
    sample_audio_bytes = _get_sample_wav_bytes()

    form_data_fields = {
        "name": "Cloned Via Test",
        "language": "en",
        "description": "Test clone desc",
    }
    files_payload = {"file": ("sample.wav", sample_audio_bytes, "audio/wav")}

    # Ensure HEADERS does not force a Content-Type that would prevent multipart handling.
    # httpx will set the correct Content-Type for multipart when 'files' is provided.
    request_headers = HEADERS.copy()
    if "Content-Type" in request_headers:
        del request_headers["Content-Type"]

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/voice/clone",
            data=form_data_fields,
            files=files_payload,
            headers=request_headers,  # Use the potentially modified headers
        )

    assert resp.status_code == 201, f"Actual response: {resp.status_code} {resp.text}"
    cloned_voice_data = resp.json()["info"]
    assert cloned_voice_data["voice_id"] == "mock-cloned-cartesia-id"
    assert cloned_voice_data["name"] == "Mock Cloned Voice"
    assert cloned_voice_data["is_preset"] is False

    mock_cartesia_service_factory.clone_voice.assert_called_once_with(
        file_content=sample_audio_bytes,
        file_name="sample.wav",
        name="Cloned Via Test",
        language="en",
        description="Test clone desc",
    )

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.delete(
            f"/v0/assistant/voice/{cloned_voice_data['voice_id']}",
            headers=HEADERS,
        )
    mock_cartesia_service_factory.delete_voice.assert_called_with(
        "mock-cloned-cartesia-id",
    )


@pytest.mark.anyio
async def test_localize_voice(
    client: AsyncClient,
    dbsession,
    mock_cartesia_service_factory,
):
    user_id = await get_user_id_from_request_state(client)
    base_voice_id = "some-base-cartesia-id"
    payload = {
        "base_cartesia_voice_id": base_voice_id,
        "name": "Localized Test Voice",
        "target_language": "es",
        "original_speaker_gender": "male",
        "description": "Test localization",
    }
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/voice/localize",
            json=payload,
            headers=HEADERS,
        )

    assert resp.status_code == 201
    localized_voice_data = resp.json()["info"]
    assert localized_voice_data["voice_id"] == "mock-localized-cartesia-id"
    assert localized_voice_data["name"] == "Mock Localized Voice"
    assert localized_voice_data["language"] == "es"
    assert localized_voice_data["is_preset"] is False
    mock_cartesia_service_factory.localize_voice.assert_called_once()

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.delete(
            f"/v0/assistant/voice/{localized_voice_data['voice_id']}",
            headers=HEADERS,
        )
    mock_cartesia_service_factory.delete_voice.assert_called_with(
        "mock-localized-cartesia-id",
    )


@pytest.mark.anyio
async def test_delete_non_preset_voice(
    client: AsyncClient,
    dbsession,
    mock_cartesia_service_factory,
):
    user_id = await get_user_id_from_request_state(client)
    voice_id_to_delete = "delete-non-preset-test"
    reg_payload = {
        "voice_id": voice_id_to_delete,
        "name": "To Del NP",
        "description": "...",
        "gender": "f",
        "language": "de",
        "is_preset": False,
    }

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.post("/v0/assistant/voice", json=reg_payload, headers=HEADERS)
        resp_del = await client.delete(
            f"/v0/assistant/voice/{voice_id_to_delete}",
            headers=HEADERS,
        )

    assert resp_del.status_code == 200
    assert "deleted successfully" in resp_del.json()["info"].lower()
    mock_cartesia_service_factory.delete_voice.assert_called_once_with(
        voice_id_to_delete,
    )


@pytest.mark.anyio
async def test_delete_preset_voice_from_user_registration(
    client: AsyncClient,
    dbsession,
    mock_cartesia_service_factory,
):
    user_id = await get_user_id_from_request_state(client)
    voice_id_to_delete = "delete-preset-registration-test"
    reg_payload = {
        "voice_id": voice_id_to_delete,
        "name": "To Del P",
        "description": "...",
        "gender": "m",
        "language": "ja",
        "is_preset": True,
    }

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.post("/v0/assistant/voice", json=reg_payload, headers=HEADERS)
        resp_del = await client.delete(
            f"/v0/assistant/voice/{voice_id_to_delete}",
            headers=HEADERS,
        )

    assert resp_del.status_code == 200
    assert "deleted successfully" in resp_del.json()["info"].lower()
    mock_cartesia_service_factory.delete_voice.assert_not_called()


@pytest.mark.anyio
async def test_delete_voice_fails_if_cartesia_fails_non_404(
    client: AsyncClient,
    dbsession,
    mock_cartesia_service_factory,
):
    user_id = await get_user_id_from_request_state(client)
    voice_id = "non-preset-cartesia-del-fail"
    reg_payload = {
        "voice_id": voice_id,
        "name": "CartFailDel",
        "description": "...",
        "gender": "f",
        "language": "en",
        "is_preset": False,
    }

    mock_cartesia_service_factory.delete_voice.side_effect = CartesiaAPIError(
        status_code=500,
        detail="Cartesia server meltdown",
    )

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.post("/v0/assistant/voice", json=reg_payload, headers=HEADERS)
        resp_del = await client.delete(
            f"/v0/assistant/voice/{voice_id}",
            headers=HEADERS,
        )

    assert resp_del.status_code == 500
    assert (
        "Failed to delete voice from Cartesia: Cartesia server meltdown"
        in resp_del.json()["detail"]
    )

    mock_cartesia_service_factory.delete_voice.side_effect = None
    mock_cartesia_service_factory.delete_voice.return_value = {"status": "success"}
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.delete(f"/v0/assistant/voice/{voice_id}", headers=HEADERS)


@pytest.mark.anyio
async def test_delete_voice_succeeds_if_cartesia_404(
    client: AsyncClient,
    dbsession,
    mock_cartesia_service_factory,
):
    user_id = await get_user_id_from_request_state(client)
    voice_id = "non-preset-cartesia-404"
    reg_payload = {
        "voice_id": voice_id,
        "name": "Cart404Del",
        "description": "...",
        "gender": "f",
        "language": "it",
        "is_preset": False,
    }

    mock_cartesia_service_factory.delete_voice.side_effect = CartesiaAPIError(
        status_code=404,
        detail="Not found on Cartesia",
    )

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.post("/v0/assistant/voice", json=reg_payload, headers=HEADERS)
        resp_del = await client.delete(
            f"/v0/assistant/voice/{voice_id}",
            headers=HEADERS,
        )

    assert resp_del.status_code == 200
    assert "deleted successfully" in resp_del.json()["info"].lower()
    mock_cartesia_service_factory.delete_voice.assert_called_once_with(voice_id)
