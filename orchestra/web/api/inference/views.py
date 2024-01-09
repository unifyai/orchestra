import base64
import re
from typing import Union

from fastapi import APIRouter, Request
from fastapi.param_functions import Depends
from fastapi.responses import StreamingResponse
from models.imagegen import ImagegenModel
from models.llm import CompletionsModel

from orchestra.db.dao.users_dao import UsersDAO
from orchestra.web.api.chat_completion.schema import ChatCompletionResponse
from orchestra.web.api.inference.schema import InferenceRequest, InferenceResponse
from orchestra.web.api.users.views import get_credits

router = APIRouter()


def get_model_type(model_name):  # noqa: D103
    chat_models = re.compile(
        r"(gpt|llama|zephyr|mistral|mixtral|pplx|falcon|wizard|mpt|claude)",  # noqa: WPS360, E501
    )
    image_models = re.compile(r"diffusion|sd")  # noqa: WPS360

    if chat_models.search(model_name):
        return "chat"
    elif image_models.search(model_name):
        return "image"


@router.post("/inference", response_model=InferenceResponse)
async def get_inference(  # noqa: C901, WPS212, WPS210, WPS231, E501
    request_fastapi: Request,
    request: InferenceRequest,
    users_dao: UsersDAO = Depends(),
) -> Union[InferenceResponse, StreamingResponse]:
    """
    Get inference result based on the request.

    :param request_fastapi: FastAPI request object.
    :param request: InferenceRequest object.
    :param users_dao: DAO for users models.
    :return: Model-specific InferenceResponse object.
    """
    # TODO: Add error 422 for incorrect arguments, model, or provider
    # TODO: Add error 500
    # TODO: Create a separate function and endpoint for updating credits
    user_id = request_fastapi.state.user_id
    users = await get_credits(user_id, users_dao)
    available_credits = float(users[0].credits if users else 0)

    model_type = get_model_type(request.model)
    if model_type == "chat":
        language_model = CompletionsModel(
            provider=request.provider,
            model=request.model,
        )

        cost_max = language_model.get_cost_max()
        if available_credits < cost_max:
            return InferenceResponse(
                response={
                    "Error": "Insufficient credits",
                },
            )
        stream = request.arguments.get("stream", False)
        response, cost = language_model.get_completion(
            messages=request.arguments["messages"],
            temperature=request.arguments.get("temperature", 0.9),  # noqa: WPS432
            max_tokens=request.arguments.get("max_tokens", None),
            stream=stream,
        )
        if stream:

            async def stream_and_update_db():  # noqa: WPS430
                async for part_json in response.generator():
                    yield part_json
                await users_dao.recharge_credit(user_id, -response.total_cost)

            return StreamingResponse(stream_and_update_db())
        else:
            await users_dao.recharge_credit(user_id, -cost)

        if not response:
            # TODO: Handle when response is None
            return InferenceResponse(
                response={
                    "model": request.model,
                    "created": 0,
                    "id": "",
                    "choices": [],
                    "object": "chat.completion",
                    "usage": {},
                },
            )
        if isinstance(response, ChatCompletionResponse):
            response.model = request.model
            return InferenceResponse(
                response=response.model_dump(),
            )

        if not isinstance(response["usage"], dict) and response["usage"]:
            usage = response["usage"].model_dump()
        elif response["usage"]:
            usage = response["usage"]
        else:
            usage = None
        if response.get("choices", None):
            choices = []
            for choice in response.get("choices", None):
                choices.append(choice.model_dump())
        return InferenceResponse(
            response={
                "model": request.model,
                "created": response.get("created", None),
                "id": response["id"],
                "choices": choices,
                "object": "chat.completion",
                "usage": usage,
            },
        )
    elif model_type == "image":
        image_model = ImagegenModel(
            provider=request.provider,
            model=request.model,
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
            return InferenceResponse(
                response={
                    "model": request.model,
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
        return InferenceResponse(
            response={
                "model": request.model,
                "created": response.get("created", None),
                "images": base64_images,
                "object": "image.generation",
            },
        )
    # TODO: Add error 422 for incorrect arguments, model, or provider
    return InferenceResponse(
        response={
            "Error": "Unknown model or provider",
        },
    )
