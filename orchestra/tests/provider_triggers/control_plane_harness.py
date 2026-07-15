"""Shared seed helpers for provider-trigger control-plane tests."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.models.core_models import Project
from orchestra.db.models.integration_provider_models import IntegrationConnection
from orchestra.db.models.provider_trigger_models import (
    EventTriggerBinding,
    EventTriggerSubscriptionGeneration,
)
from orchestra.provider_triggers.runtime_types import DesiredTriggerState
from orchestra.provider_triggers.task_trigger import ProviderEventTrigger
from orchestra.services.provider_trigger_reconciliation_service import (
    ProviderTriggerReconciliationService,
)
from orchestra.services.task_machine_state_service import TASK_MACHINE_PROJECT_NAME
from orchestra.settings import settings
from orchestra.tests.utils import HEADERS
from orchestra.workers.provider_trigger_worker import run_worker_cycle

PRIMARY_USER_ID = str(os.getenv("AUTH_ACCOUNT_USER_ID"))
WEBHOOK_SECRET = "test-composio-webhook-secret"


def _task_machine_project(dbsession: Session) -> Project:
    project = (
        dbsession.query(Project)
        .filter(
            Project.user_id == PRIMARY_USER_ID,
            Project.organization_id.is_(None),
            Project.name == TASK_MACHINE_PROJECT_NAME,
        )
        .one_or_none()
    )
    if project is not None:
        return project
    project = Project(name=TASK_MACHINE_PROJECT_NAME, user_id=PRIMARY_USER_ID)
    dbsession.add(project)
    dbsession.flush()
    return project


def seed_integration_connection(
    dbsession: Session,
    *,
    assistant_id: int,
    connection_id: str | None = None,
    provider_connection_id: str = "ca_local_stub",
    provider_user_id: str = "assistant:provider-trigger-probe",
    status: str = "connected",
) -> IntegrationConnection:
    """Insert one assistant-scoped Composio GitHub connection."""

    resolved_connection_id = connection_id or f"conn-{uuid.uuid4().hex[:10]}"
    connection = IntegrationConnection(
        connection_id=resolved_connection_id,
        owner_scope="assistant",
        assistant_id=assistant_id,
        canonical_app_slug="github",
        backend_id="composio",
        provider_app_id="GITHUB",
        provider_connection_id=provider_connection_id,
        provider_user_id=provider_user_id,
        status=status,
        credential_storage="provider_vault",
    )
    dbsession.add(connection)
    dbsession.flush()
    return connection


def provider_event_trigger_payload(
    *,
    connection_id: str,
    state: str = "enabled",
    repository: str = "octocat/Hello-World",
) -> ProviderEventTrigger:
    return ProviderEventTrigger(
        state=state,
        connection_id=connection_id,
        backend_id="composio",
        canonical_app_slug="github",
        event_slug="github.issue_created",
        schema_version="1",
        filters=[
            {
                "field": "repository",
                "operator": "is",
                "value": repository,
            },
        ],
    )


def seed_provider_event_binding(
    dbsession: Session,
    *,
    assistant_id: int,
    connection_id: str,
    state: str = "enabled",
    repository: str = "octocat/Hello-World",
) -> EventTriggerBinding:
    """Create one derived binding row ready for reconciliation."""

    project = _task_machine_project(dbsession)

    task_id = int(uuid.uuid4().int % 1_000_000) + 1
    binding_id = f"binding-{uuid.uuid4().hex[:12]}"
    dao = ProviderTriggerDAO(dbsession)
    binding = dao.create_binding(
        binding_id=binding_id,
        project_id=project.id,
        tasks_context_id=0,
        source_task_log_id=task_id,
        task_id=task_id,
        assistant_id=assistant_id,
        task_revision=1,
        trigger=provider_event_trigger_payload(
            connection_id=connection_id,
            state=state,
            repository=repository,
        ),
        execution_mode="live",
        entrypoint=None,
    )
    binding.reconcile_next_retry_at = datetime.now(timezone.utc)
    dbsession.flush()
    return binding


async def create_assistant(client: AsyncClient) -> int:
    response = await client.post(
        "/v0/assistant",
        json={"first_name": "Provider", "surname": "Trigger", "create_infra": False},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    return int(response.json()["info"]["agent_id"])


def reconcile_binding_to_active_generation(
    dbsession: Session,
    *,
    binding_id: str,
) -> EventTriggerSubscriptionGeneration:
    """Run one worker reconcile cycle and return the promoted generation."""

    monkeypatch_settings = settings
    if not monkeypatch_settings.orchestra_trigger_callback_base_url:
        monkeypatch_settings.orchestra_trigger_callback_base_url = (
            "https://orchestra.example"
        )
    service = ProviderTriggerReconciliationService(
        dbsession,
        lease_owner="test-worker",
    )
    service.process_reconcile_batch()
    dbsession.commit()
    service.process_generation_batch()
    dbsession.commit()

    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == binding_id,
        ),
    ).scalar_one()
    assert binding.active_generation_id is not None
    generation = dbsession.execute(
        select(EventTriggerSubscriptionGeneration).where(
            EventTriggerSubscriptionGeneration.generation_id
            == binding.active_generation_id,
        ),
    ).scalar_one()
    assert generation.external_trigger_id
    assert generation.ingress_key
    return generation


def run_trigger_worker_cycle() -> dict[str, int]:
    return run_worker_cycle(lease_owner="test-worker")


def apply_signing_overlap(
    generation: EventTriggerSubscriptionGeneration,
    *,
    previous_secret: str,
    overlap_seconds: int = 600,
) -> None:
    generation.previous_signing_secret_ref = previous_secret
    generation.previous_signing_secret_version = "previous"
    generation.signing_overlap_expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=overlap_seconds,
    )


def pause_binding(dbsession: Session, binding: EventTriggerBinding) -> None:
    binding.desired_trigger_state = DesiredTriggerState.paused.value
    binding.local_acceptance_open = False
    binding.reconcile_next_retry_at = datetime.now(timezone.utc)
    dbsession.flush()
