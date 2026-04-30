"""Task-machine admin routes exposed under the log admin API."""

from fastapi import APIRouter, Depends, HTTPException

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Assistant, Project
from orchestra.services.task_machine_state_service import (
    create_task_outbound_operation_if_absent,
    create_task_run_if_absent,
    get_task_activation,
    update_task_outbound_operation,
    update_task_run,
)
from orchestra.web.api.dependencies import auth_admin_key
from orchestra.web.api.log.schema import (
    TaskActivationLookupRequest,
    TaskActivationLookupResponse,
    TaskOutboundOperationCreateOrAdoptRequest,
    TaskOutboundOperationMutationResponse,
    TaskOutboundOperationUpdateRequest,
    TaskRunCreateOrAdoptRequest,
    TaskRunMutationResponse,
    TaskRunUpdateRequest,
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
    "/task-activation/current",
    response_model=TaskActivationLookupResponse,
)
def get_current_task_activation(
    request: TaskActivationLookupRequest,
    session=Depends(get_db_session),
    _=Depends(auth_admin_key),
):
    """Return the current projected activation row for one assistant/task pair."""

    project = _get_internal_project_or_404(
        session,
        project_name=request.project_name,
        assistant_id=request.assistant_id,
    )
    activation = get_task_activation(
        session=session,
        project_id=project.id,
        assistant_id=request.assistant_id,
        task_id=request.task_id,
        destination=request.destination,
    )
    return {
        "activation": dict(activation.data or {}) if activation is not None else None,
    }


@router.post(
    "/task-run/create-or-adopt",
    response_model=TaskRunMutationResponse,
)
def create_or_adopt_task_run(
    request: TaskRunCreateOrAdoptRequest,
    session=Depends(get_db_session),
    _=Depends(auth_admin_key),
):
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


@router.post(
    "/task-run/update",
    response_model=TaskRunMutationResponse,
)
def patch_task_run(
    request: TaskRunUpdateRequest,
    session=Depends(get_db_session),
    _=Depends(auth_admin_key),
):
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
    )
    return {"run": dict(run.data or {})}


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
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"operation": dict(operation.data or {})}
