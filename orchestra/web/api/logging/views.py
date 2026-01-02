"""
Includes endpoints related to logging.
"""

import json
import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional, Union

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.param_functions import Depends
from providers.completion import PROVIDER_CLASSES
from sqlalchemy.orm import Session

from orchestra.db.dao.custom_endpoint_dao import CustomEndpointDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.local_endpoint_dao import LocalEndpointDAO
from orchestra.db.dao.query_dao import QueryDAO
from orchestra.db.dao.tag_dao import TagDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.utils.http_responses import not_found

router = APIRouter()


@router.get("/tags")
def get_query_tags(
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
) -> list[str]:
    tag_dao = TagDAO(session)
    """Returns a list of the tags in your account"""
    return tag_dao.get_all_tags(request_fastapi.state.user_id)


@router.get("/queries")
def get_queries(
    request_fastapi: Request,
    tags: Union[None, str, list[str]] = Query(
        default=None,
        description="Tags to filter for queries that are marked with these tags.",
        example="my_tag",
    ),
    endpoints: Union[None, str, list[str]] = Query(
        default=None,
        description="Optionally specify an endpoint, or a list of endpoints to filter for",
        example="gpt-4o@openai",
    ),
    start_time: Optional[str] = Query(
        None,
        description="Timestamp of the earliest query to aggregate. "
        "Format is `YYYY-MM-DD hh:mm:ss`.",
        example="2024-07-12 04:20:32",
    ),
    end_time: Optional[str] = Query(
        None,
        description="Timestamp of the latest query to aggregate. "
        "Format is `YYYY-MM-DD hh:mm:ss`.",
        example="2024-08-12 04:20:32",
    ),
    page_number: Optional[int] = Query(
        1,
        description="The query history is returned in pages, with up to 20 prompts per page. Increase the page number to see older prompts.",
        example="1",
    ),
    failures: Union[bool, Literal["only"]] = Query(
        False,
        description="indicates whether to includes failures in the return (when set as True ), or whether to return failures exlusively (when set as 'only').",
        example=False,
    ),
    session=Depends(get_db_session),
):
    query_dao = QueryDAO(session)
    endpoint_dao = EndpointDAO(session)
    custom_endpoint_dao = CustomEndpointDAO(session)
    local_endpoint_dao = LocalEndpointDAO(session)
    """
    Get the queries history, optionally for a given set of tags for a narrowed search.
    """
    if tags and isinstance(tags, str):
        tags = [tags]
    if endpoints and isinstance(endpoints, str):
        endpoints = [endpoints]

    global_endpoint_ids = []
    custom_endpoint_ids = []
    local_endpoint_ids = []
    if endpoints:
        for e_str in endpoints:
            try:
                _model, _provider = e_str.split("@")
                if _provider == "external":
                    id_ = local_endpoint_dao.filter(
                        user_id=request_fastapi.state.user_id,
                        name=_model,
                    )[0].id
                    local_endpoint_ids.append(id_)
                elif _provider == "custom":
                    _id = custom_endpoint_dao.filter(
                        user_id=request_fastapi.state.user_id,
                        name=_model,
                    )[0].id
                    custom_endpoint_ids.append(_id)
                else:
                    _id = endpoint_dao.get_endpoints_of(
                        models=[_model],
                        only_from=[_provider],
                    )[0][0].id
                    global_endpoint_ids.append(_id)
            except Exception as e:
                print(e)
                raise not_found(f"Endpoint")

    LIMIT = 20
    if page_number < 1:
        raise HTTPException(
            status_code=400,
            detail=f"Page number: {page_number} must be at least 1.",
        )
    offset = (page_number - 1) * LIMIT
    ret, count = query_dao.filter(
        user_id=request_fastapi.state.user_id,
        tags=tags,
        endpoint_ids=global_endpoint_ids,
        custom_endpoint_ids=custom_endpoint_ids,
        local_endpoint_ids=local_endpoint_ids,
        start_time=start_time,
        end_time=end_time,
        limit=LIMIT,
        offset=offset,
        status_code=200 if failures == False else (400 if failures == "only" else None),
    )
    return {"queries": ret, "total_pages": math.ceil(count / LIMIT)} if ret else ret


@router.post("/queries")
def log_query(
    request_fastapi: Request,
    endpoint: str = Body(
        description="Endpoint to log query for.",
        json_schema_extra={"example": "llama-3.1-8b-chat_ollama@external"},
    ),
    query_body: dict = Body(
        description="A JSON object containing the body of the request",
        json_schema_extra={
            "example": {
                "messages": [
                    {"role": "system", "content": "You are an useful assistant"},
                    {"role": "user", "content": "Explain who Newton was."},
                ],
                "model": "llama-3.1-8b-chat_ollama@external",
                "max_tokens": 100,
                "temperature": 0.5,
            },
        },
    ),
    response_body: Optional[Dict[str, Any]] = Body(
        None,
        description="An optional JSON object containing the response to the request",
        json_schema_extra={
            "example": {
                "model": "meta.llama3-8b-instruct-v1:0",
                "created": 1725396241,
                "id": "chatcmpl-92d3b36e-7b64-4ae8-8102-9b7e3f5dd30f",
                "object": "chat.completion",
                "usage": {
                    "completion_tokens": 100,
                    "prompt_tokens": 44,
                    "total_tokens": 144,
                },
                "choices": [
                    {
                        "finish_reason": "stop",
                        "index": 0,
                        "message": {
                            "content": "Sir Isaac Newton was an English mathematician, physicist, and astronomer who lived from 1643 to 1727.\\n\\nHe is widely recognized as one of the most influential scientists in history, and his work laid the foundation for the Scientific Revolution of the 17th century.\\n\\nNewton's most famous achievement is his theory of universal gravitation, which he presented in his groundbreaking book \"Philosophi\\u00e6 Naturalis Principia Mathematica\" in 1687.\\n\\nAccording to Newton's theory, every",
                            "role": "assistant",
                        },
                    },
                ],
            },
        },
    ),
    tags: Optional[list[str]] = Body(None, description="Tags for later filtering."),
    timestamp: Optional[str] = Body(
        None,
        description="A timestamp (if not set, will be the time of sending)",
        json_schema_extra={"example": "2024-07-12T04:20:32.808410"},
    ),
    consume_credits: bool = Body(
        False,
        description="Whether to consume user credits for this query. Default is False for local model logging.",
    ),
    session=Depends(get_db_session),
):
    # Validate that response_body is provided when consume_credits=True
    if consume_credits and not response_body:
        raise HTTPException(
            status_code=400,
            detail="response_body is required when consume_credits=True",
        )

    # Validate endpoint format (must be model@provider)
    if "@" not in endpoint:
        raise HTTPException(
            status_code=400,
            detail="endpoint must be in format 'model@provider'",
        )

    try:
        _model_name, _provider_name = endpoint.split("@", 1)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="endpoint must be in format 'model@provider'",
        )

    # Validate provider exists when consuming credits
    if consume_credits and _provider_name not in PROVIDER_CLASSES:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported provider '{_provider_name}'. Supported providers: {list(PROVIDER_CLASSES.keys())}",
        )

    # Validate usage information when consuming credits
    if consume_credits:
        usage = response_body.get("usage", {})
        if not usage:
            raise HTTPException(
                status_code=400,
                detail="response_body must contain 'usage' field when consume_credits=True",
            )

        if "prompt_tokens" not in usage or "completion_tokens" not in usage:
            raise HTTPException(
                status_code=400,
                detail="response_body.usage must contain 'prompt_tokens' and 'completion_tokens' fields",
            )

    query_dao = QueryDAO(session)
    local_endpoint_dao = LocalEndpointDAO(session)
    users_dao = UsersDAO(session)
    if not timestamp:
        timestamp = str(datetime.now(timezone.utc))
    local_endpoint_id = local_endpoint_dao.get_or_create_local_endpoint(
        user_id=request_fastapi.state.user_id,
        name=_model_name,
    )

    # Get organization context from request state (None = personal query)
    organization_id = getattr(request_fastapi.state, "organization_id", None)

    # Calculate cost and consume credits if requested
    cost = 0.0
    if consume_credits and not os.environ.get("ON_PREM"):
        usage = response_body.get("usage", {})

        # Try to get cost directly from usage
        if "cost" in usage:
            cost = float(usage["cost"])
        else:
            # Use provider's cost calculation
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)

            # Instantiate the provider for cost calculation
            provider = PROVIDER_CLASSES[_provider_name](
                _model_name,
                custom_endpoint=None,
                custom_api_key=None,
            )
            cost = provider.get_response_cost(
                response=response_body,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                using_litellm=bool(provider.litellm_provider_prefix),
            )

        # Deduct credits from the appropriate billing user
        if cost > 0:
            from orchestra.lib.billing import get_billing_user_id

            billing_user_id = get_billing_user_id(
                session=session,
                user_id=request_fastapi.state.user_id,
                organization_id=organization_id,
            )
            users_dao.recharge_credit(billing_user_id, -cost)

    try:
        query_dao.create_query(
            user_id=request_fastapi.state.user_id,
            at=timestamp,
            model_provider_str=endpoint,
            endpoint_id=None,
            custom_endpoint_id=None,
            local_endpoint_id=local_endpoint_id,
            credits=cost,
            query_body=json.dumps(query_body),
            response_body=json.dumps(response_body),
            status_code=200,
            organization_id=organization_id,
            tags=tags,
        )
        return {"info": "Query logged successfully"}
    except Exception as e:
        print(e)
        raise HTTPException(status_code=400, detail="Error in logging query")
