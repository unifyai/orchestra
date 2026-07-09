"""Ownership guard for user-API-key access to assistant-scoped runtime routes.

Assistant pods historically called ``/admin/*`` routes with the shared
``ORCHESTRA_ADMIN_KEY``. The user-scoped equivalents authenticate with an
ordinary user API key instead, so every handler must verify that the caller
actually owns (or can access) the target assistant. This module centralizes
that check so all runtime routes enforce the same rules.
"""

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.models.orchestra_models import Assistant


def require_owned_assistant(
    request: Request,
    agent_id: int,
    session: Session,
    *,
    write: bool = False,
) -> Assistant:
    """Load ``agent_id`` and enforce that the authenticated caller may use it.

    System (admin-key) callers bypass ownership entirely: the assistant is
    loaded by id alone, 404 when missing. User-key callers must own the
    assistant (personal scope) or belong to its organization with the
    appropriate RBAC permission (org scope); otherwise 404/403.

    :param request: FastAPI request carrying the auth state set by
        ``auth_api_key``.
    :param agent_id: Target assistant agent id.
    :param session: Database session.
    :param write: Require ``assistant:write`` instead of ``assistant:read``
        for org-scoped RBAC checks.
    :return: The resolved :class:`Assistant`.
    :raises HTTPException: 404 when the assistant is not visible to the
        caller, 403 when org RBAC denies the operation.
    """
    assistant_dao = AssistantDAO(session)

    if getattr(request.state, "is_system_api_key", False):
        assistant = assistant_dao.get_assistant_by_agent_id(agent_id)
        if not assistant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )
        return assistant

    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=agent_id,
        organization_id=organization_id,
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )
    if organization_id is not None:
        ra_dao = ResourceAccessDAO(session)
        permission = "assistant:write" if write else "assistant:read"
        if not ra_dao.check_user_permission(
            user_id,
            "assistant",
            agent_id,
            permission,
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this assistant.",
            )
    return assistant
