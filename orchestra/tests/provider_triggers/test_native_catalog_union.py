"""Workspace facade and native catalog union tests."""

from __future__ import annotations

import os
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_secret_dao import AssistantSecretDAO
from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.models.core_models import Project
from orchestra.db.models.integration_provider_models import IntegrationConnection
from orchestra.db.models.orchestra_models import Assistant
from orchestra.provider_triggers.backend_ids import (
    ASSISTANT_WORKSPACE_SECRETS_STORAGE,
    COMPOSIO_BACKEND_ID,
    NATIVE_GOOGLE_BACKEND_ID,
    NATIVE_GOOGLE_CHAT_APP_SLUG,
    NATIVE_GOOGLE_DRIVE_APP_SLUG,
    NATIVE_GOOGLE_MEET_APP_SLUG,
    NATIVE_GOOGLE_MEET_TRANSCRIPT_SLUG,
    NATIVE_MICROSOFT_BACKEND_ID,
)
from orchestra.provider_triggers.catalog_import.native_manifest import (
    load_native_catalog_entries,
)
from orchestra.provider_triggers.ingress_rate_limit import (
    reset_ingress_rate_limiter_for_tests,
)
from orchestra.provider_triggers.task_trigger import ProviderEventTrigger
from orchestra.provider_triggers.workspace_connection_facade import (
    ensure_workspace_trigger_connections,
)
from orchestra.services.staged_trigger_catalog_service import (
    list_staged_triggers_for_assistant,
    validate_provider_event_trigger_for_assistant,
)
from orchestra.services.task_machine_state_service import TASK_MACHINE_PROJECT_NAME
from orchestra.services.trigger_catalog_import_service import (
    TriggerCatalogImportService,
)
from orchestra.tests.provider_triggers.conftest import (
    stub_healthy_provider_trigger_topology,
)
from orchestra.tests.provider_triggers.native_delivery import (
    build_native_meet_transcript_payload,
    deliver_signed_native_webhook,
)

WEBHOOK_SECRET = "native-google-ingress-test-secret"
PRIMARY_USER_ID = str(os.getenv("AUTH_ACCOUNT_USER_ID"))

_GOOGLE_CHAT_SCOPES = (
    "https://www.googleapis.com/auth/chat.messages.readonly "
    "https://www.googleapis.com/auth/chat.memberships.readonly "
    "https://www.googleapis.com/auth/chat.spaces.readonly "
    "https://www.googleapis.com/auth/chat.users.readstate.readonly "
    "https://www.googleapis.com/auth/chat.users.availability.readonly"
)
_GOOGLE_WORKSPACE_EVENT_SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly "
    "https://www.googleapis.com/auth/meetings.space.readonly "
    f"{_GOOGLE_CHAT_SCOPES}"
)


@pytest.fixture(autouse=True)
def _native_catalog_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_healthy_provider_trigger_topology(monkeypatch)
    monkeypatch.setenv("PROVIDER_TRIGGER_CATALOG_ENVIRONMENT", "selfhost")
    monkeypatch.setenv("NATIVE_GOOGLE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    reset_ingress_rate_limiter_for_tests()
    # Facade app gate set is cached at first call; clear so scope-constant
    # edits in this module always take effect under pytest reloads.
    from orchestra.provider_triggers.workspace_connection_facade import (
        _workspace_facade_apps,
    )

    _workspace_facade_apps.cache_clear()


def _seed_workspace_google_assistant(
    dbsession: Session,
    *,
    email: str = "meet.user@example.com",
    granted_scopes: str = _GOOGLE_WORKSPACE_EVENT_SCOPES,
) -> tuple[Assistant, IntegrationConnection]:
    assistant = Assistant(user_id=PRIMARY_USER_ID, first_name="Meet", surname="User")
    dbsession.add(assistant)
    dbsession.flush()

    secret_dao = AssistantSecretDAO(dbsession)
    secret_dao.upsert(
        PRIMARY_USER_ID,
        assistant.agent_id,
        "GOOGLE_GRANTED_SCOPES",
        granted_scopes,
    )
    secret_dao.upsert(
        PRIMARY_USER_ID,
        assistant.agent_id,
        "GOOGLE_ACCOUNT_EMAIL",
        email,
    )
    secret_dao.upsert(
        PRIMARY_USER_ID,
        assistant.agent_id,
        "GOOGLE_ACCESS_TOKEN",
        "test-access-token",
    )
    dbsession.flush()

    connections = ensure_workspace_trigger_connections(
        dbsession,
        assistant_id=assistant.agent_id,
    )
    connected = [c for c in connections if c.status == "connected"]
    assert len(connected) == 3
    connection = next(
        row
        for row in connections
        if row.canonical_app_slug == NATIVE_GOOGLE_MEET_APP_SLUG
    )
    assert connection.backend_id == NATIVE_GOOGLE_BACKEND_ID
    assert connection.canonical_app_slug == NATIVE_GOOGLE_MEET_APP_SLUG
    assert connection.credential_storage == ASSISTANT_WORKSPACE_SECRETS_STORAGE
    return assistant, connection


def test_meet_facade_hidden_without_meet_scopes(dbsession: Session) -> None:
    """Gmail/drive-only Google connect must not expose the google_meet facade."""
    assistant = Assistant(user_id=PRIMARY_USER_ID, first_name="NoMeet", surname="User")
    dbsession.add(assistant)
    dbsession.flush()

    secret_dao = AssistantSecretDAO(dbsession)
    secret_dao.upsert(
        PRIMARY_USER_ID,
        assistant.agent_id,
        "GOOGLE_GRANTED_SCOPES",
        "https://www.googleapis.com/auth/drive.readonly",
    )
    secret_dao.upsert(
        PRIMARY_USER_ID,
        assistant.agent_id,
        "GOOGLE_ACCOUNT_EMAIL",
        "nomeet.user@example.com",
    )
    dbsession.flush()

    connections = ensure_workspace_trigger_connections(
        dbsession,
        assistant_id=assistant.agent_id,
    )
    meet = next(
        (c for c in connections if c.canonical_app_slug == NATIVE_GOOGLE_MEET_APP_SLUG),
        None,
    )
    assert meet is None or meet.status == "disconnected"
    connected_slugs = {
        c.canonical_app_slug for c in connections if c.status == "connected"
    }
    assert NATIVE_GOOGLE_MEET_APP_SLUG not in connected_slugs


def test_meet_facade_disconnects_when_meet_scope_revoked(dbsession: Session) -> None:
    """Dropping the Meet scope on a re-consent flips the facade to disconnected."""
    assistant, connection = _seed_workspace_google_assistant(
        dbsession,
        email="revoke.user@example.com",
    )
    assert connection.status == "connected"

    secret_dao = AssistantSecretDAO(dbsession)
    secret_dao.upsert(
        PRIMARY_USER_ID,
        assistant.agent_id,
        "GOOGLE_GRANTED_SCOPES",
        "https://www.googleapis.com/auth/drive.readonly",
    )
    dbsession.flush()

    ensure_workspace_trigger_connections(dbsession, assistant_id=assistant.agent_id)
    dbsession.refresh(connection)
    assert connection.status == "disconnected"


def test_drive_facade_connects_with_readonly_drive_event_scope(
    dbsession: Session,
) -> None:
    """Least-privilege drive.readonly unlocks google_drive; dropping it disconnects."""
    assistant = Assistant(user_id=PRIMARY_USER_ID, first_name="Drive", surname="Gate")
    dbsession.add(assistant)
    dbsession.flush()

    secret_dao = AssistantSecretDAO(dbsession)
    secret_dao.upsert(
        PRIMARY_USER_ID,
        assistant.agent_id,
        "GOOGLE_GRANTED_SCOPES",
        "https://www.googleapis.com/auth/drive.readonly",
    )
    secret_dao.upsert(
        PRIMARY_USER_ID,
        assistant.agent_id,
        "GOOGLE_ACCOUNT_EMAIL",
        "drive.gate@example.com",
    )
    dbsession.flush()

    connections = ensure_workspace_trigger_connections(
        dbsession,
        assistant_id=assistant.agent_id,
    )
    drive = next(
        c for c in connections if c.canonical_app_slug == NATIVE_GOOGLE_DRIVE_APP_SLUG
    )
    assert drive.status == "connected"

    secret_dao.upsert(
        PRIMARY_USER_ID,
        assistant.agent_id,
        "GOOGLE_GRANTED_SCOPES",
        "https://www.googleapis.com/auth/meetings.space.readonly",
    )
    dbsession.flush()
    ensure_workspace_trigger_connections(dbsession, assistant_id=assistant.agent_id)
    dbsession.refresh(drive)
    assert drive.status == "disconnected"


def test_chat_facade_requires_full_chat_event_scope_set(dbsession: Session) -> None:
    """google_chat connects only with the full Chat Workspace Events scope set."""
    assistant = Assistant(user_id=PRIMARY_USER_ID, first_name="Chat", surname="Gate")
    dbsession.add(assistant)
    dbsession.flush()

    secret_dao = AssistantSecretDAO(dbsession)
    secret_dao.upsert(
        PRIMARY_USER_ID,
        assistant.agent_id,
        "GOOGLE_ACCOUNT_EMAIL",
        "chat.gate@example.com",
    )
    secret_dao.upsert(
        PRIMARY_USER_ID,
        assistant.agent_id,
        "GOOGLE_GRANTED_SCOPES",
        _GOOGLE_CHAT_SCOPES,
    )
    dbsession.flush()

    connections = ensure_workspace_trigger_connections(
        dbsession,
        assistant_id=assistant.agent_id,
    )
    chat = next(
        c for c in connections if c.canonical_app_slug == NATIVE_GOOGLE_CHAT_APP_SLUG
    )
    assert chat.status == "connected"

    secret_dao.upsert(
        PRIMARY_USER_ID,
        assistant.agent_id,
        "GOOGLE_GRANTED_SCOPES",
        (
            "https://www.googleapis.com/auth/chat.messages.readonly "
            "https://www.googleapis.com/auth/chat.spaces.readonly"
        ),
    )
    dbsession.flush()
    ensure_workspace_trigger_connections(dbsession, assistant_id=assistant.agent_id)
    dbsession.refresh(chat)
    assert chat.status == "disconnected"


def test_native_catalog_import_is_idempotent(dbsession: Session) -> None:
    service = TriggerCatalogImportService(dbsession)
    expected_counts = {
        NATIVE_GOOGLE_BACKEND_ID: len(
            load_native_catalog_entries(NATIVE_GOOGLE_BACKEND_ID)[1],
        ),
        NATIVE_MICROSOFT_BACKEND_ID: len(
            load_native_catalog_entries(NATIVE_MICROSOFT_BACKEND_ID)[1],
        ),
    }
    for backend_id in (NATIVE_GOOGLE_BACKEND_ID, NATIVE_MICROSOFT_BACKEND_ID):
        first = service.import_catalog(backend_id=backend_id, environment="selfhost")
        second = service.import_catalog(backend_id=backend_id, environment="selfhost")
        assert first.skipped is False
        assert second.skipped is True
        assert first.entry_count == expected_counts[backend_id]


def test_catalog_union_shows_native_meet_only_for_workspace_google(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    service = TriggerCatalogImportService(dbsession)
    service.import_catalog(backend_id=COMPOSIO_BACKEND_ID, environment="selfhost")
    service.import_catalog(backend_id=NATIVE_GOOGLE_BACKEND_ID, environment="selfhost")
    service.import_catalog(
        backend_id=NATIVE_MICROSOFT_BACKEND_ID,
        environment="selfhost",
    )
    dbsession.commit()

    assistant, _connection = _seed_workspace_google_assistant(dbsession)
    dbsession.add(
        IntegrationConnection(
            connection_id=f"conn-{uuid.uuid4().hex[:10]}",
            owner_scope="assistant",
            assistant_id=assistant.agent_id,
            canonical_app_slug="github",
            backend_id=COMPOSIO_BACKEND_ID,
            provider_app_id="GITHUB",
            provider_connection_id="ca_github_only",
            provider_user_id="github-user",
            status="connected",
            credential_storage="provider_vault",
        ),
    )
    dbsession.commit()

    catalog = list_staged_triggers_for_assistant(
        dbsession,
        assistant_id=assistant.agent_id,
    )
    slugs = {
        (row["backend_id"], row["canonical_app_slug"], row["provider_trigger_slug"])
        for row in catalog["triggers"]
    }
    assert (
        NATIVE_GOOGLE_BACKEND_ID,
        NATIVE_GOOGLE_MEET_APP_SLUG,
        NATIVE_GOOGLE_MEET_TRANSCRIPT_SLUG,
    ) in slugs
    assert not any(
        row["backend_id"] == NATIVE_MICROSOFT_BACKEND_ID for row in catalog["triggers"]
    )
    assert not any(
        row["backend_id"] == COMPOSIO_BACKEND_ID
        and row["canonical_app_slug"] == "gmail"
        for row in catalog["triggers"]
    )


def test_validate_provider_event_trigger_accepts_workspace_facade_connection(
    dbsession: Session,
) -> None:
    service = TriggerCatalogImportService(dbsession)
    service.import_catalog(backend_id=NATIVE_GOOGLE_BACKEND_ID, environment="selfhost")
    dbsession.commit()

    assistant, connection = _seed_workspace_google_assistant(dbsession)
    trigger = ProviderEventTrigger(
        state="enabled",
        connection_id=connection.connection_id,
        backend_id=NATIVE_GOOGLE_BACKEND_ID,
        canonical_app_slug=NATIVE_GOOGLE_MEET_APP_SLUG,
        provider_trigger_slug=NATIVE_GOOGLE_MEET_TRANSCRIPT_SLUG,
        trigger_config={},
    )
    validate_provider_event_trigger_for_assistant(
        dbsession,
        assistant_id=assistant.agent_id,
        trigger=trigger,
    )


def _seed_native_meet_ingress_binding(
    dbsession: Session,
    *,
    assistant: Assistant,
    connection: IntegrationConnection,
) -> tuple[str, str]:
    project = (
        dbsession.query(Project)
        .filter(
            Project.user_id == PRIMARY_USER_ID,
            Project.organization_id.is_(None),
            Project.name == TASK_MACHINE_PROJECT_NAME,
        )
        .one_or_none()
    )
    if project is None:
        project = Project(name=TASK_MACHINE_PROJECT_NAME, user_id=PRIMARY_USER_ID)
        dbsession.add(project)
        dbsession.flush()

    task_id = int(uuid.uuid4().int % 1_000_000) + 1
    binding_id = f"binding-{uuid.uuid4().hex[:12]}"
    dao = ProviderTriggerDAO(dbsession)
    trigger = ProviderEventTrigger(
        state="enabled",
        connection_id=connection.connection_id,
        backend_id=NATIVE_GOOGLE_BACKEND_ID,
        canonical_app_slug=NATIVE_GOOGLE_MEET_APP_SLUG,
        provider_trigger_slug=NATIVE_GOOGLE_MEET_TRANSCRIPT_SLUG,
        trigger_config={},
    )
    binding = dao.create_binding(
        binding_id=binding_id,
        project_id=project.id,
        tasks_context_id=0,
        source_task_log_id=task_id,
        task_id=task_id,
        assistant_id=assistant.agent_id,
        task_revision=1,
        trigger=trigger,
        execution_mode="live",
        entrypoint=None,
    )
    generation = dao.create_generation(binding=binding)
    generation.external_trigger_id = "ng_test_subscription"
    generation.signing_secret_ref = "env:NATIVE_GOOGLE_WEBHOOK_SECRET"
    generation.signing_secret_version = "project"
    dao.promote_generation(binding=binding, generation=generation)
    dbsession.flush()
    return generation.ingress_key, binding.binding_id


@pytest.mark.anyio
async def test_native_meet_transcript_delivery_accepts_once(
    dbsession: Session,
    client: AsyncClient,
) -> None:
    service = TriggerCatalogImportService(dbsession)
    service.import_catalog(backend_id=NATIVE_GOOGLE_BACKEND_ID, environment="selfhost")
    dbsession.commit()

    assistant, connection = _seed_workspace_google_assistant(dbsession)
    ingress_key, _binding_id = _seed_native_meet_ingress_binding(
        dbsession,
        assistant=assistant,
        connection=connection,
    )

    payload = build_native_meet_transcript_payload(
        event_id="meet-transcript-event-1",
        external_trigger_id="ng_test_subscription",
        connected_account_id=connection.provider_connection_id,
        provider_user_id=connection.provider_user_id,
    )
    first = await deliver_signed_native_webhook(
        client,
        backend_id=NATIVE_GOOGLE_BACKEND_ID,
        ingress_key=ingress_key,
        payload=payload,
        signing_secret=WEBHOOK_SECRET,
        webhook_id="native-msg-1",
    )
    second = await deliver_signed_native_webhook(
        client,
        backend_id=NATIVE_GOOGLE_BACKEND_ID,
        ingress_key=ingress_key,
        payload=payload,
        signing_secret=WEBHOOK_SECRET,
        webhook_id="native-msg-2",
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    first_body = first.json()
    second_body = second.json()
    assert first_body["status"] == "accepted"
    assert second_body["receipt_id"] == first_body["receipt_id"]
