import base64

from fastapi import APIRouter
from models.imagegen import ImagegenModel

from orchestra.web.api.image_generation.schema import (
    ImageGenerationRequest,
    ImageGenerationResponse,
)

router = APIRouter()


@router.post("/image/generation", response_model=ImageGenerationResponse)
async def get_generations(request: ImageGenerationRequest) -> ImageGenerationResponse:
    """
    Get image generations based on the request.

    :param request: ImageGenerationRequest object.

    :return: ImageGenerationResponse object.
    """
    imagegen_model = ImagegenModel(
        provider=request.model.split("/")[0],
        model=request.model.split("/")[1],
    )
    kwargs = {
        "image": request.image,
        "height": request.height,
        "width": request.width,
        "steps": request.steps,
        "samples": request.samples,
        "cfg_scale": request.cfg_scale,
        "sampler": request.sampler,
        "seed": request.seed,
        "mask_image": request.mask_image,
        "start_schedule": request.start_schedule,
        "end_schedule": request.end_schedule,
    }
    response = imagegen_model.get_image(prompt=request.prompt, kwargs=kwargs)

    if not response:
        # TODO: Handle when response is None
        return ImageGenerationResponse(
            model=request.model,
            created=0,
            images=[],
            object="image.generations",
        )

    base64_images = [
        base64.b64encode(image).decode("utf-8")
        for image in response.get("images", [])  # Use empty list for default
        if image is not None
    ]

    return ImageGenerationResponse(
        model=request.model,
        created=response.get("created", None),
        images=base64_images,
        object=response.get("object", None),
    )
