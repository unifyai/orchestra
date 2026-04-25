"""Hive-awareness tests for ``task_machine_state_service``.

Exercises the classifier asymmetry between the projection trigger
(``is_task_surface_context_name``) and the guards that reject Hive paths,
the refusal of path-based assistant inference for Hive contexts, the
loud failure when Hive task rows omit ``_assistant_id``, and the
grouped-by-assistant projection path in
:func:`sync_task_activations_for_task_ids`.

Tests exercise the real service functions; only the Communication-bound
reconciliation side effect is stubbed so the database assertions stay
fast and hermetic.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from orchestra.db.models.orchestra_models import Context, Project
from orchestra.services import task_machine_state_service
from orchestra.services.task_machine_state_service import (
    HIVE_CONTEXT_PREFIX,
    _assistant_id_from_context_name,
    _resolve_assistant_id,
    _TaskRow,
    is_internal_task_machine_context_name,
    is_protected_task_surface_context_name,
    is_task_surface_context_name,
)
from orchestra.tests.test_log import _create_log, _delete_logs
from orchestra.tests.test_log.test_task_machine_projection import (
    _ensure_task_machine_project,
    _get_context_logs,
    _make_assistant,
    _scheduled_task_entries,
)

TASK_MACHINE_PROJECT_NAME = task_machine_state_service.TASK_MACHINE_PROJECT_NAME


# --------------------------------------------------------------------------- #
# Unit coverage: predicates and helpers (no DB)
# --------------------------------------------------------------------------- #


def test_hive_context_prefix_constant_is_single_source_of_truth():
    """``HIVE_CONTEXT_PREFIX`` is the only literal Hive prefix in the service."""

    assert HIVE_CONTEXT_PREFIX == "Hives/"


def test_is_internal_rejects_hive_activations_path():
    """``Hives/{h}/Tasks/Activations`` must never be classified as machine state."""

    assert is_internal_task_machine_context_name("Hives/42/Tasks/Activations") is False


def test_is_protected_rejects_hive_runs_path():
    """``Hives/{h}/Tasks/Runs`` must not be treated as a protected system context."""

    assert is_protected_task_surface_context_name("Hives/42/Tasks/Runs") is False


def test_is_task_surface_accepts_hive_tasks_path():
    """Projection trigger must keep firing for ``Hives/{h}/Tasks`` (asymmetry)."""

    assert is_task_surface_context_name("Hives/42/Tasks") is True


def test_assistant_id_parser_refuses_hive_paths():
    """Hive paths encode a hive id in segment -2, not an assistant id."""

    assert _assistant_id_from_context_name("Hives/42/Tasks") is None


def test_assistant_id_parser_still_works_for_per_body_paths():
    """Non-Hive paths continue to yield ``segments[-2]``."""

    assert _assistant_id_from_context_name("1/42/Tasks") == "42"


def test_resolve_assistant_id_raises_on_hive_row_missing_owner_stamp():
    """Hive rows must stamp ``_assistant_id``; missing stamp surfaces loudly."""

    row = _TaskRow(
        log_event_id=12345,
        data={"task_id": 7, "_user_id": "1"},
        updated_at=None,
        created_at=None,
    )
    with pytest.raises(ValueError) as excinfo:
        _resolve_assistant_id(
            task_row=row,
            tasks_context_name="Hives/42/Tasks",
        )
    assert "Hive task row missing _assistant_id" in str(excinfo.value)
    assert "log_event_id=12345" in str(excinfo.value)


def test_resolve_assistant_id_returns_stamped_owner_for_hive_row():
    """When the row stamps an owner, Hive resolution returns it verbatim."""

    row = _TaskRow(
        log_event_id=99,
        data={"task_id": 7, "_assistant_id": "7"},
        updated_at=None,
        created_at=None,
    )
    assert (
        _resolve_assistant_id(
            task_row=row,
            tasks_context_name="Hives/42/Tasks",
        )
        == "7"
    )


def test_resolve_assistant_id_falls_through_to_path_for_per_body_rows():
    """Non-Hive rows with no owner stamp fall back to path inference."""

    row = _TaskRow(
        log_event_id=1,
        data={"task_id": 7},
        updated_at=None,
        created_at=None,
    )
    assert (
        _resolve_assistant_id(
            task_row=row,
            tasks_context_name="1/42/Tasks",
        )
        == "42"
    )


# --------------------------------------------------------------------------- #
# Integration coverage: grouped-by-assistant Hive projection + solo regression
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _stub_materialization(monkeypatch):
    """Prevent projection tests from reaching Communication's scheduler."""

    monkeypatch.setattr(
        task_machine_state_service,
        "_reconcile_scheduled_activation_materialization",
        lambda *, previous_activation, current_activation: None,
    )


def _hive_scheduled_entries(
    *,
    hive_id: str,
    user_id: str,
    assistant_id: int,
    task_id: int,
    instance_id: int = 0,
) -> dict:
    """Return a scheduled task row that lives on the Hive definition surface."""

    entries = _scheduled_task_entries(task_id=task_id, instance_id=instance_id)
    entries["_hive_id"] = hive_id
    entries["_user_id"] = user_id
    entries["_assistant_id"] = str(assistant_id)
    return entries


@pytest.mark.anyio
async def test_sync_fans_out_hive_batch_to_each_owning_body(
    client: AsyncClient,
    dbsession,
):
    """Two Hive rows owned by distinct assistants project into both bodies' trees.

    Each row stamps ``_assistant_id`` on the Hive surface; the service must
    resolve ``user_id`` from the ``Assistant`` table per bucket and land one
    activation under each owning body's ``{user}/{assistant}/Tasks/Activations``
    context. No activation should ever appear under ``Hives/.../Activations``.
    """

    await _ensure_task_machine_project(client)

    assistant_a = _make_assistant(dbsession, user_id="user1")
    assistant_b = _make_assistant(dbsession, user_id="user2")
    dbsession.commit()

    hive_id = "77"
    hive_tasks_context = f"Hives/{hive_id}/Tasks"
    task_id_a = 1101
    task_id_b = 1102

    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=hive_tasks_context,
        entries=_hive_scheduled_entries(
            hive_id=hive_id,
            user_id=str(assistant_a.user_id),
            assistant_id=assistant_a.agent_id,
            task_id=task_id_a,
        ),
    )
    assert response.status_code == 200, response.json()

    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=hive_tasks_context,
        entries=_hive_scheduled_entries(
            hive_id=hive_id,
            user_id=str(assistant_b.user_id),
            assistant_id=assistant_b.agent_id,
            task_id=task_id_b,
        ),
    )
    assert response.status_code == 200, response.json()

    activations_a_context = (
        f"{assistant_a.user_id}/{assistant_a.agent_id}/Tasks/Activations"
    )
    activations_b_context = (
        f"{assistant_b.user_id}/{assistant_b.agent_id}/Tasks/Activations"
    )

    activations_a = await _get_context_logs(client, context_name=activations_a_context)
    activations_b = await _get_context_logs(client, context_name=activations_b_context)

    assert len(activations_a) == 1
    assert activations_a[0]["entries"]["assistant_id"] == str(assistant_a.agent_id)
    assert activations_a[0]["entries"]["task_id"] == task_id_a
    assert activations_a[0]["entries"]["activation_key"] == (
        f"{assistant_a.agent_id}:{task_id_a}"
    )

    assert len(activations_b) == 1
    assert activations_b[0]["entries"]["assistant_id"] == str(assistant_b.agent_id)
    assert activations_b[0]["entries"]["task_id"] == task_id_b
    assert activations_b[0]["entries"]["activation_key"] == (
        f"{assistant_b.agent_id}:{task_id_b}"
    )

    hive_activations_context_exists = (
        dbsession.query(Context)
        .filter(Context.name == f"{hive_tasks_context}/Activations")
        .first()
    )
    assert hive_activations_context_exists is None


@pytest.mark.anyio
async def test_sync_invokes_ensure_task_machine_contexts_once_per_owner(
    client: AsyncClient,
    dbsession,
    monkeypatch,
):
    """A mixed-owner Hive batch must call ``ensure_task_machine_contexts`` per body.

    Wraps the service's own ``ensure_task_machine_contexts`` to capture the
    ``(user_id, assistant_id)`` pairs it receives, then verifies every
    distinct owner in the batch shows up exactly once.
    """

    await _ensure_task_machine_project(client)

    assistant_a = _make_assistant(dbsession, user_id="user3")
    assistant_b = _make_assistant(dbsession, user_id="user4")
    dbsession.commit()

    real_ensure = task_machine_state_service.ensure_task_machine_contexts
    captured_pairs: list[tuple[str, str]] = []

    def _capture_ensure(session, project_id, *, user_id, assistant_id):
        captured_pairs.append((user_id, assistant_id))
        return real_ensure(
            session,
            project_id,
            user_id=user_id,
            assistant_id=assistant_id,
        )

    monkeypatch.setattr(
        task_machine_state_service,
        "ensure_task_machine_contexts",
        _capture_ensure,
    )

    hive_id = "1212"
    hive_tasks_context = f"Hives/{hive_id}/Tasks"

    resp_a = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=hive_tasks_context,
        entries=_hive_scheduled_entries(
            hive_id=hive_id,
            user_id=str(assistant_a.user_id),
            assistant_id=assistant_a.agent_id,
            task_id=2201,
        ),
    )
    assert resp_a.status_code == 200, resp_a.json()

    captured_for_first = [
        pair for pair in captured_pairs if pair[1] == str(assistant_a.agent_id)
    ]
    assert captured_for_first == [
        (str(assistant_a.user_id), str(assistant_a.agent_id)),
    ]

    captured_pairs.clear()

    resp_b = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=hive_tasks_context,
        entries=_hive_scheduled_entries(
            hive_id=hive_id,
            user_id=str(assistant_b.user_id),
            assistant_id=assistant_b.agent_id,
            task_id=2202,
        ),
    )
    assert resp_b.status_code == 200, resp_b.json()

    captured_for_second = [
        pair for pair in captured_pairs if pair[1] == str(assistant_b.agent_id)
    ]
    assert captured_for_second == [
        (str(assistant_b.user_id), str(assistant_b.agent_id)),
    ]


@pytest.mark.anyio
async def test_sync_solo_per_body_path_still_projects_single_bucket(
    client: AsyncClient,
    dbsession,
):
    """Non-Hive surface keeps its single-bucket behavior (solo regression)."""

    await _ensure_task_machine_project(client)

    assistant = _make_assistant(dbsession, user_id="seconday_user")
    dbsession.commit()

    tasks_context = f"{assistant.user_id}/{assistant.agent_id}/Tasks"
    task_id = 3301

    entries = _scheduled_task_entries(task_id=task_id)
    entries["_user_id"] = str(assistant.user_id)
    entries["_assistant_id"] = str(assistant.agent_id)

    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=tasks_context,
        entries=entries,
    )
    assert response.status_code == 200, response.json()

    activations_context = f"{assistant.user_id}/{assistant.agent_id}/Tasks/Activations"
    activations = await _get_context_logs(client, context_name=activations_context)
    assert len(activations) == 1
    activation = activations[0]["entries"]
    assert activation["assistant_id"] == str(assistant.agent_id)
    assert activation["task_id"] == task_id
    assert activation["activation_key"] == f"{assistant.agent_id}:{task_id}"


@pytest.mark.anyio
async def test_delete_hive_task_definition_clears_per_body_machine_rows(
    client: AsyncClient,
    dbsession,
):
    """Deleting a Hive task row uses owner hints to clear per-body state."""

    await _ensure_task_machine_project(client)

    assistant = _make_assistant(dbsession, user_id="user1")
    dbsession.commit()

    hive_id = "909"
    hive_tasks_context = f"Hives/{hive_id}/Tasks"
    task_id = 4401
    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=hive_tasks_context,
        entries=_hive_scheduled_entries(
            hive_id=hive_id,
            user_id=str(assistant.user_id),
            assistant_id=assistant.agent_id,
            task_id=task_id,
        ),
    )
    assert response.status_code == 200, response.json()
    source_task_log_id = response.json()["log_event_ids"][0]

    dbsession.expire_all()
    project = (
        dbsession.query(Project).filter(Project.name == TASK_MACHINE_PROJECT_NAME).one()
    )
    context_ids = task_machine_state_service.ensure_task_machine_contexts(
        session=dbsession,
        project_id=project.id,
        user_id=str(assistant.user_id),
        assistant_id=str(assistant.agent_id),
    )
    task_machine_state_service._upsert_machine_row(
        session=dbsession,
        project_id=project.id,
        context_id=context_ids.runs_context_id,
        unique_field_name=task_machine_state_service._TASK_RUN_UNIQUE_FIELD,
        unique_field_value=f"{assistant.agent_id}:{task_id}:run",
        payload={
            "run_key": f"{assistant.agent_id}:{task_id}:run",
            "assistant_id": str(assistant.agent_id),
            "task_id": task_id,
            "source_task_log_id": source_task_log_id,
            "source_type": "scheduled",
            "execution_mode": "live",
            "activation_revision": "test-revision",
        },
    )
    dbsession.commit()

    delete_response = await _delete_logs(
        client,
        [(source_task_log_id, None)],
        project_name=TASK_MACHINE_PROJECT_NAME,
        context=hive_tasks_context,
    )
    assert delete_response.status_code == 200, delete_response.json()

    activations_context = f"{assistant.user_id}/{assistant.agent_id}/Tasks/Activations"
    runs_context = f"{assistant.user_id}/{assistant.agent_id}/Tasks/Runs"
    activations = await _get_context_logs(client, context_name=activations_context)
    runs = await _get_context_logs(client, context_name=runs_context)
    assert all(log["entries"]["task_id"] != task_id for log in activations)
    assert all(log["entries"]["task_id"] != task_id for log in runs)
