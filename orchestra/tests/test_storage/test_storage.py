"""Tests for the storage API endpoints."""

from unittest.mock import MagicMock

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.services.bucket_service import BucketService as OriginalBucketService
from orchestra.tests.utils import HEADERS


@pytest.fixture(autouse=True)
def mock_bucket_service(fastapi_app):
    """Provides a mock BucketService for storage tests."""
    # Create a mock storage client
    mock_storage_client = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()

    # Default blob behavior - object exists
    mock_blob.exists.return_value = True
    mock_blob.download_as_bytes.return_value = b"test content"
    mock_blob.content_type = "text/plain"
    mock_blob.generate_signed_url.return_value = (
        "https://storage.googleapis.com/test-bucket/test-object"
        "?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Credential=..."
    )
    mock_blob.reload.return_value = None  # reload() doesn't return anything

    mock_bucket.blob.return_value = mock_blob
    mock_storage_client.bucket.return_value = mock_bucket

    # Create a mock BucketService instance
    bucket_mock = MagicMock(spec=OriginalBucketService)
    bucket_mock.storage_client = mock_storage_client

    # Override the dependency - use a factory function
    def get_mock_bucket_service():
        return bucket_mock

    fastapi_app.dependency_overrides[OriginalBucketService] = get_mock_bucket_service

    yield {
        "service": bucket_mock,
        "storage_client": mock_storage_client,
        "bucket": mock_bucket,
        "blob": mock_blob,
    }

    fastapi_app.dependency_overrides.pop(OriginalBucketService, None)


# ─────────────────────────────────────────────────────────────────────────────
# Signed URL Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_signed_url_success(client: AsyncClient, mock_bucket_service):
    """Test successful signed URL generation."""
    payload = {"gcs_uri": "gs://test-bucket/path/to/object.jpg"}

    resp = await client.post(
        "/v0/storage/signed-url",
        json=payload,
        headers=HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert "signed_url" in data
    assert data["expires_in_minutes"] == 60  # Default

    # Verify the mock was called correctly
    mock_bucket_service["storage_client"].bucket.assert_called_once_with("test-bucket")
    mock_bucket_service["bucket"].blob.assert_called_once_with("path/to/object.jpg")
    mock_bucket_service["blob"].exists.assert_called_once()
    mock_bucket_service["blob"].generate_signed_url.assert_called_once()


@pytest.mark.anyio
async def test_signed_url_custom_expiration(client: AsyncClient, mock_bucket_service):
    """Test signed URL generation with custom expiration."""
    payload = {
        "gcs_uri": "gs://test-bucket/object.png",
        "expiration_minutes": 120,
    }

    resp = await client.post(
        "/v0/storage/signed-url",
        json=payload,
        headers=HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["expires_in_minutes"] == 120


@pytest.mark.anyio
async def test_signed_url_with_download_flag(client: AsyncClient, mock_bucket_service):
    """Test signed URL generation with download=True sets Content-Disposition."""
    payload = {
        "gcs_uri": "gs://test-bucket/path/to/document.pdf",
        "download": True,
    }

    resp = await client.post(
        "/v0/storage/signed-url",
        json=payload,
        headers=HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK

    # Verify generate_signed_url was called with response_disposition
    call_kwargs = mock_bucket_service["blob"].generate_signed_url.call_args.kwargs
    assert "response_disposition" in call_kwargs
    assert call_kwargs["response_disposition"] == 'attachment; filename="document.pdf"'


@pytest.mark.anyio
async def test_signed_url_with_download_and_custom_filename(
    client: AsyncClient,
    mock_bucket_service,
):
    """Test signed URL with download=True and custom filename."""
    payload = {
        "gcs_uri": "gs://test-bucket/123/att-456_quarterly_report.pdf",
        "download": True,
        "filename": "quarterly_report.pdf",  # Original filename without ID prefix
    }

    resp = await client.post(
        "/v0/storage/signed-url",
        json=payload,
        headers=HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK

    # Verify the custom filename is used in Content-Disposition
    call_kwargs = mock_bucket_service["blob"].generate_signed_url.call_args.kwargs
    assert "response_disposition" in call_kwargs
    assert (
        call_kwargs["response_disposition"]
        == 'attachment; filename="quarterly_report.pdf"'
    )


@pytest.mark.anyio
async def test_signed_url_without_download_flag(
    client: AsyncClient,
    mock_bucket_service,
):
    """Test signed URL generation without download flag does not set Content-Disposition."""
    payload = {
        "gcs_uri": "gs://test-bucket/image.png",
        "download": False,
    }

    resp = await client.post(
        "/v0/storage/signed-url",
        json=payload,
        headers=HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK

    # Verify generate_signed_url was called WITHOUT response_disposition
    call_kwargs = mock_bucket_service["blob"].generate_signed_url.call_args.kwargs
    assert "response_disposition" not in call_kwargs


@pytest.mark.anyio
async def test_signed_url_invalid_uri_format(client: AsyncClient):
    """Test signed URL with invalid GCS URI format."""
    # Missing gs:// prefix
    payload = {"gcs_uri": "test-bucket/object.jpg"}

    resp = await client.post(
        "/v0/storage/signed-url",
        json=payload,
        headers=HEADERS,
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "Invalid GCS URI format" in resp.json()["detail"]


@pytest.mark.anyio
async def test_signed_url_bucket_only_uri(client: AsyncClient):
    """Test signed URL with bucket-only URI (no object path)."""
    payload = {"gcs_uri": "gs://test-bucket"}

    resp = await client.post(
        "/v0/storage/signed-url",
        json=payload,
        headers=HEADERS,
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "Invalid GCS URI format" in resp.json()["detail"]


@pytest.mark.anyio
async def test_signed_url_object_not_found(client: AsyncClient, mock_bucket_service):
    """Test signed URL for non-existent object."""
    mock_bucket_service["blob"].exists.return_value = False

    payload = {"gcs_uri": "gs://test-bucket/nonexistent.jpg"}

    resp = await client.post(
        "/v0/storage/signed-url",
        json=payload,
        headers=HEADERS,
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert "Object not found" in resp.json()["detail"]


@pytest.mark.anyio
async def test_signed_url_expiration_validation(client: AsyncClient):
    """Test signed URL with invalid expiration values."""
    # Too short
    payload = {"gcs_uri": "gs://bucket/object", "expiration_minutes": 0}
    resp = await client.post("/v0/storage/signed-url", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    # Too long (> 7 days = 10080 minutes)
    payload = {"gcs_uri": "gs://bucket/object", "expiration_minutes": 10081}
    resp = await client.post("/v0/storage/signed-url", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ─────────────────────────────────────────────────────────────────────────────
# Download Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_download_success(client: AsyncClient, mock_bucket_service):
    """Test successful object download."""
    test_content = b"Hello, World!"
    mock_bucket_service["blob"].download_as_bytes.return_value = test_content
    mock_bucket_service["blob"].content_type = "text/plain"

    payload = {"gcs_uri": "gs://test-bucket/hello.txt"}

    resp = await client.post(
        "/v0/storage/download",
        json=payload,
        headers=HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()

    # Verify base64 content
    import base64

    decoded = base64.b64decode(data["content_base64"])
    assert decoded == test_content

    assert data["content_type"] == "text/plain"
    assert data["size_bytes"] == len(test_content)


@pytest.mark.anyio
async def test_download_binary_content(client: AsyncClient, mock_bucket_service):
    """Test downloading binary content (e.g., image)."""
    # Simulate PNG header bytes
    test_content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    mock_bucket_service["blob"].download_as_bytes.return_value = test_content
    mock_bucket_service["blob"].content_type = "image/png"

    payload = {"gcs_uri": "gs://test-bucket/image.png"}

    resp = await client.post(
        "/v0/storage/download",
        json=payload,
        headers=HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()

    import base64

    decoded = base64.b64decode(data["content_base64"])
    assert decoded == test_content
    assert data["content_type"] == "image/png"
    assert data["size_bytes"] == len(test_content)


@pytest.mark.anyio
async def test_download_invalid_uri_format(client: AsyncClient):
    """Test download with invalid GCS URI format."""
    payload = {"gcs_uri": "https://storage.googleapis.com/bucket/object"}

    resp = await client.post(
        "/v0/storage/download",
        json=payload,
        headers=HEADERS,
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "Invalid GCS URI format" in resp.json()["detail"]


@pytest.mark.anyio
async def test_download_object_not_found(client: AsyncClient, mock_bucket_service):
    """Test download for non-existent object."""
    mock_bucket_service["blob"].exists.return_value = False

    payload = {"gcs_uri": "gs://test-bucket/nonexistent.jpg"}

    resp = await client.post(
        "/v0/storage/download",
        json=payload,
        headers=HEADERS,
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert "Object not found" in resp.json()["detail"]


@pytest.mark.anyio
async def test_download_no_content_type(client: AsyncClient, mock_bucket_service):
    """Test download when content type is not set."""
    mock_bucket_service["blob"].download_as_bytes.return_value = b"data"
    mock_bucket_service["blob"].content_type = None

    payload = {"gcs_uri": "gs://test-bucket/unknown"}

    resp = await client.post(
        "/v0/storage/download",
        json=payload,
        headers=HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["content_type"] is None


@pytest.mark.anyio
async def test_download_nested_path(client: AsyncClient, mock_bucket_service):
    """Test download with deeply nested object path."""
    mock_bucket_service["blob"].download_as_bytes.return_value = b"nested content"

    payload = {"gcs_uri": "gs://bucket/a/b/c/d/e/file.txt"}

    resp = await client.post(
        "/v0/storage/download",
        json=payload,
        headers=HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    mock_bucket_service["bucket"].blob.assert_called_once_with("a/b/c/d/e/file.txt")


# ─────────────────────────────────────────────────────────────────────────────
# Authentication Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_signed_url_requires_auth(client: AsyncClient):
    """Test that signed-url endpoint requires authentication."""
    payload = {"gcs_uri": "gs://bucket/object"}

    resp = await client.post(
        "/v0/storage/signed-url",
        json=payload,
        # No headers - no auth
    )

    assert resp.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_download_requires_auth(client: AsyncClient):
    """Test that download endpoint requires authentication."""
    payload = {"gcs_uri": "gs://bucket/object"}

    resp = await client.post(
        "/v0/storage/download",
        json=payload,
        # No headers - no auth
    )

    assert resp.status_code == status.HTTP_403_FORBIDDEN
