"""
Tests for the assistant status system:
1. get_running_jobs — queries K8s via the comms service
2. get_runtime_status — reads the Comms runtime aggregate
3. admin_get_assistant_status — the admin endpoint that returns online/offline
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import AsyncClient

from orchestra.tests.utils import ADMIN_HEADERS
from orchestra.web.api.utils.assistant_infra import (
    RUNTIME_JOB_LOOKBACK_HOURS,
    get_running_jobs,
    get_runtime_status,
)

# =============================================================================
# get_running_jobs unit tests
# =============================================================================


def _mock_response(status_code: int, json_data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


@pytest.mark.anyio
async def test_get_running_jobs_returns_running_job_names():
    mock_resp = _mock_response(
        200,
        {
            "success": True,
            "jobs": [
                {
                    "job_name": "unity-abc-2026-03-19",
                    "status": "Running",
                    "assistant_id": "abc",
                },
            ],
        },
    )
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch(
        "orchestra.web.api.utils.assistant_infra.httpx.AsyncClient",
        return_value=mock_client,
    ), patch(
        "orchestra.web.api.utils.assistant_infra.COMMS_URL",
        "http://comms:8000",
    ), patch(
        "orchestra.web.api.utils.assistant_infra.ADMIN_KEY",
        "test-key",
    ):
        result = await get_running_jobs("abc")

    assert result == ["unity-abc-2026-03-19"]
    mock_client.get.assert_called_once()
    call_kwargs = mock_client.get.call_args
    assert (
        "app=unity,assistant-id=abc" in call_kwargs.kwargs["params"]["label_selector"]
    )
    assert call_kwargs.kwargs["params"]["hours"] == RUNTIME_JOB_LOOKBACK_HOURS


@pytest.mark.anyio
async def test_get_running_jobs_filters_out_completed_jobs():
    mock_resp = _mock_response(
        200,
        {
            "success": True,
            "jobs": [
                {
                    "job_name": "unity-old-job",
                    "status": "Completed",
                    "assistant_id": "abc",
                },
                {
                    "job_name": "unity-failed-job",
                    "status": "Failed",
                    "assistant_id": "abc",
                },
            ],
        },
    )
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch(
        "orchestra.web.api.utils.assistant_infra.httpx.AsyncClient",
        return_value=mock_client,
    ), patch(
        "orchestra.web.api.utils.assistant_infra.COMMS_URL",
        "http://comms:8000",
    ), patch(
        "orchestra.web.api.utils.assistant_infra.ADMIN_KEY",
        "test-key",
    ):
        result = await get_running_jobs("abc")

    assert result == []


@pytest.mark.anyio
async def test_get_running_jobs_empty_jobs_list():
    mock_resp = _mock_response(200, {"success": True, "jobs": []})
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch(
        "orchestra.web.api.utils.assistant_infra.httpx.AsyncClient",
        return_value=mock_client,
    ), patch(
        "orchestra.web.api.utils.assistant_infra.COMMS_URL",
        "http://comms:8000",
    ), patch(
        "orchestra.web.api.utils.assistant_infra.ADMIN_KEY",
        "test-key",
    ):
        result = await get_running_jobs("abc")

    assert result == []


@pytest.mark.anyio
async def test_get_running_jobs_comms_returns_500():
    mock_resp = _mock_response(500, {"detail": "Internal Server Error"})
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch(
        "orchestra.web.api.utils.assistant_infra.httpx.AsyncClient",
        return_value=mock_client,
    ), patch(
        "orchestra.web.api.utils.assistant_infra.COMMS_URL",
        "http://comms:8000",
    ), patch(
        "orchestra.web.api.utils.assistant_infra.ADMIN_KEY",
        "test-key",
    ):
        result = await get_running_jobs("abc")

    assert result == []


@pytest.mark.anyio
async def test_get_running_jobs_comms_unreachable():
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

    with patch(
        "orchestra.web.api.utils.assistant_infra.httpx.AsyncClient",
        return_value=mock_client,
    ), patch(
        "orchestra.web.api.utils.assistant_infra.COMMS_URL",
        "http://comms:8000",
    ), patch(
        "orchestra.web.api.utils.assistant_infra.ADMIN_KEY",
        "test-key",
    ):
        result = await get_running_jobs("abc")

    assert result == []


@pytest.mark.anyio
async def test_get_running_jobs_no_comms_url():
    with patch("orchestra.web.api.utils.assistant_infra.COMMS_URL", None), patch(
        "orchestra.web.api.utils.assistant_infra.ADMIN_KEY",
        "test-key",
    ):
        result = await get_running_jobs("abc")

    assert result == []


@pytest.mark.anyio
async def test_get_running_jobs_no_admin_key():
    with patch(
        "orchestra.web.api.utils.assistant_infra.COMMS_URL",
        "http://comms:8000",
    ), patch("orchestra.web.api.utils.assistant_infra.ADMIN_KEY", None):
        result = await get_running_jobs("abc")

    assert result == []


@pytest.mark.anyio
async def test_get_running_jobs_normalizes_assistant_id():
    """assistant-id labels are lowercased with underscores replaced by hyphens."""
    mock_resp = _mock_response(200, {"success": True, "jobs": []})
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch(
        "orchestra.web.api.utils.assistant_infra.httpx.AsyncClient",
        return_value=mock_client,
    ), patch(
        "orchestra.web.api.utils.assistant_infra.COMMS_URL",
        "http://comms:8000",
    ), patch(
        "orchestra.web.api.utils.assistant_infra.ADMIN_KEY",
        "test-key",
    ):
        await get_running_jobs("ABC_DEF_123")

    call_kwargs = mock_client.get.call_args
    assert "assistant-id=abc-def-123" in call_kwargs.kwargs["params"]["label_selector"]


@pytest.mark.anyio
async def test_get_runtime_status_returns_payload():
    mock_resp = _mock_response(
        200,
        {
            "assistant_id": "abc",
            "assistant_session_exists": True,
            "assistant_session_phase": "Active",
            "active_job_names": ["unity-abc-2026-03-19"],
        },
    )
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch(
        "orchestra.web.api.utils.assistant_infra.httpx.AsyncClient",
        return_value=mock_client,
    ), patch(
        "orchestra.web.api.utils.assistant_infra.COMMS_URL",
        "http://comms:8000",
    ), patch(
        "orchestra.web.api.utils.assistant_infra.ADMIN_KEY",
        "test-key",
    ):
        result = await get_runtime_status("abc")

    assert result is not None
    assert result["active_job_names"] == ["unity-abc-2026-03-19"]
    mock_client.get.assert_called_once_with(
        "http://comms:8000/infra/runtime/abc",
        headers={"Authorization": "Bearer test-key"},
        timeout=10,
    )


# =============================================================================
# admin_get_assistant_status endpoint tests
# =============================================================================


@pytest.fixture(autouse=True)
def mock_assistant_infra_calls(request):
    if "no_mock_infra" in request.keywords:
        yield
        return

    with patch(
        "orchestra.web.api.assistant.views.wake_up_assistant",
        new_callable=AsyncMock,
    ) as mock_wake_up, patch(
        "orchestra.web.api.assistant.views.reawaken_assistant",
        new_callable=AsyncMock,
    ) as mock_reawaken, patch(
        "orchestra.web.api.assistant.views.process_assistant_cleanup_tasks",
        new_callable=AsyncMock,
    ) as mock_cleanup_tasks, patch(
        "orchestra.web.api.assistant.views.settings",
    ) as mock_settings:
        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})
        mock_cleanup_tasks.return_value = {
            "processed": 1,
            "completed": 1,
            "retried": 0,
            "failed": 0,
            "errors": [],
        }
        mock_settings.is_staging = True

        yield mock_wake_up, mock_reawaken, mock_cleanup_tasks


@pytest.mark.anyio
async def test_status_endpoint_running(client: AsyncClient):
    with patch(
        "orchestra.web.api.assistant.views.get_runtime_status",
        new_callable=AsyncMock,
        return_value={
            "assistant_session_exists": True,
            "assistant_session_phase": "Active",
            "active_job_names": ["unity-job-123"],
        },
    ):
        resp = await client.get(
            "/v0/admin/assistant/test-id/status",
            headers=ADMIN_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["info"]["running"] is True
    assert data["info"]["job_name"] == "unity-job-123"


@pytest.mark.anyio
async def test_status_endpoint_offline(client: AsyncClient):
    with patch(
        "orchestra.web.api.assistant.views.get_runtime_status",
        new_callable=AsyncMock,
        return_value={
            "assistant_session_exists": True,
            "assistant_session_phase": "Released",
            "active_job_names": [],
        },
    ):
        resp = await client.get(
            "/v0/admin/assistant/test-id/status",
            headers=ADMIN_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["info"]["running"] is False
    assert data["info"]["job_name"] is None


@pytest.mark.anyio
async def test_status_endpoint_error(client: AsyncClient):
    with patch(
        "orchestra.web.api.assistant.views.get_runtime_status",
        new_callable=AsyncMock,
        side_effect=RuntimeError("comms exploded"),
    ):
        resp = await client.get(
            "/v0/admin/assistant/test-id/status",
            headers=ADMIN_HEADERS,
        )

    assert resp.status_code == 500
    assert "Failed to get assistant status" in resp.json()["detail"]
