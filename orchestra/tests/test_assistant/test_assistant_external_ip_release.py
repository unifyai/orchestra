from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestra.services.assistant_cleanup_service import (
    build_cleanup_spec_from_assistant,
)
from orchestra.services.assistant_external_ip_service import (
    external_ip_region_slug,
    release_assistant_external_ip,
)


def test_external_ip_region_slug_normalizes_gcp_self_link() -> None:
    assert external_ip_region_slug("regions/us-central1") == "us-central1"
    assert external_ip_region_slug("us-central1") == "us-central1"


def test_build_cleanup_spec_from_assistant_persists_external_ip_region() -> None:
    assistant = MagicMock()
    assistant.agent_id = 4242
    assistant.desktop_mode = "ubuntu"
    assistant.profile_photo = None
    assistant.profile_video = None
    assistant.external_ip = MagicMock(region="regions/us-central1")

    spec = build_cleanup_spec_from_assistant(assistant)

    assert spec.release_external_ip is True
    assert spec.external_ip_region == "us-central1"
    assert spec.to_payload()["external_ip_region"] == "us-central1"


@pytest.mark.anyio
async def test_release_assistant_external_ip_uses_persisted_region_without_db_row() -> (
    None
):
    with (
        patch(
            "orchestra.services.assistant_external_ip_service.assistant_infra._comms_url",
            return_value="https://comms.example",
        ),
        patch(
            "orchestra.services.assistant_external_ip_service.assistant_infra.ADMIN_KEY",
            "admin-key",
        ),
        patch(
            "orchestra.services.assistant_external_ip_service.get_async_client",
        ) as mock_client_factory,
    ):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"released": True}
        mock_response.raise_for_status = MagicMock()
        mock_client.delete = AsyncMock(return_value=mock_response)
        mock_client_factory.return_value = mock_client

        result = await release_assistant_external_ip(
            None,
            assistant_id=4242,
            region="regions/us-central1",
        )

    assert result["success"] is True
    mock_client.delete.assert_awaited_once_with(
        "https://comms.example/infra/vm/assistant-static-ip/4242",
        headers={"Authorization": "Bearer admin-key"},
        params={"region": "us-central1"},
        timeout=20.0,
    )
