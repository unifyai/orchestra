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
    form_data = {
        "prompt": "Make it winter",
        "input_image_url": "https://example.com/summer.jpg",
        "aspect_ratio": "match_input_image",
        "output_format": "jpg",
        "safety_tolerance": "2.0",
    }
    # Pass an empty file part to force multipart/form-data content type
    files = {"input_image_file": (None, b"", "application/octet-stream")}

    resp = await client.post(
        "/v0/assistant/photo/edit",
        data=form_data,
        files=files,  # Must send files to trigger multipart parsing
        headers=HEADERS,
    )

    assert resp.status_code == 201, resp.text
    data = resp.json()["info"]
    assert data["url"] == "https://replicate.delivery/pbxt/mock-edited-url"
    replicate_mock.edit_photo.assert_called_once_with(
        prompt=form_data["prompt"],
        input_image=form_data["input_image_url"],
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
    form_data = {
        "prompt": "Add a cat",
        "aspect_ratio": "match_input_image",
        "output_format": "jpg",
        "safety_tolerance": "2.0",
    }
    file_content = b"fake image data"
    files = {"input_image_file": ("test.jpg", io.BytesIO(file_content), "image/jpeg")}

    resp = await client.post(
        "/v0/assistant/photo/edit",
        data=form_data,
        files=files,
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
        prompt=form_data["prompt"],
        input_image="https://storage.googleapis.com/mock-bucket/_temp/test-user/temp_image.jpg",
        aspect_ratio=form_data["aspect_ratio"],
        output_format=form_data["output_format"],
        safety_tolerance=float(form_data["safety_tolerance"]),
    )
    bucket_mock.delete_assistant_photo.assert_called_once_with(
        "gs://mock-bucket/_temp/test-user/temp_image.jpg",
    )


@pytest.mark.anyio
async def test_edit_photo_invalid_input(client: AsyncClient):
    # Base form data required to pass initial validation
    base_form_data = {
        "prompt": "test",
        "aspect_ratio": "1:1",
        "output_format": "webp",
        "safety_tolerance": "2.0",
    }

    # Test with no input image provided
    # We send an empty file part and no URL
    resp_none = await client.post(
        "/v0/assistant/photo/edit",
        data=base_form_data,
        files={"input_image_file": (None, b"", "application/octet-stream")},
        headers=HEADERS,
    )
    assert resp_none.status_code == 400
    assert "Provide either" in resp_none.json()["detail"]

    # Test with both URL and file provided
    form_data_both = {**base_form_data, "input_image_url": "http://a.com/b.jpg"}
    files_both = {"input_image_file": ("test.jpg", io.BytesIO(b"data"), "image/jpeg")}
    resp_both = await client.post(
        "/v0/assistant/photo/edit",
        data=form_data_both,
        files=files_both,
        headers=HEADERS,
    )
    assert resp_both.status_code == 400
    assert "Provide either" in resp_both.json()["detail"]
