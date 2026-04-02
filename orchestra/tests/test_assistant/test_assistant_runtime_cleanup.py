from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestra.db.models.orchestra_models import AssistantCleanupTask
from orchestra.services.assistant_cleanup_service import (
    CleanupSource,
    process_assistant_cleanup_tasks,
)
from orchestra.web.api.utils import assistant_infra


@pytest.mark.anyio
async def test_teardown_assistant_runtime_reports_incomplete_steps():
    with patch(
        "orchestra.web.api.utils.assistant_infra.stop_jobs",
        new_callable=AsyncMock,
    ) as mock_stop_jobs, patch(
        "orchestra.web.api.utils.assistant_infra.delete_assistant_session",
        new_callable=AsyncMock,
    ) as mock_delete_session, patch(
        "orchestra.web.api.utils.assistant_infra.delete_pubsub_topic",
        new_callable=AsyncMock,
    ) as mock_delete_topic:
        mock_stop_jobs.return_value = {
            "success": True,
            "job_names": [],
            "steps": {
                "discover_jobs": {"success": True},
                "stop_job": {"success": True, "skipped": True},
                "release_pool_vm": {"success": True},
            },
            "errors": [],
        }
        mock_delete_session.return_value = {
            "name": "delete_assistant_session",
            "success": True,
        }
        mock_delete_topic.return_value = {
            "name": "delete_pubsub_topic",
            "success": False,
            "timed_out": True,
            "error": "request timed out",
        }

        result = await assistant_infra.teardown_assistant_runtime("42")

    assert result["success"] is False
    assert "delete_pubsub_topic: request timed out" in result["errors"]
    assert result["steps"]["delete_pubsub_topic"]["timed_out"] is True


def test_teardown_assistant_runtime_sync_reports_incomplete_steps():
    mock_client = MagicMock()
    mock_jobs_response = MagicMock()
    mock_jobs_response.raise_for_status.return_value = None
    mock_jobs_response.json.return_value = {"jobs": []}
    mock_client.get.return_value = mock_jobs_response

    with patch.object(assistant_infra, "COMMS_URL", "https://comms.test"), patch.object(
        assistant_infra,
        "ADMIN_KEY",
        "test-key",
    ), patch(
        "orchestra.web.api.utils.assistant_infra.httpx.Client",
    ) as mock_httpx_client, patch(
        "orchestra.web.api.utils.assistant_infra._request_cleanup_step_sync",
    ) as mock_request_step:
        mock_httpx_client.return_value.__enter__.return_value = mock_client

        def _step(*, name, **_kwargs):
            if name == "delete_pubsub_topic":
                return {
                    "name": name,
                    "success": False,
                    "timed_out": True,
                    "error": "request timed out",
                }
            return {"name": name, "success": True}

        mock_request_step.side_effect = _step

        result = assistant_infra.teardown_assistant_runtime_sync("42")

    assert result["success"] is False
    assert "delete_pubsub_topic: request timed out" in result["errors"]
    assert result["steps"]["delete_pubsub_topic"]["timed_out"] is True


@pytest.mark.anyio
async def test_process_assistant_cleanup_tasks_retries_incomplete_runtime(dbsession):
    task = AssistantCleanupTask(
        assistant_id=42,
        deploy_env=None,
        desktop_mode="ubuntu",
        source_flow=CleanupSource.ASSISTANT_DELETE,
        cleanup_payload={"contacts": []},
        status="pending",
    )
    dbsession.add(task)
    dbsession.commit()

    with patch(
        "orchestra.services.assistant_cleanup_service.teardown_assistant_runtime",
        new_callable=AsyncMock,
    ) as mock_teardown, patch(
        "orchestra.services.assistant_cleanup_service.deprovision_assistant_contacts",
        new_callable=AsyncMock,
    ) as mock_deprovision:
        mock_teardown.return_value = {
            "success": False,
            "assistant_id": "42",
            "steps": {
                "delete_pubsub_topic": {
                    "success": False,
                    "timed_out": True,
                    "error": "request timed out",
                },
            },
            "errors": ["delete_pubsub_topic: request timed out"],
        }
        mock_deprovision.return_value = {
            "success": True,
            "attempted": 0,
            "soft_deleted": 0,
            "errors": [],
        }

        result = await process_assistant_cleanup_tasks(dbsession, task_ids=[task.id])

    dbsession.expire_all()
    refreshed = dbsession.get(AssistantCleanupTask, task.id)
    assert result["retried"] == 1
    assert refreshed is not None
    assert refreshed.status == "pending"
    assert refreshed.attempt_count == 1
    assert refreshed.last_error == "delete_pubsub_topic: request timed out"
