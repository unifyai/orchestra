"""Onboarding render refresh after integration connection completion."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestra.db.models.integration_provider_models import IntegrationConnection
from orchestra.services import coordinator_service as svc
from orchestra.web.api.integrations.operations import (
    OwnerContext,
    complete_connection,
    start_connection,
)


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


def test_notify_after_integration_connected_emits_when_apps_newly_complete() -> None:
    session = MagicMock()
    coordinator = SimpleNamespace(
        agent_id=42,
        user_id="user-1",
        is_coordinator=True,
        first_name="T-W1N",
        surname=None,
    )
    session.get.return_value = coordinator

    with (
        patch.object(
            svc,
            "onboarding_baseline_for_integration_connect",
            return_value=["email-reference"],
        ),
        patch.object(
            svc,
            "notify_onboarding_render_if_changed_sync",
            return_value=True,
        ) as notify_render,
    ):
        svc.notify_coordinator_onboarding_after_integration_connected(
            session,
            assistant_id=42,
            canonical_app_slug="clickup",
            baseline_completed_step_ids=["email-reference"],
        )

    notify_render.assert_called_once()
    assert notify_render.call_args.kwargs["reason"] == "integration_connected"


def _mock_start_connection_dao(
    dao_cls: MagicMock,
    *,
    connection: IntegrationConnection,
) -> MagicMock:
    dao = dao_cls.return_value
    dao.get_backend.return_value = SimpleNamespace(status="enabled")
    dao.create_connection.return_value = connection
    return dao


def test_start_connection_notifies_onboarding_render_when_api_key_connects_immediately() -> (
    None
):
    session = MagicMock()
    conn = IntegrationConnection(
        connection_id="conn-api-key-1",
        owner_scope="assistant",
        assistant_id=42,
        canonical_app_slug="custom-api",
        backend_id="composio",
        provider_app_id="CUSTOM_API",
        status="connected",
        credential_storage="secret_manager",
    )
    owner = OwnerContext(owner_scope="assistant", assistant_id=42)

    with (
        patch(
            "orchestra.web.api.integrations.operations.seed_default_provider_catalog",
        ),
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
        _mock_start_connection_dao(dao_cls, connection=conn)
        start_connection(
            session,
            owner=owner,
            canonical_app_slug="custom-api",
            backend_id="composio",
            provider_app_id="CUSTOM_API",
            requested_scopes=[],
            auth_mode="api_key",
            api_key_fields={"token": "secret"},
            created_by="user-1",
            redirect_url=None,
        )

    baseline.assert_called_once_with(session, assistant_id=42)
    notify.assert_called_once()
    assert notify.call_args.kwargs["canonical_app_slug"] == "custom-api"


def test_start_connection_does_not_notify_when_oauth_pending() -> None:
    session = MagicMock()
    conn = IntegrationConnection(
        connection_id="conn-oauth-pending",
        owner_scope="assistant",
        assistant_id=42,
        canonical_app_slug="clickup",
        backend_id="composio",
        provider_app_id="CLICKUP",
        status="pending",
        credential_storage="provider_vault",
    )
    owner = OwnerContext(owner_scope="assistant", assistant_id=42)

    with (
        patch(
            "orchestra.web.api.integrations.operations.seed_default_provider_catalog",
        ),
        patch(
            "orchestra.web.api.integrations.operations.IntegrationProviderDAO",
        ) as dao_cls,
        patch(
            "orchestra.web.api.integrations.operations._provider_connect_url",
            return_value="https://oauth.example/connect",
        ),
        patch.object(
            svc,
            "onboarding_baseline_for_integration_connect",
        ) as baseline,
        patch.object(
            svc,
            "notify_coordinator_onboarding_after_integration_connected",
        ) as notify,
    ):
        _mock_start_connection_dao(dao_cls, connection=conn)
        start_connection(
            session,
            owner=owner,
            canonical_app_slug="clickup",
            backend_id="composio",
            provider_app_id="CLICKUP",
            requested_scopes=[],
            auth_mode="oauth",
            api_key_fields={},
            created_by="user-1",
            redirect_url="https://console.example/callback",
        )

    baseline.assert_not_called()
    notify.assert_not_called()


def test_start_connection_does_not_notify_when_api_key_fields_empty() -> None:
    session = MagicMock()
    conn = IntegrationConnection(
        connection_id="conn-api-key-pending",
        owner_scope="assistant",
        assistant_id=42,
        canonical_app_slug="custom-api",
        backend_id="composio",
        provider_app_id="CUSTOM_API",
        status="pending",
        credential_storage="secret_manager",
    )
    owner = OwnerContext(owner_scope="assistant", assistant_id=42)

    with (
        patch(
            "orchestra.web.api.integrations.operations.seed_default_provider_catalog",
        ),
        patch(
            "orchestra.web.api.integrations.operations.IntegrationProviderDAO",
        ) as dao_cls,
        patch.object(
            svc,
            "onboarding_baseline_for_integration_connect",
        ) as baseline,
        patch.object(
            svc,
            "notify_coordinator_onboarding_after_integration_connected",
        ) as notify,
    ):
        _mock_start_connection_dao(dao_cls, connection=conn)
        start_connection(
            session,
            owner=owner,
            canonical_app_slug="custom-api",
            backend_id="composio",
            provider_app_id="CUSTOM_API",
            requested_scopes=[],
            auth_mode="api_key",
            api_key_fields={},
            created_by="user-1",
            redirect_url=None,
        )

    baseline.assert_not_called()
    notify.assert_not_called()
