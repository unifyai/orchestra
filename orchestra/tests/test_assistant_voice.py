import pytest
from httpx import AsyncClient
from unittest.mock import MagicMock, patch
import io

from orchestra.db.models.orchestra_models import (
    Voice as VoiceModel,
)  # For direct DB checks if needed
from orchestra.services.cartesia_service import CartesiaService as OriginalCartesiaService
from orchestra.services.cartesia_service import CartesiaAPIError
from orchestra.tests.utils import (
    HEADERS,
)  # Assuming HEADERS provides auth for a test user
from orchestra.tests.test_assistants import (
    _get_sample_wav_bytes,
)  # Re-use for sample audio


@pytest.fixture
def mock_cartesia_service_factory(
    fastapi_app,
):  # autouse to apply to all tests in this file
    """
    Provides a mock CartesiaService instance and overrides the dependency for FastAPI.
    Yields the mock instance for tests to customize.
    """
    mock_instance = MagicMock(spec=OriginalCartesiaService)

    # Default successful mock behaviors - can be overridden in tests
    mock_instance.clone_voice = MagicMock(
        return_value={
            "id": "mock-cloned-cartesia-id",
            "name": "Mock Cloned Voice",
            "description": "Description of mock cloned voice",
            "gender": "female",  # Cartesia clone actually doesn't return gender
            "language": "en",
        }
    )
    mock_instance.localize_voice = MagicMock(
        return_value={
            "id": "mock-localized-cartesia-id",
            "name": "Mock Localized Voice",
            "description": "Description of mock localized voice",
            "gender": "male",  # Gender is provided in request for localize
            "language": "es",
        }
    )
    mock_instance.delete_voice = MagicMock(return_value={"status": "success"})

    # Override the dependency in the FastAPI application
    # The key is the original class used in Depends()
    fastapi_app.dependency_overrides[OriginalCartesiaService] = lambda: mock_instance

    yield mock_instance

    # Clean up the override
    fastapi_app.dependency_overrides.pop(OriginalCartesiaService, None)

async def get_user_id_from_request_state(
    client: AsyncClient, path: str = "/v0/assistant/voice"
) -> str:
    """Helper to get user_id if your HEADERS setup implies a specific user."""
    # This is a bit indirect. A better way might be a fixture that provides the test user's ID.
    # For now, we assume HEADERS are tied to a consistent test user.
    # If your authentication middleware sets request.state.user_id, this could be used
    # by making a dummy authenticated request if necessary, or by having a known test user ID.
    # Let's assume a fixed test user ID for clarity in tests when patching.
    return "test-user-id-default"

# --- Test Cases ---


@pytest.mark.anyio
async def test_register_preset_voice(
    client: AsyncClient, dbsession, mock_cartesia_service_factory
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

    # Cleanup: Delete the registered preset from the user's list
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.delete(
            f"/v0/assistant/voice/{payload['voice_id']}", headers=HEADERS
         )


@pytest.mark.anyio
async def test_register_non_preset_voice(
    client: AsyncClient, session, mock_cartesia_service_factory
):
    user_id = await get_user_id_from_request_state(client)
    payload = {
        "voice_id": "user-owned-cartesia-voice-123",  # Assume this exists in Cartesia
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
            f"/v0/assistant/voice/{payload['voice_id']}", headers=HEADERS
        )
    # mock_cartesia_service_factory.delete_voice would have been called by the delete if not preset


@pytest.mark.anyio
async def test_register_voice_already_exists_in_db(
    client: AsyncClient, dbsession, mock_cartesia_service_factory
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
        resp2 = await client.post(
            "/v0/assistant/voice", json=payload, headers=HEADERS
        )  # Attempt to register again

    assert resp2.status_code == 409
    assert "already exists" in resp2.json()["detail"].lower()

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.delete(
            f"/v0/assistant/voice/{payload['voice_id']}", headers=HEADERS
        )


@pytest.mark.anyio
async def test_list_voices_scenario(
    client: AsyncClient, dbsession, mock_cartesia_service_factory
):
    user_id = await get_user_id_from_request_state(client)

    # Voice 1: User's custom (cloned/localized, then registered)
    custom_voice_payload = {
        "voice_id": "user-custom-list",
        "name": "My Listed Custom",
        "description": "D1",
        "gender": "f",
        "language": "en",
        "is_preset": False,
    }
    # Voice 2: A preset registered by this user
    user_registered_preset_payload = {
        "voice_id": "preset-listed-by-user",
        "name": "My Registered Preset",
        "description": "D2",
        "gender": "m",
        "language": "es",
        "is_preset": True,
    }
    # Voice 3: A global preset NOT registered by this user (simulate its existence in DB)
    # To do this properly, it needs to be in DB with is_preset=True but different/no user_id or handled by DAO.
    # For this test, we'll register it under a different dummy user to ensure it appears due to global preset logic in view.
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
            "/v0/assistant/voice", json=custom_voice_payload, headers=HEADERS
        )
        await client.post(
            "/v0/assistant/voice", json=user_registered_preset_payload, headers=HEADERS
        )

    # Simulate global preset registration by another entity/admin
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = other_user_id
        await client.post(
            "/v0/assistant/voice", json=global_preset_payload, headers=HEADERS
        )

    # Now list for the original user
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.get("/v0/assistant/voice", headers=HEADERS)

    assert resp.status_code == 200
    listed_voices = resp.json()["info"]
    listed_voice_ids = {v["voice_id"] for v in listed_voices}

    # Expected: User's custom, user's registered preset, AND the global preset
    assert custom_voice_payload["voice_id"] in listed_voice_ids
    assert user_registered_preset_payload["voice_id"] in listed_voice_ids
    assert global_preset_payload["voice_id"] in listed_voice_ids
    assert len(listed_voices) == 3

    # Cleanup
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.delete(
            f"/v0/assistant/voice/{custom_voice_payload['voice_id']}", headers=HEADERS
        )
        await client.delete(
            f"/v0/assistant/voice/{user_registered_preset_payload['voice_id']}",
            headers=HEADERS,
        )
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = other_user_id
        await client.delete(
            f"/v0/assistant/voice/{global_preset_payload['voice_id']}", headers=HEADERS
        )


@pytest.mark.anyio
async def test_clone_voice(
    client: AsyncClient, dbsession, mock_cartesia_service_factory
):
    user_id = await get_user_id_from_request_state(client)
    sample_audio_bytes = _get_sample_wav_bytes()
    files = {"file": ("sample.wav", sample_audio_bytes, "audio/wav")}
    data = {
        "name": "Cloned Via Test",
        "language": "en",
        "description": "Test clone desc",
    }

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        resp = await client.post(
            "/v0/assistant/voice/clone", files=files, data=data, headers=HEADERS
        )

    assert resp.status_code == 201
    cloned_voice_data = resp.json()["info"]
    assert cloned_voice_data["voice_id"] == "mock-cloned-cartesia-id"
    assert cloned_voice_data["name"] == "Mock Cloned Voice"  # Name from Cartesia mock
    assert cloned_voice_data["is_preset"] is False
    mock_cartesia_service_factory.clone_voice.assert_called_once()
    # Check call arguments if necessary:
    # call_args = mock_cartesia_service_factory.clone_voice.call_args
    # assert call_args[1]['name'] == data['name']

    # Cleanup
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.delete(
            f"/v0/assistant/voice/{cloned_voice_data['voice_id']}", headers=HEADERS
        )
    mock_cartesia_service_factory.delete_voice.assert_called_with(
        "mock-cloned-cartesia-id"
    )


@pytest.mark.anyio
async def test_clone_voice_db_integrity_error_rolls_back_cartesia(
    client: AsyncClient, dbsession, mock_cartesia_service_factory
):
    user_id = await get_user_id_from_request_state(client)
    sample_audio_bytes = _get_sample_wav_bytes()
    files = {"file": ("sample.wav", sample_audio_bytes, "audio/wav")}
    data = {"name": "DB Fail Clone Test", "language": "en"}

    # Cartesia clone succeeds and returns a new ID
    mock_cartesia_service_factory.clone_voice.return_value = {
        "id": "cartesia-id-to-rollback",
        "name": "Temp Clone",
        "language": "en",
    }

    # Simulate DB IntegrityError during voice_dao.create_voice
    with patch(
        "orchestra.db.dao.voice_dao.VoiceDAO.create_voice",
        side_effect=Exception("Simulated DB Integrity Error"),
    ):  # Use general Exception for broader catch
        with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
            mock_state.user_id = user_id
            resp = await client.post(
                "/v0/assistant/voice/clone", files=files, data=data, headers=HEADERS
            )

    assert resp.status_code == 500  # Or 409 if specific IntegrityError is caught
    assert "Failed to clone and save voice" in resp.json()["detail"]
    mock_cartesia_service_factory.delete_voice.assert_called_once_with(
        "cartesia-id-to-rollback"
    )


@pytest.mark.anyio
async def test_localize_voice(
    client: AsyncClient, dbsession, mock_cartesia_service_factory
):
    user_id = await get_user_id_from_request_state(client)
    base_voice_id = (
        "some-base-cartesia-id"  # Does not need to exist in DB for localization itself
    )
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
            "/v0/assistant/voice/localize", json=payload, headers=HEADERS
        )

    assert resp.status_code == 201
    localized_voice_data = resp.json()["info"]
    assert localized_voice_data["voice_id"] == "mock-localized-cartesia-id"
    assert localized_voice_data["name"] == "Mock Localized Voice"
    assert localized_voice_data["language"] == "es"
    assert localized_voice_data["is_preset"] is False
    mock_cartesia_service_factory.localize_voice.assert_called_once()

    # Cleanup
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.delete(
            f"/v0/assistant/voice/{localized_voice_data['voice_id']}", headers=HEADERS
        )
    mock_cartesia_service_factory.delete_voice.assert_called_with(
        "mock-localized-cartesia-id"
    )


@pytest.mark.anyio
async def test_delete_non_preset_voice(
    client: AsyncClient, dbsession, mock_cartesia_service_factory
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
            f"/v0/assistant/voice/{voice_id_to_delete}", headers=HEADERS
        )

    assert resp_del.status_code == 200
    assert "deleted successfully" in resp_del.json()["info"].lower()
    mock_cartesia_service_factory.delete_voice.assert_called_once_with(
        voice_id_to_delete
    )


@pytest.mark.anyio
async def test_delete_preset_voice_from_user_registration(
    client: AsyncClient, dbsession, mock_cartesia_service_factory
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
            f"/v0/assistant/voice/{voice_id_to_delete}", headers=HEADERS
        )

    assert resp_del.status_code == 200
    assert "deleted successfully" in resp_del.json()["info"].lower()
    mock_cartesia_service_factory.delete_voice.assert_not_called()  # Key check: Cartesia NOT called for preset


@pytest.mark.anyio
async def test_delete_voice_fails_if_cartesia_fails_non_404(
    client: AsyncClient, dbsession, mock_cartesia_service_factory
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
        status_code=500, detail="Cartesia server meltdown"
    )

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.post(
            "/v0/assistant/voice", json=reg_payload, headers=HEADERS
        )  # Register it
        resp_del = await client.delete(
            f"/v0/assistant/voice/{voice_id}", headers=HEADERS
        )

    assert resp_del.status_code == 500
    assert (
        "Failed to delete voice from Cartesia: Cartesia server meltdown"
        in resp_del.json()["detail"]
    )
    # Voice should still exist in DB due to rollback

    # Cleanup (mock Cartesia delete to succeed this time for cleanup)
    mock_cartesia_service_factory.delete_voice.side_effect = None
    mock_cartesia_service_factory.delete_voice.return_value = {"status": "success"}
    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        # This delete will now succeed fully
        await client.delete(f"/v0/assistant/voice/{voice_id}", headers=HEADERS)


@pytest.mark.anyio
async def test_delete_voice_succeeds_if_cartesia_404(
    client: AsyncClient, dbsession, mock_cartesia_service_factory
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
        status_code=404, detail="Not found on Cartesia"
    )

    with patch("orchestra.web.api.assistant.views.Request.state") as mock_state:
        mock_state.user_id = user_id
        await client.post("/v0/assistant/voice", json=reg_payload, headers=HEADERS)
        resp_del = await client.delete(
            f"/v0/assistant/voice/{voice_id}", headers=HEADERS
        )

    assert resp_del.status_code == 200
    assert "deleted successfully" in resp_del.json()["info"].lower()
    mock_cartesia_service_factory.delete_voice.assert_called_once_with(voice_id)
