"""Hive CRUD endpoints.

All endpoints require an organization API key (organization_id on request.state).
``org:write`` is required for create / update / delete; ``org:read`` for reads.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.hive_dao import HiveDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dependencies import get_db_session
from orchestra.services.hive_service import cascade_delete_hive
from orchestra.web.api.assistant.schema import InfoResponse
from orchestra.web.api.hives.schema import HiveCreate, HiveMember, HiveRead, HiveUpdate

router = APIRouter()

logger = logging.getLogger(__name__)


def _require_org(request: Request) -> int:
    """Return organization_id from request state, 400 if not present."""
    organization_id = getattr(request.state, "organization_id", None)
    if organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Hive endpoints require an organization API key.",
        )
    return organization_id


def _require_org_write(request: Request, session: Session) -> int:
    """Return organization_id after asserting org:write for the calling user."""
    organization_id = _require_org(request)
    user_id = request.state.user_id
    resource_access_dao = ResourceAccessDAO(session)
    if not resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:write",
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage Hives in this organization.",
        )
    return organization_id


def _require_org_read(request: Request, session: Session) -> int:
    """Return organization_id after asserting org:read for the calling user."""
    organization_id = _require_org(request)
    user_id = request.state.user_id
    resource_access_dao = ResourceAccessDAO(session)
    if not resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:read",
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view Hives in this organization.",
        )
    return organization_id


def _get_hive_for_org(hive_dao: HiveDAO, hive_id: int, organization_id: int):
    """Return the Hive, raising 404 if it doesn't exist or belongs to another org."""
    hive = hive_dao.get_by_id(hive_id)
    if hive is None or hive.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Hive not found.",
        )
    return hive


@router.post(
    "/hives",
    response_model=HiveRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a Hive",
    tags=["Hives"],
)
async def create_hive(
    body: HiveCreate,
    request: Request,
    session: Session = Depends(get_db_session),
) -> HiveRead:
    """Create a new Hive for the requesting organization.

    One Hive per organization is enforced by a DB-level unique index
    (``ux_hives_one_per_org``). A duplicate create returns 409 Conflict.
    """
    organization_id = _require_org_write(request, session)
    hive_dao = HiveDAO(session)
    try:
        hive = hive_dao.create(
            organization_id=organization_id,
            name=body.name,
            description=body.description,
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A Hive already exists for this organization.",
        )
    return HiveRead.model_validate(hive)


@router.get(
    "/hives",
    response_model=list[HiveRead],
    status_code=status.HTTP_200_OK,
    summary="List Hives",
    tags=["Hives"],
)
async def list_hives(
    request: Request,
    session: Session = Depends(get_db_session),
) -> list[HiveRead]:
    """Return all Hives that belong to the requesting organization."""
    organization_id = _require_org_read(request, session)
    hive_dao = HiveDAO(session)
    hives = hive_dao.list_for_org(organization_id)
    return [HiveRead.from_orm(h) for h in hives]


@router.get(
    "/hives/{hive_id}",
    response_model=HiveRead,
    status_code=status.HTTP_200_OK,
    summary="Get a Hive",
    tags=["Hives"],
)
async def get_hive(
    hive_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> HiveRead:
    """Return detail for a single Hive owned by the requesting organization."""
    organization_id = _require_org_read(request, session)
    hive_dao = HiveDAO(session)
    hive = _get_hive_for_org(hive_dao, hive_id, organization_id)
    return HiveRead.model_validate(hive)


@router.get(
    "/hives/{hive_id}/assistants",
    response_model=list[HiveMember],
    status_code=status.HTTP_200_OK,
    summary="List the assistants that belong to a Hive",
    tags=["Hives"],
)
async def list_hive_assistants(
    hive_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> list[HiveMember]:
    """Return the (user_id, assistant_id) pair for every Hive member.

    Unity callers use this to enumerate member bodies when they need
    to fan out per-body writes (e.g. rewriting ``ContactMembership``
    overlays after merging shared contact rows). The caller must have
    ``org:read`` on the Hive's organization.
    """
    organization_id = _require_org_read(request, session)
    hive_dao = HiveDAO(session)
    _get_hive_for_org(hive_dao, hive_id, organization_id)

    assistant_dao = AssistantDAO(session)
    members = assistant_dao.list_for_hive(
        hive_id=hive_id,
        organization_id=organization_id,
    )
    return [
        HiveMember(user_id=user_id, assistant_id=agent_id)
        for user_id, agent_id in members
    ]


@router.patch(
    "/hives/{hive_id}",
    response_model=HiveRead,
    status_code=status.HTTP_200_OK,
    summary="Update a Hive",
    tags=["Hives"],
)
async def update_hive(
    hive_id: int,
    body: HiveUpdate,
    request: Request,
    session: Session = Depends(get_db_session),
) -> HiveRead:
    """Rename a Hive or update its description.

    Returns 409 when the hive's ``status`` is ``'deleting'``.
    """
    organization_id = _require_org_write(request, session)
    hive_dao = HiveDAO(session)
    hive = _get_hive_for_org(hive_dao, hive_id, organization_id)
    if hive.status == "deleting":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Hive is currently being deleted.",
        )
    try:
        hive = hive_dao.update(hive, name=body.name, description=body.description)
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A Hive with that name already exists in this organization.",
        )
    return HiveRead.model_validate(hive)


@router.delete(
    "/hives/{hive_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete a Hive",
    tags=["Hives"],
)
async def delete_hive(
    hive_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[str]:
    """Cascade-delete a Hive and all of its member assistants.

    Phase ordering:
    1. Mark hive as ``'deleting'`` under a ``SELECT FOR UPDATE`` lock; commit.
    2. Fan out ``delete_assistant`` across each member body in parallel.
    3. Delete shared ``Hives/{hive_id}/...`` contexts via ContextDAO.
    4. Delete the hive row.

    Per-body runtime teardown (runtime health, GCS, pubsub) runs asynchronously
    on the durable ``AssistantCleanupTask`` queue and is not awaited here.
    """
    organization_id = _require_org_write(request, session)
    hive_dao = HiveDAO(session)
    hive = _get_hive_for_org(hive_dao, hive_id, organization_id)
    # Surface the hive_id so the service can re-fetch under its own sessions.
    _ = hive.hive_id

    session_factory = request.app.state.db_session_factory
    try:
        await cascade_delete_hive(
            hive_id=hive_id,
            organization_id=organization_id,
            session_factory=session_factory,
        )
    except Exception as exc:
        logger.exception("cascade_delete_hive failed for hive %d: %s", hive_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Hive deletion failed.",
        )
    return InfoResponse(info="Hive deleted successfully")
