import io
from unittest.mock import ANY, MagicMock

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.services.bucket_service import BucketService as OriginalBucketService
from orchestra.services.openai_service import ImageAnalysisResponse
from orchestra.services.openai_service import OpenAIService as OriginalOpenAIService
from orchestra.services.openai_service import (
    TextModerationResponse,
    TextModerationResult,
)
from orchestra.services.replicate_service import (
    ReplicateService as OriginalReplicateService,
)
from orchestra.settings import settings
from orchestra.tests.utils import ADMIN_HEADERS, HEADERS


@pytest.fixture(scope="function", autouse=True)
async def approve_default_user(client: AsyncClient):
    """Ensures the default test user for this module is approved for hiring."""
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    user_id = credits_resp.json()["id"]
    approve_url = f"/v0/admin/user/{user_id}/assistant-hiring-approval/approved"
    approve_resp = await client.put(approve_url, headers=ADMIN_HEADERS)
    assert (
        approve_resp.status_code == status.HTTP_200_OK
    ), f"Failed to approve default user {user_id}: {approve_resp.json()}"


@pytest.fixture(
    autouse=True,
)
def mock_media_services_factory(fastapi_app):
    """Provides mock ReplicateService, BucketService, and OpenAIService instances."""
    replicate_mock = MagicMock(spec=OriginalReplicateService)
    replicate_mock.generate_photo.return_value = (
        "https://replicate.delivery/pbxt/mock-generated-url"
    )
    replicate_mock.edit_photo.return_value = (
        "https://replicate.delivery/pbxt/mock-edited-url"
    )

    # Mock the new asynchronous methods for video animation
    mock_prediction = MagicMock()
    mock_prediction.id = "video_pred_123"
    mock_prediction.status = "starting"
    mock_prediction.model = "zsxkib/sonic"
    mock_prediction.version = (
        "a2aad29ea95f19747a5ea22ab14fc6594654506e5815f7f5ba4293e888d3e20f"
    )
    mock_prediction.input = {"image": "http://example.com/image.png"}
    mock_prediction.output = None
    mock_prediction.error = None
    mock_prediction.logs = None
    mock_prediction.created_at = "2025-08-12T10:00:00.000000Z"
    mock_prediction.completed_at = None
    mock_prediction.urls = {
        "get": "https://api.replicate.com/v1/predictions/video_pred_123",
        "cancel": "https://api.replicate.com/v1/predictions/video_pred_123/cancel",
    }

    replicate_mock.create_video_animation.return_value = mock_prediction
    replicate_mock.get_prediction.return_value = mock_prediction
    replicate_mock.cancel_prediction.return_value = mock_prediction

    bucket_mock = MagicMock(spec=OriginalBucketService)
    bucket_mock.upload_temp_assistant_file.return_value = (
        "https://storage.googleapis.com/mock-bucket/_temp/test-user/temp_image.jpg",
        "gs://mock-bucket/_temp/test-user/temp_image.jpg",
    )
    bucket_mock.delete_assistant_file.return_value = True

    # OpenAIService methods are sync
    openai_mock = MagicMock(spec=OriginalOpenAIService)
    openai_mock.analyze_image = MagicMock()
    openai_mock.analyze_audio = MagicMock()
    openai_mock.moderate_text = MagicMock()
    # Default success response for moderation checks
    openai_mock.analyze_image.return_value = ImageAnalysisResponse(
        has_human_face=True,
        is_nsfw=False,
        reason="Image is OK.",
    )
    openai_mock.analyze_audio.return_value = TextModerationResponse(
        contains_speech=True,
        is_nsfw=False,
        reason="Audio is OK.",
    )
    openai_mock.moderate_text.return_value = TextModerationResult(
        is_nsfw=False,
        reason="Text is clean.",
    )

    fastapi_app.dependency_overrides[OriginalReplicateService] = lambda: replicate_mock
    fastapi_app.dependency_overrides[OriginalBucketService] = lambda: bucket_mock
    fastapi_app.dependency_overrides[OriginalOpenAIService] = lambda: openai_mock

    yield replicate_mock, bucket_mock, openai_mock

    fastapi_app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_generate_photo_success(client: AsyncClient, mock_media_services_factory):
    replicate_mock, _, openai_mock = mock_media_services_factory
    payload = {"prompt": "A beautiful landscape"}
    resp = await client.post(
        "/v0/assistant/photo/generate",
        json=payload,
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()["info"]
    assert data == "https://replicate.delivery/pbxt/mock-generated-url"

    openai_mock.moderate_text.assert_called_once_with(payload["prompt"])
    replicate_mock.generate_photo.assert_called_once()
    assert replicate_mock.generate_photo.call_args[1]["prompt"] == payload["prompt"]


@pytest.mark.anyio
async def test_generate_photo_fails_moderation(
    client: AsyncClient,
    mock_media_services_factory,
):
    replicate_mock, _, openai_mock = mock_media_services_factory
    openai_mock.moderate_text.return_value = TextModerationResult(
        is_nsfw=True,
        reason="Inappropriate prompt.",
    )
    payload = {"prompt": "a bad prompt"}
    resp = await client.post(
        "/v0/assistant/photo/generate",
        json=payload,
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "Prompt moderation failed" in resp.json()["detail"]
    openai_mock.moderate_text.assert_called_once_with(payload["prompt"])
    replicate_mock.generate_photo.assert_not_called()


@pytest.mark.anyio
async def test_edit_photo_with_url_success(
    client: AsyncClient,
    mock_media_services_factory,
):
    replicate_mock, bucket_mock, openai_mock = mock_media_services_factory

    # Separate form data from files. Send form fields in `data`.
    data_payload = {
        "prompt": "Make it winter",
        "input_image_url": "https://example.com/summer.jpg",
        "aspect_ratio": "match_input_image",
        "output_format": "jpg",
        "safety_tolerance": "2.0",
    }

    # httpx needs Content-Type to be unset to create the correct multipart boundary.
    request_headers = HEADERS.copy()
    request_headers.pop("Content-Type", None)

    # The endpoint expects multipart/form-data, so pass `files={}` to force it.
    resp = await client.post(
        "/v0/assistant/photo/edit",
        data=data_payload,
        files={},
        headers=request_headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["info"]
    assert data == "https://replicate.delivery/pbxt/mock-edited-url"

    openai_mock.moderate_text.assert_called_once_with("Make it winter")
    openai_mock.analyze_image.assert_called_once_with(
        image_url="https://example.com/summer.jpg",
    )
    replicate_mock.edit_photo.assert_called_once_with(
        prompt="Make it winter",
        input_image="https://example.com/summer.jpg",
        aspect_ratio="match_input_image",
        output_format="jpg",
        safety_tolerance=2.0,
    )
    bucket_mock.upload_temp_assistant_file.assert_not_called()
    bucket_mock.delete_assistant_file.assert_not_called()


@pytest.mark.anyio
async def test_edit_photo_with_file_success(
    client: AsyncClient,
    mock_media_services_factory,
):
    replicate_mock, bucket_mock, openai_mock = mock_media_services_factory
    file_content = b"fake image data"

    # Separate form data from files for clarity and correctness.
    data_payload = {
        "prompt": "Add a cat",
        "aspect_ratio": "match_input_image",
        "output_format": "jpg",
        "safety_tolerance": "2.0",
    }
    files_payload = {
        "input_image_file": ("test.jpg", io.BytesIO(file_content), "image/jpeg"),
    }

    # httpx needs Content-Type to be unset to create the correct multipart boundary.
    request_headers = HEADERS.copy()
    request_headers.pop("Content-Type", None)

    resp = await client.post(
        "/v0/assistant/photo/edit",
        data=data_payload,
        files=files_payload,
        headers=request_headers,
    )

    assert resp.status_code == 201, resp.text
    data = resp.json()["info"]
    assert data == "https://replicate.delivery/pbxt/mock-edited-url"

    bucket_mock.upload_temp_assistant_file.assert_called_once_with(
        file_content,
        ANY,
        "image/jpeg",
    )
    openai_mock.moderate_text.assert_called_once_with("Add a cat")
    openai_mock.analyze_image.assert_called_once_with(
        image_url="https://storage.googleapis.com/mock-bucket/_temp/test-user/temp_image.jpg",
    )
    replicate_mock.edit_photo.assert_called_once_with(
        prompt="Add a cat",
        input_image="https://storage.googleapis.com/mock-bucket/_temp/test-user/temp_image.jpg",
        aspect_ratio="match_input_image",
        output_format="jpg",
        safety_tolerance=2.0,
    )
    bucket_mock.delete_assistant_file.assert_called_once_with(
        "gs://mock-bucket/_temp/test-user/temp_image.jpg",
    )


@pytest.mark.anyio
async def test_edit_photo_fails_prompt_moderation(
    client: AsyncClient,
    mock_media_services_factory,
):
    replicate_mock, _, openai_mock = mock_media_services_factory
    openai_mock.moderate_text.return_value = TextModerationResult(
        is_nsfw=True,
        reason="Inappropriate prompt.",
    )
    data_payload = {
        "prompt": "a bad prompt",
        "input_image_url": "https://example.com/clean.jpg",
    }
    request_headers = HEADERS.copy()
    request_headers.pop("Content-Type", None)
    resp = await client.post(
        "/v0/assistant/photo/edit",
        data=data_payload,
        files={},
        headers=request_headers,
    )
    assert resp.status_code == 400
    assert "Prompt moderation failed" in resp.json()["detail"]
    openai_mock.moderate_text.assert_called_once_with("a bad prompt")


@pytest.mark.anyio
async def test_edit_photo_fails_image_moderation(
    client: AsyncClient,
    mock_media_services_factory,
):
    replicate_mock, _, openai_mock = mock_media_services_factory
    openai_mock.analyze_image.return_value = ImageAnalysisResponse(
        has_human_face=True,
        is_nsfw=True,
        reason="Inappropriate image.",
    )
    data_payload = {
        "prompt": "a clean prompt",
        "input_image_url": "https://example.com/nsfw.jpg",
    }
    request_headers = HEADERS.copy()
    request_headers.pop("Content-Type", None)
    resp = await client.post(
        "/v0/assistant/photo/edit",
        data=data_payload,
        files={},
        headers=request_headers,
    )
    assert resp.status_code == 400
    assert "Image moderation failed" in resp.json()["detail"]
    openai_mock.moderate_text.assert_called_once_with("a clean prompt")
    openai_mock.analyze_image.assert_called_once_with(
        image_url="https://example.com/nsfw.jpg",
    )
    replicate_mock.edit_photo.assert_not_called()


@pytest.mark.anyio
async def test_edit_photo_invalid_input(client: AsyncClient):
    # Base form fields for invalid requests
    data_payload = {
        "prompt": "test",
        "aspect_ratio": "1:1",
        "output_format": "webp",
        "safety_tolerance": "2.0",
    }

    # httpx needs Content-Type to be unset to create the correct multipart boundary.
    request_headers = HEADERS.copy()
    request_headers.pop("Content-Type", None)

    # Test with no input image provided (neither URL nor file)
    resp_none = await client.post(
        "/v0/assistant/photo/edit",
        data=data_payload,
        files={},  # Force multipart/form-data
        headers=request_headers,
    )
    assert resp_none.status_code == 400
    assert "Provide either" in resp_none.json()["detail"]

    # Test with both URL and file provided
    file_content = b"fake image data"
    data_with_url = {**data_payload, "input_image_url": "http://a.com/b.jpg"}
    files_with_file = {
        "input_image_file": ("test.jpg", io.BytesIO(file_content), "image/jpeg"),
    }

    resp_both = await client.post(
        "/v0/assistant/photo/edit",
        data=data_with_url,
        files=files_with_file,
        headers=request_headers,
    )
    assert resp_both.status_code == 400
    assert "Provide either" in resp_both.json()["detail"]


@pytest.mark.anyio
async def test_animate_video_with_urls_success(
    client: AsyncClient,
    mock_media_services_factory,
    dbsession,
):
    replicate_mock, bucket_mock, openai_mock = mock_media_services_factory
    # Ensure user has credits if staging is false
    if not settings.is_staging:
        from orchestra.db.dao.billing_account_dao import BillingAccountDAO
        from orchestra.db.dao.user_dao import UserDAO

        user_dao = UserDAO(dbsession)
        ba_dao = BillingAccountDAO(dbsession)
        user_obj = user_dao.get_user_with_id("test-user")
        ba_dao.add_credits(
            user_obj.billing_account_id,
            (settings.video_generation_cost * settings.default_video_duration) * 2,
        )  # give enough credits
        dbsession.commit()

    data_payload = {
        "image_url": "https://example.com/image.png",
        "audio_url": "https://example.com/audio.mp3",
    }
    request_headers = HEADERS.copy()
    request_headers.pop("Content-Type", None)

    resp = await client.post(
        "/v0/assistant/photo/animate",
        data=data_payload,
        files={},  # Force multipart
        headers=request_headers,
    )
    assert resp.status_code == 202, resp.text
    data = resp.json()["info"]
    assert data["id"] == "video_pred_123"
    assert data["status"] == "starting"

    openai_mock.analyze_image.assert_called_once_with(
        image_url="https://example.com/image.png",
    )
    openai_mock.analyze_audio.assert_called_once_with(
        audio_url="https://example.com/audio.mp3",
    )

    replicate_mock.create_video_animation.assert_called_once_with(
        image_url="https://example.com/image.png",
        audio_url="https://example.com/audio.mp3",
        seed=None,
    )
    bucket_mock.upload_temp_assistant_file.assert_not_called()
    bucket_mock.delete_assistant_file.assert_not_called()


@pytest.mark.anyio
async def test_animate_video_with_files_success(
    client: AsyncClient,
    mock_media_services_factory,
    dbsession,
):
    replicate_mock, bucket_mock, openai_mock = mock_media_services_factory
    if not settings.is_staging:
        from orchestra.db.dao.billing_account_dao import BillingAccountDAO
        from orchestra.db.dao.user_dao import UserDAO

        user_dao = UserDAO(dbsession)
        ba_dao = BillingAccountDAO(dbsession)
        user_obj = user_dao.get_user_with_id("test-user")
        ba_dao.add_credits(
            user_obj.billing_account_id,
            settings.video_generation_cost * 2,
        )
        dbsession.commit()

    image_content = b"fake image data"
    audio_content = b"fake audio data"

    bucket_mock.upload_temp_assistant_file.side_effect = [
        (
            "https://storage.googleapis.com/mock-bucket/_temp/test-user/temp_image.jpg",
            "gs://mock-bucket/_temp/test-user/temp_image.jpg",
        ),
        (
            "https://storage.googleapis.com/mock-bucket/_temp/test-user/temp_audio.mp3",
            "gs://mock-bucket/_temp/test-user/temp_audio.mp3",
        ),
    ]

    data_payload = {}
    files_payload = {
        "image_file": ("image.jpg", io.BytesIO(image_content), "image/jpeg"),
        "audio_file": ("audio.mp3", io.BytesIO(audio_content), "audio/mpeg"),
    }
    request_headers = HEADERS.copy()
    request_headers.pop("Content-Type", None)

    resp = await client.post(
        "/v0/assistant/photo/animate",
        data=data_payload,
        files=files_payload,
        headers=request_headers,
    )
    assert resp.status_code == 202, resp.text
    data = resp.json()["info"]
    assert data["id"] == "video_pred_123"
    assert data["status"] == "starting"

    assert bucket_mock.upload_temp_assistant_file.call_count == 2
    bucket_mock.upload_temp_assistant_file.assert_any_call(
        image_content,
        ANY,
        "image/jpeg",
    )
    bucket_mock.upload_temp_assistant_file.assert_any_call(
        audio_content,
        ANY,
        "audio/mpeg",
    )

    openai_mock.analyze_image.assert_called_once_with(
        image_url="https://storage.googleapis.com/mock-bucket/_temp/test-user/temp_image.jpg",
    )
    openai_mock.analyze_audio.assert_called_once_with(
        audio_url="https://storage.googleapis.com/mock-bucket/_temp/test-user/temp_audio.mp3",
    )

    replicate_mock.create_video_animation.assert_called_once_with(
        image_url="https://storage.googleapis.com/mock-bucket/_temp/test-user/temp_image.jpg",
        audio_url="https://storage.googleapis.com/mock-bucket/_temp/test-user/temp_audio.mp3",
        seed=None,
    )

    assert bucket_mock.delete_assistant_file.call_count == 2
    bucket_mock.delete_assistant_file.assert_any_call(
        "gs://mock-bucket/_temp/test-user/temp_image.jpg",
    )
    bucket_mock.delete_assistant_file.assert_any_call(
        "gs://mock-bucket/_temp/test-user/temp_audio.mp3",
    )


@pytest.mark.anyio
async def test_animate_video_fails_moderation_no_face(
    client: AsyncClient,
    mock_media_services_factory,
):
    replicate_mock, _, openai_mock = mock_media_services_factory

    # Mock OpenAI to reject the image
    openai_mock.analyze_image.return_value = ImageAnalysisResponse(
        has_human_face=False,
        is_nsfw=False,
        reason="No face detected.",
    )

    data_payload = {
        "image_url": "https://example.com/no_face.png",
        "audio_url": "https://example.com/clean_audio.mp3",
    }
    request_headers = HEADERS.copy()
    request_headers.pop("Content-Type", None)

    resp = await client.post(
        "/v0/assistant/photo/animate",
        data=data_payload,
        files={},
        headers=request_headers,
    )

    assert resp.status_code == 400
    assert "requires an image with a clear human face" in resp.json()["detail"]
    assert "No face detected" in resp.json()["detail"]

    openai_mock.analyze_image.assert_called_once_with(
        image_url="https://example.com/no_face.png",
    )
    openai_mock.analyze_audio.assert_not_called()
    replicate_mock.create_video_animation.assert_not_called()


@pytest.mark.anyio
async def test_animate_video_fails_moderation_image_nsfw(
    client: AsyncClient,
    mock_media_services_factory,
):
    replicate_mock, _, openai_mock = mock_media_services_factory

    # Mock OpenAI to reject the image as NSFW
    openai_mock.analyze_image.return_value = ImageAnalysisResponse(
        has_human_face=True,
        is_nsfw=True,
        reason="Inappropriate content.",
    )

    data_payload = {
        "image_url": "https://example.com/nsfw_image.png",
        "audio_url": "https://example.com/clean_audio.mp3",
    }
    request_headers = HEADERS.copy()
    request_headers.pop("Content-Type", None)

    resp = await client.post(
        "/v0/assistant/photo/animate",
        data=data_payload,
        files={},
        headers=request_headers,
    )

    assert resp.status_code == 400
    assert "Image moderation failed" in resp.json()["detail"]
    assert "Inappropriate content" in resp.json()["detail"]

    openai_mock.analyze_image.assert_called_once_with(
        image_url="https://example.com/nsfw_image.png",
    )
    openai_mock.analyze_audio.assert_not_called()
    replicate_mock.create_video_animation.assert_not_called()


@pytest.mark.anyio
async def test_animate_video_fails_moderation_audio_nsfw(
    client: AsyncClient,
    mock_media_services_factory,
):
    replicate_mock, _, openai_mock = mock_media_services_factory

    # Mock OpenAI to pass the image but reject the audio
    openai_mock.analyze_image.return_value = ImageAnalysisResponse(
        has_human_face=True,
        is_nsfw=False,
        reason="OK",
    )
    openai_mock.analyze_audio.return_value = TextModerationResponse(
        contains_speech=True,
        is_nsfw=True,
        reason="Explicit language detected.",
    )

    data_payload = {
        "image_url": "https://example.com/clean_image.png",
        "audio_url": "https://example.com/nsfw_audio.mp3",
    }
    request_headers = HEADERS.copy()
    request_headers.pop("Content-Type", None)

    resp = await client.post(
        "/v0/assistant/photo/animate",
        data=data_payload,
        files={},
        headers=request_headers,
    )

    assert resp.status_code == 400
    assert "Audio moderation failed" in resp.json()["detail"]
    assert "Explicit language detected" in resp.json()["detail"]

    openai_mock.analyze_image.assert_called_once_with(
        image_url="https://example.com/clean_image.png",
    )
    openai_mock.analyze_audio.assert_called_once_with(
        audio_url="https://example.com/nsfw_audio.mp3",
    )
    replicate_mock.create_video_animation.assert_not_called()


@pytest.mark.anyio
async def test_animate_video_fails_moderation_no_speech(
    client: AsyncClient,
    mock_media_services_factory,
):
    replicate_mock, _, openai_mock = mock_media_services_factory

    # Mock OpenAI to pass the image but reject the audio due to no speech
    openai_mock.analyze_image.return_value = ImageAnalysisResponse(
        has_human_face=True,
        is_nsfw=False,
        reason="OK",
    )
    openai_mock.analyze_audio.return_value = TextModerationResponse(
        contains_speech=False,
        is_nsfw=False,
        reason="No speech detected.",
    )

    data_payload = {
        "image_url": "https://example.com/clean_image.png",
        "audio_url": "https://example.com/silent_audio.mp3",
    }
    request_headers = HEADERS.copy()
    request_headers.pop("Content-Type", None)

    resp = await client.post(
        "/v0/assistant/photo/animate",
        data=data_payload,
        files={},
        headers=request_headers,
    )

    assert resp.status_code == 400
    assert "No speech was detected" in resp.json()["detail"]
    assert "No speech detected" in resp.json()["detail"]

    openai_mock.analyze_image.assert_called_once_with(
        image_url="https://example.com/clean_image.png",
    )
    openai_mock.analyze_audio.assert_called_once_with(
        audio_url="https://example.com/silent_audio.mp3",
    )
    replicate_mock.create_video_animation.assert_not_called()


@pytest.mark.anyio
async def test_animate_video_invalid_input_combinations(client: AsyncClient):
    request_headers = HEADERS.copy()
    request_headers.pop("Content-Type", None)

    # Both image_url and image_file
    resp = await client.post(
        "/v0/assistant/photo/animate",
        data={"image_url": "http://a.com/img.jpg", "audio_url": "http://a.com/aud.mp3"},
        files={"image_file": ("img.jpg", io.BytesIO(b"img"), "image/jpeg")},
        headers=request_headers,
    )
    assert resp.status_code == 400
    assert "Provide either 'image_url' or 'image_file'" in resp.json()["detail"]

    # Neither image_url nor image_file
    resp = await client.post(
        "/v0/assistant/photo/animate",
        data={"audio_url": "http://a.com/aud.mp3"},
        files={},
        headers=request_headers,
    )
    assert resp.status_code == 400
    assert "Provide either 'image_url' or 'image_file'" in resp.json()["detail"]

    # Both audio_url and audio_file
    resp = await client.post(
        "/v0/assistant/photo/animate",
        data={"image_url": "http://a.com/img.jpg", "audio_url": "http://a.com/aud.mp3"},
        files={"audio_file": ("aud.mp3", io.BytesIO(b"aud"), "audio/mpeg")},
        headers=request_headers,
    )
    assert resp.status_code == 400
    assert "Provide either 'audio_url' or 'audio_file'" in resp.json()["detail"]

    # Neither audio_url nor audio_file
    resp = await client.post(
        "/v0/assistant/photo/animate",
        data={"image_url": "http://a.com/img.jpg"},
        files={},
        headers=request_headers,
    )
    assert resp.status_code == 400
    assert "Provide either 'audio_url' or 'audio_file'" in resp.json()["detail"]


@pytest.mark.anyio
async def test_get_animation_prediction(
    client: AsyncClient,
    mock_media_services_factory,
):
    replicate_mock, _, _ = mock_media_services_factory
    prediction_id = "video_pred_123"

    resp = await client.get(
        f"/v0/assistant/photo/animate/{prediction_id}",
        headers=HEADERS,
    )

    assert resp.status_code == 200
    data = resp.json()["info"]
    assert data["id"] == prediction_id
    assert data["status"] == "starting"
    replicate_mock.get_prediction.assert_called_once_with(prediction_id)


@pytest.mark.anyio
async def test_cancel_animation_prediction(
    client: AsyncClient,
    mock_media_services_factory,
):
    replicate_mock, _, _ = mock_media_services_factory
    prediction_id = "video_pred_123"

    resp = await client.post(
        f"/v0/assistant/photo/animate/{prediction_id}/cancel",
        headers=HEADERS,
    )

    assert resp.status_code == 200
    data = resp.json()["info"]
    assert data["id"] == prediction_id
    replicate_mock.cancel_prediction.assert_called_once_with(prediction_id)
