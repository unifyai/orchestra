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

    Org reads are role-based: any member whose org role grants
    ``assistant:read`` may access non-coordinator org assistants, matching
    the assistant list surface. This matters because every org assistant
    carries a bootstrap Owner grant for its creator, which flips the
    per-resource RBAC check into explicit-grants-only mode; without the
    role fallback, reads would be denied to every other member. Writes
    stay strictly per-resource. Org coordinators are per-user twins and
    404 for everyone except their own user.

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
        if assistant.is_coordinator and assistant.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )
        # The assistant's own user always passes: the runtime pod
        # authenticates with the creator's key, and org RBAC must never
        # block an assistant acting as itself (org coordinators carry no
        # bootstrap resource grant, and a Viewer-role creator's role lacks
        # assistant:write).
        if assistant.user_id == user_id:
            return assistant
        ra_dao = ResourceAccessDAO(session)
        permission = "assistant:write" if write else "assistant:read"
        allowed = ra_dao.check_user_permission(
            user_id,
            "assistant",
            agent_id,
            permission,
        )
        if not allowed and not write:
            allowed = ra_dao.check_org_member_permission(
                user_id,
                organization_id,
                permission,
            )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this assistant.",
            )
    return assistant
