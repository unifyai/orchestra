"""Task-machine admin routes exposed under the log admin API."""

from fastapi import APIRouter, Depends, HTTPException

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Assistant, Project
from orchestra.routines.task_supervisor_sweep import sweep_task_supervision
from orchestra.services.task_machine_state_service import (
    create_task_outbound_operation_if_absent,
    create_task_run_if_absent,
    get_latest_task_execution_for_task,
    get_open_task_execution,
    get_task_execution,
    release_stuck_task_executions,
    resolve_tasks_context_name,
    sync_task_executions_for_task_ids,
    update_task_outbound_operation,
    update_task_run,
)
from orchestra.web.api.dependencies import auth_admin_key
from orchestra.web.api.log.task_machine_schema import (
    TaskExecutionCreateOrAdoptRequest,
    TaskExecutionGetRequest,
    TaskExecutionGetResponse,
    TaskExecutionLatestRequest,
    TaskExecutionLatestResponse,
    TaskExecutionLookupRequest,
    TaskExecutionLookupResponse,
    TaskExecutionMutationResponse,
    TaskExecutionReprojectRequest,
    TaskExecutionReprojectResponse,
    TaskExecutionUpdateRequest,
    TaskOutboundOperationCreateOrAdoptRequest,
    TaskOutboundOperationMutationResponse,
    TaskOutboundOperationUpdateRequest,
    TaskSourceReleaseRequest,
    TaskSourceReleaseResponse,
    TaskSupervisorSweepResponse,
)

router = APIRouter()


def _projects_with_name_limited(session, *, project_name: str) -> list[Project]:
    """Return up to two matching projects for fallback disambiguation."""

    return (
        session.query(Project)
        .filter(Project.name == project_name)
        .order_by(Project.id.asc())
        .limit(2)
        .all()
    )


def _get_internal_project_or_404(
    session,
    *,
    project_name: str,
    assistant_id: str,
) -> Project:
    """Resolve the assistant-scoped internal project for task-machine admin IO.

    The hot path should stay index-friendly: look up the assistant by primary
    key, then resolve the matching project through the owner's `(user_id, name)`
    or `(organization_id, name)` uniqueness constraint. Local tests sometimes
    omit Assistant rows, so we keep a unique-project fallback only for that
    narrow path.
    """

    try:
        assistant_id_int = int(str(assistant_id))
    except (TypeError, ValueError) as exc:
        projects = _projects_with_name_limited(
            session,
            project_name=project_name,
        )
        if len(projects) == 1:
            return projects[0]
        if not projects:
            raise HTTPException(
                status_code=404,
                detail=f"Project '{project_name}' not found.",
            ) from exc
        raise HTTPException(
            status_code=404,
            detail=f"Assistant '{assistant_id}' not found.",
        ) from exc

    assistant = (
        session.query(Assistant)
        .filter(Assistant.agent_id == assistant_id_int)
        .one_or_none()
    )
    if assistant is None:
        projects = _projects_with_name_limited(
            session,
            project_name=project_name,
        )
        if len(projects) == 1:
            return projects[0]
        if not projects:
            raise HTTPException(
                status_code=404,
                detail=f"Project '{project_name}' not found.",
            )
        raise HTTPException(
            status_code=404,
            detail=f"Assistant '{assistant_id}' not found.",
        )

    project_query = session.query(Project).filter(Project.name == project_name)
    if assistant.organization_id is not None:
        project_query = project_query.filter(
            Project.organization_id == assistant.organization_id,
        )
    else:
        project_query = project_query.filter(
            Project.user_id == assistant.user_id,
            Project.organization_id.is_(None),
        )

    project = project_query.one_or_none()
    if project is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Project '{project_name}' not found for assistant '{assistant_id}'."
            ),
        )
    return project


@router.post(
    "/task-execution/current",
    response_model=TaskExecutionLookupResponse,
)
def get_current_task_execution(
    request: TaskExecutionLookupRequest,
    session=Depends(get_db_session),
    _=Depends(auth_admin_key),
):
    """Return the current open execution row for one assistant/task pair."""

    project = _get_internal_project_or_404(
        session,
        project_name=request.project_name,
        assistant_id=request.assistant_id,
    )
    execution = get_open_task_execution(
        session=session,
        project_id=project.id,
        assistant_id=request.assistant_id,
        task_id=request.task_id,
        destination=request.destination,
    )
    return {
        "execution": dict(execution.data or {}) if execution is not None else None,
    }


@router.post(
    "/task-execution/reproject",
    response_model=TaskExecutionReprojectResponse,
)
def reproject_task_execution(
    request: TaskExecutionReprojectRequest,
    session=Depends(get_db_session),
    _=Depends(auth_admin_key),
):
    """Recompute one task's projected open execution row from the current Tasks table."""

    project = _get_internal_project_or_404(
        session,
        project_name=request.project_name,
        assistant_id=request.assistant_id,
    )
    try:
        tasks_context_name = resolve_tasks_context_name(
            session=session,
            project_id=project.id,
            assistant_id=request.assistant_id,
        )
        result = sync_task_executions_for_task_ids(
            session=session,
            project_id=project.id,
            task_ids=[request.task_id],
            tasks_context_name=tasks_context_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    execution = get_open_task_execution(
        session=session,
        project_id=project.id,
        assistant_id=request.assistant_id,
        task_id=request.task_id,
    )
    return {
        **result,
        "execution": dict(execution.data or {}) if execution is not None else None,
    }


def create_or_adopt_task_execution_core(
    session,
    request: TaskExecutionCreateOrAdoptRequest,
) -> dict:
    """Create a task run by run_key if absent, otherwise return the existing row."""

    project = _get_internal_project_or_404(
        session,
        project_name=request.project_name,
        assistant_id=request.assistant_id,
    )
    payload = request.model_dump(
        exclude={"project_name"},
        exclude_none=True,
        mode="json",
    )
    run, created = create_task_run_if_absent(
        session=session,
        project_id=project.id,
        payload=payload,
    )
    return {"run": dict(run.data or {}), "created": created}


def patch_task_execution_core(session, request: TaskExecutionUpdateRequest) -> dict:
    """Apply a partial payload update to an existing task run row."""

    project = _get_internal_project_or_404(
        session,
        project_name=request.project_name,
        assistant_id=request.assistant_id,
    )
    run = update_task_run(
        session=session,
        project_id=project.id,
        assistant_id=request.assistant_id,
        run_key=request.run_key,
        updates=request.updates,
        source_task_log_id=request.source_task_log_id,
    )
    return {"run": dict(run.data or {})}


def release_active_task_source_core(
    session,
    request: TaskSourceReleaseRequest,
) -> dict:
    """Terminalize executions left running after their offline worker vanished."""

    project = _get_internal_project_or_404(
        session,
        project_name=request.project_name,
        assistant_id=request.assistant_id,
    )
    try:
        return release_stuck_task_executions(
            session=session,
            project_id=project.id,
            source_task_log_id=request.source_task_log_id,
            info=request.info,
            run_key=request.run_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def get_latest_task_execution_core(
    session,
    request: TaskExecutionLatestRequest,
) -> dict:
    """Return the most recently updated task run for one assistant/task pair."""

    project = _get_internal_project_or_404(
        session,
        project_name=request.project_name,
        assistant_id=request.assistant_id,
    )
    run = get_latest_task_execution_for_task(
        session=session,
        project_id=project.id,
        assistant_id=request.assistant_id,
        task_id=request.task_id,
        source_task_log_id=request.source_task_log_id,
    )
    return {"run": dict(run.data or {}) if run is not None else None}


def get_task_execution_core(session, request: TaskExecutionGetRequest) -> dict:
    """Return one task run row by run_key without creating or adopting."""

    project = _get_internal_project_or_404(
        session,
        project_name=request.project_name,
        assistant_id=request.assistant_id,
    )
    run = get_task_execution(
        session=session,
        project_id=project.id,
        run_key=request.run_key,
        assistant_id=request.assistant_id,
        source_task_log_id=request.source_task_log_id,
    )
    return {"run": dict(run.data or {}) if run is not None else None}


def create_or_adopt_task_outbound_operation_core(
    session,
    request: TaskOutboundOperationCreateOrAdoptRequest,
) -> dict:
    """Create an outbound operation by operation_key if absent, otherwise adopt it."""

    project = _get_internal_project_or_404(
        session,
        project_name=request.project_name,
        assistant_id=request.assistant_id,
    )
    payload = request.model_dump(
        exclude={"project_name"},
        exclude_none=True,
        mode="json",
    )
    operation, created = create_task_outbound_operation_if_absent(
        session=session,
        project_id=project.id,
        payload=payload,
    )
    return {"operation": dict(operation.data or {}), "created": created}


def patch_task_outbound_operation_core(
    session,
    request: TaskOutboundOperationUpdateRequest,
) -> dict:
    """Apply a partial payload update to an existing outbound operation row."""

    project = _get_internal_project_or_404(
        session,
        project_name=request.project_name,
        assistant_id=request.assistant_id,
    )
    try:
        operation = update_task_outbound_operation(
            session=session,
            project_id=project.id,
            assistant_id=request.assistant_id,
            operation_key=request.operation_key,
            updates=request.updates,
            source_task_log_id=request.source_task_log_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"operation": dict(operation.data or {})}


@router.post(
    "/task-execution/create-or-adopt",
    response_model=TaskExecutionMutationResponse,
)
def create_or_adopt_task_execution(
    request: TaskExecutionCreateOrAdoptRequest,
    session=Depends(get_db_session),
    _=Depends(auth_admin_key),
):
    """Create a task run by run_key if absent, otherwise return the existing row."""

    return create_or_adopt_task_execution_core(session, request)


@router.post(
    "/task-execution/update",
    response_model=TaskExecutionMutationResponse,
)
def patch_task_execution(
    request: TaskExecutionUpdateRequest,
    session=Depends(get_db_session),
    _=Depends(auth_admin_key),
):
    """Apply a partial payload update to an existing task run row."""

    return patch_task_execution_core(session, request)


@router.post(
    "/task-source/release-active",
    response_model=TaskSourceReleaseResponse,
)
def release_active_task_source_route(
    request: TaskSourceReleaseRequest,
    session=Depends(get_db_session),
    _=Depends(auth_admin_key),
):
    """Release a Tasks row left active after its offline worker vanished."""

    return release_active_task_source_core(session, request)


@router.post(
    "/task-execution/latest",
    response_model=TaskExecutionLatestResponse,
)
def get_latest_task_execution(
    request: TaskExecutionLatestRequest,
    session=Depends(get_db_session),
    _=Depends(auth_admin_key),
):
    """Return the most recently updated task run for one assistant/task pair."""

    return get_latest_task_execution_core(session, request)


@router.post(
    "/task-execution/get",
    response_model=TaskExecutionGetResponse,
)
def get_task_execution_by_key(
    request: TaskExecutionGetRequest,
    session=Depends(get_db_session),
    _=Depends(auth_admin_key),
):
    """Return one task run row by run_key without creating or adopting."""

    return get_task_execution_core(session, request)


@router.post(
    "/task-supervisor/sweep",
    response_model=TaskSupervisorSweepResponse,
)
def trigger_task_supervisor_sweep(
    session=Depends(get_db_session),
    _=Depends(auth_admin_key),
):
    """Re-project the open head for every enabled, armed task definition.

    The floor under recurrence: projection normally rides the run-start
    transition, so a series only loses its head if that transition never
    happened. Such a series is advanced from its repeat rule; healthy
    series are untouched. The sweep commits each surface as it heals it —
    see ``orchestra.routines.task_supervisor_sweep``.

    A pass that failed across most of the fleet answers 5xx, because the
    caller here is Cloud Scheduler and a 200 is the only thing it records.
    Answering 200 with the failures in the body is how a sweep repaired
    nothing for two days while its schedule reported success every fifteen
    minutes. One broken tenant still answers 200: a job that goes red for
    that gets ignored, which costs more than it saves.
    """

    result = sweep_task_supervision(session)
    payload = result.to_dict()
    if result.status == "broken":
        raise HTTPException(status_code=500, detail=payload)
    return payload


@router.post(
    "/task-outbound-operation/create-or-adopt",
    response_model=TaskOutboundOperationMutationResponse,
)
def create_or_adopt_task_outbound_operation(
    request: TaskOutboundOperationCreateOrAdoptRequest,
    session=Depends(get_db_session),
    _=Depends(auth_admin_key),
):
    """Create an outbound operation by operation_key if absent, otherwise adopt it."""

    return create_or_adopt_task_outbound_operation_core(session, request)


@router.post(
    "/task-outbound-operation/update",
    response_model=TaskOutboundOperationMutationResponse,
)
def patch_task_outbound_operation(
    request: TaskOutboundOperationUpdateRequest,
    session=Depends(get_db_session),
    _=Depends(auth_admin_key),
):
    """Apply a partial payload update to an existing outbound operation row."""

    return patch_task_outbound_operation_core(session, request)
