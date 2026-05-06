"""Tests for GCS cleanup behavior during organization deletion.

Assistant-scoped GCS cleanup is owned by the durable cleanup task path, while
organization account photos are still cleaned up directly in the request flow.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.models.orchestra_models import AssistantCleanupTask, AssistantContact
from orchestra.tests.utils import create_test_user


@pytest.fixture(autouse=True)
def mock_infra_and_bucket(request):
    """Mock assistant infrastructure webhooks and BucketService for all tests."""
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
    ) as mock_assistant_cleanup, patch(
        "orchestra.web.api.assistant.views.settings",
    ) as mock_settings, patch(
        "orchestra.web.api.organization.views.process_assistant_cleanup_tasks",
        new_callable=AsyncMock,
    ) as mock_org_cleanup, patch(
        "orchestra.web.api.organization.views.BucketService",
    ) as mock_bucket_cls:
        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})
        mock_assistant_cleanup.return_value = {
            "processed": 1,
            "completed": 1,
            "retried": 0,
            "failed": 0,
            "errors": [],
        }
        mock_org_cleanup.return_value = {
            "processed": 1,
            "completed": 1,
            "retried": 0,
            "failed": 0,
            "errors": [],
        }
        mock_settings.is_staging = True

        mock_bucket_instance = MagicMock()
        mock_bucket_instance.delete_all_assistant_data.return_value = {
            "media_files": 0,
            "call_recordings": 0,
            "message_attachments": 0,
        }
        mock_bucket_instance.delete_org_account_photos.return_value = 0
        mock_bucket_cls.return_value = mock_bucket_instance

        yield {
            "bucket_cls": mock_bucket_cls,
            "bucket_instance": mock_bucket_instance,
            "runtime_teardown": mock_org_cleanup,
        }


@pytest.mark.anyio
async def test_org_deletion_cleans_gcs_for_all_assistants(
    client: AsyncClient,
    dbsession,
    mock_infra_and_bucket,
):
    """Deleting an org leaves assistant GCS cleanup to the durable task path."""
    mock_bucket = mock_infra_and_bucket["bucket_instance"]
    mock_runtime_cleanup = mock_infra_and_bucket["runtime_teardown"]

    owner = await create_test_user(client, "org_del_gcs_owner@test.com")

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "OrgDel GCS Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Create several assistants in this org
    assistant_dao = AssistantDAO(dbsession)
    for i in range(3):
        assistant = assistant_dao.create_assistant(
            user_id=owner["id"],
            first_name=f"Bot{i}",
            surname="OrgDel",
            age=None,
            nationality=None,
            about=None,
            weekly_limit=None,
            max_parallel=None,
            organization_id=org_id,
        )
        dbsession.flush()
    dbsession.commit()

    # Delete the organization
    del_resp = await client.delete(
        f"/v0/organizations/{org_id}",
        headers=owner["headers"],
    )
    assert del_resp.status_code == status.HTTP_204_NO_CONTENT

    mock_runtime_cleanup.assert_awaited_once()
    mock_bucket.delete_all_assistant_data.assert_not_called()

    # Verify account photos were also cleaned up
    mock_bucket.delete_org_account_photos.assert_called_once_with(org_id)


@pytest.mark.anyio
async def test_org_deletion_deprovisions_contacts_and_persists_cleanup_tasks(
    client: AsyncClient,
    dbsession,
    mock_infra_and_bucket,
):
    owner = await create_test_user(client, "org_del_contacts_owner@test.com")

    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Org Contact Cleanup"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    assistant_dao = AssistantDAO(dbsession)
    assistant = assistant_dao.create_assistant(
        user_id=owner["id"],
        first_name="Cleanup",
        surname="OrgBot",
        age=None,
        nationality=None,
        about=None,
        weekly_limit=None,
        max_parallel=None,
        organization_id=org_id,
    )
    dbsession.flush()
    agent_id = assistant.agent_id

    dbsession.add_all(
        [
            AssistantContact(
                assistant_id=agent_id,
                contact_type="phone",
                contact_value="+15553334444",
            ),
            AssistantContact(
                assistant_id=agent_id,
                contact_type="email",
                contact_value="org-cleanup@assistant.unify.ai",
                provisioned_by="user",
            ),
            AssistantContact(
                assistant_id=agent_id,
                contact_type="whatsapp",
                contact_value="+15554445555",
            ),
        ],
    )
    dbsession.commit()

    with patch(
        "orchestra.services.assistant_cleanup_service.delete_phone_number",
        new_callable=AsyncMock,
    ) as mock_delete_phone, patch(
        "orchestra.db.dao.shared_pool_dao.SharedPoolDAO.delete_routes_for_assistant",
        return_value=1,
    ) as mock_delete_routes:
        del_resp = await client.delete(
            f"/v0/organizations/{org_id}",
            headers=owner["headers"],
        )

    assert del_resp.status_code == status.HTTP_204_NO_CONTENT
    mock_delete_phone.assert_awaited_once_with("+15553334444", deploy_env=None)
    mock_delete_routes.assert_called_once_with(agent_id)

    dbsession.expire_all()
    cleanup_task = (
        dbsession.query(AssistantCleanupTask)
        .filter(AssistantCleanupTask.assistant_id == agent_id)
        .one()
    )
    assert cleanup_task.status == "pending"


@pytest.mark.anyio
async def test_org_deletion_no_assistant_gcs_calls_when_no_assistants(
    client: AsyncClient,
    dbsession,
    mock_infra_and_bucket,
):
    """Deleting an org with no assistants should not call delete_all_assistant_data,
    but should still clean up account photos."""
    mock_bucket = mock_infra_and_bucket["bucket_instance"]

    owner = await create_test_user(client, "org_del_empty_owner@test.com")

    # Create organization (no assistants)
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Empty Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Delete the organization
    del_resp = await client.delete(
        f"/v0/organizations/{org_id}",
        headers=owner["headers"],
    )
    assert del_resp.status_code == status.HTTP_204_NO_CONTENT

    # No assistant data cleanup calls
    mock_bucket.delete_all_assistant_data.assert_not_called()

    # Account photos should still be cleaned up
    mock_bucket.delete_org_account_photos.assert_called_once_with(org_id)


@pytest.mark.anyio
async def test_org_deletion_gcs_failure_does_not_block(
    client: AsyncClient,
    dbsession,
    mock_infra_and_bucket,
):
    """Org account photo cleanup failure does not prevent org deletion."""
    mock_bucket = mock_infra_and_bucket["bucket_instance"]
    mock_bucket.delete_org_account_photos.side_effect = Exception("GCS unreachable")

    owner = await create_test_user(client, "org_del_fail_owner@test.com")

    # Create organization with an assistant
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "GCS Fail Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    assistant_dao = AssistantDAO(dbsession)
    assistant = assistant_dao.create_assistant(
        user_id=owner["id"],
        first_name="FailBot",
        surname="Test",
        age=None,
        nationality=None,
        about=None,
        weekly_limit=None,
        max_parallel=None,
        organization_id=org_id,
    )
    dbsession.commit()

    # Delete org - should succeed even though account photo cleanup fails
    del_resp = await client.delete(
        f"/v0/organizations/{org_id}",
        headers=owner["headers"],
    )
    assert del_resp.status_code == status.HTTP_204_NO_CONTENT

    # Verify org is actually deleted
    get_resp = await client.get(
        f"/v0/organizations/{org_id}",
        headers=owner["headers"],
    )
    assert get_resp.status_code == status.HTTP_404_NOT_FOUND
