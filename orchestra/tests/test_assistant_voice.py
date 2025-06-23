from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError

from orchestra.db.dao.voice_dao import VoiceDAO
from orchestra.services.cartesia_service import CartesiaAPIError
from orchestra.services.cartesia_service import (
    CartesiaService as OriginalCartesiaService,
)
from orchestra.services.elevenlabs_service import ElevenLabsAPIError
from orchestra.services.elevenlabs_service import (
    ElevenLabsService as OriginalElevenLabsService,
)
from orchestra.tests.utils import HEADERS


def _get_sample_wav_bytes() -> bytes:
    sample_path = Path(__file__).parent / "sample_datasets" / "sample_recording.wav"
    return sample_path.read_bytes()


@pytest.fixture(autouse=True)
def mock_tts_services_factory(fastapi_app):
    """
    Provides a mock TTS provider instance and overrides the dependency for FastAPI.
    Yields the mock instance for tests to customize.
    Also patches send_pubsub_msg from where it's called by the middleware.
    """
    cartesia_mock = MagicMock(spec=OriginalCartesiaService)
    elevenlabs_mock = MagicMock(spec=OriginalElevenLabsService)

    # Default mock returns for successful calls
    mock_audio_bytes = b"mock_audio_data"
    cartesia_mock.generate_speech.return_value = (mock_audio_bytes, "audio/mpeg")
    elevenlabs_mock.generate_speech.return_value = (mock_audio_bytes, "audio/mpeg")

    # Mock other methods used in existing tests if this fixture is shared
    cartesia_mock.clone_voice.return_value = {
        "id": "mock-cloned-cartesia-id",
        "name": "Mock Cloned Voice",
        "description": "Desc",
        "gender": "female",
        "language": "en",
    }
    elevenlabs_mock.clone_voice.return_value = {
        "id": "mock-cloned-elevenlabs-id",
    }

    cartesia_mock.delete_voice.return_value = {"status": "success"}
    elevenlabs_mock.delete_voice.return_value = {"status": "ok"}

    # Patch send_pubsub_msg where it's looked up by the middleware's log_production_traffic function.
    # The log_production_traffic function is in the same module as ProductionTrafficMiddleware,
    # and it calls send_pubsub_msg directly.
    with patch(
        "orchestra.web.api.utils.production_traffic_middleware.send_pubsub_msg",
    ) as mock_send_pubsub:

        fastapi_app.dependency_overrides[
            OriginalCartesiaService
        ] = lambda: cartesia_mock
        fastapi_app.dependency_overrides[
            OriginalElevenLabsService
        ] = lambda: elevenlabs_mock

        yield cartesia_mock, elevenlabs_mock

        fastapi_app.dependency_overrides.clear()


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
    mock_tts_services_factory,
):
    cartesia_mock, _ = mock_tts_services_factory
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
    mock_tts_services_factory,
):
    cartesia_mock, _ = mock_tts_services_factory
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
    mock_tts_services_factory,
):
    cartesia_mock, _ = mock_tts_services_factory
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
    mock_tts_services_factory,
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
    mock_tts_services_factory: MagicMock,
):
    cartesia_mock, _ = mock_tts_services_factory
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
            f"/v0/assistant/voice/{cloned_voice_data['voice_id']}",
            headers=HEADERS,
        )
    cartesia_mock.delete_voice.assert_called_with(
        "mock-cloned-cartesia-id",
    )


@pytest.mark.anyio
async def test_delete_non_preset_voice(
    client: AsyncClient,
    dbsession,
    mock_tts_services_factory,
):
    cartesia_mock, _ = mock_tts_services_factory
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
    cartesia_mock.delete_voice.assert_called_once_with(
        voice_id_to_delete,
    )


@pytest.mark.anyio
async def test_delete_preset_voice_from_user_registration(
    client: AsyncClient,
    dbsession,
    mock_tts_services_factory,
):
    cartesia_mock, _ = mock_tts_services_factory
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
    cartesia_mock.delete_voice.assert_not_called()


@pytest.mark.anyio
async def test_delete_voice_fails_if_cartesia_fails_non_404(
    client: AsyncClient,
    dbsession,
    mock_tts_services_factory,
):
    cartesia_mock, _ = mock_tts_services_factory
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

    cartesia_mock.delete_voice.side_effect = CartesiaAPIError(
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

    cartesia_mock.delete_voice.side_effect = None
    cartesia_mock.delete_voice.return_value = {"status": "success"}
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.delete(f"/v0/assistant/voice/{voice_id}", headers=HEADERS)


@pytest.mark.anyio
async def test_delete_voice_succeeds_if_cartesia_404(
    client: AsyncClient,
    dbsession,
    mock_tts_services_factory,
):
    cartesia_mock, _ = mock_tts_services_factory
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

    cartesia_mock.delete_voice.side_effect = CartesiaAPIError(
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
    cartesia_mock.delete_voice.assert_called_once_with(voice_id)


@pytest.mark.anyio
async def test_generate_speech_cartesia_success(
    client: AsyncClient,
    mock_tts_services_factory,
    dbsession,
):
    cartesia_mock, _ = mock_tts_services_factory
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
            "/v0/assistant/voice/generate", json=payload, headers=HEADERS
        )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.content == b"mock_audio_data"
    assert resp.headers["content-type"] == "audio/mpeg"
    cartesia_mock.generate_speech.assert_called_once_with(
        transcript="Hello Cartesia",
        voice_id="cartesia-voice-123",
        model_id="sonic-2",
        output_format_container="mp3",
        output_sample_rate=None,  # Explicitly pass None if schema defaults to None
        output_bit_rate=None,  # Same as above
        language="en",
    )


@pytest.mark.anyio
async def test_generate_speech_elevenlabs_success(
    client: AsyncClient,
    mock_tts_services_factory,
    dbsession,
):
    _, elevenlabs_mock = mock_tts_services_factory
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
            "/v0/assistant/voice/generate", json=payload, headers=HEADERS
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
async def test_generate_speech_provider_api_error(
    client: AsyncClient,
    mock_tts_services_factory,
    dbsession,
):
    cartesia_mock, _ = mock_tts_services_factory
    user_id = "test-user"

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
            "/v0/assistant/voice/generate", json=payload, headers=HEADERS
        )

    assert (
        resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    )  # The error from Cartesia
    assert "TTS provider error: Cartesia down" in resp.json()["detail"]


@pytest.mark.anyio
async def test_design_generate_previews_success(
    client: AsyncClient, mock_tts_services_factory, dbsession
):
    _, elevenlabs_mock = mock_tts_services_factory
    user_id = "test-user"

    payload = {
        "voice_description": "A happy robot voice",
        "gender": "male",
        "accent": "american",
    }
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/voice/design/generate-previews",
            json=payload,
            headers=HEADERS,
        )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()["info"]
    assert "previews" in data
    assert len(data["previews"]) == 1
    assert data["previews"][0]["generated_voice_id"] == "temp_preview_123"
    assert data["text"] == "Test voice description"  # EL mock returns this

    elevenlabs_mock.design_voice_generate_previews.assert_called_once_with(
        voice_prompt="A happy robot voice",
        gender="male",
        accent="american",
        age=None,
        accent_strength=None,
    )


@pytest.mark.anyio
async def test_design_create_from_preview_success(
    client: AsyncClient, mock_tts_services_factory, dbsession
):
    _, elevenlabs_mock = mock_tts_services_factory
    user_id = await get_user_id_from_request_state(client)

    payload = {
        "generated_voice_id": "temp_preview_123",
        "voice_name": "Awesome Robot Voice",
        "final_voice_gender": "robot",  # Or 'male'/'female' as per your DB constraints
        "final_voice_language": "en",
        "voice_description_for_el_and_db": "A cool robot voice designed via text.",
    }

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/voice/design/create-from-preview",
            json=payload,
            headers=HEADERS,
        )

    assert resp.status_code == status.HTTP_201_CREATED
    data = resp.json()["info"]
    assert data["voice_id"] == "final_el_voice_id_456"
    assert data["name"] == "Awesome Robot Voice"
    assert data["provider"] == "elevenlabs"
    assert data["is_preset"] is False

    elevenlabs_mock.create_voice_from_generated_id.assert_called_once_with(
        voice_name="Awesome Robot Voice",
        generated_voice_id="temp_preview_123",
        description="A cool robot voice designed via text.",
        labels=None,
    )

    # Check DB
    db_voice = VoiceDAO(dbsession).get_voice_by_id(
        user_id=user_id, voice_id="final_el_voice_id_456"
    )
    assert db_voice is not None
    assert db_voice.name == "Awesome Robot Voice"
    assert db_voice.user_id == user_id
    assert db_voice.provider == "elevenlabs"

    # Clean up
    VoiceDAO(dbsession).delete_voice(user_id=user_id, voice_id="final_el_voice_id_456")
    dbsession.commit()


@pytest.mark.anyio
async def test_design_generate_previews_el_api_error(
    client: AsyncClient, mock_tts_services_factory, dbsession
):
    _, elevenlabs_mock = mock_tts_services_factory
    user_id = await get_user_id_from_request_state(client)

    elevenlabs_mock.design_voice_generate_previews.side_effect = ElevenLabsAPIError(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid description for EL",
    )
    payload = {"voice_description": "Invalid"}
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/voice/design/generate-previews",
            json=payload,
            headers=HEADERS,
        )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "ElevenLabs API error: Invalid description for EL" in resp.json()["detail"]


@pytest.mark.anyio
async def test_design_create_from_preview_el_api_error_cleanup(
    client: AsyncClient, mock_tts_services_factory, dbsession
):
    _, elevenlabs_mock = mock_tts_services_factory
    user_id = await get_user_id_from_request_state(client)

    # Simulate EL creating the voice but then an error occurring (e.g., DB error simulation later, or EL error on a subsequent step)
    # For this test, EL creation is mocked to succeed, but we'll check if delete is called if DB save (mocked by raising) fails
    elevenlabs_mock.create_voice_from_generated_id.return_value = {
        "voice_id": "el_voice_to_cleanup_789"
    }

    # Mock VoiceDAO.create_voice to raise an IntegrityError to simulate DB conflict
    with patch(
        "orchestra.db.dao.voice_dao.VoiceDAO.create_voice",
        side_effect=IntegrityError("mocked db error", params={}, orig=None),
    ):
        payload = {
            "generated_voice_id": "temp_preview_xyz",
            "voice_name": "Cleanup Test Voice",
            "final_voice_gender": "male",
            "final_voice_language": "fr",
        }
        with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
            mock_state.user_id = user_id
            resp = await client.post(
                "/v0/assistant/voice/design/create-from-preview",
                json=payload,
                headers=HEADERS,
            )

    assert resp.status_code == status.HTTP_409_CONFLICT  # Due to IntegrityError
    assert "Database error creating voice" in resp.json()["detail"]
    elevenlabs_mock.delete_voice.assert_called_once_with("el_voice_to_cleanup_789")
