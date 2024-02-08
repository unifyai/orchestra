import asyncio
import base64
import json
import re

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.param_functions import Depends
from fastapi.responses import JSONResponse, StreamingResponse
from litellm.utils import Usage
from models.imagegen import ImagegenModel
from models.llm import CompletionsModel

from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.model_dao import ModelDAO
from orchestra.db.dao.provider_dao import ProviderDAO
from orchestra.db.dao.query_dao import QueryDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.web.api.chat_completion.schema import ChatCompletionResponse
from orchestra.web.api.inference.schema import InferenceRequest
from orchestra.web.api.users.views import get_credits
from orchestra.web.api.utils import db_operations

router = APIRouter()


def get_model_type(model_name):  # noqa: D103
    chat_models = re.compile(
        r"(gpt|llama|zephyr|mistral|mixtral|pplx|falcon|wizard|mpt|claude|yi|chronos|alpaca|nous|hermes|platypus|pythia|qwen|redpajama)",  # noqa: WPS360, E501
    )
    image_models = re.compile(r"diffusion|sd")  # noqa: WPS360

    if chat_models.search(model_name):
        return "chat"
    elif image_models.search(model_name):
        return "image"


@router.post("/inference")
async def get_inference(  # noqa: C901, WPS212, WPS210, WPS231, E501, WPS211, WPS217, WPS238
    background_tasks: BackgroundTasks,
    request_fastapi: Request,
    request: InferenceRequest,
    users_dao: UsersDAO = Depends(),
    model_dao: ModelDAO = Depends(),
    provider_dao: ProviderDAO = Depends(),
    endpoint_dao: EndpointDAO = Depends(),
    query_dao: QueryDAO = Depends(),
):
    """
    Get inference result based on the request.

    :param background_tasks: FastAPI background tasks.
    :param request_fastapi: FastAPI request object.
    :param request: InferenceRequest object.
    :param users_dao: DAO for users models.
    :param model_dao: DAO for model models.
    :param provider_dao: DAO for provider models.
    :param endpoint_dao: DAO for endpoint models.
    :param query_dao: DAO for query models.

    :return: Model-specific JSONResponse object.

    :raises HTTPException: when user has insufficient credits.
    """
    # TODO: Add error 422 for incorrect arguments, model, or provider
    # TODO: Add error 500
    # TODO: Create a separate function and endpoint for updating credits
    user_id = request_fastapi.state.user_id
    user = await get_credits(request_fastapi, users_dao=users_dao)
    available_credits = float(user.credits if user else 0)

    try:
        model = request.model
        provider = request.provider
    except Exception:
        raise HTTPException(
            status_code=400,  # noqa: WPS432
            detail="Invalid input. Model or provider not in input.",
        )

    model_type = get_model_type(model)
    if model_type == "chat":
        language_model = CompletionsModel(
            provider=provider,
            model=model,
        )

        cost_max = language_model.get_cost_max()
        if available_credits < cost_max:
            raise HTTPException(
                status_code=402,  # noqa: WPS432
                detail="Insufficient credits",
            )
        stream = request.arguments.get("stream", False)
        response, cost = language_model.get_completion(
            messages=request.arguments["messages"],
            temperature=request.arguments.get("temperature", 0.9),  # noqa: WPS432
            max_tokens=request.arguments.get("max_tokens", None),
            stream=stream,
        )

        if not response:
            # TODO: Handle when response is None
            return JSONResponse(
                {
                    "model": model,
                    "provider": provider,
                    "created": 0,
                    "id": "",
                    "choices": [],
                    "object": "chat.completion",
                    "usage": {},
                },
            )

        if stream:

            async def stream_and_update_db():  # noqa: WPS430
                async for part_dict in response.generator():
                    part_dict["model"] = model
                    part_dict["provider"] = provider
                    yield f"data: {json.dumps(part_dict)}\n\n"
                    await asyncio.sleep(0)
                await users_dao.recharge_credit(user_id, -response.total_cost)
                background_tasks.add_task(
                    db_operations,
                    user_id,
                    response.total_cost,
                    model,
                    provider,
                    model_dao,
                    provider_dao,
                    endpoint_dao,
                    query_dao,
                )

            return StreamingResponse(stream_and_update_db())
        else:
            await users_dao.recharge_credit(user_id, -cost)
            background_tasks.add_task(
                db_operations,
                user_id,
                cost,
                model,
                provider,
                model_dao,
                provider_dao,
                endpoint_dao,
                query_dao,
            )

        if isinstance(response, ChatCompletionResponse):
            response = response.model_dump()
            response["model"] = model
            response["provider"] = provider
            return JSONResponse(
                response,
            )

        if isinstance(response["usage"], Usage):
            usage = response["usage"].model_dump()
        else:
            usage = response["usage"]

        choices = [
            getattr(choice, "model_dump", lambda: None)()
            for choice in response.get("choices", [])
        ]

        return JSONResponse(
            {
                "model": model,
                "provider": provider,
                "created": response.get("created", None),
                "id": response["id"],
                "choices": choices,
                "object": "chat.completion",
                "usage": usage,
            },
        )
    elif model_type == "image":
        image_model = ImagegenModel(
            provider=provider,
            model=model,
        )
        kwargs = {
            "init_image": request.arguments.get("init_image", None),
            "height": request.arguments.get("height", None),
            "width": request.arguments.get("width", None),
            "steps": request.arguments.get("steps", None),
            "samples": request.arguments.get("samples", None),
            "cfg_scale": request.arguments.get("cfg_scale", None),
            "sampler": request.arguments.get("sampler", None),
            "seed": request.arguments.get("seed", None),
            "mask_image": request.arguments.get("mask_image", None),
            "strength": request.arguments.get("strength", None),
            "use_refiner": request.arguments.get("use_refiner", False),
            "high_noise_frac": request.arguments.get("high_noise_frac", None),
            "checkpoint": request.arguments.get("checkpoint", None),
            "loras": request.arguments.get("loras", None),
            "textual_inversions": request.arguments.get("textual_inversions", None),
        }
        response = image_model.get_image(
            prompt=request.arguments["prompt"],
            kwargs=kwargs,
        )
        if not response:
            # TODO: Handle when response is None
            return JSONResponse(
                {
                    "model": model,
                    "created": 0,
                    "images": [],
                    "object": "image.generations",
                },
            )
        base64_images = [
            base64.b64encode(image).decode("utf-8")
            for image in response.get("images", [])  # Use empty list for default
            if image is not None
        ]
        return JSONResponse(
            {
                "model": model,
                "created": response.get("created", None),
                "images": base64_images,
                "object": "image.generation",
            },
        )
    # TODO: Add error 422 for incorrect arguments, model, or provider
    return JSONResponse(
        {
            "Error": "Unknown model or provider",
        },
    )
