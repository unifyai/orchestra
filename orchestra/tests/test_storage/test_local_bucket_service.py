from __future__ import annotations

import pytest

from orchestra.services.local_bucket_service import LocalBucketService


def test_local_bucket_reads_object_with_content_type(monkeypatch, tmp_path):
    monkeypatch.setenv("SELF_HOST_LOCAL_BUCKET_DIR", str(tmp_path))
    bucket_service = LocalBucketService()

    bucket_service._write_object(
        bucket_service.bucket_name,
        "tmp/example.txt",
        b"hello",
        content_type="text/plain",
    )

    content, content_type = bucket_service.read_local_object(
        bucket_service.bucket_name,
        "tmp/example.txt",
    )

    assert content == b"hello"
    assert content_type == "text/plain"


def test_local_bucket_rejects_paths_outside_bucket(monkeypatch, tmp_path):
    monkeypatch.setenv("SELF_HOST_LOCAL_BUCKET_DIR", str(tmp_path))
    bucket_service = LocalBucketService()

    with pytest.raises(FileNotFoundError):
        bucket_service.read_local_object(
            bucket_service.bucket_name,
            "../outside.txt",
        )
