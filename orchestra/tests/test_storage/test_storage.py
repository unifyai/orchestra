"""Tests for the storage API endpoints."""

from unittest.mock import MagicMock

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.services.bucket_service import BucketService as OriginalBucketService
from orchestra.services.bucket_service import create_bucket_service
from orchestra.tests.utils import HEADERS
from orchestra.web.api.storage.views import (
    create_bucket_service as storage_create_bucket_service,
)


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
    bucket_mock.is_allowed_bucket.return_value = True
    # Set in BucketService.__init__, so `spec=` does not pick them up. The
    # signed-URL route compares against call_recordings_bucket_name to route
    # recordings to the comms gateway, and the object-authorization check
    # matches every bucket name; without them every request raises
    # AttributeError.
    bucket_mock.bucket_name = "generic-bucket"
    bucket_mock.assistant_media_bucket_name = "assistant-media-test"
    bucket_mock.message_attachments_bucket_name = "attachments-test"
    bucket_mock.account_photo_bucket_name = "account-photo-test"
    bucket_mock.presets_bucket_name = "assistant-media-presets"
    bucket_mock.call_recordings_bucket_name = "unity-call-recordings"

    # Override the dependency - use a factory function
    def get_mock_bucket_service():
        return bucket_mock

    fastapi_app.dependency_overrides[create_bucket_service] = get_mock_bucket_service
    fastapi_app.dependency_overrides[storage_create_bucket_service] = (
        get_mock_bucket_service
    )

    yield {
        "service": bucket_mock,
        "storage_client": mock_storage_client,
        "bucket": mock_bucket,
        "blob": mock_blob,
    }

    fastapi_app.dependency_overrides.pop(create_bucket_service, None)
    fastapi_app.dependency_overrides.pop(storage_create_bucket_service, None)


# ─────────────────────────────────────────────────────────────────────────────
# Signed URL Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_signed_url_success(client: AsyncClient, mock_bucket_service):
    """Test successful signed URL generation."""
    payload = {"gcs_uri": "gs://bucket/path/to/object.jpg"}

    resp = await client.post(
        "/v0/storage/signed-url",
        json=payload,
        headers=HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK, resp.json()
    data = resp.json()
    assert "signed_url" in data
    assert data["expires_in_minutes"] == 60  # Default

    # Verify the mock was called correctly
    mock_bucket_service["storage_client"].bucket.assert_called_once_with(
        "assistant-media-presets",
    )
    mock_bucket_service["bucket"].blob.assert_called_once_with("path/to/object.jpg")
    mock_bucket_service["blob"].exists.assert_called_once()
    mock_bucket_service["blob"].generate_signed_url.assert_called_once()


@pytest.mark.anyio
async def test_signed_url_custom_expiration(client: AsyncClient, mock_bucket_service):
    """Test signed URL generation with custom expiration."""
    payload = {
        "gcs_uri": "gs://bucket/object.png",
        "expiration_minutes": 120,
    }

    resp = await client.post(
        "/v0/storage/signed-url",
        json=payload,
        headers=HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK, resp.json()
    data = resp.json()
    assert data["expires_in_minutes"] == 120


@pytest.mark.anyio
async def test_signed_url_with_download_flag(client: AsyncClient, mock_bucket_service):
    """Test signed URL generation with download=True sets Content-Disposition."""
    payload = {
        "gcs_uri": "gs://bucket/path/to/document.pdf",
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
        "gcs_uri": "gs://bucket/123/att-456_quarterly_report.pdf",
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
        "gcs_uri": "gs://bucket/image.png",
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
    payload = {"gcs_uri": "gs://bucket"}

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

    payload = {"gcs_uri": "gs://bucket/nonexistent.jpg"}

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

    payload = {"gcs_uri": "gs://bucket/hello.txt"}

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

    payload = {"gcs_uri": "gs://bucket/image.png"}

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

    payload = {"gcs_uri": "gs://bucket/nonexistent.jpg"}

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

    payload = {"gcs_uri": "gs://bucket/unknown"}

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

    assert resp.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.anyio
async def test_download_requires_auth(client: AsyncClient):
    """Test that download endpoint requires authentication."""
    payload = {"gcs_uri": "gs://bucket/object"}

    resp = await client.post(
        "/v0/storage/download",
        json=payload,
        # No headers - no auth
    )

    assert resp.status_code == status.HTTP_401_UNAUTHORIZED


# ─────────────────────────────────────────────────────────────────────────────
# Call-recording delegation
#
# Recordings live in the comms project, which this service cannot read. The
# signing request is proxied to the comms gateway; authorization stays here
# because the gateway is called with the platform admin key and so cannot see
# the end user.
# ─────────────────────────────────────────────────────────────────────────────

RECORDING_URI = "gs://bucket/staging/42/unity_call_abc_2026-07-27.mp3"


@pytest.fixture
def comms_recording_env(monkeypatch):
    monkeypatch.setenv("UNIFY_COMMS_URL", "https://comms.example.com")
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "test-admin-key")


def _comms_response(status_code: int, payload: dict | None = None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload if payload is not None else {}
    return response


@pytest.mark.anyio
async def test_recording_signing_is_delegated_to_comms(
    client: AsyncClient,
    mock_bucket_service,
    comms_recording_env,
    monkeypatch,
):
    """The recordings bucket is signed by comms, not locally."""
    from orchestra.web.api.storage import views as storage_views

    posted = {}

    def fake_post(url, **kwargs):
        posted["url"] = url
        posted["json"] = kwargs.get("json")
        posted["headers"] = kwargs.get("headers")
        return _comms_response(
            200,
            {
                "signed_url": "https://signed.example.com/a.mp3",
                "expires_in_minutes": 60,
            },
        )

    monkeypatch.setattr(storage_views.httpx, "post", fake_post)
    monkeypatch.setattr(
        storage_views,
        "require_owned_assistant",
        lambda request, agent_id, session: agent_id,
    )

    resp = await client.post(
        "/v0/storage/signed-url",
        json={"gcs_uri": RECORDING_URI},
        headers=HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["signed_url"] == "https://signed.example.com/a.mp3"
    assert posted["url"] == "https://comms.example.com/phone/recording-url"
    assert posted["json"] == {"gcs_uri": RECORDING_URI}
    assert posted["headers"]["Authorization"] == "Bearer test-admin-key"
    # Never signed locally: this service has no access to that bucket.
    mock_bucket_service["blob"].generate_signed_url.assert_not_called()


@pytest.mark.anyio
async def test_recording_signing_enforces_assistant_access(
    client: AsyncClient,
    mock_bucket_service,
    comms_recording_env,
    monkeypatch,
):
    """Authorization runs here, keyed on the assistant in the object path."""
    from fastapi import HTTPException

    from orchestra.web.api.storage import views as storage_views

    seen = {}

    def deny(request, agent_id, session):
        seen["agent_id"] = agent_id
        raise HTTPException(status_code=404, detail="Assistant not found.")

    def fail_post(*_args, **_kwargs):
        raise AssertionError("comms must not be called when access is denied")

    monkeypatch.setattr(storage_views, "require_owned_assistant", deny)
    monkeypatch.setattr(storage_views.httpx, "post", fail_post)

    resp = await client.post(
        "/v0/storage/signed-url",
        json={"gcs_uri": RECORDING_URI},
        headers=HEADERS,
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert seen["agent_id"] == 42


@pytest.mark.anyio
@pytest.mark.parametrize("comms_status", [403, 404])
async def test_recording_signing_passes_comms_status_through(
    client: AsyncClient,
    mock_bucket_service,
    comms_recording_env,
    monkeypatch,
    comms_status,
):
    """404 (never written) and 403 (no access) must stay distinguishable.

    Collapsing them into 500 is what made a failed egress indistinguishable
    from a broken pipeline.
    """
    from orchestra.web.api.storage import views as storage_views

    monkeypatch.setattr(
        storage_views.httpx,
        "post",
        lambda *_a, **_k: _comms_response(comms_status, {"detail": "nope"}),
    )
    monkeypatch.setattr(
        storage_views,
        "require_owned_assistant",
        lambda request, agent_id, session: agent_id,
    )

    resp = await client.post(
        "/v0/storage/signed-url",
        json={"gcs_uri": RECORDING_URI},
        headers=HEADERS,
    )

    assert resp.status_code == comms_status


@pytest.mark.anyio
async def test_recording_signing_rejects_unattributable_path(
    client: AsyncClient,
    mock_bucket_service,
    comms_recording_env,
    monkeypatch,
):
    """Without an assistant segment there is nothing to authorize against."""
    from orchestra.web.api.storage import views as storage_views

    def fail_post(*_args, **_kwargs):
        raise AssertionError("comms must not be called for an unattributable path")

    monkeypatch.setattr(storage_views.httpx, "post", fail_post)

    resp = await client.post(
        "/v0/storage/signed-url",
        json={"gcs_uri": "gs://bucket/staging/unity__phone_2026.mp3"},
        headers=HEADERS,
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.anyio
async def test_recording_signing_reports_unconfigured_playback(
    client: AsyncClient,
    mock_bucket_service,
    monkeypatch,
):
    """No comms URL configured is a 503, not a signing attempt."""
    from orchestra.web.api.storage import views as storage_views

    monkeypatch.delenv("UNIFY_COMMS_URL", raising=False)
    monkeypatch.setattr(
        storage_views,
        "require_owned_assistant",
        lambda request, agent_id, session: agent_id,
    )

    resp = await client.post(
        "/v0/storage/signed-url",
        json={"gcs_uri": RECORDING_URI},
        headers=HEADERS,
    )

    assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


# ─────────────────────────────────────────────────────────────────────────────
# Object-level authorization
#
# The endpoints historically checked only the bucket allowlist, so any
# authenticated user could read any object in any allowed bucket (external
# report 2026-08-12, finding F1). Every bucket now derives an ownership
# check from the object path and fails closed when a path cannot be
# attributed to the caller. System (admin-key) callers bypass ownership.
# ─────────────────────────────────────────────────────────────────────────────

import os
from types import SimpleNamespace

from fastapi import HTTPException

from orchestra.tests.utils import ADMIN_HEADERS
from orchestra.web.api.storage import authorization as storage_authorization

ACCOUNT_USER_ID = str(os.getenv("AUTH_ACCOUNT_USER_ID"))


@pytest.fixture
def org_memberships(monkeypatch):
    """Replace OrganizationDAO with an in-memory membership table.

    Yields a dict of ``user_id -> [org_id, ...]`` that tests populate.
    """
    memberships: dict[str, list[int]] = {}

    class FakeOrganizationDAO:
        def __init__(self, session):
            pass

        def get_user_organizations(self, user_id):
            return [
                SimpleNamespace(id=org_id)
                for org_id in memberships.get(str(user_id), [])
            ]

    monkeypatch.setattr(
        storage_authorization,
        "OrganizationDAO",
        FakeOrganizationDAO,
    )
    return memberships


@pytest.fixture
def owned_assistants(monkeypatch):
    """Replace the assistant ownership check with an id allowlist."""
    allowed: set[int] = set()
    calls: list[int] = []

    def fake_require_owned_assistant(request, agent_id, session, **kwargs):
        calls.append(agent_id)
        if agent_id not in allowed:
            raise HTTPException(status_code=404, detail="Assistant not found.")
        return SimpleNamespace(agent_id=agent_id)

    monkeypatch.setattr(
        storage_authorization,
        "require_owned_assistant",
        fake_require_owned_assistant,
    )
    return {"allowed": allowed, "calls": calls}


async def _sign(client, gcs_uri, headers=HEADERS):
    return await client.post(
        "/v0/storage/signed-url",
        json={"gcs_uri": gcs_uri},
        headers=headers,
    )


async def _download(client, gcs_uri, headers=HEADERS):
    return await client.post(
        "/v0/storage/download",
        json={"gcs_uri": gcs_uri},
        headers=headers,
    )


@pytest.mark.anyio
async def test_generic_bucket_denied_for_user_keys(
    client: AsyncClient,
    mock_bucket_service,
):
    """Flat hash-named generic-bucket objects carry no owner: fail closed."""
    for call in (_sign, _download):
        resp = await call(client, "gs://bucket/abc123_ff.png")
        assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.json()
        assert "not permitted" in resp.json()["detail"]


@pytest.mark.anyio
async def test_generic_bucket_allowed_for_system_key(
    client: AsyncClient,
    mock_bucket_service,
):
    """Admin-key callers keep full access, matching other runtime surfaces."""
    resp = await _sign(client, "gs://bucket/abc123_ff.png", ADMIN_HEADERS)
    assert resp.status_code == status.HTTP_200_OK, resp.json()


@pytest.mark.anyio
async def test_assistant_media_requires_assistant_access(
    client: AsyncClient,
    mock_bucket_service,
    owned_assistants,
):
    """Assistant-media paths authorize via the leading assistant id."""
    owned_assistants["allowed"].add(42)

    resp = await _sign(client, "gs://bucket/42/image/pic.jpg")
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    resp = await _sign(client, "gs://bucket/43/image/pic.jpg")
    assert resp.status_code == status.HTTP_404_NOT_FOUND

    assert owned_assistants["calls"] == [42, 43]


@pytest.mark.anyio
async def test_assistant_media_tmp_objects_denied(
    client: AsyncClient,
    mock_bucket_service,
    owned_assistants,
):
    """tmp/ objects are only consumed via upload-time signed URLs."""
    resp = await _sign(client, "gs://bucket/tmp/abc123.png")
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert owned_assistants["calls"] == []


@pytest.mark.anyio
async def test_attachments_assistant_scope(
    client: AsyncClient,
    mock_bucket_service,
    owned_assistants,
):
    """Attachment paths with a numeric prefix follow assistant access."""
    owned_assistants["allowed"].add(7)

    resp = await _download(client, "gs://bucket/7/att-1_doc.pdf")
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    resp = await _download(client, "gs://bucket/8/att-1_doc.pdf")
    assert resp.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_attachments_org_scope(
    client: AsyncClient,
    mock_bucket_service,
    org_memberships,
):
    """org-{id} attachment prefixes require membership of that org."""
    org_memberships[ACCOUNT_USER_ID] = [7]

    resp = await _sign(client, "gs://bucket/org-7/att-1_doc.pdf")
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    resp = await _sign(client, "gs://bucket/org-8/att-1_doc.pdf")
    assert resp.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_attachments_legacy_user_scope(
    client: AsyncClient,
    mock_bucket_service,
):
    """Legacy {user_id}/ attachment prefixes only match the caller."""
    resp = await _sign(client, f"gs://bucket/{ACCOUNT_USER_ID}/doc.pdf")
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    resp = await _sign(client, "gs://bucket/some-other-user/doc.pdf")
    assert resp.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_account_photo_user_scope(
    client: AsyncClient,
    mock_bucket_service,
    org_memberships,
):
    """User photos are visible to the user and users sharing an org."""
    resp = await _sign(client, f"gs://bucket/user/{ACCOUNT_USER_ID}/p.jpg")
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    # Colleague in a shared org: allowed.
    org_memberships[ACCOUNT_USER_ID] = [5]
    org_memberships["colleague-user"] = [5]
    resp = await _sign(client, "gs://bucket/user/colleague-user/p.jpg")
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    # Stranger with no shared org: denied.
    org_memberships["stranger-user"] = [6]
    resp = await _sign(client, "gs://bucket/user-voice/stranger-user/v.mp3")
    assert resp.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_account_photo_org_scope(
    client: AsyncClient,
    mock_bucket_service,
    org_memberships,
):
    """Org (and nested team) photos require membership of the org."""
    org_memberships[ACCOUNT_USER_ID] = [5]

    resp = await _sign(client, "gs://bucket/organization/5/logo.jpg")
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    resp = await _sign(client, "gs://bucket/organization/5/team/3/t.jpg")
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    resp = await _sign(client, "gs://bucket/organization/9/logo.jpg")
    assert resp.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_recording_download_follows_assistant_access(
    client: AsyncClient,
    mock_bucket_service,
    owned_assistants,
):
    """Recording reads outside the comms-gateway branch stay assistant-scoped."""
    owned_assistants["allowed"].add(42)

    resp = await _download(client, "gs://bucket/prod/42/room_1.mp3")
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    resp = await _download(client, "gs://bucket/prod/43/room_1.mp3")
    assert resp.status_code == status.HTTP_404_NOT_FOUND

    resp = await _download(client, "gs://bucket/loose-file.mp3")
    assert resp.status_code == status.HTTP_403_FORBIDDEN
