"""Ticket 25 — native honest capability bar (config schemas + fail-closed).

Exercises the three fail-closed chokepoints (typed create/enable validation,
reconciliation prerequisites, and adapters that raise instead of stubbing) plus
the capability matrix surfaced on the staged catalog.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import pytest
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_secret_dao import AssistantSecretDAO
from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.models.orchestra_models import Assistant
from orchestra.db.models.provider_trigger_models import EventTriggerBinding
from orchestra.provider_triggers.backend_ids import (
    NATIVE_GOOGLE_BACKEND_ID,
    NATIVE_GOOGLE_CHAT_APP_SLUG,
    NATIVE_GOOGLE_DRIVE_APP_SLUG,
    NATIVE_GOOGLE_MEET_APP_SLUG,
    NATIVE_MICROSOFT_BACKEND_ID,
    NATIVE_MICROSOFT_DIRECTORY_APP_SLUG,
    NATIVE_MICROSOFT_OUTLOOK_APP_SLUG,
)
from orchestra.provider_triggers.runtime_types import (
    BindingRuntimeHealth,
    ReconcileErrorCode,
)
from orchestra.provider_triggers.task_trigger import ProviderEventTrigger
from orchestra.provider_triggers.trigger_adapter import (
    NormalizedProviderDelivery,
    ProviderAccountIdentity,
    TriggerDeleteRequest,
    TriggerHealthResult,
    TriggerProviderAdapter,
    TriggerProvisionRequest,
    TriggerProvisionResult,
)
from orchestra.provider_triggers.workspace_connection_facade import (
    ensure_workspace_trigger_connections,
)
from orchestra.services.provider_trigger_reconciliation_service import (
    ProviderTriggerReconciliationService,
)
from orchestra.services.staged_trigger_catalog_service import (
    list_staged_triggers_for_assistant,
    missing_required_config,
    resolve_trigger_capability,
    validate_provider_event_trigger_for_assistant,
)
from orchestra.services.trigger_catalog_import_service import (
    TriggerCatalogImportService,
)
from orchestra.tests.provider_triggers.conftest import (
    stub_healthy_provider_trigger_topology,
)

PRIMARY_USER_ID = str(os.getenv("AUTH_ACCOUNT_USER_ID"))

MEET_SLUG = "google.workspace.meet.transcript.v2.fileGenerated"
DRIVE_FILE_CREATED_SLUG = "google.workspace.drive.file.v3.created"
CHAT_BATCH_SLUG = "google.workspace.chat.message.v1.batchCreated"
MS_APP_ONLY_SLUG = "microsoft.graph.user.updated"
MS_DELEGATED_SLUG = "microsoft.graph.mailMessage.created"


@pytest.fixture(autouse=True)
def _native_env(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_healthy_provider_trigger_topology(monkeypatch)
    monkeypatch.setenv("PROVIDER_TRIGGER_CATALOG_ENVIRONMENT", "selfhost")
    from orchestra.provider_triggers.workspace_connection_facade import (
        _workspace_facade_apps,
    )

    _workspace_facade_apps.cache_clear()


def _seed_google_assistant(dbsession: Session) -> tuple[Assistant, dict[str, str]]:
    assistant = Assistant(user_id=PRIMARY_USER_ID, first_name="Cap", surname="Bar")
    dbsession.add(assistant)
    dbsession.flush()

    secret_dao = AssistantSecretDAO(dbsession)
    secret_dao.upsert(
        PRIMARY_USER_ID,
        assistant.agent_id,
        "GOOGLE_GRANTED_SCOPES",
        (
            "https://www.googleapis.com/auth/drive.readonly "
            "https://www.googleapis.com/auth/meetings.space.readonly "
            "https://www.googleapis.com/auth/chat.messages.readonly "
            "https://www.googleapis.com/auth/chat.memberships.readonly "
            "https://www.googleapis.com/auth/chat.spaces.readonly "
            "https://www.googleapis.com/auth/chat.users.readstate.readonly "
            "https://www.googleapis.com/auth/chat.users.availability.readonly"
        ),
    )
    secret_dao.upsert(
        PRIMARY_USER_ID,
        assistant.agent_id,
        "GOOGLE_ACCOUNT_EMAIL",
        "cap.bar@example.com",
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
    by_app = {c.canonical_app_slug: c.connection_id for c in connections}
    return assistant, by_app


def _seed_microsoft_assistant(dbsession: Session) -> tuple[Assistant, dict[str, str]]:
    assistant = Assistant(user_id=PRIMARY_USER_ID, first_name="MS", surname="Cap")
    dbsession.add(assistant)
    dbsession.flush()

    secret_dao = AssistantSecretDAO(dbsession)
    secret_dao.upsert(
        PRIMARY_USER_ID,
        assistant.agent_id,
        "MICROSOFT_GRANTED_SCOPES",
        "https://graph.microsoft.com/User.Read",
    )
    secret_dao.upsert(
        PRIMARY_USER_ID,
        assistant.agent_id,
        "MICROSOFT_ACCOUNT_EMAIL",
        "ms.cap@example.com",
    )
    secret_dao.upsert(
        PRIMARY_USER_ID,
        assistant.agent_id,
        "MICROSOFT_ACCESS_TOKEN",
        "test-ms-token",
    )
    dbsession.flush()

    connections = ensure_workspace_trigger_connections(
        dbsession,
        assistant_id=assistant.agent_id,
    )
    by_app = {c.canonical_app_slug: c.connection_id for c in connections}
    return assistant, by_app


def _import_google(dbsession: Session) -> None:
    service = TriggerCatalogImportService(dbsession)
    service.import_catalog(backend_id=NATIVE_GOOGLE_BACKEND_ID, environment="selfhost")
    dbsession.commit()


def _import_microsoft(dbsession: Session) -> None:
    service = TriggerCatalogImportService(dbsession)
    service.import_catalog(
        backend_id=NATIVE_MICROSOFT_BACKEND_ID,
        environment="selfhost",
    )
    dbsession.commit()


# --------------------------------------------------------------------------- #
# Capability matrix surfacing + resolver
# --------------------------------------------------------------------------- #


def test_capability_resolver_encodes_families(dbsession: Session) -> None:
    _import_google(dbsession)
    _import_microsoft(dbsession)

    meet = resolve_trigger_capability(
        dbsession,
        backend_id=NATIVE_GOOGLE_BACKEND_ID,
        provider_trigger_slug=MEET_SLUG,
    )
    assert meet is not None
    assert meet.live_ready is True
    assert meet.delivery_only is False
    assert meet.provisionable is True
    assert meet.config_schema == {}
    assert meet.target_resource_family == "google_meet_user"

    # Build-out honesty: Drive keeps its target-resource schema/family but is not
    # live_ready until ticket 26 resource targeting lands, so it is not provisionable.
    drive = resolve_trigger_capability(
        dbsession,
        backend_id=NATIVE_GOOGLE_BACKEND_ID,
        provider_trigger_slug=DRIVE_FILE_CREATED_SLUG,
    )
    assert drive is not None
    assert drive.live_ready is False
    assert drive.provisionable is False
    assert drive.target_resource_family == "google_drive_resource"
    assert drive.config_schema.get("required") == ["target_resource"]

    batch = resolve_trigger_capability(
        dbsession,
        backend_id=NATIVE_GOOGLE_BACKEND_ID,
        provider_trigger_slug=CHAT_BATCH_SLUG,
    )
    assert batch is not None
    assert batch.delivery_only is True
    assert batch.provisionable is False

    ms_app_only = resolve_trigger_capability(
        dbsession,
        backend_id=NATIVE_MICROSOFT_BACKEND_ID,
        provider_trigger_slug=MS_APP_ONLY_SLUG,
    )
    assert ms_app_only is not None
    assert ms_app_only.live_ready is False
    assert ms_app_only.target_resource_family == "microsoft_graph_app_only"

    # All Microsoft shapes — including delegated — are not live_ready until the
    # Graph transport lands (ticket 31).
    ms_delegated = resolve_trigger_capability(
        dbsession,
        backend_id=NATIVE_MICROSOFT_BACKEND_ID,
        provider_trigger_slug=MS_DELEGATED_SLUG,
    )
    assert ms_delegated is not None
    assert ms_delegated.live_ready is False
    assert ms_delegated.provisionable is False
    assert ms_delegated.target_resource_family == "microsoft_graph_delegated"


def test_catalog_listing_exposes_capability(dbsession: Session) -> None:
    _import_google(dbsession)
    assistant, _apps = _seed_google_assistant(dbsession)

    catalog = list_staged_triggers_for_assistant(
        dbsession,
        assistant_id=assistant.agent_id,
        backend_id=NATIVE_GOOGLE_BACKEND_ID,
    )
    rows = {row["provider_trigger_slug"]: row for row in catalog["triggers"]}
    # Discovery still lists the full curated set, but only Meet is provisionable.
    assert rows[MEET_SLUG]["provisionable"] is True
    assert rows[CHAT_BATCH_SLUG]["delivery_only"] is True
    assert rows[CHAT_BATCH_SLUG]["provisionable"] is False
    assert rows[DRIVE_FILE_CREATED_SLUG]["live_ready"] is False
    assert rows[DRIVE_FILE_CREATED_SLUG]["provisionable"] is False
    assert rows[DRIVE_FILE_CREATED_SLUG]["target_resource_family"] == (
        "google_drive_resource"
    )


# --------------------------------------------------------------------------- #
# Chokepoint 1: typed create/enable validation
# --------------------------------------------------------------------------- #


def test_validate_rejects_delivery_only_enable(dbsession: Session) -> None:
    _import_google(dbsession)
    assistant, apps = _seed_google_assistant(dbsession)
    trigger = ProviderEventTrigger(
        state="enabled",
        connection_id=apps[NATIVE_GOOGLE_CHAT_APP_SLUG],
        backend_id=NATIVE_GOOGLE_BACKEND_ID,
        canonical_app_slug=NATIVE_GOOGLE_CHAT_APP_SLUG,
        provider_trigger_slug=CHAT_BATCH_SLUG,
        trigger_config={},
    )
    with pytest.raises(ValueError, match="provider_event_trigger_delivery_only"):
        validate_provider_event_trigger_for_assistant(
            dbsession,
            assistant_id=assistant.agent_id,
            trigger=trigger,
        )


def test_validate_allows_delivery_only_draft(dbsession: Session) -> None:
    """A draft may reference a delivery-only slug; only enable fails closed."""
    _import_google(dbsession)
    assistant, apps = _seed_google_assistant(dbsession)
    trigger = ProviderEventTrigger(
        state="draft",
        connection_id=apps[NATIVE_GOOGLE_CHAT_APP_SLUG],
        backend_id=NATIVE_GOOGLE_BACKEND_ID,
        canonical_app_slug=NATIVE_GOOGLE_CHAT_APP_SLUG,
        provider_trigger_slug=CHAT_BATCH_SLUG,
        trigger_config={},
    )
    validate_provider_event_trigger_for_assistant(
        dbsession,
        assistant_id=assistant.agent_id,
        trigger=trigger,
    )


def test_validate_rejects_not_live_ready_microsoft_enable(dbsession: Session) -> None:
    _import_microsoft(dbsession)
    assistant, apps = _seed_microsoft_assistant(dbsession)
    trigger = ProviderEventTrigger(
        state="enabled",
        connection_id=apps[NATIVE_MICROSOFT_DIRECTORY_APP_SLUG],
        backend_id=NATIVE_MICROSOFT_BACKEND_ID,
        canonical_app_slug=NATIVE_MICROSOFT_DIRECTORY_APP_SLUG,
        provider_trigger_slug=MS_APP_ONLY_SLUG,
        trigger_config={},
    )
    with pytest.raises(ValueError, match="provider_event_trigger_not_live_ready"):
        validate_provider_event_trigger_for_assistant(
            dbsession,
            assistant_id=assistant.agent_id,
            trigger=trigger,
        )


def test_validate_rejects_drive_not_live_ready(dbsession: Session) -> None:
    """Drive is not live_ready during build-out; enable fails closed regardless
    of whether the required target-resource config is supplied (ticket 26)."""
    _import_google(dbsession)
    assistant, apps = _seed_google_assistant(dbsession)
    trigger = ProviderEventTrigger(
        state="enabled",
        connection_id=apps[NATIVE_GOOGLE_DRIVE_APP_SLUG],
        backend_id=NATIVE_GOOGLE_BACKEND_ID,
        canonical_app_slug=NATIVE_GOOGLE_DRIVE_APP_SLUG,
        provider_trigger_slug=DRIVE_FILE_CREATED_SLUG,
        trigger_config={"target_resource": "items/abc123"},
    )
    with pytest.raises(ValueError, match="provider_event_trigger_not_live_ready"):
        validate_provider_event_trigger_for_assistant(
            dbsession,
            assistant_id=assistant.agent_id,
            trigger=trigger,
        )


def test_validate_rejects_delegated_microsoft_enable(dbsession: Session) -> None:
    """All Microsoft shapes (delegated included) fail closed until Graph (31)."""
    _import_microsoft(dbsession)
    assistant, apps = _seed_microsoft_assistant(dbsession)
    trigger = ProviderEventTrigger(
        state="enabled",
        connection_id=apps[NATIVE_MICROSOFT_OUTLOOK_APP_SLUG],
        backend_id=NATIVE_MICROSOFT_BACKEND_ID,
        canonical_app_slug=NATIVE_MICROSOFT_OUTLOOK_APP_SLUG,
        provider_trigger_slug=MS_DELEGATED_SLUG,
        trigger_config={},
    )
    with pytest.raises(ValueError, match="provider_event_trigger_not_live_ready"):
        validate_provider_event_trigger_for_assistant(
            dbsession,
            assistant_id=assistant.agent_id,
            trigger=trigger,
        )


def test_missing_required_config_helper() -> None:
    """The config-required chokepoint helper still flags absent required fields.

    No live_ready native slug currently declares required config (only Meet is
    live_ready, and its schema is empty), so exercise the helper directly to keep
    the config-required path covered for when live families gain required config.
    """
    schema = {
        "type": "object",
        "properties": {"target_resource": {"type": "string"}},
        "required": ["target_resource"],
    }
    assert missing_required_config(schema, {}) == ["target_resource"]
    assert missing_required_config(schema, {"target_resource": "items/abc"}) == []
    assert missing_required_config({}, {}) == []


def test_validate_accepts_meet_user_level_enable(dbsession: Session) -> None:
    _import_google(dbsession)
    assistant, apps = _seed_google_assistant(dbsession)
    trigger = ProviderEventTrigger(
        state="enabled",
        connection_id=apps[NATIVE_GOOGLE_MEET_APP_SLUG],
        backend_id=NATIVE_GOOGLE_BACKEND_ID,
        canonical_app_slug=NATIVE_GOOGLE_MEET_APP_SLUG,
        provider_trigger_slug=MEET_SLUG,
        trigger_config={},
    )
    validate_provider_event_trigger_for_assistant(
        dbsession,
        assistant_id=assistant.agent_id,
        trigger=trigger,
    )


# --------------------------------------------------------------------------- #
# Chokepoint 2: reconciliation prerequisites
# --------------------------------------------------------------------------- #


class _StubGoogleAccountAdapter(TriggerProviderAdapter):
    """Resolves account identity so reconcile reaches the capability gate."""

    backend_id = NATIVE_GOOGLE_BACKEND_ID

    def resolve_account_identity(
        self,
        *,
        provider_connection_id: str,
    ) -> ProviderAccountIdentity:
        return ProviderAccountIdentity(
            subject=provider_connection_id,
            display_label=provider_connection_id,
            subject_hmac=None,
            connected_account_id=provider_connection_id,
            provider_user_id=provider_connection_id,
        )

    def provision(
        self,
        request: TriggerProvisionRequest,
    ) -> TriggerProvisionResult:  # pragma: no cover - must never be reached
        raise AssertionError("provision must not run for delivery-only slugs")

    def delete(self, request: TriggerDeleteRequest) -> None:  # pragma: no cover
        return None

    def verify_delivery(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        signing_secrets: Sequence[str],
        tolerance_seconds: int | None = None,
    ) -> bool:  # pragma: no cover
        return True

    def normalize_delivery(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes | Mapping[str, Any],
    ) -> NormalizedProviderDelivery:  # pragma: no cover
        raise NotImplementedError

    def stable_event_identity(
        self,
        delivery: Mapping[str, Any] | NormalizedProviderDelivery,
    ) -> str | None:  # pragma: no cover
        return None

    def health(
        self,
        *,
        external_trigger_id: str | None,
        provider_connection_id: str | None,
    ) -> TriggerHealthResult:  # pragma: no cover
        return TriggerHealthResult(status="ok")


def test_reconcile_delivery_only_binding_needs_attention(dbsession: Session) -> None:
    _import_google(dbsession)
    assistant, apps = _seed_google_assistant(dbsession)

    dao = ProviderTriggerDAO(dbsession)
    trigger = ProviderEventTrigger(
        state="enabled",
        connection_id=apps[NATIVE_GOOGLE_CHAT_APP_SLUG],
        backend_id=NATIVE_GOOGLE_BACKEND_ID,
        canonical_app_slug=NATIVE_GOOGLE_CHAT_APP_SLUG,
        provider_trigger_slug=CHAT_BATCH_SLUG,
        trigger_config={},
    )
    binding_id = f"binding-{uuid.uuid4().hex[:12]}"
    binding = dao.create_binding(
        binding_id=binding_id,
        project_id=1,
        tasks_context_id=1,
        source_task_log_id=abs(hash(binding_id)) % (2**30),
        task_id=abs(hash(binding_id)) % (2**30),
        assistant_id=assistant.agent_id,
        task_revision=1,
        trigger=trigger,
        execution_mode="live",
        entrypoint=None,
    )
    binding.reconcile_next_retry_at = datetime.now(timezone.utc)
    dbsession.flush()
    binding_pk = binding.id

    service = ProviderTriggerReconciliationService(
        dbsession,
        lease_owner="cap-bar-worker",
        adapter_resolver=lambda _backend_id: _StubGoogleAccountAdapter(),
    )
    service.process_reconcile_batch()
    dbsession.commit()

    refreshed = dbsession.get(EventTriggerBinding, binding_pk)
    assert refreshed is not None
    assert refreshed.runtime_health == BindingRuntimeHealth.needs_attention.value
    assert refreshed.last_stable_error_code == (
        ReconcileErrorCode.trigger_delivery_only.value
    )
    assert refreshed.active_generation_id is None


# --------------------------------------------------------------------------- #
# Chokepoint 3: adapters raise instead of stubbing healthy generations
# --------------------------------------------------------------------------- #


@dataclass
class _FakeCredentials:
    connection_id: str
    account_email: str = "live.user@example.com"
    access_token: str = "live-token"


@dataclass
class _FakeCredentialLoader:
    """Minimal loader stub — the adapter only reads access_token/account_email."""

    def load_for_connection_id(self, connection_id: str) -> _FakeCredentials:
        return _FakeCredentials(connection_id=connection_id)


def _provision_request(slug: str, *, app_slug: str) -> TriggerProvisionRequest:
    return TriggerProvisionRequest(
        connection_id="conn-1",
        provider_connection_id="google:live.user@example.com",
        provider_user_id="live.user@example.com",
        canonical_app_slug=app_slug,
        provider_trigger_slug=slug,
        trigger_config={},
        callback_url="https://orchestra.example/v0/webhooks/integrations/x/y",
        idempotency_key="idem-1",
        ingress_key="ingress-1",
        generation_id="gen-1",
    )


def test_microsoft_adapter_provision_fails_closed_with_session() -> None:
    from orchestra.provider_triggers.native_microsoft_trigger_adapter import (
        NativeMicrosoftTriggerAdapter,
    )

    adapter = NativeMicrosoftTriggerAdapter(
        credential_loader=_FakeCredentialLoader(),
        webhook_secret="secret",
        account_subject_pepper="pepper",
    )
    with pytest.raises(RuntimeError, match="not yet available"):
        adapter.provision(
            _provision_request(
                "microsoft.graph.mailMessage.created",
                app_slug="microsoft_outlook",
            ),
        )


def test_google_adapter_provision_fails_closed_for_non_meet() -> None:
    from orchestra.provider_triggers.native_google_trigger_adapter import (
        NativeGoogleTriggerAdapter,
    )

    adapter = NativeGoogleTriggerAdapter(
        credential_loader=_FakeCredentialLoader(),
        webhook_secret="secret",
        account_subject_pepper="pepper",
        pubsub_topic="projects/p/topics/t",
    )
    with pytest.raises(RuntimeError, match="resource targeting"):
        adapter.provision(
            _provision_request(
                DRIVE_FILE_CREATED_SLUG,
                app_slug=NATIVE_GOOGLE_DRIVE_APP_SLUG,
            ),
        )
