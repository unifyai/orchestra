"""Tests for Pipedream File Stash execution and download."""

from __future__ import annotations

import base64

from orchestra.integrations.providers.base import ProviderExecutionRequest
from orchestra.integrations.providers.pipedream import PipedreamProviderAdapter


def test_execute_forwards_stash_id_and_surfaces_filestash_uploads(monkeypatch) -> None:
    adapter = PipedreamProviderAdapter(
        access_token="token",
        project_id="proj_test",
        environment="development",
    )
    monkeypatch.setattr(adapter, "_access_token", lambda: ("token", None))
    captured: dict[str, object] = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {
                "stashId": "stash-123",
                "exports": {
                    "$filestash_uploads": [
                        {
                            "path": "invoice.pdf",
                            "s3Key": "1day/proj_test/u/invoice.pdf",
                            "get_url": "https://example.test/invoice.pdf",
                        },
                    ],
                },
            }

    def fake_post(url, **kwargs):  # noqa: ANN001
        captured["url"] = url
        captured["json"] = kwargs["json"]
        return _Resp()

    import requests

    monkeypatch.setattr(requests, "post", fake_post)

    result = adapter.execute(
        ProviderExecutionRequest(
            backend_id="pipedream",
            tool_id="pipedream:google_drive:download_file",
            canonical_app_slug="google_drive",
            provider_tool_id="google_drive-download-file",
            connection_id="conn-1",
            provider_connection_id="apn_test",
            action_class="read",
            user_id="user-1",
            arguments={"filePath": "/tmp/invoice.pdf", "stash_id": ""},
        ),
    )

    assert result.status == "ok"
    assert captured["json"]["stash_id"] == ""
    assert "stash_id" not in captured["json"]["configured_props"]
    assert result.result["stash_id"] == "stash-123"
    assert len(result.result["filestash_uploads"]) == 1


def test_download_file_returns_base64_payload(monkeypatch) -> None:
    adapter = PipedreamProviderAdapter(
        access_token="token",
        project_id="proj_test",
        environment="development",
    )
    monkeypatch.setattr(adapter, "_access_token", lambda: ("token", None))
    captured: dict[str, object] = {}

    class _Resp:
        status_code = 200
        content = b"pdf-bytes"
        headers = {"Content-Type": "application/pdf"}

        @staticmethod
        def raise_for_status() -> None:
            return None

    def fake_get(url, **kwargs):  # noqa: ANN001
        captured["url"] = url
        captured["params"] = kwargs["params"]
        return _Resp()

    import requests

    monkeypatch.setattr(requests, "get", fake_get)

    result = adapter.download_file(s3_key="1day/proj_test/u/invoice.pdf")

    assert result["status"] == "ok"
    assert (
        captured["url"]
        == "https://api.pipedream.com/v1/connect/proj_test/file_stash/download"
    )
    assert captured["params"] == {"s3_key": "1day/proj_test/u/invoice.pdf"}
    assert base64.b64decode(result["content_base64"]) == b"pdf-bytes"
    assert result["filename"] == "invoice.pdf"


def test_stage_file_is_not_supported_for_pipedream() -> None:
    adapter = PipedreamProviderAdapter(
        access_token="token",
        project_id="proj_test",
    )
    result = adapter.stage_file(
        content=b"data",
        filename="invoice.pdf",
        mimetype="application/pdf",
        toolkit_slug="google_drive",
        tool_slug="upload-file",
    )
    assert result["status"] == "error"
    assert result["error"]["code"] == "provider_file_staging_not_supported"
