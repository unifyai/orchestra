import base64
from typing import Any, Dict
from unittest.mock import MagicMock, patch
import pytest
from httpx import AsyncClient
from orchestra.tests.utils import HEADERS


async def get_user_id_from_request_state(
    client: AsyncClient,
    path: str = "/v0/assistant/photo",
) -> str:
    return "test-user-id-default"


def _get_sample_png_bytes() -> bytes:
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=",
    )


def _get_sample_text_file_bytes() -> bytes:
    return b"This is not an image."


@pytest.mark.anyio
async def test_upload_assistant_photo_success(
    client: AsyncClient,
):
    """
    Test `POST /v0/assistant/photo/upload` successful photo upload.
    Mocks BucketService to avoid actual GCS upload.
    """
    user_id = await get_user_id_from_request_state(client)
    mock_gcs_url = f"gs://test-bucket/assistant_photos/{user_id}/sample_photo.png"

    with patch("orchestra.web.api.assistant.views.BucketService") as MockBucketService:
        mock_bucket_instance = MockBucketService.return_value
        mock_bucket_instance.upload_assistant_photo_file = MagicMock(
            return_value=mock_gcs_url,
        )

        image_bytes = _get_sample_png_bytes()
        files = {"file": ("sample_photo.png", image_bytes, "image/png")}

        upload_headers = {
            "accept": "application/json",
            "Authorization": HEADERS["Authorization"],
        }

        resp = await client.post(
            "/v0/assistant/photo/upload",
            files=files,
            headers=upload_headers,
        )

        assert resp.status_code == 201, f"Response: {resp.text}"
        body = resp.json()
        assert "info" in body
        assert body["info"]["gcs_url"] == mock_gcs_url

        mock_bucket_instance.upload_assistant_photo_file.assert_called_once()
        args, kwargs = mock_bucket_instance.upload_assistant_photo_file.call_args
        assert kwargs.get("user_id") == user_id
        assert kwargs.get("content_type") == "image/png"
        assert kwargs.get("file_content") == image_bytes


@pytest.mark.anyio
async def test_upload_assistant_photo_invalid_file_type(
    client: AsyncClient,
):
    """Test `POST /v0/assistant/photo/upload` with a non-image file type."""
    text_bytes = _get_sample_text_file_bytes()
    files = {"file": ("not_an_image.txt", text_bytes, "text/plain")}

    upload_headers = {
        "accept": "application/json",
        "Authorization": HEADERS["Authorization"],
    }
    resp = await client.post(
        "/v0/assistant/photo/upload",
        files=files,
        headers=upload_headers,
    )

    assert resp.status_code == 400, f"Response: {resp.text}"
    body = resp.json()
    assert "detail" in body
    assert "invalid file type" in body["detail"].lower()


@pytest.mark.anyio
async def test_upload_assistant_photo_file_too_large(
    client: AsyncClient,
):
    """Test `POST /v0/assistant/photo/upload` with a file exceeding size limits."""
    large_file_content = b"A" * (6 * 1024 * 1024)  # 6MB
    files = {"file": ("large_image.png", large_file_content, "image/png")}

    upload_headers = {
        "accept": "application/json",
        "Authorization": HEADERS["Authorization"],
    }
    with patch("orchestra.web.api.assistant.views.BucketService"):
        resp = await client.post(
            "/v0/assistant/photo/upload",
            files=files,
            headers=upload_headers,
        )

    assert resp.status_code == 413, f"Response: {resp.text}"
    body = resp.json()
    assert "detail" in body
    assert (
        "file size exceeds" in body["detail"].lower()
        or "content size exceeds" in body["detail"].lower()
    )


@pytest.mark.anyio
async def test_upload_assistant_photo_gcs_failure(
    client: AsyncClient,
):
    """Test `POST /v0/assistant/photo/upload` when GCS upload fails."""
    with patch("orchestra.web.api.assistant.views.BucketService") as MockBucketService:
        mock_bucket_instance = MockBucketService.return_value
        mock_bucket_instance.upload_assistant_photo_file = MagicMock(
            side_effect=Exception("GCS upload failed"),
        )

        image_bytes = _get_sample_png_bytes()
        files = {"file": ("sample_photo.png", image_bytes, "image/png")}
        upload_headers = {
            "accept": "application/json",
            "Authorization": HEADERS["Authorization"],
        }
        resp = await client.post(
            "/v0/assistant/photo/upload",
            files=files,
            headers=upload_headers,
        )

        assert resp.status_code == 500, f"Response: {resp.text}"
        body = resp.json()
        assert "detail" in body
        assert "could not upload photo" in body["detail"].lower()
        assert "gcs upload failed" in body["detail"].lower()


@pytest.mark.anyio
async def test_create_assistant_with_uploaded_gcs_photo(
    client: AsyncClient,
):
    """
    Test creating an assistant with a profile_photo URL that
    would have come from a previous photo upload.
    """
    user_id = await get_user_id_from_request_state(client)
    mock_gcs_photo_url = "gs://my-assistant-photos/user-xyz/photo-abc.jpg"
    payload = {
        "first_name": "PhotoAs",
        "surname": "GCSUser",
        "profile_photo": mock_gcs_photo_url,
        "age": 30,
        "weekly_limit": 10.0,
        "max_parallel": 2,
        "user_phone": "+15551234567",
        "email": f"test.photo_{user_id}@unify.ai",
    }

    with patch(
        "orchestra.web.api.assistant.views.create_email",
        return_value={"user": {"primaryEmail": payload["email"]}},
    ), patch(
        "orchestra.web.api.assistant.views.watch_email",
        return_value={"status": "ok"},
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
        "orchestra.db.dao.users_dao.UsersDAO.get_user_with_id",
    ) as mock_get_user, patch(
        "orchestra.db.dao.users_dao.UsersDAO.recharge_credit",
    ) as mock_recharge_credit:

        mock_user_db_obj = MagicMock()
        mock_user_db_obj.credits = 100
        mock_get_user.return_value = mock_user_db_obj

        resp = await client.post(
            "/v0/assistant",
            json=payload,
            headers=HEADERS,
        )

    assert resp.status_code == 200, f"Response: {resp.text}"
    body = resp.json()
    assert "info" in body
    created_assistant = body["info"]
    assert created_assistant["profile_photo"] == mock_gcs_photo_url


@pytest.mark.anyio
async def test_delete_assistant_with_gcs_photo(
    client: AsyncClient,
):
    """
    Test `DELETE /v0/assistant/{assistant_id}` for an assistant
    that has a GCS photo URL. Mocks BucketService.delete_assistant_photo.
    """
    user_id = await get_user_id_from_request_state(client)
    mock_gcs_photo_url = f"gs://my-assistant-photos/{user_id}/photo-to-delete.jpg"
    unique_email_for_delete = f"delete.photo_{user_id}@unify.ai"
    create_payload = {
        "first_name": "ToDelete",
        "surname": "WithPhoto",
        "profile_photo": mock_gcs_photo_url,
        "age": 31,
        "weekly_limit": 11.0,
        "max_parallel": 1,
        "user_phone": "+15550001111",
        "email": unique_email_for_delete,
    }
    assistant_id_to_delete = None

    with patch(
        "orchestra.web.api.assistant.views.create_email",
        return_value={"user": {"primaryEmail": unique_email_for_delete}},
    ), patch(
        "orchestra.web.api.assistant.views.watch_email",
        return_value={"status": "ok"},
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
        "orchestra.web.api.assistant.views.delete_email",
    ) as mock_delete_email, patch(
        "orchestra.web.api.assistant.views.delete_phone_number",
    ) as mock_delete_phone, patch(
        "orchestra.web.api.assistant.views.delete_pubsub_topic",
    ) as mock_delete_pubsub, patch(
        "orchestra.web.api.assistant.views.delete_cloud_run_job",
    ) as mock_delete_job, patch(
        "orchestra.web.api.assistant.views.stop_cloud_run_job",
    ) as mock_stop_job, patch(
        "orchestra.web.api.assistant.views.BucketService",
    ) as MockBucketServiceCreateDelete, patch(
        "orchestra.db.dao.users_dao.UsersDAO.get_user_with_id",
    ) as mock_get_user_del, patch(
        "orchestra.db.dao.users_dao.UsersDAO.recharge_credit",
    ) as mock_recharge_credit_del:

        mock_user_del = MagicMock()
        mock_user_del.credits = 100
        mock_get_user_del.return_value = mock_user_del

        mock_bucket_service_instance = MockBucketServiceCreateDelete.return_value
        mock_bucket_service_instance.delete_assistant_photo = MagicMock(
            return_value=True,
        )

        create_resp = await client.post(
            "/v0/assistant",
            json=create_payload,
            headers=HEADERS,
        )
        assert create_resp.status_code == 200, f"Response: {create_resp.text}"
        assistant_id_to_delete = create_resp.json()["info"]["agent_id"]
        # Get the actual email and phone assigned during creation for accurate mock assertion
        created_email = create_resp.json()["info"]["email"]
        created_phone = create_resp.json()["info"]["phone"]

        del_resp = await client.delete(
            f"/v0/assistant/{assistant_id_to_delete}",
            headers=HEADERS,
        )
        assert del_resp.status_code == 200, f"Response: {del_resp.text}"
        assert "assistant deleted successfully" in del_resp.json()["info"].lower()

        mock_bucket_service_instance.delete_assistant_photo.assert_called_once_with(
            mock_gcs_photo_url,
        )
        if (
            created_email
        ):  # email might not be set if creation failed before that step, but for success it should be
            mock_delete_email.assert_called_once_with(created_email)
        if created_phone:
            mock_delete_phone.assert_called_once_with(created_phone)
        mock_delete_pubsub.assert_called_once_with(str(assistant_id_to_delete))
        mock_delete_job.assert_called_once_with(str(assistant_id_to_delete))
        mock_stop_job.assert_called_once_with(str(assistant_id_to_delete))


@pytest.mark.anyio
async def test_delete_assistant_with_non_gcs_photo(
    client: AsyncClient,
):
    """
    Test `DELETE /v0/assistant/{assistant_id}` for an assistant
    with a non-GCS (e.g., http) photo URL.
    BucketService.delete_assistant_photo should NOT be called.
    """
    user_id = await get_user_id_from_request_state(client)
    http_photo_url = "https://example.com/some_preset_image.jpg"
    unique_email_for_preset = f"preset.photo_{user_id}@unify.ai"
    create_payload = {
        "first_name": "PresetUser",
        "surname": "PhotoTest",
        "profile_photo": http_photo_url,
        "age": 32,
        "weekly_limit": 12.0,
        "max_parallel": 3,
        "user_phone": "+15552223333",
        "email": unique_email_for_preset,
    }
    assistant_id_to_delete = None

    with patch(
        "orchestra.web.api.assistant.views.create_email",
        return_value={"user": {"primaryEmail": unique_email_for_preset}},
    ), patch(
        "orchestra.web.api.assistant.views.watch_email",
        return_value={"status": "ok"},
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
        "orchestra.web.api.assistant.views.delete_email",
    ), patch(
        "orchestra.web.api.assistant.views.delete_phone_number",
    ), patch(
        "orchestra.web.api.assistant.views.delete_pubsub_topic",
    ), patch(
        "orchestra.web.api.assistant.views.delete_cloud_run_job",
    ), patch(
        "orchestra.web.api.assistant.views.stop_cloud_run_job",
    ), patch(
        "orchestra.web.api.assistant.views.BucketService",
    ) as MockBucketServiceNoCall, patch(
        "orchestra.db.dao.users_dao.UsersDAO.get_user_with_id",
    ) as mock_get_user_pre, patch(
        "orchestra.db.dao.users_dao.UsersDAO.recharge_credit",
    ) as mock_recharge_credit_pre:

        mock_user_pre = MagicMock()
        mock_user_pre.credits = 100
        mock_get_user_pre.return_value = mock_user_pre

        mock_bucket_instance_no_call = MockBucketServiceNoCall.return_value
        mock_bucket_instance_no_call.delete_assistant_photo = MagicMock()

        create_resp = await client.post(
            "/v0/assistant",
            json=create_payload,
            headers=HEADERS,
        )
        assert create_resp.status_code == 200, f"Response: {create_resp.text}"
        assistant_id_to_delete = create_resp.json()["info"]["agent_id"]

        del_resp = await client.delete(
            f"/v0/assistant/{assistant_id_to_delete}",
            headers=HEADERS,
        )
        assert del_resp.status_code == 200, f"Response: {del_resp.text}"

        mock_bucket_instance_no_call.delete_assistant_photo.assert_not_called()
