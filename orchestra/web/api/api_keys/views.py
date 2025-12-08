"""API key management endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.api_keys.schema import ApiKeyResponse, ApiKeysListResponse

router = APIRouter()


@router.get("/api-keys", response_model=ApiKeysListResponse)
async def list_api_keys(
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
) -> ApiKeysListResponse:
    """
    List all API keys for the authenticated user.

    Returns both personal API keys and organization-specific API keys,
    grouped by organization.
    """
    user_id = request_fastapi.state.user_id
    api_key_dao = ApiKeyDAO(session)
    org_dao = OrganizationDAO(session)

    # Get personal keys
    personal_keys_rows = api_key_dao.get_personal_keys(user_id)
    personal_keys = [
        ApiKeyResponse(
            id=key[0].id,
            name=key[0].name or "Default",
            key=key[0].key,
            created_at=key[0].created_at,
            organization_id=None,
            organization_name=None,
        )
        for key in personal_keys_rows
    ]

    # Get organization keys
    org_keys_rows = api_key_dao.get_organization_keys(user_id)

    # Group by organization
    org_keys_dict = {}
    for key_row in org_keys_rows:
        key = key_row[0]
        org = org_dao.get(key.organization_id)
        if org:
            org_name = org.name
            if org_name not in org_keys_dict:
                org_keys_dict[org_name] = []

            org_keys_dict[org_name].append(
                ApiKeyResponse(
                    id=key.id,
                    name=key.name or f"org_{org_name}",
                    key=key.key,
                    created_at=key.created_at,
                    organization_id=key.organization_id,
                    organization_name=org_name,
                ),
            )

    return ApiKeysListResponse(
        personal_keys=personal_keys,
        organization_keys=org_keys_dict,
    )


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    request_fastapi: Request,
    key_id: int,
    session: Session = Depends(get_db_session),
) -> None:
    """
    Revoke (delete) an API key.

    Users can only revoke their own keys. Organization keys can only be revoked
    if the user is still a member of that organization.
    """
    user_id = request_fastapi.state.user_id
    api_key_dao = ApiKeyDAO(session)

    # Get the key
    keys = api_key_dao.filter(id=key_id)
    if not keys:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found",
        )

    key = keys[0][0]

    # Verify ownership
    if key.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only revoke your own API keys",
        )

    # Delete the key
    try:
        api_key_dao.delete(key_id)
        return None
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to revoke API key: {str(e)}",
        )
