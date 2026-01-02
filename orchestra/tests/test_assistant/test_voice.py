import base64
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.services.cartesia_service import CartesiaAPIError
from orchestra.services.cartesia_service import (
    CartesiaService as OriginalCartesiaService,
)
from orchestra.services.deepgram_service import (
    DeepgramService as OriginalDeepgramService,
)
from orchestra.services.elevenlabs_service import ElevenLabsAPIError
from orchestra.services.elevenlabs_service import (
    ElevenLabsService as OriginalElevenLabsService,
)
from orchestra.services.openai_service import OpenAIService as OriginalOpenAIService
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
    ) as mock_wake_up, patch(
        "orchestra.web.api.assistant.views.reawaken_assistant",
    ) as mock_reawaken:

        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})

        yield mock_wake_up, mock_reawaken


def _get_sample_wav_bytes() -> bytes:
    sample_path = Path(__file__).parent / "sample_datasets" / "sample_recording.wav"
    if not sample_path.exists():
        # Create a tiny dummy wav if not found, to prevent test setup failure
        # This is a placeholder and not a valid WAV for actual processing
        return b"RIFF\x00\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x08\x00data\x00\x00\x00\x00"
    return sample_path.read_bytes()


def _get_dummy_base64_audio() -> str:
    # Create a very short, minimal (likely invalid for actual playback) base64 audio string
    # This is just to have some data for the audio_base_64 field in mocks
    dummy_bytes = b"This is not real audio but will be base64 encoded."
    return base64.b64encode(dummy_bytes).decode("utf-8")


@pytest.fixture(autouse=True)
def mock_tts_services_factory(fastapi_app):
    """
    Provides mock provider instances and overrides the dependencies for FastAPI.
    Yields the mock instances for tests to customize.
    """
    cartesia_mock = MagicMock(spec=OriginalCartesiaService)
    elevenlabs_mock = MagicMock(spec=OriginalElevenLabsService)
    deepgram_mock = MagicMock(spec=OriginalDeepgramService)
    openai_mock = MagicMock(spec=OriginalOpenAIService)

    # Generate speech endpoint data
    mock_audio_bytes = b"mock_audio_data"
    cartesia_mock.generate_speech.return_value = (mock_audio_bytes, "audio/mpeg")
    elevenlabs_mock.generate_speech.return_value = (mock_audio_bytes, "audio/mpeg")
    openai_mock.generate_speech.return_value = (mock_audio_bytes, "audio/mpeg")

    # Clone voice endpoint data
    cartesia_mock.clone_voice.return_value = {
        "id": "mock-cloned-cartesia-id",
        "name": "Mock Cloned Voice",
        "description": "Desc",
        "gender": "female",
        "language": "en",
    }
    elevenlabs_mock.clone_voice.return_value = {
        "voice_id": "mock-cloned-elevenlabs-id",
    }

    # Delete voice endpoint data
    cartesia_mock.delete_voice.return_value = {"status": "success"}
    elevenlabs_mock.delete_voice.return_value = {"status": "ok"}

    # Design voice endpoint data
    elevenlabs_mock.design_voice_generate_previews.return_value = {
        "previews": [
            {
                "generated_voice_id": "temp_preview_el_123",
                "audio_base_64": _get_dummy_base64_audio(),
                "media_type": "audio/mpeg",
            },
        ],
        "text": "Mock text used for generating ElevenLabs preview.",
    }
    elevenlabs_mock.create_voice_from_generated_id.return_value = {
        "voice_id": "final_el_voice_id_abc_789",
    }

    # Language detection mocks
    deepgram_mock.detect_language_from_audio.return_value = "en"
    openai_mock.detect_language_from_text.return_value = "en"

    # Patch send_pubsub_msg where it's looked up by the middleware's log_production_traffic function.
    with patch(
        "orchestra.web.api.utils.production_traffic_middleware.send_pubsub_msg",
    ) as mock_send_pubsub:
        fastapi_app.dependency_overrides[
            OriginalCartesiaService
        ] = lambda: cartesia_mock
        fastapi_app.dependency_overrides[
            OriginalElevenLabsService
        ] = lambda: elevenlabs_mock
        fastapi_app.dependency_overrides[
            OriginalDeepgramService
        ] = lambda: deepgram_mock
        fastapi_app.dependency_overrides[OriginalOpenAIService] = lambda: openai_mock

        yield cartesia_mock, elevenlabs_mock, deepgram_mock, openai_mock

        fastapi_app.dependency_overrides.clear()


async def get_user_id_from_request_state(
    client: AsyncClient,
    path: str = "/v0/assistant/voice",
) -> str:
    return "test-user"


# --- Test Voice CRUD ---


@pytest.mark.anyio
async def test_register_preset_voice(
    client: AsyncClient,
    dbsession,
    mock_tts_services_factory,
):
    _, _, _, _ = mock_tts_services_factory
    user_id = await get_user_id_from_request_state(client)
    payload = {
        "voice_id": "cartesia-preset-echo",
        "name": "Echo (Preset)",
        "description": "A standard preset voice.",
        "gender": "female",
        "language": "en",
        "provider": "cartesia",
        "is_preset": True,
    }
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post("/v0/assistant/voice", json=payload, headers=HEADERS)

    assert resp.status_code == status.HTTP_201_CREATED
    data = resp.json()["info"]
    assert data["voice_id"] == payload["voice_id"]
    assert data["is_preset"] is True

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.delete(
            f"/v0/assistant/voice/{payload['voice_id']}?provider={payload['provider']}",
            headers=HEADERS,
        )


@pytest.mark.anyio
async def test_register_non_preset_voice(
    client: AsyncClient,
    dbsession,
    mock_tts_services_factory,
):
    _, _, _, _ = mock_tts_services_factory
    user_id = await get_user_id_from_request_state(client)
    payload = {
        "voice_id": "user-owned-cartesia-voice-123",
        "name": "My Existing Cartesia Voice",
        "description": "A voice I already have in Cartesia.",
        "gender": "male",
        "language": "fr",
        "provider": "cartesia",
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
            f"/v0/assistant/voice/{payload['voice_id']}?provider={payload['provider']}",
            headers=HEADERS,
        )


@pytest.mark.anyio
async def test_register_voice_already_exists_in_db(
    client: AsyncClient,
    dbsession,
    mock_tts_services_factory,
):
    _, _, _, _ = mock_tts_services_factory
    user_id = await get_user_id_from_request_state(client)
    payload = {
        "voice_id": "db-conflict-voice",
        "name": "DB Conflict",
        "description": "...",
        "gender": "f",
        "language": "en",
        "is_preset": False,
        "provider": "cartesia",
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
            f"/v0/assistant/voice/{payload['voice_id']}?provider={payload['provider']}",
            headers=HEADERS,
        )


@pytest.mark.anyio
async def test_register_voice_missing_required_field(
    client: AsyncClient,
    mock_tts_services_factory,
):
    user_id = await get_user_id_from_request_state(client)

    # Payload missing 'name' which is required
    payload = {
        "voice_id": "schema-test-voice-invalid",
        "description": "A voice for schema validation test.",
        "gender": "female",
        "language": "en",
        "provider": "cartesia",
        "is_preset": False,
    }
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post("/v0/assistant/voice", json=payload, headers=HEADERS)

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    error_details = resp.json()["detail"]
    found_error = False
    for e in error_details:
        if (
            isinstance(e["loc"], list)
            and "name" in e["loc"]
            and (
                "field required" in e["msg"].lower()
                or "value_error.missing" in e.get("type", "").lower()
            )
        ):
            found_error = True
            break
    assert found_error, f"Expected error for missing 'name' field, got: {error_details}"


@pytest.mark.anyio
async def test_delete_non_preset_voice(
    client: AsyncClient,
    dbsession,
    mock_tts_services_factory,
):
    cartesia_mock, _, _, _ = mock_tts_services_factory
    user_id = await get_user_id_from_request_state(client)
    voice_id_to_delete = "delete-non-preset-test"
    provider = "cartesia"
    reg_payload = {
        "voice_id": voice_id_to_delete,
        "name": "To Del NP",
        "description": "...",
        "gender": "f",
        "language": "de",
        "is_preset": False,
        "provider": provider,
    }

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.post("/v0/assistant/voice", json=reg_payload, headers=HEADERS)
        resp_del = await client.delete(
            f"/v0/assistant/voice/{voice_id_to_delete}?provider={provider}",
            headers=HEADERS,
        )

    assert resp_del.status_code == 200
    assert "deleted successfully" in resp_del.json()["info"].lower()
    cartesia_mock.delete_voice.assert_called_once_with(
        voice_id_to_delete,
    )


@pytest.mark.anyio
async def test_delete_preset_voice(
    client: AsyncClient,
    dbsession,
    mock_tts_services_factory,
):
    cartesia_mock, _, _, _ = mock_tts_services_factory
    user_id = await get_user_id_from_request_state(client)
    voice_id_to_delete = "delete-preset-registration-test"
    provider = "cartesia"
    reg_payload = {
        "voice_id": voice_id_to_delete,
        "name": "To Del P",
        "description": "...",
        "gender": "m",
        "language": "ja",
        "is_preset": True,
        "provider": provider,
    }

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.post("/v0/assistant/voice", json=reg_payload, headers=HEADERS)
        resp_del = await client.delete(
            f"/v0/assistant/voice/{voice_id_to_delete}?provider={provider}",
            headers=HEADERS,
        )

    assert resp_del.status_code == 200
    assert "deleted successfully" in resp_del.json()["info"].lower()
    cartesia_mock.delete_voice.assert_not_called()


@pytest.mark.anyio
async def test_delete_voice_in_use_fails(
    client: AsyncClient,
    dbsession,
    mock_tts_services_factory,
):
    cartesia_mock, _, _, _ = mock_tts_services_factory
    user_id = await get_user_id_from_request_state(client)
    voice_id_in_use = "voice-in-use-test"
    provider = "cartesia"

    # 1. Register the voice
    reg_payload = {
        "voice_id": voice_id_in_use,
        "name": "Voice In Use",
        "description": "...",
        "gender": "f",
        "language": "en",
        "is_preset": False,
        "provider": provider,
    }
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        reg_resp = await client.post(
            "/v0/assistant/voice",
            json=reg_payload,
            headers=HEADERS,
        )
        assert reg_resp.status_code == status.HTTP_201_CREATED

    # 2. Create an assistant that uses this voice
    assistant_payload = {
        "first_name": "Voice",
        "surname": "User",
        "voice_id": voice_id_in_use,
        "voice_provider": provider,
        "voice_mode": "tts",
        "create_infra": False,
    }
    create_resp = await client.post(
        "/v0/assistant",
        json=assistant_payload,
        headers=HEADERS,
    )
    assert create_resp.status_code == status.HTTP_200_OK
    assistant_id = create_resp.json()["info"]["agent_id"]

    # 3. Attempt to delete the voice (should fail)
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp_del_fail = await client.delete(
            f"/v0/assistant/voice/{voice_id_in_use}?provider={provider}",
            headers=HEADERS,
        )

    assert resp_del_fail.status_code == status.HTTP_409_CONFLICT
    assert "in use by at least one assistant" in resp_del_fail.json()["detail"]

    # 4. Clean up: Delete assistant, then the voice
    del_assistant_resp = await client.delete(
        f"/v0/assistant/{assistant_id}",
        headers=HEADERS,
    )
    assert del_assistant_resp.status_code == 200

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp_del_success = await client.delete(
            f"/v0/assistant/voice/{voice_id_in_use}?provider={provider}",
            headers=HEADERS,
        )
    assert resp_del_success.status_code == 200
    cartesia_mock.delete_voice.assert_called_once_with(voice_id_in_use)


@pytest.mark.anyio
async def test_list_voices(
    client: AsyncClient,
    dbsession,
    mock_tts_services_factory,
):
    user_id = await get_user_id_from_request_state(client)
    other_user = await create_test_user(client, "other_user@voice.com")
    other_user_id = other_user["id"]

    custom_voice_payload = {
        "voice_id": "user-custom-list",
        "name": "My Listed Custom",
        "description": "D1",
        "gender": "f",
        "language": "en",
        "provider": "cartesia",
        "is_preset": False,
    }
    user_registered_preset_payload = {
        "voice_id": "preset-listed-by-user",
        "name": "My Registered Preset",
        "description": "D2",
        "gender": "m",
        "language": "es",
        "provider": "elevenlabs",
        "is_preset": True,
    }
    global_preset_payload = {
        "voice_id": "global-preset-for-list",
        "name": "Global Preset EN",
        "description": "D3",
        "gender": "f",
        "language": "en",
        "provider": "cartesia",
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
            headers=other_user["headers"],
        )

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.get("/v0/assistant/voice", headers=HEADERS)

    assert resp.status_code == 200
    listed_voices = resp.json()["info"]
    listed_voice_ids = {v["voice_id"] for v in listed_voices}

    assert custom_voice_payload["voice_id"] in listed_voice_ids
    assert user_registered_preset_payload["voice_id"] in listed_voice_ids
    assert global_preset_payload["voice_id"] not in listed_voice_ids
    assert len(listed_voices) == 2

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.delete(
            f"/v0/assistant/voice/{custom_voice_payload['voice_id']}?provider={custom_voice_payload['provider']}",
            headers=HEADERS,
        )
        await client.delete(
            f"/v0/assistant/voice/{user_registered_preset_payload['voice_id']}?provider={user_registered_preset_payload['provider']}",
            headers=HEADERS,
        )
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = other_user_id
        await client.delete(
            f"/v0/assistant/voice/{global_preset_payload['voice_id']}?provider={global_preset_payload['provider']}",
            headers=other_user["headers"],
        )


# --- Test Clone Voice ---


@pytest.mark.anyio
async def test_clone_voice_cartesia(
    client: AsyncClient,
    dbsession,
    mock_tts_services_factory: MagicMock,
):
    cartesia_mock, _, _, _ = mock_tts_services_factory
    user_id = await get_user_id_from_request_state(client)
    sample_audio_bytes = _get_sample_wav_bytes()

    form_data_fields = {
        "name": "Cloned Via Test",
        "language": "en",
        "description": "Test clone desc",
        "provider": "cartesia",
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
            headers=request_headers,
        )

    assert resp.status_code == 201, f"Actual response: {resp.status_code} {resp.text}"
    cloned_voice_data = resp.json()["info"]
    assert cloned_voice_data["voice_id"] == "mock-cloned-cartesia-id"
    assert cloned_voice_data["name"] == "Cloned Via Test"
    assert cloned_voice_data["is_preset"] is False

    cartesia_mock.clone_voice.assert_called_once_with(
        file_content=sample_audio_bytes,
        file_name="sample.wav",
        name="Cloned Via Test",
        language="en",
        description="Test clone desc",
    )

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.delete(
            f"/v0/assistant/voice/{cloned_voice_data['voice_id']}?provider=cartesia",
            headers=HEADERS,
        )
    cartesia_mock.delete_voice.assert_called_with(
        "mock-cloned-cartesia-id",
    )


@pytest.mark.anyio
async def test_clone_voice_autodetect_language(
    client: AsyncClient,
    dbsession,
    mock_tts_services_factory: MagicMock,
):
    cartesia_mock, _, deepgram_mock, _ = mock_tts_services_factory
    deepgram_mock.detect_language_from_audio.return_value = "fr"
    user_id = await get_user_id_from_request_state(client)
    sample_audio_bytes = _get_sample_wav_bytes()

    form_data_fields = {
        "name": "Cloned Via Test Autodetect",
        "description": "Test clone desc autodetect",
        "provider": "cartesia",
    }
    files_payload = {"file": ("sample.wav", sample_audio_bytes, "audio/wav")}

    request_headers = HEADERS.copy()
    if "Content-Type" in request_headers:
        del request_headers["Content-Type"]

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/voice/clone",
            data=form_data_fields,
            files=files_payload,
            headers=request_headers,
        )

    assert resp.status_code == 201
    cloned_voice_data = resp.json()["info"]
    assert cloned_voice_data["language"] == "fr"

    deepgram_mock.detect_language_from_audio.assert_called_once_with(
        sample_audio_bytes,
        ANY,
        "audio/wav",
    )
    cartesia_mock.clone_voice.assert_called_once_with(
        file_content=sample_audio_bytes,
        file_name="sample.wav",
        name="Cloned Via Test Autodetect",
        language="fr",
        description="Test clone desc autodetect",
    )
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.delete(
            f"/v0/assistant/voice/{cloned_voice_data['voice_id']}?provider=cartesia",
            headers=HEADERS,
        )


@pytest.mark.anyio
async def test_clone_voice_cartesia_api_error_on_clone(
    client: AsyncClient,
    mock_tts_services_factory,
):
    cartesia_mock, _, _, _ = mock_tts_services_factory
    user_id = await get_user_id_from_request_state(client)
    sample_audio_bytes = _get_sample_wav_bytes()

    cartesia_mock.clone_voice.side_effect = CartesiaAPIError(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Cartesia clone service down",
    )

    form_data_fields = {
        "name": "Clone Fails API",
        "language": "en",
        "provider": "cartesia",
    }
    files_payload = {"file": ("sample.wav", sample_audio_bytes, "audio/wav")}
    request_headers = HEADERS.copy()
    if "Content-Type" in request_headers:
        del request_headers["Content-Type"]

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/voice/clone",
            data=form_data_fields,
            files=files_payload,
            headers=request_headers,
        )

    assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert "Cartesia API error: Cartesia clone service down" in resp.json()["detail"]
    cartesia_mock.delete_voice.assert_not_called()


@pytest.mark.anyio
async def test_clone_voice_elevenlabs(
    client: AsyncClient,
    dbsession,
    mock_tts_services_factory: MagicMock,
):
    _, elevenlabs_mock, _, _ = mock_tts_services_factory
    user_id = await get_user_id_from_request_state(client)
    sample_audio_bytes = _get_sample_wav_bytes()

    form_data_fields = {
        "name": "Cloned Via Test",
        "language": "en",
        "description": "Test clone desc",
        "provider": "elevenlabs",
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
            headers=request_headers,
        )

    assert resp.status_code == 201, f"Actual response: {resp.status_code} {resp.text}"
    cloned_voice_data = resp.json()["info"]
    assert cloned_voice_data["voice_id"] == "mock-cloned-elevenlabs-id"
    assert cloned_voice_data["name"] == "Cloned Via Test"
    assert cloned_voice_data["is_preset"] is False

    elevenlabs_mock.clone_voice.assert_called_once_with(
        file_content=sample_audio_bytes,
        file_name="sample.wav",
        name="Cloned Via Test",
        description="Test clone desc",
    )

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.delete(
            f"/v0/assistant/voice/{cloned_voice_data['voice_id']}?provider=elevenlabs",
            headers=HEADERS,
        )
    elevenlabs_mock.delete_voice.assert_called_with(
        "mock-cloned-elevenlabs-id",
    )


@pytest.mark.anyio
async def test_clone_voice_elevenlabs_api_error_on_clone(
    client: AsyncClient,
    mock_tts_services_factory,
):
    _, elevenlabs_mock, _, _ = mock_tts_services_factory
    user_id = await get_user_id_from_request_state(client)
    sample_audio_bytes = _get_sample_wav_bytes()

    elevenlabs_mock.clone_voice.side_effect = ElevenLabsAPIError(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="ElevenLabs clone failed: Invalid audio format",
    )

    form_data_fields = {
        "name": "EL Clone Fails API",
        "language": "en",
        "provider": "elevenlabs",
    }
    files_payload = {"file": ("sample.wav", sample_audio_bytes, "audio/wav")}
    request_headers = HEADERS.copy()
    if "Content-Type" in request_headers:
        del request_headers["Content-Type"]

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/voice/clone",
            data=form_data_fields,
            files=files_payload,
            headers=request_headers,
        )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert (
        "ElevenLabs API error: ElevenLabs clone failed: Invalid audio format"
        in resp.json()["detail"]
    )
    elevenlabs_mock.delete_voice.assert_not_called()


# --- Test Generate Speech ---


@pytest.mark.anyio
async def test_generate_speech_cartesia_success(
    client: AsyncClient,
    mock_tts_services_factory,
    dbsession,
):
    cartesia_mock, _, _, _ = mock_tts_services_factory
    user_id = await get_user_id_from_request_state(client)

    payload = {
        "text": "Hello Cartesia",
        "provider": "cartesia",
        "voice_id": "cartesia-voice-123",
        "model_id": "sonic-2",
        "output_format": "mp3",
        "cartesia_language": "en",
    }
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/voice/generate",
            json=payload,
            headers=HEADERS,
        )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.content == b"mock_audio_data"
    assert resp.headers["content-type"] == "audio/mpeg"
    cartesia_mock.generate_speech.assert_called_once_with(
        transcript="Hello Cartesia",
        voice_id="cartesia-voice-123",
        model_id="sonic-2",
        output_format_container="mp3",
        output_sample_rate=None,
        output_bit_rate=None,
        language="en",
    )


@pytest.mark.anyio
async def test_generate_speech_elevenlabs_success(
    client: AsyncClient,
    mock_tts_services_factory,
    dbsession,
):
    _, elevenlabs_mock, _, _ = mock_tts_services_factory
    user_id = await get_user_id_from_request_state(client)

    payload = {
        "text": "Hello ElevenLabs",
        "provider": "elevenlabs",
        "voice_id": "elevenlabs-voice-123",
        "model_id": "eleven_multilingual_v2",
        "output_format": "wav",
        "elevenlabs_voice_settings_stability": 0.5,
    }
    elevenlabs_mock.generate_speech.return_value = (b"mock_wav_audio", "audio/wav")

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/voice/generate",
            json=payload,
            headers=HEADERS,
        )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.content == b"mock_wav_audio"
    assert resp.headers["content-type"] == "audio/wav"
    elevenlabs_mock.generate_speech.assert_called_once_with(
        text="Hello ElevenLabs",
        voice_id="elevenlabs-voice-123",
        model_id="eleven_multilingual_v2",
        output_format="wav",
        optimize_streaming_latency=None,
        stability=0.5,
        similarity_boost=None,
    )


@pytest.mark.anyio
async def test_generate_speech_openai_success(
    client: AsyncClient,
    mock_tts_services_factory,
    dbsession,
):
    _, _, _, openai_mock = mock_tts_services_factory
    user_id = await get_user_id_from_request_state(client)

    payload = {
        "text": "Hello OpenAI",
        "provider": "openai",
        "voice_id": "marin",
        "model_id": "gpt-4o-mini-tts",
        "output_format": "mp3",
    }
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/voice/generate",
            json=payload,
            headers=HEADERS,
        )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.content == b"mock_audio_data"
    assert resp.headers["content-type"] == "audio/mpeg"
    openai_mock.generate_speech.assert_called_once_with(
        text="Hello OpenAI",
        voice_id="marin",
        model_id="gpt-4o-mini-tts",
        output_format="mp3",
    )


@pytest.mark.anyio
async def test_generate_speech_provider_api_error(
    client: AsyncClient,
    mock_tts_services_factory,
    dbsession,
):
    cartesia_mock, _, _, _ = mock_tts_services_factory
    user_id = await get_user_id_from_request_state(client)

    cartesia_mock.generate_speech.side_effect = CartesiaAPIError(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Cartesia down",
    )
    payload = {
        "text": "Test error",
        "provider": "cartesia",
        "voice_id": "v-err",
        "output_format": "mp3",
    }
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/voice/generate",
            json=payload,
            headers=HEADERS,
        )

    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert "TTS provider error: Cartesia down" in resp.json()["detail"]


# --- Test Design Voice ---


@pytest.mark.anyio
async def test_design_generate_previews_success(
    client: AsyncClient,
    mock_tts_services_factory,
    dbsession,
):
    _, elevenlabs_mock, _, _ = mock_tts_services_factory
    user_id = await get_user_id_from_request_state(client)

    payload = {
        "voice_description": "A very happy and cheerful robot voice, beep boop.",
    }
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/voice/design/preview",
            json=payload,
            headers=HEADERS,
        )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()["info"]
    assert "previews" in data
    assert len(data["previews"]) == 1
    assert data["previews"][0]["generated_voice_id"] == "temp_preview_el_123"
    assert data["text"] == "Mock text used for generating ElevenLabs preview."

    elevenlabs_mock.design_voice_generate_previews.assert_called_once_with(
        voice_description=payload["voice_description"],
        text_for_preview=None,
        auto_generate_text_flag=None,
        model_id_for_design=None,
    )


@pytest.mark.anyio
async def test_design_generate_previews_el_api_error(
    client: AsyncClient,
    mock_tts_services_factory,
    dbsession,
):
    _, elevenlabs_mock, _, _ = mock_tts_services_factory
    user_id = await get_user_id_from_request_state(client)

    elevenlabs_mock.design_voice_generate_previews.side_effect = ElevenLabsAPIError(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid description for EL",
    )
    payload = {
        "voice_description": "This is a sufficiently long description for testing API errors.",
    }
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/voice/design/preview",
            json=payload,
            headers=HEADERS,
        )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "ElevenLabs API error: Invalid description for EL" in resp.json()["detail"]
