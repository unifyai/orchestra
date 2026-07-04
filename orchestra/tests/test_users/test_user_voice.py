"""
Tests for the user voice-enrollment sample endpoints.

Covers:
- POST /v0/user/voice/upload (WAV upload, type validation, sample replacement)
- DELETE /v0/user/voice
- voice_sample exposure on /v0/user/basic-info and /v0/admin/user/by-email
"""

import io
import os
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

from orchestra.services.bucket_service import BucketService as OriginalBucketService

ADMIN_HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}",
}

MOCK_GCS_URL = "gs://bucket/user-voice/abc/sample.wav"


@pytest.fixture
def mock_bucket_service():
    bucket_mock = MagicMock(spec=OriginalBucketService)
    bucket_mock.upload_user_voice_file.return_value = MOCK_GCS_URL
    bucket_mock.delete_user_voice_samples.return_value = 1
    with patch(
        "orchestra.services.bucket_service.create_bucket_service",
        return_value=bucket_mock,
    ):
        yield bucket_mock


async def _create_user_with_key(client: AsyncClient, email: str) -> tuple[str, dict]:
    """Create a user and return (user_id, user-key auth headers)."""
    response = await client.post(
        "/v0/admin/user",
        json={"email": email},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.json()
    user_id = response.json()["id"]

    response = await client.get(
        f"/v0/admin/user/by-email?email={email}",
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.json()
    api_key = response.json()["api_key"]
    return user_id, {
        "accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }


def _wav_upload(filename: str = "enrollment.wav") -> dict:
    return {"file": (filename, io.BytesIO(b"RIFF....WAVEfmt fake-audio"), "audio/wav")}


@pytest.mark.anyio
async def test_voice_upload_stores_sample_and_exposes_it(
    client: AsyncClient,
    mock_bucket_service,
):
    """Uploading a WAV stores the gs:// URL + timestamp and exposes both on
    basic-info and the admin by-email payload."""
    user_id, user_headers = await _create_user_with_key(
        client,
        "voice_upload@example.com",
    )

    response = await client.post(
        "/v0/user/voice/upload",
        files=_wav_upload(),
        headers=user_headers,
    )
    assert response.status_code == 201, response.json()
    assert response.json()["gcs_url"] == MOCK_GCS_URL

    mock_bucket_service.upload_user_voice_file.assert_called_once()
    # Any previous sample is replaced, not accumulated.
    mock_bucket_service.delete_user_voice_samples.assert_called_once_with(user_id)

    response = await client.get("/v0/user/basic-info", headers=user_headers)
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["voice_sample"] == MOCK_GCS_URL
    assert data["voice_sample_uploaded_at"] is not None

    response = await client.get(
        "/v0/admin/user/by-email?email=voice_upload@example.com",
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["voice_sample"] == MOCK_GCS_URL
    assert data["voice_sample_uploaded_at"] is not None


@pytest.mark.anyio
async def test_voice_upload_rejects_non_wav(
    client: AsyncClient,
    mock_bucket_service,
):
    """Non-WAV uploads are rejected with 400."""
    _, user_headers = await _create_user_with_key(
        client,
        "voice_upload_bad_type@example.com",
    )

    response = await client.post(
        "/v0/user/voice/upload",
        files={"file": ("clip.mp3", io.BytesIO(b"ID3fake"), "audio/mpeg")},
        headers=user_headers,
    )
    assert response.status_code == 400, response.json()
    mock_bucket_service.upload_user_voice_file.assert_not_called()


@pytest.mark.anyio
async def test_voice_delete_clears_sample(
    client: AsyncClient,
    mock_bucket_service,
):
    """DELETE /user/voice removes stored objects and clears the user columns."""
    _, user_headers = await _create_user_with_key(
        client,
        "voice_delete@example.com",
    )

    response = await client.post(
        "/v0/user/voice/upload",
        files=_wav_upload(),
        headers=user_headers,
    )
    assert response.status_code == 201, response.json()

    response = await client.delete("/v0/user/voice", headers=user_headers)
    assert response.status_code == 200, response.json()

    response = await client.get("/v0/user/basic-info", headers=user_headers)
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["voice_sample"] is None
    assert data["voice_sample_uploaded_at"] is None


@pytest.mark.anyio
async def test_voice_upload_requires_user_key(
    client: AsyncClient,
    mock_bucket_service,
):
    """The upload endpoint is user-key data-scoped: no/invalid key is rejected."""
    response = await client.post(
        "/v0/user/voice/upload",
        files=_wav_upload(),
        headers={"Authorization": "Bearer not-a-real-key"},
    )
    assert response.status_code in (401, 403), response.text
    mock_bucket_service.upload_user_voice_file.assert_not_called()
