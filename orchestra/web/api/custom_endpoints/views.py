from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.param_functions import Depends

from orchestra.db.dao.custom_api_key_dao import CustomApiKeyDAO
from orchestra.db.dao.custom_endpoint_dao import CustomEndpointDAO
from orchestra.web.api.utils.http_responses import custom_endpoint_not_found

router = APIRouter()


@router.put(
    "/custom_endpoint",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Custom endpoint created successfully!"},
                },
            },
        },
        404: {
            "description": "Custom API Key Not Found",
            "content": {
                "application/json": {
                    "example": {"detail": "Custom API Key not found."},
                },
            },
        },
    },
)
def create_custom_endpoint(
    request_fastapi: Request,
    name: str = Query(
        description="Alias for the custom endpoint."
                    "This will be the name used to call the endpoint.",
        example="endpoint1",
    ),
    url: str = Query(
        description="Base URL of the endpoint being called. "
                    "Must support the OpenAI format.",
        example="https://api.url1.com",
    ),
    key_name: str = Query(
        description="Name of the API key that will be passed as part of the query.",
        example="key1",
    ),
    model_name: str = Query(
        None,
        description=(
            "Named passed to the custom endpoint as model name. "
            "If not specified, it will default to the endpoint alias."
        ),
        example="llama-3.1-8b-finetuned",
    ),
    provider: str = Query(
        None,
        description=(
            "The provider used, if a fine-tuned model was trained directly via one of "
            "the supported providers. The unification logic will be used for this "
            "custom fine-tuned model behind the scenes, in the same manner as used for "
            "the foundation models with the same provider."
        ),
        example="llama-3.1-8b-finetuned",
    ),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
    custom_api_key_dao: CustomApiKeyDAO = Depends(),
) -> None:
    """
    Creates a custom endpoint. This endpoint must either be a fine-tuned model from one
    of the supported providers (`/v0/providers`), in which case the "provider" argument
    must be set accordingly. Otherwise, the endpoint must support the OpenAI
    `/chat/completions` format. To query your custom endpoint, replace your endpoint
    string with `<name>@custom` when querying any general custom endpoint. You can show
    all *custom* endpoints by querying `/v0/endpoints` and passing `custom` as the
    `provider` argument.

    """
    # ToDo: add support for provider argument
    user_id = request_fastapi.state.user_id
    try:
        key_id = custom_api_key_dao.filter(user_id=user_id, key=key_name)[0].id
    except Exception:
        raise HTTPException(status_code=404, detail="Custom API Key not found.")

    custom_endpoint_dao.create_custom_endpoint(
        user_id=user_id,
        name=name,
        mdl_name=model_name if model_name else name,
        url=url,
        key_id=key_id,
    )
    return {"info": "Custom endpoint created successfully!"}


@router.delete(
    "/custom_endpoint",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Custom endpoint deleted successfully!"},
                },
            },
        },
    },
)
def delete_custom_endpoint(
    request_fastapi: Request,
    name: str = Query(
        description="Name of the custom endpoint to delete.",
        example="endpoint1",
    ),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
) -> None:
    """
    Deletes a custom endpoint in your account.

    """
    user_id = request_fastapi.state.user_id

    existing_endpoint = custom_endpoint_dao.filter(user_id=user_id, name=name)
    if not existing_endpoint:
        raise custom_endpoint_not_found

    custom_endpoint_dao.delete(
        user_id=user_id,
        name=name,
    )
    return {"info": "Custom endpoint deleted successfully!"}


@router.post(
    "/custom_endpoint/rename",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Custom endpoint renamed successfully!"},
                },
            },
        },
        404: {
            "description": "Custom endpoint Not Found",
            "content": {
                "application/json": {
                    "example": {"detail": "Custom endpoint not found."},
                },
            },
        },
    },
)
def rename_custom_endpoint(
    request_fastapi: Request,
    name: str = Query(
        description="Name of the custom endpoint to be updated.",
        example="name1",
    ),
    new_name: str = Query(
        description="New name for the custom endpoint.",
        example="name2",
    ),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
) -> None:
    """
    Renames a custom endpoint in your account.

    """
    user_id = request_fastapi.state.user_id

    existing_endpoint = custom_endpoint_dao.filter(user_id=user_id, name=name)
    if not existing_endpoint:
        raise custom_endpoint_not_found

    custom_endpoint_dao.rename(
        user_id=user_id,
        name=name,
        new_name=new_name,
    )
    return {"info": "Custom endpoint renamed successfully!"}
