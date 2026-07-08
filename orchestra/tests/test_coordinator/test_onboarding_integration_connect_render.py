"""Onboarding render refresh after integration connection completion."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestra.db.models.integration_provider_models import IntegrationConnection
from orchestra.services import coordinator_service as svc
from orchestra.web.api.integrations.operations import complete_connection


def test_complete_connection_notifies_onboarding_render_when_progress_changes() -> None:
    session = MagicMock()
    conn = IntegrationConnection(
        connection_id="conn-notify-1",
        owner_scope="assistant",
        assistant_id=42,
        canonical_app_slug="clickup",
        backend_id="composio",
        provider_app_id="CLICKUP",
        status="pending",
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
            return_value=[],
        ) as baseline,
        patch.object(
            svc,
            "notify_coordinator_onboarding_after_integration_connected",
        ) as notify,
    ):
        dao = dao_cls.return_value
        dao.get_connection.return_value = conn
        complete_connection(
            session,
            connection_id="conn-notify-1",
            provider_connection_id="provider-1",
            granted_scopes=["read"],
            external_account_label="acct",
            status="connected",
        )

    baseline.assert_called_once_with(session, assistant_id=42)
    notify.assert_called_once()
    assert notify.call_args.kwargs["canonical_app_slug"] == "clickup"
