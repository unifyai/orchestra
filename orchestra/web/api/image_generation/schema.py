from typing import List, Optional

from pydantic import BaseModel


class ImageGenerationRequest(BaseModel):
    """
    Request model for image generation based language model.

    Attributes:
        model (str): The model identifier.
        prompt (str): The prompt for image generation.
        image (Optional[bytes]): The image for image generation.
        height (Optional[int]): The height of the generated image.
        width (Optional[int]): The width of the generated image.
        steps (Optional[int]): The number of diffusion steps for image generation.
        samples (Optional[int]): The number of images to be generated.
        cfg_scale (Optional[float]): Dictates how closely a generation matches
        provided prompt.
        sampler (Optional[str]): The sampling engine to be used for image generation.
        seed (Optional[int]): The seed for random latent noise generation.
        mask_image (Optional[bytes]): Greyscale mask to exclude diffusion
        for some pixels.
        start_schedule (Optional[float]): The start schedule for image generation.
        end_schedule (Optional[float]): The end schedule for image generation.
    """

    model: str
    prompt: str
    image: Optional[bytes] = None
    height: Optional[int] = None
    width: Optional[int] = None
    steps: Optional[int] = None
    samples: Optional[int] = None
    cfg_scale: Optional[float] = None
    sampler: Optional[str] = None
    seed: Optional[int] = None
    mask_image: Optional[bytes] = None
    start_schedule: Optional[float] = None
    end_schedule: Optional[float] = None


class ImageGenerationResponse(BaseModel):
    """
    Response model for image generation based language model.

    Attributes:
        model (str): The model identifier.
        created (int): Timestamp indicating when the response was created.
        id (str): Identifier for the completion response.
        object (str): The type of object, defaults to "image.generation".
        usage (dict): Usage statistics or additional information.
        choices (List[Dict]): List of completion choices.
    """

    created: Optional[int] = None
    object: Optional[str] = "image.generation"
    images: List[str]
    model: str
