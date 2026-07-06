"""Tests for Composio file staging used by social publishers."""

from __future__ import annotations

from orchestra.integrations.providers.composio import ComposioProviderAdapter


def test_stage_file_uploads_to_presigned_url(monkeypatch) -> None:
    adapter = ComposioProviderAdapter(api_key="test-key")
    calls: list[tuple[str, str]] = []

    class _Resp:
        def __init__(self, payload: dict, *, status_code: int = 200) -> None:
            self._payload = payload
            self.status_code = status_code

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self) -> dict:
            return self._payload

    def fake_post(url, **kwargs):  # noqa: ANN001
        calls.append(("post", url))
        assert kwargs["json"]["toolkit_slug"] == "tiktok"
        assert kwargs["json"]["tool_slug"] == "TIKTOK_UPLOAD_VIDEO"
        return _Resp(
            {
                "key": "projects/pr_test/requests/tiktok/video.mp4",
                "new_presigned_url": "https://upload.example/presigned",
            }
        )

    def fake_put(url, **kwargs):  # noqa: ANN001
        calls.append(("put", url))
        assert kwargs["headers"]["Content-Type"] == "video/mp4"
        assert kwargs["data"] == b"video-bytes"
        return _Resp({}, status_code=200)

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("requests.put", fake_put)

    result = adapter.stage_file(
        content=b"video-bytes",
        filename="final_social.mp4",
        mimetype="video/mp4",
        toolkit_slug="tiktok",
        tool_slug="TIKTOK_UPLOAD_VIDEO",
    )

    assert result["status"] == "ok"
    assert result["file"] == {
        "name": "final_social.mp4",
        "mimetype": "video/mp4",
        "s3key": "projects/pr_test/requests/tiktok/video.mp4",
    }
    assert calls == [
        ("post", "https://backend.composio.dev/api/v3.1/files/upload/request"),
        ("put", "https://upload.example/presigned"),
    ]
