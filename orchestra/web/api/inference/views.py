import base64
import re

from fastapi import APIRouter
from models.imagegen import ImagegenModel
from models.llm import CompletionsModel

from orchestra.web.api.chat_completion.schema import ChatCompletionResponse
from orchestra.web.api.inference.schema import InferenceRequest, InferenceResponse

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
    request: InferenceRequest,
) -> InferenceResponse:
    """
    Get inference result based on the request.

    :param request: InferenceRequest object.

    :return: Model-specific InferenceResponse object.
    """
    # TODO: Add error 422 for incorrect arguments, model, or provider
    # TODO: Add error 500
    model_type = get_model_type(request.model)
    if model_type == "chat":
        language_model = CompletionsModel(
            provider=request.provider,
            model=request.model,
        )
        response = language_model.get_completion(
            messages=request.arguments["messages"],
            temperature=request.arguments["temperature"],
        )
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
        usage = response["usage"].model_dump() if response["usage"] else None
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
            "image": request.arguments.get("image", None),
            "height": request.arguments.get("height", None),
            "width": request.arguments.get("width", None),
            "steps": request.arguments.get("steps", None),
            "samples": request.arguments.get("samples", None),
            "cfg_scale": request.arguments.get("cfg_scale", None),
            "sampler": request.arguments.get("sampler", None),
            "seed": request.arguments.get("seed", None),
            "mask_image": request.arguments.get("mask_image", None),
            "start_schedule": request.arguments.get("start_schedule", None),
            "end_schedule": request.arguments.get("end_schedule", None),
            "prompt_2": request.arguments.get("prompt_2", None),
            "negative_prompt": request.arguments.get("negative_prompt", None),
            "negative_prompt_2": request.arguments.get("negative_prompt_2", None),
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
