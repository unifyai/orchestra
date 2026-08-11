"""Regression: typed Tasks create against a pre-collapse (legacy) Tasks context.

Contexts created before run state moved off Tasks definitions (unity
``ef49040db``) still register ``instance_id`` as auto-counting under
``task_id``. The typed create path provides ``task_id`` explicitly, so the
stale registration made every create 400 with ``Cannot generate auto-counting
value for 'instance_id' …`` (first seen on prod assistant 526, 2026-08-11).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.legacy_tasks_schema import drop_legacy_tasks_instance_id_schema
from orchestra.db.models.orchestra_models import Context, ContextCounter, Project
from orchestra.tests.provider_triggers.conftest import (
    stub_healthy_provider_trigger_topology,
)
from orchestra.tests.provider_triggers.control_plane_harness import (
    seed_provider_event_fixture_prerequisites,
)
from orchestra.tests.test_tasks.test_trigger_task import _auth_user_id, _seed_task
from orchestra.tests.utils import HEADERS

_FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "task_trigger_contract"
)

_LEGACY_AUTO_COUNTING = {"task_id": None, "instance_id": "task_id"}
_LEGACY_UNIQUE_KEY_NAMES = ["task_id", "instance_id"]
_LEGACY_UNIQUE_KEY_TYPES = ["int", "int"]


def _provider_event_task_payload() -> dict:
    trigger = json.loads(
        (_FIXTURE_DIR / "task_trigger.provider_event.v1.json").read_text(
            encoding="utf-8",
        ),
    )
    return {
        "name": "Daily sync briefing",
        "description": "Summarize the daily sync transcript.",
        "status": "triggerable",
        "trigger": trigger,
        "enabled": True,
        "offline": False,
        "priority": "normal",
    }


@pytest.fixture
async def assistant_id(
    client: AsyncClient,
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    stub_healthy_provider_trigger_topology(monkeypatch)
    monkeypatch.setenv("PROVIDER_TRIGGER_CATALOG_ENVIRONMENT", "selfhost")
    response = await client.post(
        "/v0/assistant",
        json={"first_name": "Legacy", "surname": "Tasks", "create_infra": False},
        headers=HEADERS,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    agent_id = int(response.json()["info"]["agent_id"])
    seed_provider_event_fixture_prerequisites(dbsession, assistant_id=agent_id)
    dbsession.commit()
    return agent_id


def _reshape_tasks_context_to_legacy(
    dbsession: Session,
    *,
    assistant_id: int,
) -> Context:
    """Recreate the pre-collapse Tasks context shape, with one legacy row.

    The seeded ``task_id=1`` row mirrors production: ``_allocate_task_id``
    then hands the next create ``task_id=2``, the exact id the failing
    auto-count parent check named.
    """
    _seed_task(
        dbsession,
        assistant_id=assistant_id,
        user_id=_auth_user_id(),
        task_id=1,
    )
    project = (
        dbsession.query(Project)
        .filter(
            Project.user_id == _auth_user_id(),
            Project.organization_id.is_(None),
            Project.name == "Assistants",
        )
        .one()
    )
    context = (
        dbsession.query(Context)
        .filter(
            Context.project_id == project.id,
            Context.name == f"{_auth_user_id()}/{assistant_id}/Tasks",
        )
        .one()
    )
    context.auto_counting = dict(_LEGACY_AUTO_COUNTING)
    context.unique_key_names = list(_LEGACY_UNIQUE_KEY_NAMES)
    context.unique_key_types = list(_LEGACY_UNIQUE_KEY_TYPES)
    dbsession.add(
        ContextCounter(
            context_id=context.id,
            column_name="instance_id",
            parent_values_hash='{"task_id": 1}',
            parent_values={"task_id": 1},
            next_value=2,
        ),
    )
    dbsession.add(
        ContextCounter(
            context_id=context.id,
            column_name="task_id",
            parent_values_hash="__root__",
            parent_values={},
            next_value=2,
        ),
    )
    dbsession.commit()
    return context


@pytest.mark.anyio
async def test_typed_create_fails_on_legacy_context_and_recovers_after_drop(
    client: AsyncClient,
    dbsession: Session,
    assistant_id: int,
) -> None:
    context = _reshape_tasks_context_to_legacy(dbsession, assistant_id=assistant_id)

    # Reproduce the production failure: the stale instance_id registration
    # rejects a well-formed typed create.
    broken = await client.post(
        f"/v0/assistants/{assistant_id}/tasks",
        json=_provider_event_task_payload(),
        headers=HEADERS,
    )
    assert broken.status_code == status.HTTP_400_BAD_REQUEST, broken.json()
    detail = str(broken.json().get("detail", ""))
    assert "auto-counting" in detail
    assert "instance_id" in detail

    drop_legacy_tasks_instance_id_schema(dbsession.connection())
    dbsession.commit()
    # The app shares this session; drop the identity-mapped Context so the
    # next request re-reads the migrated schema (a fresh prod session would).
    dbsession.expire_all()

    created = await client.post(
        f"/v0/assistants/{assistant_id}/tasks",
        json=_provider_event_task_payload(),
        headers=HEADERS,
    )
    assert created.status_code == status.HTTP_201_CREATED, created.json()
    body = created.json()["info"]
    assert body["task_id"] == 2
    assert body["task_revision"] == 1
    assert body["provider_event_binding_id"]

    dbsession.refresh(context)
    assert context.auto_counting == {"task_id": None}
    assert context.unique_key_names == ["task_id"]
    assert context.unique_key_types == ["int"]

    counters = (
        dbsession.query(ContextCounter)
        .filter(ContextCounter.context_id == context.id)
        .all()
    )
    assert all(counter.column_name != "instance_id" for counter in counters)
    assert any(counter.column_name == "task_id" for counter in counters)


@pytest.mark.anyio
async def test_drop_is_idempotent_and_leaves_other_contexts_alone(
    client: AsyncClient,
    dbsession: Session,
    assistant_id: int,
) -> None:
    context = _reshape_tasks_context_to_legacy(dbsession, assistant_id=assistant_id)

    # A non-Tasks context and a Tasks child context with the same shape must
    # not be touched.
    bystanders = []
    for name in (
        f"{_auth_user_id()}/{assistant_id}/Widgets",
        f"{_auth_user_id()}/{assistant_id}/Tasks/Meta",
    ):
        bystander = Context(
            project_id=context.project_id,
            name=name,
            auto_counting=dict(_LEGACY_AUTO_COUNTING),
            unique_key_names=list(_LEGACY_UNIQUE_KEY_NAMES),
            unique_key_types=list(_LEGACY_UNIQUE_KEY_TYPES),
        )
        dbsession.add(bystander)
        bystanders.append(bystander)
    dbsession.commit()

    drop_legacy_tasks_instance_id_schema(dbsession.connection())
    drop_legacy_tasks_instance_id_schema(dbsession.connection())
    dbsession.commit()
    dbsession.expire_all()

    dbsession.refresh(context)
    assert context.auto_counting == {"task_id": None}
    assert context.unique_key_names == ["task_id"]
    assert context.unique_key_types == ["int"]

    for bystander in bystanders:
        dbsession.refresh(bystander)
        assert bystander.auto_counting == _LEGACY_AUTO_COUNTING
        assert bystander.unique_key_names == _LEGACY_UNIQUE_KEY_NAMES
        assert bystander.unique_key_types == _LEGACY_UNIQUE_KEY_TYPES
