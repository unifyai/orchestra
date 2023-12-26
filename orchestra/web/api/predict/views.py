import base64
import re

from fastapi import APIRouter
from models.imagegen import ImagegenModel
from models.llm import CompletionsModel

from orchestra.web.api.chat_completion.schema import ChatCompletionResponse
from orchestra.web.api.predict.schema import PredictRequest, PredictResponse

router = APIRouter()


def get_model_type(model_name):  # noqa: D103
    chat_models = re.compile(
        r"(gpt|llama|zephyr|mistral|mixtral|pplx|falcon|wizard|mpt|claude)",  # noqa: WPS360, E501
    )
    image_models = re.compile(r"diffusion")  # noqa: WPS360

    if chat_models.search(model_name):
        return "chat"
    elif image_models.search(model_name):
        return "image"


@router.post("/predict", response_model=PredictResponse)
async def get_prediction(
    request: PredictRequest,
) -> PredictResponse:  # noqa: C901, WPS212, WPS210, WPS231, E501
    """
    Get prediction result based on the request.

    :param request: PredictRequest object.

    :return: Model-specific PredictResponse object.
    """
    # TODO: Abstract this so that the modality and the task is
    # used to get the class (in this case, CompletionsModel)

    # TODO: Specify the arguments in the docs, discuss how are we going to define it
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
            return PredictResponse(
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
            return PredictResponse(
                response=response.model_dump(),
            )
        usage = response["usage"].model_dump() if response["usage"] else None
        if response.get("choices", None):
            choices = []
            for choice in response.get("choices", None):
                choices.append(choice.model_dump())
        return PredictResponse(
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
            "image": request.arguments["image"],
            "height": request.arguments["height"],
            "width": request.arguments["width"],
            "steps": request.arguments["steps"],
            "samples": request.arguments["samples"],
            "cfg_scale": request.arguments["cfg_scale"],
            "sampler": request.arguments["sampler"],
            "seed": request.arguments["seed"],
            "mask_image": request.arguments["mask_image"],
            "start_schedule": request.arguments["start_schedule"],
            "end_schedule": request.arguments["end_schedule"],
        }
        response = image_model.get_image(
            prompt=request.arguments["prompt"],
            kwargs=kwargs,
        )
        if not response:
            # TODO: Handle when response is None
            return PredictResponse(
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
        return PredictResponse(
            response={
                "model": request.model,
                "created": response.get("created", None),
                "images": base64_images,
                "object": "image.generation",
            },
        )
    # TODO: Add error 422 for incorrect arguments, model, or provider
    return PredictResponse(
        response={
            "Error": "Unknown model or provider",
        },
    )
