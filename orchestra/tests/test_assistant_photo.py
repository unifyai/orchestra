import io
from unittest.mock import MagicMock

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
