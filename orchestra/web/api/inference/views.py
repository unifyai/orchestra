import json
import re

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.param_functions import Depends
from fastapi.responses import JSONResponse, StreamingResponse
from providers.completion import PROVIDER_CLASSES
from starlette import status

from orchestra.db.dao.benchmark_run_dao import BenchmarkRunDAO
from orchestra.db.dao.datapoint_dao import DatapointDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.model_dao import ModelDAO
from orchestra.db.dao.provider_dao import ProviderDAO
from orchestra.db.dao.query_dao import QueryDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.web.api.inference.schema import InferenceRequest
from orchestra.web.api.users.views import get_credits
from orchestra.web.api.utils.bg_tasks import db_operations
from orchestra.web.api.utils.dynamic_routing import dynamic_routing, parse_endpoint
from orchestra.web.api.utils.helpers import filter_request_params
from orchestra.web.api.utils.http_responses import (
    insufficient_credits_error,
    invalid_messages,
)

router = APIRouter()


def _get_model_type(model_name):
    # TODO: Do this properly based on the model task
    image_models = re.compile(r"diffusion|sd")  # noqa: WPS360
    if image_models.search(model_name):
        return "text-to-image"
    return "text-generation"


def _verify_field(request, field):
    if not hasattr(request, field) or getattr(request, field) == "":
        raise HTTPException(  # TODO: Move to utils file
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid input. A {field} has to be specified.",
        )
    return getattr(request, field)


@router.post("/inference")
async def post_inference(  # noqa: C901, WPS212, WPS210, WPS231, E501, WPS211, WPS217, WPS238
    background_tasks: BackgroundTasks,
    request_fastapi: Request,
    request: InferenceRequest,
    users_dao: UsersDAO = Depends(),
    model_dao: ModelDAO = Depends(),
    provider_dao: ProviderDAO = Depends(),
    endpoint_dao: EndpointDAO = Depends(),
    query_dao: QueryDAO = Depends(),
    benchmark_run_dao: BenchmarkRunDAO = Depends(),
    datapoint_dao: DatapointDAO = Depends(),
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
    # TODO: Add error 500
    # TODO: Create a separate function and endpoint for updating credits
    # TODO: check that the model exists (another error).
    # TODO: Check that the model exists and that the provider is hosting
    # the provider. (With different errors)
    model = _verify_field(request, "model")
    provider = _verify_field(request, "provider")

    user_id = request_fastapi.state.user_id
    user = get_credits(request_fastapi, users_dao=users_dao)
    available_credits = float(user.credits if user else 0)

    if provider not in PROVIDER_CLASSES:
        # Dynamic routing
        target_metric, metrics_thresholds = parse_endpoint(provider)
        provider = dynamic_routing(
            endpoint_dao,
            benchmark_run_dao,
            target_metric,
            models=(model,),
            metrics_thresholds=metrics_thresholds,
        )

    # TODO: This will fail when it's not a llm
    try:
        messages = request.arguments["messages"]
    except Exception:
        raise invalid_messages

    model_type = _get_model_type(model)

    # TODO: Decompose this further
    if model_type == "text-generation":

        lm = PROVIDER_CLASSES[provider](model)
        if available_credits < lm.max_cost:
            raise insufficient_credits_error

        stream = request.arguments.get("stream", False)

        filtered_params = filter_request_params(request.arguments)

        db_operations_kwargs = {
            "user_id": user_id,
            "model": model,
            "provider": provider,
            "model_dao": model_dao,
            "provider_dao": provider_dao,
            "endpoint_dao": endpoint_dao,
            "query_dao": query_dao,
            "users_dao": users_dao,
        }

        response, cost = lm(messages=messages, **filtered_params)

        # TODO: Handle when response is None
        if not response:
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

            def stream_and_update_db():  # noqa: WPS430
                for part_dict in response.generator():
                    part_dict["model"] = model
                    part_dict["provider"] = provider
                    yield f"data: {json.dumps(part_dict)}\n\n"
                background_tasks.add_task(
                    db_operations,
                    cost_deferred_fn=response.total_cost,
                    **db_operations_kwargs,
                )

            return StreamingResponse(stream_and_update_db())

        else:
            background_tasks.add_task(
                db_operations, cost_deferred_fn=cost, **db_operations_kwargs
            )

        response["model"] = model
        response["provider"] = provider
        return JSONResponse(response)

    """
    elif model_type == "text-to-image":
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
    """
