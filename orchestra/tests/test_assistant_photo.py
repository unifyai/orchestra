import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from orchestra.services.bucket_service import BucketService
from orchestra.tests.utils import (
    HEADERS,
)  # Assuming HEADERS implies an authenticated user


# A simple 1x1 pixel PNG image (transparent)
# You can replace this with a small actual image file if preferred
# For example, create a 'sample_photo.png' in a 'sample_data' directory
# and load it like _get_sample_wav_bytes.
def _get_sample_png_bytes() -> bytes:
    # Minimal PNG: 1x1 transparent pixel
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )


def _get_sample_text_file_bytes() -> bytes:
    return b"This is not an image."


@pytest.mark.anyio
async def test_upload_assistant_photo_success(client: AsyncClient):
    """
    Test `POST /v0/assistant/photo/upload` successful photo upload.
    Mocks BucketService to avoid actual GCS upload.
    """
    mock_gcs_url = "gs://test-bucket/assistant_photos/user123/sample_photo.png"

    # Patch the BucketService instance within the view's context
    # The path to patch depends on how BucketService is instantiated/injected in your views.
    # Assuming it's instantiated directly or via a simple dependency:
    with patch("orchestra.web.api.assistant.views.BucketService") as MockBucketService:
        mock_bucket_instance = MockBucketService.return_value
        mock_bucket_instance.upload_assistant_photo_file = MagicMock(
            return_value=mock_gcs_url
        )

        image_bytes = _get_sample_png_bytes()
        files = {"file": ("sample_photo.png", image_bytes, "image/png")}

        resp = await client.post(
            "/v0/assistant/photo/upload", files=files, headers=HEADERS
        )

        assert resp.status_code == 201
        body = resp.json()
        assert "info" in body
        assert body["info"]["gcs_url"] == mock_gcs_url

        mock_bucket_instance.upload_assistant_photo_file.assert_called_once()
        # You can add more specific assertions on the call arguments if needed,
        # e.g., checking user_id, content_type.
        # args, kwargs = mock_bucket_instance.upload_assistant_photo_file.call_args
        # assert kwargs['user_id'] is not None # Requires user_id to be accessible in test
        # assert kwargs['content_type'] == "image/png"
        # assert kwargs['file_content'] == image_bytes


@pytest.mark.anyio
async def test_upload_assistant_photo_invalid_file_type(client: AsyncClient):
    """Test `POST /v0/assistant/photo/upload` with a non-image file type."""
    text_bytes = _get_sample_text_file_bytes()
    files = {"file": ("not_an_image.txt", text_bytes, "text/plain")}

    resp = await client.post("/v0/assistant/photo/upload", files=files, headers=HEADERS)

    assert resp.status_code == 400
    body = resp.json()
    assert "detail" in body
    assert "invalid file type" in body["detail"].lower()


@pytest.mark.anyio
async def test_upload_assistant_photo_file_too_large(client: AsyncClient):
    """Test `POST /v0/assistant/photo/upload` with a file exceeding size limits."""
    # Create a dummy file larger than 5MB (adjust MAX_SIZE_BYTES if different in your view)
    # This is a simplified way; a real large file or mocking UploadFile.size might be needed
    # if the check happens before reading the full content.
    # The view checks `file.size` and `len(file_content)`.
    # We'll mock the content to be large.
    large_file_content = b"A" * (6 * 1024 * 1024)  # 6MB
    files = {"file": ("large_image.png", large_file_content, "image/png")}

    with patch(
        "orchestra.web.api.assistant.views.BucketService"
    ):  # Mock to prevent GCS call
        resp = await client.post(
            "/v0/assistant/photo/upload", files=files, headers=HEADERS
        )

    assert resp.status_code == 413  # Request Entity Too Large
    body = resp.json()
    assert "detail" in body
    assert (
        "file size exceeds" in body["detail"].lower()
        or "content size exceeds" in body["detail"].lower()
    )


@pytest.mark.anyio
async def test_upload_assistant_photo_gcs_failure(client: AsyncClient):
    """Test `POST /v0/assistant/photo/upload` when GCS upload fails."""
    with patch("orchestra.web.api.assistant.views.BucketService") as MockBucketService:
        mock_bucket_instance = MockBucketService.return_value
        mock_bucket_instance.upload_assistant_photo_file = MagicMock(
            side_effect=Exception("GCS upload failed")
        )

        image_bytes = _get_sample_png_bytes()
        files = {"file": ("sample_photo.png", image_bytes, "image/png")}

        resp = await client.post(
            "/v0/assistant/photo/upload", files=files, headers=HEADERS
        )

        assert resp.status_code == 500
        body = resp.json()
        assert "detail" in body
        assert "could not upload photo" in body["detail"].lower()
        assert "gcs upload failed" in body["detail"].lower()


@pytest.mark.anyio
async def test_create_assistant_with_uploaded_gcs_photo(client: AsyncClient):
    """
    Test creating an assistant with a profile_photo URL that
    would have come from a previous photo upload.
    """
    mock_gcs_photo_url = "gs://my-assistant-photos/user-xyz/photo-abc.jpg"
    payload = {
        "first_name": "PhotoAs",
        "surname": "GCSUser",
        "profile_photo": mock_gcs_photo_url,  # This is the key part
        # ... other required fields from your test_create_assistant_success
        "age": 30,
        "weekly_limit": 10.0,
        "max_parallel": 2,
        "user_phone": "+15551234567",  # Assuming this is now required
        "email": "test.photo@unify.ai",  # Assuming this is now required
    }

    # We need to mock the infrastructure creation calls within create_assistant
    with patch(
        "orchestra.web.api.assistant.views.create_email",
        return_value={"user": {"primaryEmail": "test@example.com"}},
    ), patch(
        "orchestra.web.api.assistant.views.watch_email", return_value={"status": "ok"}
    ), patch(
        "orchestra.web.api.assistant.views.create_phone_number",
        return_value={"phoneNumber": "+1234567890"},
    ), patch(
        "orchestra.web.api.assistant.views.create_whatsapp_sender",
        return_value={"sid": "SMwhatsapp123"},
    ), patch(
        "orchestra.web.api.assistant.views.create_pubsub_topic",
        return_value={"name": "projects/p/topics/t"},
    ), patch(
        "orchestra.web.api.assistant.views.create_cloud_run_job",
        return_value={"status": "ok"},
    ), patch(
        "orchestra.db.dao.users_dao.UsersDAO.get_user_with_id"
    ) as mock_get_user, patch(
        "orchestra.db.dao.users_dao.UsersDAO.recharge_credit"
    ) as mock_recharge_credit:

        mock_user = MagicMock()
        mock_user.credits = 100  # Ensure sufficient credits
        mock_get_user.return_value = mock_user

        resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)

    assert resp.status_code == 200  # Or 201 depending on your create success code
    body = resp.json()
    assert "info" in body
    created_assistant = body["info"]
    assert created_assistant["profile_photo"] == mock_gcs_photo_url


@pytest.mark.anyio
async def test_delete_assistant_with_gcs_photo(client: AsyncClient):
    """
    Test `DELETE /v0/assistant/{assistant_id}` for an assistant
    that has a GCS photo URL. Mocks BucketService.delete_assistant_photo.
    """
    mock_gcs_photo_url = "gs://my-assistant-photos/user-abc/photo-to-delete.jpg"
    create_payload = {
        "first_name": "ToDelete",
        "surname": "WithPhoto",
        "profile_photo": mock_gcs_photo_url,
        "age": 31,
        "weekly_limit": 11.0,
        "max_parallel": 1,
        "user_phone": "+15550001111",
        "email": "delete.photo@unify.ai",
    }

    assistant_id_to_delete = None

    # Mock infrastructure and BucketService for create and delete
    with patch(
        "orchestra.web.api.assistant.views.create_email",
        return_value={"user": {"primaryEmail": "del@example.com"}},
    ), patch(
        "orchestra.web.api.assistant.views.watch_email", return_value={"status": "ok"}
    ), patch(
        "orchestra.web.api.assistant.views.create_phone_number",
        return_value={"phoneNumber": "+1234567891"},
    ), patch(
        "orchestra.web.api.assistant.views.create_whatsapp_sender",
        return_value={"sid": "SMwhatsappdel"},
    ), patch(
        "orchestra.web.api.assistant.views.create_pubsub_topic",
        return_value={"name": "projects/p/topics/tdel"},
    ), patch(
        "orchestra.web.api.assistant.views.create_cloud_run_job",
        return_value={"status": "ok"},
    ), patch(
        "orchestra.web.api.assistant.views.delete_email"
    ) as mock_delete_email, patch(
        "orchestra.web.api.assistant.views.delete_phone_number"
    ) as mock_delete_phone, patch(
        "orchestra.web.api.assistant.views.delete_pubsub_topic"
    ) as mock_delete_pubsub, patch(
        "orchestra.web.api.assistant.views.delete_cloud_run_job"
    ) as mock_delete_job, patch(
        "orchestra.web.api.assistant.views.stop_cloud_run_job"
    ) as mock_stop_job, patch(
        "orchestra.web.api.assistant.views.BucketService"
    ) as MockBucketServiceCreateDelete, patch(
        "orchestra.db.dao.users_dao.UsersDAO.get_user_with_id"
    ) as mock_get_user_del, patch(
        "orchestra.db.dao.users_dao.UsersDAO.recharge_credit"
    ) as mock_recharge_credit_del:

        mock_user_del = MagicMock()
        mock_user_del.credits = 100
        mock_get_user_del.return_value = mock_user_del

        mock_bucket_service_instance = MockBucketServiceCreateDelete.return_value
        mock_bucket_service_instance.delete_assistant_photo = MagicMock(
            return_value=True
        )

        # Create assistant
        create_resp = await client.post(
            "/v0/assistant", json=create_payload, headers=HEADERS
        )
        assert create_resp.status_code == 200  # or 201
        assistant_id_to_delete = create_resp.json()["info"]["agent_id"]
        created_email = create_resp.json()["info"][
            "email"
        ]  # Get the email assigned during creation
        created_phone = create_resp.json()["info"]["phone"]  # Get the phone assigned

        # Delete the assistant
        del_resp = await client.delete(
            f"/v0/assistant/{assistant_id_to_delete}", headers=HEADERS
        )
        assert del_resp.status_code == 200
        assert "assistant deleted successfully" in del_resp.json()["info"].lower()

        # Assert that BucketService.delete_assistant_photo was called
        mock_bucket_service_instance.delete_assistant_photo.assert_called_once_with(
            mock_gcs_photo_url
        )

        # Assert infrastructure deletion calls
        if created_email:
            mock_delete_email.assert_called_once_with(created_email)
        if created_phone:
            mock_delete_phone.assert_called_once_with(created_phone)
        mock_delete_pubsub.assert_called_once_with(str(assistant_id_to_delete))
        mock_delete_job.assert_called_once_with(str(assistant_id_to_delete))
        mock_stop_job.assert_called_once_with(str(assistant_id_to_delete))


@pytest.mark.anyio
async def test_delete_assistant_with_non_gcs_photo(client: AsyncClient):
    """
    Test `DELETE /v0/assistant/{assistant_id}` for an assistant
    with a non-GCS (e.g., http) photo URL.
    BucketService.delete_assistant_photo should NOT be called.
    """
    http_photo_url = "https://example.com/some_preset_image.jpg"
    create_payload = {
        "first_name": "PresetUser",
        "surname": "PhotoTest",
        "profile_photo": http_photo_url,
        "age": 32,
        "weekly_limit": 12.0,
        "max_parallel": 3,
        "user_phone": "+15552223333",
        "email": "preset.photo@unify.ai",
    }
    assistant_id_to_delete = None

    with patch(
        "orchestra.web.api.assistant.views.create_email",
        return_value={"user": {"primaryEmail": "pre@example.com"}},
    ), patch(
        "orchestra.web.api.assistant.views.watch_email", return_value={"status": "ok"}
    ), patch(
        "orchestra.web.api.assistant.views.create_phone_number",
        return_value={"phoneNumber": "+1234567892"},
    ), patch(
        "orchestra.web.api.assistant.views.create_whatsapp_sender",
        return_value={"sid": "SMwhatsapppre"},
    ), patch(
        "orchestra.web.api.assistant.views.create_pubsub_topic",
        return_value={"name": "projects/p/topics/tpre"},
    ), patch(
        "orchestra.web.api.assistant.views.create_cloud_run_job",
        return_value={"status": "ok"},
    ), patch(
        "orchestra.web.api.assistant.views.delete_email"
    ), patch(
        "orchestra.web.api.assistant.views.delete_phone_number"
    ), patch(
        "orchestra.web.api.assistant.views.delete_pubsub_topic"
    ), patch(
        "orchestra.web.api.assistant.views.delete_cloud_run_job"
    ), patch(
        "orchestra.web.api.assistant.views.stop_cloud_run_job"
    ), patch(
        "orchestra.web.api.assistant.views.BucketService"
    ) as MockBucketServiceNoCall, patch(
        "orchestra.db.dao.users_dao.UsersDAO.get_user_with_id"
    ) as mock_get_user_pre, patch(
        "orchestra.db.dao.users_dao.UsersDAO.recharge_credit"
    ) as mock_recharge_credit_pre:

        mock_user_pre = MagicMock()
        mock_user_pre.credits = 100
        mock_get_user_pre.return_value = mock_user_pre

        mock_bucket_instance_no_call = MockBucketServiceNoCall.return_value
        mock_bucket_instance_no_call.delete_assistant_photo = MagicMock()

        # Create assistant
        create_resp = await client.post(
            "/v0/assistant", json=create_payload, headers=HEADERS
        )
        assert create_resp.status_code == 200
        assistant_id_to_delete = create_resp.json()["info"]["agent_id"]

        # Delete the assistant
        del_resp = await client.delete(
            f"/v0/assistant/{assistant_id_to_delete}", headers=HEADERS
        )
        assert del_resp.status_code == 200

        # Assert that BucketService.delete_assistant_photo was NOT called
        mock_bucket_instance_no_call.delete_assistant_photo.assert_not_called()
