"""Private provider-event storage tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestra.provider_triggers.private_event_storage import (
    PRIVATE_EVENT_BUCKET_NAME,
    EventBlobService,
    TriggerKeyWrappingService,
)
from orchestra.services.local_bucket_service import LocalBucketService
from orchestra.web.api.storage.views import public_router


@pytest.fixture
def private_blob_root(tmp_path: Path) -> Path:
    return tmp_path / "provider-event-private"


def test_trigger_event_blob_round_trips_through_private_service_self_host(
    private_blob_root: Path,
) -> None:
    wrapping = TriggerKeyWrappingService(
        master_key=b"provider-trigger-self-host-master",
    )
    service = EventBlobService(root_dir=private_blob_root, wrapping_service=wrapping)
    stored = service.write_object(
        binding_id="binding-1",
        receipt_id="receipt-1",
        plaintext=b'{"title":"fixture"}',
    )
    plaintext = service.read_object(
        stored,
        binding_id="binding-1",
        receipt_id="receipt-1",
    )
    assert plaintext == b'{"title":"fixture"}'
    assert stored.integrity_hash
    assert service.bucket_name == PRIVATE_EVENT_BUCKET_NAME


def test_private_trigger_event_blob_is_unreachable_from_public_local_storage(
    private_blob_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SELF_HOST", "1")
    monkeypatch.setenv(
        "ORCHESTRA_LOCAL_BUCKET_ROOT",
        str(private_blob_root.parent / "public-local-bucket"),
    )

    wrapping = TriggerKeyWrappingService(
        master_key=b"provider-trigger-self-host-master",
    )
    private_service = EventBlobService(
        root_dir=private_blob_root,
        wrapping_service=wrapping,
    )
    stored = private_service.write_object(
        binding_id="binding-1",
        receipt_id="receipt-1",
        plaintext=b"secret-provider-payload",
    )

    bucket_service = LocalBucketService()
    assert not bucket_service.is_allowed_bucket(PRIVATE_EVENT_BUCKET_NAME)
    assert PRIVATE_EVENT_BUCKET_NAME not in {
        bucket_service.bucket_name,
        bucket_service.assistant_media_bucket_name,
        bucket_service.message_attachments_bucket_name,
        bucket_service.call_recordings_bucket_name,
        bucket_service.account_photo_bucket_name,
        bucket_service.presets_bucket_name,
    }

    app = FastAPI()
    app.include_router(public_router, prefix="/v0")
    client = TestClient(app)
    response = client.get(
        f"/v0/storage/local/{PRIVATE_EVENT_BUCKET_NAME}/{stored.namespace_key}",
    )
    assert response.status_code == 403

    public_bucket = (
        bucket_service._allowed_buckets.pop() if False else "assistant-media"
    )
    allowed = next(iter(bucket_service._allowed_buckets))
    assert allowed != PRIVATE_EVENT_BUCKET_NAME
