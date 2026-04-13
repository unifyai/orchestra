"""Task-machine admin routes exposed under the log admin API."""

from fastapi import APIRouter, Depends, HTTPException

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Project
from orchestra.services.task_machine_state_service import (
    create_task_run_if_absent,
    get_task_activation,
    update_task_run,
)
from orchestra.web.api.dependencies import auth_admin_key
from orchestra.web.api.log.schema import (
    TaskActivationLookupRequest,
    TaskActivationLookupResponse,
    TaskRunCreateOrAdoptRequest,
    TaskRunMutationResponse,
    TaskRunUpdateRequest,
)

router = APIRouter()


def _get_internal_project_or_404(session, project_name: str) -> Project:
    """Resolve an internal project by name for admin-only task-machine endpoints."""

    project = (
        session.query(Project)
        .filter(Project.name == project_name)
        .order_by(Project.id.asc())
        .first()
    )
    if project is None:
        raise HTTPException(
            status_code=404,
            detail=f"Project '{project_name}' not found.",
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

    project = _get_internal_project_or_404(session, request.project_name)
    activation = get_task_activation(
        session=session,
        project_id=project.id,
        assistant_id=request.assistant_id,
        task_id=request.task_id,
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

    project = _get_internal_project_or_404(session, request.project_name)
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

    project = _get_internal_project_or_404(session, request.project_name)
    run = update_task_run(
        session=session,
        project_id=project.id,
        run_key=request.run_key,
        updates=request.updates,
    )
    return {"run": dict(run.data or {})}
