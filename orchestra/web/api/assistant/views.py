from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.web.api.assistant.schema import (
    AssistantCreate,
    AssistantRead,
    AssistantUpdate,
)

router = APIRouter()


@router.post(
    "/assistant",
    response_model=AssistantRead,
    status_code=status.HTTP_200_OK,
    summary="Create a new assistant",
    description="Creates a new assistant for the authenticated user with the specified configuration.",
    tags=["Assistants"],
    responses={
        200: {
            "description": "Assistant created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "agent_id": "123",
                        "first_name": "Alice",
                        "surname": "Smith",
                        "age": 25,
                        "weekly_limit": 40.0,
                        "max_parallel": 3,
                        "created_at": "2025-04-25T12:00:00Z",
                        "updated_at": "2025-04-25T12:00:00Z",
                    },
                },
            },
        },
        422: {
            "description": "Validation Error",
            "content": {
                "application/json": {
                    "example": {
                        "detail": [
                            {
                                "loc": ["body", "first_name"],
                                "msg": "field required",
                                "type": "value_error.missing",
                            },
                        ],
                    },
                },
            },
        },
    },
)
def create_assistant(
    assistant_in: AssistantCreate,
    request: Request,
    dao: AssistantDAO = Depends(),
) -> AssistantRead:
    """
    Create a new assistant for the authenticated user.

    This endpoint allows users to create a personalized assistant with specific
    attributes like name, age, and operational limits. Each assistant is tied
    to the authenticated user's account.
    """
    try:
        assistant = dao.create_assistant(
            user_id=request.state.user_id,
            first_name=assistant_in.first_name,
            surname=assistant_in.surname,
            age=assistant_in.age,
            weekly_limit=Decimal(assistant_in.weekly_limit),
            max_parallel=assistant_in.max_parallel,
        )

        return AssistantRead(
            agent_id=str(assistant.agent_id),
            first_name=assistant.first_name,
            surname=assistant.surname,
            age=assistant.age,
            weekly_limit=float(assistant.weekly_limit),
            max_parallel=assistant.max_parallel,
            created_at=assistant.created_at,
            updated_at=assistant.updated_at,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error creating assistant: {str(e)}",
        )


@router.get(
    "/assistant",
    response_model=List[AssistantRead],
    status_code=status.HTTP_200_OK,
    summary="List all assistants",
    description="Returns a list of all assistants belonging to the authenticated user.",
    tags=["Assistants"],
    responses={
        200: {
            "description": "List of assistants retrieved successfully",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "agent_id": "123",
                            "first_name": "Alice",
                            "surname": "Smith",
                            "age": 25,
                            "weekly_limit": 40.0,
                            "max_parallel": 3,
                            "created_at": "2025-04-25T12:00:00Z",
                            "updated_at": "2025-04-25T12:00:00Z",
                        },
                        {
                            "agent_id": "456",
                            "first_name": "Bob",
                            "surname": "Jones",
                            "age": 30,
                            "weekly_limit": 35.5,
                            "max_parallel": 2,
                            "created_at": "2025-04-24T10:30:00Z",
                            "updated_at": "2025-04-24T10:30:00Z",
                        },
                    ],
                },
            },
        },
    },
)
def list_assistants(
    request: Request,
    dao: AssistantDAO = Depends(),
) -> List[AssistantRead]:
    """
    List all assistants for the authenticated user.

    Retrieves all assistants created by the current user, including their
    configuration details and operational limits.
    """
    try:
        assistants = dao.list_assistants_for_user(request.state.user_id)
        return [
            AssistantRead(
                agent_id=str(a.agent_id),
                first_name=a.first_name,
                surname=a.surname,
                age=a.age,
                weekly_limit=float(a.weekly_limit),
                max_parallel=a.max_parallel,
                created_at=a.created_at,
                updated_at=a.updated_at,
            )
            for a in assistants
        ]
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error fetching assistants: {str(e)}",
        )


@router.delete(
    "/assistant/{assistant_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete an assistant",
    description="Deletes a specific assistant by ID for the authenticated user.",
    tags=["Assistants"],
    responses={
        200: {
            "description": "Assistant deleted successfully",
            "content": {
                "application/json": {
                    "example": {"info": "Assistant deleted successfully"},
                },
            },
        },
        404: {
            "description": "Assistant Not Found",
            "content": {
                "application/json": {"example": {"detail": "Assistant not found."}},
            },
        },
    },
)
def delete_assistant(
    assistant_id: int,
    request: Request,
    dao: AssistantDAO = Depends(),
) -> Response:
    """
    Delete an assistant by ID for the authenticated user.

    Permanently removes the specified assistant from the user's account.
    This action cannot be undone.
    """
    try:
        dao.delete_assistant(user_id=request.state.user_id, agent_id=assistant_id)
        return Response(
            content="Assistant deleted successfully",
            status_code=status.HTTP_200_OK,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )


@router.patch(
    "/assistant/{assistant_id}/config",
    response_model=AssistantRead,
    status_code=status.HTTP_200_OK,
    summary="Update assistant configuration",
    description="Updates the configuration parameters of an existing assistant.",
    tags=["Assistants"],
    responses={
        200: {
            "description": "Assistant configuration updated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "agent_id": "123",
                        "first_name": "Alice",
                        "surname": "Smith",
                        "age": 25,
                        "weekly_limit": 45.0,
                        "max_parallel": 4,
                        "created_at": "2025-04-25T12:00:00Z",
                        "updated_at": "2025-04-25T14:30:00Z",
                    },
                },
            },
        },
        404: {
            "description": "Assistant Not Found",
            "content": {
                "application/json": {"example": {"detail": "Assistant not found."}},
            },
        },
        422: {
            "description": "Validation Error",
            "content": {
                "application/json": {
                    "example": {
                        "detail": [
                            {
                                "loc": ["body", "weekly_limit"],
                                "msg": "value is not a valid float",
                                "type": "type_error.float",
                            },
                        ],
                    },
                },
            },
        },
    },
)
def update_assistant_config(
    assistant_id: int,
    update: AssistantUpdate,
    request: Request,
    dao: AssistantDAO = Depends(),
) -> AssistantRead:
    """
    Update weekly_limit and/or max_parallel for an existing assistant.

    Allows partial updates to an assistant's configuration. Only the fields
    provided in the request will be updated, while others remain unchanged.
    """
    try:
        weekly_limit: Optional[Decimal] = None
        if update.weekly_limit is not None:
            weekly_limit = Decimal(update.weekly_limit)

        updated = dao.update_assistant_config(
            user_id=request.state.user_id,
            agent_id=assistant_id,
            weekly_limit=weekly_limit,
            max_parallel=update.max_parallel,
        )
        if not updated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )
        return AssistantRead(
            agent_id=str(updated.agent_id),
            first_name=updated.first_name,
            surname=updated.surname,
            age=updated.age,
            weekly_limit=float(updated.weekly_limit),
            max_parallel=updated.max_parallel,
            created_at=updated.created_at,
            updated_at=updated.updated_at,
        )
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error updating assistant config: {str(e)}",
        )
