"""Onboarding render refresh after integration connection disconnect."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy.exc import OperationalError

from orchestra.db.models.integration_provider_models import IntegrationConnection
from orchestra.services import coordinator_service as svc
from orchestra.web.api.integrations.operations import disconnect_connection


def test_disconnect_connection_notifies_onboarding_render_when_progress_changes() -> (
    None
):
    session = MagicMock()
    conn = IntegrationConnection(
        connection_id="conn-disconnect-1",
        owner_scope="assistant",
        assistant_id=42,
        canonical_app_slug="clickup",
        backend_id="composio",
        provider_app_id="CLICKUP",
        status="connected",
        credential_storage="provider_vault",
    )
    coordinator = SimpleNamespace(
        agent_id=42,
        user_id="user-1",
        is_coordinator=True,
    )
    session.get.return_value = coordinator

    with (
        patch(
            "orchestra.web.api.integrations.operations.IntegrationProviderDAO",
        ) as dao_cls,
        patch.object(
            svc,
            "onboarding_baseline_for_integration_connect",
            return_value=["apps", "email-reference"],
        ) as baseline,
        patch.object(
            svc,
            "notify_coordinator_onboarding_after_integration_disconnected",
        ) as notify,
    ):
        dao = dao_cls.return_value
        dao.get_connection.return_value = conn
        disconnect_connection(session, connection_id="conn-disconnect-1")

    baseline.assert_called_once_with(session, assistant_id=42)
    notify.assert_called_once()
    assert notify.call_args.kwargs["canonical_app_slug"] == "clickup"


def test_disconnect_connection_persists_when_onboarding_baseline_lock_times_out() -> (
    None
):
    session = MagicMock()
    conn = IntegrationConnection(
        connection_id="conn-disconnect-lock",
        owner_scope="assistant",
        assistant_id=42,
        canonical_app_slug="clickup",
        backend_id="composio",
        provider_app_id="CLICKUP",
        status="connected",
        credential_storage="provider_vault",
    )
    lock_err = OperationalError(
        "SELECT",
        {},
        Exception("canceling statement due to lock timeout"),
    )

    with (
        patch(
            "orchestra.web.api.integrations.operations.IntegrationProviderDAO",
        ) as dao_cls,
        patch.object(
            svc,
            "onboarding_baseline_for_integration_connect",
            side_effect=lock_err,
        ),
        patch.object(
            svc,
            "notify_coordinator_onboarding_after_integration_disconnected",
        ) as notify,
    ):
        dao = dao_cls.return_value
        dao.get_connection.return_value = conn
        response = disconnect_connection(
            session,
            connection_id="conn-disconnect-lock",
        )

    assert response.connection_id == "conn-disconnect-lock"
    session.rollback.assert_called()
    dao.update_connection_fields.assert_called_once()
    session.commit.assert_called()
    notify.assert_called_once()
    assert notify.call_args.kwargs["baseline_completed_step_ids"] is None
