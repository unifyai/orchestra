from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, call, patch

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
        "orchestra.web.api.utils.assistant_infra.stop_assistant_session_runtime",
        new_callable=AsyncMock,
    ) as mock_stop_session, patch(
        "orchestra.web.api.utils.assistant_infra.delete_assistant_session",
        new_callable=AsyncMock,
    ) as mock_delete_session, patch(
        "orchestra.web.api.utils.assistant_infra.wait_for_runtime_cleanup",
        new_callable=AsyncMock,
    ) as mock_wait_for_runtime_cleanup, patch(
        "orchestra.web.api.utils.assistant_infra.delete_pubsub_topic",
        new_callable=AsyncMock,
    ) as mock_delete_topic, patch(
        "orchestra.web.api.utils.assistant_infra.delete_assistant_disk",
        new_callable=AsyncMock,
    ) as mock_delete_disk:
        mock_stop_session.return_value = {
            "name": "stop_assistant_session_runtime",
            "success": True,
        }
        mock_wait_for_runtime_cleanup.return_value = {
            "name": "wait_for_runtime_cleanup",
            "success": True,
        }
        mock_delete_session.return_value = {
            "name": "delete_assistant_session",
            "success": True,
            "response": {"deleted": True},
        }
        mock_delete_topic.return_value = {
            "name": "delete_pubsub_topic",
            "success": False,
            "timed_out": True,
            "error": "request timed out",
        }
        mock_delete_disk.return_value = {
            "name": "delete_assistant_disk",
            "success": True,
        }

        result = await assistant_infra.teardown_assistant_runtime("42")

    assert result["success"] is False
    assert "delete_pubsub_topic: request timed out" in result["errors"]
    assert result["steps"]["delete_pubsub_topic"]["timed_out"] is True
    assert result["steps"]["stop_assistant_session_runtime"]["success"] is True


def test_teardown_assistant_runtime_sync_reports_incomplete_steps():
    with patch.object(assistant_infra, "COMMS_URL", "https://comms.test"), patch.object(
        assistant_infra,
        "ADMIN_KEY",
        "test-key",
    ), patch(
        "orchestra.web.api.utils.assistant_infra._request_cleanup_step_sync",
    ) as mock_request_step:

        def _step(*, name, **_kwargs):
            if name == "stop_assistant_session_runtime":
                return {
                    "name": name,
                    "success": True,
                }
            if name == "delete_assistant_session":
                return {
                    "name": name,
                    "success": True,
                    "response": {"deleted": True},
                }
            if name == "runtime_status":
                return {
                    "name": name,
                    "success": True,
                    "response": {"runtime_cleanup_complete": True},
                }
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
async def test_teardown_assistant_runtime_handles_missing_session_after_cleanup():
    with patch(
        "orchestra.web.api.utils.assistant_infra.stop_assistant_session_runtime",
        new_callable=AsyncMock,
    ) as mock_stop_session, patch(
        "orchestra.web.api.utils.assistant_infra.delete_assistant_session",
        new_callable=AsyncMock,
    ) as mock_delete_session, patch(
        "orchestra.web.api.utils.assistant_infra.wait_for_runtime_cleanup",
        new_callable=AsyncMock,
    ) as mock_wait_for_runtime_cleanup, patch(
        "orchestra.web.api.utils.assistant_infra.delete_pubsub_topic",
        new_callable=AsyncMock,
    ) as mock_delete_topic, patch(
        "orchestra.web.api.utils.assistant_infra.delete_assistant_disk",
        new_callable=AsyncMock,
    ) as mock_delete_disk:
        mock_stop_session.return_value = {
            "name": "stop_assistant_session_runtime",
            "success": True,
        }
        mock_wait_for_runtime_cleanup.return_value = {
            "name": "wait_for_runtime_cleanup",
            "success": True,
            "response": {"runtime_cleanup_complete": True},
        }
        mock_delete_session.return_value = {
            "name": "delete_assistant_session",
            "success": True,
            "skipped": True,
            "reason": "not_found",
        }
        mock_delete_topic.return_value = {
            "name": "delete_pubsub_topic",
            "success": True,
        }
        mock_delete_disk.return_value = {
            "name": "delete_assistant_disk",
            "success": True,
        }

        result = await assistant_infra.teardown_assistant_runtime(
            "42",
            desktop_mode="ubuntu",
        )

    assert result["success"] is True
    mock_stop_session.assert_awaited_once_with("42", deploy_env=None)


@pytest.mark.anyio
async def test_wait_for_runtime_cleanup_skips_when_runtime_status_reports_missing_comms():
    with patch(
        "orchestra.web.api.utils.assistant_infra._request_cleanup_step",
        new_callable=AsyncMock,
    ) as mock_request_step:
        mock_request_step.return_value = {
            "name": "runtime_status",
            "success": True,
            "skipped": True,
            "reason": "missing_comms_config",
        }

        result = await assistant_infra.wait_for_runtime_cleanup(
            "42",
            timeout=0,
            poll_interval=0,
        )

    assert result == {
        "name": "wait_for_runtime_cleanup",
        "success": True,
        "skipped": True,
        "reason": "missing_comms_config",
    }
    mock_request_step.assert_awaited_once()


@pytest.mark.anyio
async def test_teardown_assistant_runtime_skips_wait_when_stop_step_missing_comms():
    with patch(
        "orchestra.web.api.utils.assistant_infra.stop_assistant_session_runtime",
        new_callable=AsyncMock,
    ) as mock_stop_session, patch(
        "orchestra.web.api.utils.assistant_infra.wait_for_runtime_cleanup",
        new_callable=AsyncMock,
    ) as mock_wait_for_runtime_cleanup, patch(
        "orchestra.web.api.utils.assistant_infra.delete_assistant_session",
        new_callable=AsyncMock,
    ) as mock_delete_session, patch(
        "orchestra.web.api.utils.assistant_infra.delete_pubsub_topic",
        new_callable=AsyncMock,
    ) as mock_delete_topic, patch(
        "orchestra.web.api.utils.assistant_infra.delete_assistant_disk",
        new_callable=AsyncMock,
    ) as mock_delete_disk:
        mock_stop_session.return_value = {
            "name": "stop_assistant_session_runtime",
            "success": True,
            "skipped": True,
            "reason": "missing_comms_config",
        }
        mock_wait_for_runtime_cleanup.return_value = {
            "name": "wait_for_runtime_cleanup",
            "success": False,
            "reason": "should_not_be_called",
        }
        mock_delete_session.return_value = {
            "name": "delete_assistant_session",
            "success": False,
            "reason": "should_not_be_called",
        }
        mock_delete_topic.return_value = {
            "name": "delete_pubsub_topic",
            "success": True,
            "skipped": True,
            "reason": "missing_comms_config",
        }
        mock_delete_disk.return_value = {
            "name": "delete_assistant_disk",
            "success": True,
            "skipped": True,
            "reason": "missing_comms_config",
        }

        result = await assistant_infra.teardown_assistant_runtime(
            "42",
            desktop_mode="ubuntu",
        )

    assert result["success"] is True
    assert result["steps"]["wait_for_runtime_cleanup"] == {
        "name": "wait_for_runtime_cleanup",
        "success": True,
        "skipped": True,
        "reason": "missing_comms_config",
    }
    assert result["steps"]["delete_assistant_session"] == {
        "name": "delete_assistant_session",
        "success": True,
        "skipped": True,
        "reason": "missing_comms_config",
    }
    mock_wait_for_runtime_cleanup.assert_not_awaited()
    mock_delete_session.assert_not_awaited()
    mock_delete_topic.assert_awaited_once_with("42", deploy_env=None)
    mock_delete_disk.assert_awaited_once_with("42", deploy_env=None)


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


@pytest.mark.anyio
async def test_process_assistant_cleanup_tasks_with_task_ids_ignores_retry_backoff(
    dbsession,
):
    task = AssistantCleanupTask(
        assistant_id=43,
        deploy_env=None,
        desktop_mode="ubuntu",
        source_flow=CleanupSource.ASSISTANT_DELETE,
        cleanup_payload={"contacts": []},
        status="pending",
        next_retry_at=datetime.now(timezone.utc) + timedelta(minutes=5),
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
            "success": True,
            "assistant_id": "43",
            "steps": {},
            "errors": [],
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
    assert result["processed"] == 1
    assert result["completed"] == 1
    assert refreshed is not None
    assert refreshed.status == "completed"
    assert refreshed.next_retry_at is None


@pytest.mark.anyio
async def test_process_assistant_cleanup_tasks_without_task_ids_respects_retry_backoff(
    dbsession,
):
    task = AssistantCleanupTask(
        assistant_id=44,
        deploy_env=None,
        desktop_mode="ubuntu",
        source_flow=CleanupSource.ASSISTANT_DELETE,
        cleanup_payload={"contacts": []},
        status="pending",
        next_retry_at=datetime.now(timezone.utc) + timedelta(minutes=5),
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
        result = await process_assistant_cleanup_tasks(dbsession)

    dbsession.expire_all()
    refreshed = dbsession.get(AssistantCleanupTask, task.id)
    assert result["processed"] == 0
    assert refreshed is not None
    assert refreshed.status == "pending"
    mock_teardown.assert_not_called()
    mock_deprovision.assert_not_called()


@pytest.mark.anyio
async def test_teardown_assistant_runtime_runs_sessionless_fallback_when_stop_is_not_found():
    with patch(
        "orchestra.web.api.utils.assistant_infra.stop_assistant_session_runtime",
        new_callable=AsyncMock,
    ) as mock_stop_session, patch(
        "orchestra.web.api.utils.assistant_infra._cleanup_sessionless_runtime",
        new_callable=AsyncMock,
    ) as mock_sessionless_fallback, patch(
        "orchestra.web.api.utils.assistant_infra.wait_for_runtime_cleanup",
        new_callable=AsyncMock,
    ) as mock_wait_for_runtime_cleanup, patch(
        "orchestra.web.api.utils.assistant_infra.delete_assistant_session",
        new_callable=AsyncMock,
    ) as mock_delete_session, patch(
        "orchestra.web.api.utils.assistant_infra.delete_pubsub_topic",
        new_callable=AsyncMock,
    ) as mock_delete_topic, patch(
        "orchestra.web.api.utils.assistant_infra.delete_assistant_disk",
        new_callable=AsyncMock,
    ) as mock_delete_disk:
        mock_stop_session.return_value = {
            "name": "stop_assistant_session_runtime",
            "success": True,
            "response": {
                "success": True,
                "assistant_id": "42",
                "stopped": False,
                "reason": "not_found",
                "binding_id": None,
            },
        }
        mock_sessionless_fallback.return_value = {
            "name": "sessionless_runtime_fallback",
            "success": True,
            "response": {"errors": []},
        }
        mock_wait_for_runtime_cleanup.return_value = {
            "name": "wait_for_runtime_cleanup",
            "success": True,
            "response": {"runtime_cleanup_complete": True},
        }
        mock_delete_session.return_value = {
            "name": "delete_assistant_session",
            "success": True,
            "skipped": True,
            "reason": "not_found",
        }
        mock_delete_topic.return_value = {
            "name": "delete_pubsub_topic",
            "success": True,
        }
        mock_delete_disk.return_value = {
            "name": "delete_assistant_disk",
            "success": True,
        }

        result = await assistant_infra.teardown_assistant_runtime(
            "42",
            desktop_mode="ubuntu",
        )

    assert result["success"] is True
    mock_sessionless_fallback.assert_awaited_once_with("42", deploy_env=None)
    assert result["steps"]["sessionless_runtime_fallback"]["success"] is True


@pytest.mark.anyio
async def test_cleanup_sessionless_runtime_uses_binding_scoped_vm_release():
    runtime_status = {
        "assistant_session_exists": False,
        "active_job_names": ["unity-job-1"],
        "owned_vms": [{"binding_id": "binding-1", "vm_name": "vm-1"}],
        "other_owned_vms": [{"binding_id": "binding-2", "vm_name": "vm-2"}],
        "disk_vm_name": "vm-1",
    }
    with patch(
        "orchestra.web.api.utils.assistant_infra._request_cleanup_step",
        new_callable=AsyncMock,
    ) as mock_request_step, patch(
        "orchestra.web.api.utils.assistant_infra.stop_jobs",
        new_callable=AsyncMock,
    ) as mock_stop_jobs, patch(
        "orchestra.web.api.utils.assistant_infra.release_pool_vm",
        new_callable=AsyncMock,
    ) as mock_release_pool_vm:
        mock_request_step.return_value = {
            "name": "runtime_status",
            "success": True,
            "response": runtime_status,
        }
        mock_stop_jobs.return_value = {
            "success": True,
            "job_names": ["unity-job-1"],
            "steps": {},
            "errors": [],
        }
        mock_release_pool_vm.return_value = {
            "name": "release_pool_vm",
            "success": True,
            "response": {"released": True},
        }

        result = await assistant_infra._cleanup_sessionless_runtime("42")

    assert result["success"] is True
    mock_stop_jobs.assert_awaited_once_with("42", deploy_env=None)
    mock_release_pool_vm.assert_has_awaits(
        [
            call("42", "binding-1", vm_name="vm-1", deploy_env=None),
            call("42", "binding-2", vm_name="vm-2", deploy_env=None),
        ],
    )
    assert result["response"]["errors"] == []


def test_teardown_assistant_runtime_sync_runs_sessionless_fallback_when_stop_is_not_found():
    with patch.object(assistant_infra, "COMMS_URL", "https://comms.test"), patch.object(
        assistant_infra,
        "ADMIN_KEY",
        "test-key",
    ), patch(
        "orchestra.web.api.utils.assistant_infra._request_cleanup_step_sync",
    ) as mock_request_step, patch(
        "orchestra.web.api.utils.assistant_infra._wait_for_runtime_cleanup_sync",
    ) as mock_wait_for_runtime_cleanup, patch(
        "orchestra.web.api.utils.assistant_infra._cleanup_sessionless_runtime_sync",
    ) as mock_sessionless_fallback:

        def _step(*, name, **_kwargs):
            if name == "stop_assistant_session_runtime":
                return {
                    "name": name,
                    "success": True,
                    "response": {
                        "success": True,
                        "assistant_id": "42",
                        "stopped": False,
                        "reason": "not_found",
                        "binding_id": None,
                    },
                }
            if name == "delete_assistant_session":
                return {
                    "name": name,
                    "success": True,
                    "skipped": True,
                    "reason": "not_found",
                }
            return {"name": name, "success": True}

        mock_request_step.side_effect = _step
        mock_wait_for_runtime_cleanup.return_value = {
            "name": "wait_for_runtime_cleanup",
            "success": True,
            "response": {"runtime_cleanup_complete": True},
        }
        mock_sessionless_fallback.return_value = {
            "name": "sessionless_runtime_fallback",
            "success": True,
            "response": {"errors": []},
        }

        result = assistant_infra.teardown_assistant_runtime_sync(
            "42",
            desktop_mode="ubuntu",
        )

    assert result["success"] is True
    mock_sessionless_fallback.assert_called_once_with("42", deploy_env=None)
    assert result["steps"]["sessionless_runtime_fallback"]["success"] is True


@pytest.mark.anyio
async def test_process_assistant_cleanup_tasks_deletes_assistant_gcs_after_runtime_success(
    dbsession,
):
    task = AssistantCleanupTask(
        assistant_id=45,
        deploy_env=None,
        desktop_mode="ubuntu",
        source_flow=CleanupSource.ASSISTANT_DELETE,
        cleanup_payload={
            "profile_photo": "gs://assistant-media-production/45/image/photo.png",
            "profile_video": "gs://assistant-media-production/45/video/video.mp4",
            "contacts": [],
        },
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
    ) as mock_deprovision, patch(
        "orchestra.services.assistant_cleanup_service.BucketService",
    ) as mock_bucket_cls, patch(
        "orchestra.services.assistant_cleanup_service.settings",
    ) as mock_settings:
        mock_teardown.return_value = {
            "success": True,
            "assistant_id": "45",
            "steps": {},
            "errors": [],
        }
        mock_deprovision.return_value = {
            "success": True,
            "attempted": 0,
            "soft_deleted": 0,
            "errors": [],
        }
        mock_settings.is_staging = True
        mock_bucket = mock_bucket_cls.return_value
        mock_bucket.delete_all_assistant_data.return_value = {
            "media": 2,
            "recordings": 1,
            "attachments": 0,
        }

        result = await process_assistant_cleanup_tasks(dbsession, task_ids=[task.id])

    dbsession.expire_all()
    refreshed = dbsession.get(AssistantCleanupTask, task.id)
    assert result["completed"] == 1
    assert refreshed is not None
    assert refreshed.status == "completed"
    mock_bucket.delete_assistant_file.assert_any_call(
        "gs://assistant-media-production/45/image/photo.png",
    )
    mock_bucket.delete_assistant_file.assert_any_call(
        "gs://assistant-media-production/45/video/video.mp4",
    )
    mock_bucket.delete_all_assistant_data.assert_called_once_with(
        45,
        is_staging=True,
    )


@pytest.mark.anyio
async def test_process_assistant_cleanup_tasks_defers_gcs_until_runtime_is_clean(
    dbsession,
):
    task = AssistantCleanupTask(
        assistant_id=46,
        deploy_env=None,
        desktop_mode="ubuntu",
        source_flow=CleanupSource.ASSISTANT_DELETE,
        cleanup_payload={
            "profile_photo": "gs://assistant-media-production/46/image/photo.png",
            "contacts": [],
        },
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
    ) as mock_deprovision, patch(
        "orchestra.services.assistant_cleanup_service.BucketService",
    ) as mock_bucket_cls:
        mock_teardown.return_value = {
            "success": False,
            "assistant_id": "46",
            "steps": {
                "wait_for_runtime_cleanup": {
                    "success": False,
                    "reason": "runtime_cleanup_in_progress",
                },
            },
            "errors": ["wait_for_runtime_cleanup: runtime_cleanup_in_progress"],
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
    assert refreshed.last_result["storage"]["skipped"] is True
    mock_bucket_cls.assert_not_called()
