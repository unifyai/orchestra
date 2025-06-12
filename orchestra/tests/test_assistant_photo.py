import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

from orchestra.services.bucket_service import BucketService as OriginalBucketService
from orchestra.services.replicate_service import (
    ReplicateService as OriginalReplicateService,
)
from orchestra.tests.utils import HEADERS


@pytest.fixture(
    autouse=True,
)
def mock_photo_services_factory(fastapi_app):
    """Provides mock ReplicateService and BucketService instances."""
    replicate_mock = MagicMock(spec=OriginalReplicateService)
    replicate_mock.generate_photo.return_value = (
        "https://replicate.delivery/pbxt/mock-generated-url"
    )
    replicate_mock.edit_photo.return_value = (
        "https://replicate.delivery/pbxt/mock-edited-url"
    )

    bucket_mock = MagicMock(spec=OriginalBucketService)
    bucket_mock.upload_temp_assistant_photo_file.return_value = (
        "https://storage.googleapis.com/mock-bucket/_temp/test-user/temp_image.jpg",
        "gs://mock-bucket/_temp/test-user/temp_image.jpg",
    )
    bucket_mock.delete_assistant_photo.return_value = True

    fastapi_app.dependency_overrides[OriginalReplicateService] = lambda: replicate_mock
    fastapi_app.dependency_overrides[OriginalBucketService] = lambda: bucket_mock

    yield replicate_mock, bucket_mock

    fastapi_app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_generate_photo_success(client: AsyncClient, mock_photo_services_factory):
    replicate_mock, _ = mock_photo_services_factory
    payload = {"prompt": "A beautiful landscape"}
    resp = await client.post(
        "/v0/assistant/photo/generate",
        json=payload,
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()["info"]
    assert data["url"] == "https://replicate.delivery/pbxt/mock-generated-url"
    replicate_mock.generate_photo.assert_called_once()
    assert replicate_mock.generate_photo.call_args[1]["prompt"] == payload["prompt"]


@pytest.mark.anyio
async def test_edit_photo_with_url_success(
    client: AsyncClient,
    mock_photo_services_factory,
):
    replicate_mock, bucket_mock = mock_photo_services_factory
    # When sending multipart/form-data with httpx, all form fields must
    # be in the `files` dictionary as (None, value) tuples.
    files_payload = {
        "prompt": (None, "Make it winter"),
        "input_image_url": (None, "https://example.com/summer.jpg"),
        "aspect_ratio": (None, "match_input_image"),
        "output_format": (None, "jpg"),
        "safety_tolerance": (None, "2.0"),
        # An empty file part is not needed if other fields are present
        # "input_image_file": (None, b"", "application/octet-stream")
    }

    resp = await client.post(
        "/v0/assistant/photo/edit",
        files=files_payload,  # data parameter is not used for multipart
        headers=HEADERS,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["info"]
    assert data["url"] == "https://replicate.delivery/pbxt/mock-edited-url"
    replicate_mock.edit_photo.assert_called_once_with(
        prompt="Make it winter",
        input_image="https://example.com/summer.jpg",
        aspect_ratio="match_input_image",
        output_format="jpg",
        safety_tolerance=2.0,
    )
    bucket_mock.upload_temp_assistant_photo_file.assert_not_called()
    bucket_mock.delete_assistant_photo.assert_not_called()


@pytest.mark.anyio
async def test_edit_photo_with_file_success(
    client: AsyncClient,
    mock_photo_services_factory,
):
    replicate_mock, bucket_mock = mock_photo_services_factory
    file_content = b"fake image data"
    # All form fields and the file are placed in the 'files' dictionary.
    files_payload = {
        "prompt": (None, "Add a cat"),
        "aspect_ratio": (None, "match_input_image"),
        "output_format": (None, "jpg"),
        "safety_tolerance": (None, "2.0"),
        "input_image_file": ("test.jpg", io.BytesIO(file_content), "image/jpeg"),
    }

    resp = await client.post(
        "/v0/assistant/photo/edit",
        files=files_payload,  # data parameter is not used for multipart
        headers=HEADERS,
    )

    assert resp.status_code == 201, resp.text
    data = resp.json()["info"]
    assert data["url"] == "https://replicate.delivery/pbxt/mock-edited-url"

    bucket_mock.upload_temp_assistant_photo_file.assert_called_once_with(
        file_content,
        "test-user-id-default",  # user_id from HEADERS
        "image/jpeg",
    )
    replicate_mock.edit_photo.assert_called_once_with(
        prompt="Add a cat",
        input_image="https://storage.googleapis.com/mock-bucket/_temp/test-user/temp_image.jpg",
        aspect_ratio="match_input_image",
        output_format="jpg",
        safety_tolerance=2.0,
    )
    bucket_mock.delete_assistant_photo.assert_called_once_with(
        "gs://mock-bucket/_temp/test-user/temp_image.jpg",
    )


@pytest.mark.anyio
async def test_edit_photo_invalid_input(client: AsyncClient):
    # Base form fields to be included in the multipart request
    base_fields = {
        "prompt": (None, "test"),
        "aspect_ratio": (None, "1:1"),
        "output_format": (None, "webp"),
        "safety_tolerance": (None, "2.0"),
    }

    # Test with no input image provided (neither URL nor file)
    resp_none = await client.post(
        "/v0/assistant/photo/edit",
        files=base_fields,
        headers=HEADERS,
    )
    assert resp_none.status_code == 400
    assert "Provide either" in resp_none.json()["detail"]

    # Test with both URL and file provided
    file_content = b"fake image data"
    fields_with_both = {
        **base_fields,
        "input_image_url": (None, "http://a.com/b.jpg"),
        "input_image_file": ("test.jpg", io.BytesIO(file_content), "image/jpeg"),
    }
    resp_both = await client.post(
        "/v0/assistant/photo/edit",
        files=fields_with_both,
        headers=HEADERS,
    )
    assert resp_both.status_code == 400
    assert "Provide either" in resp_both.json()["detail"]