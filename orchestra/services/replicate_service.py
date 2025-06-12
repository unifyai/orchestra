import logging
from typing import List, Optional

import replicate
from fastapi import HTTPException, status

from orchestra.settings import settings


class ReplicateAPIError(HTTPException):
    def __init__(self, status_code: int, detail: str):
        super().__init__(status_code=status_code, detail=detail)


class ReplicateService:
    """
    Service for interacting with the Replicate API.
    """

    def __init__(self):
        """
        Initializes the Replicate service.
        The Replicate Python client automatically uses the REPLICATE_API_TOKEN
        environment variable. We check for its presence in settings.
        """
        if (
            not hasattr(settings, "replicate_api_token")
            or not settings.replicate_api_token
        ):
            raise ValueError("replicate_api_token is not set in settings.")
        self.client = replicate

    def generate_photo(
        self,
        prompt: str,
        aspect_ratio: str,
        output_format: str,
        output_quality: int,
        safety_tolerance: float,
        prompt_upsampling: bool,
    ) -> str:
        """
        Generates a new image from a text prompt using FLUX 1.1 Pro.
        """
        try:
            # Pinned version for model for reproducibility
            model_version = "black-forest-labs/flux-1.1-pro:1a2c1db0e073c6838a3f8dee7125b3a6e84d416b474709214a1a3e8784d48344"
            output: Optional[List[str]] = self.client.run(
                model_version,
                input={
                    "prompt": prompt,
                    "aspect_ratio": aspect_ratio,
                    "output_format": output_format,
                    "output_quality": output_quality,
                    "safety_tolerance": safety_tolerance,
                    "prompt_upsampling": prompt_upsampling,
                },
            )
            if not output or not isinstance(output, list) or len(output) == 0:
                raise ReplicateAPIError(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Replicate API did not return an image URL.",
                )
            return output[0]
        except Exception as e:
            logging.error(f"Replicate generate_photo failed: {e}")
            raise ReplicateAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to Replicate API failed: {e}",
            )

    def edit_photo(
        self,
        prompt: str,
        input_image: str,
        aspect_ratio: str,
        output_format: str,
        safety_tolerance: float,
    ) -> str:
        """
        Edits an image with a text prompt using FLUX Kontext Pro.
        """
        try:
            # Pinned version for model for reproducibility
            model_version = "black-forest-labs/flux-kontext-pro:178e63a93645b9508628ec23537449a03b6441b8c85743b1ac6146193f0b8d0e"
            output: Optional[List[str]] = self.client.run(
                model_version,
                input={
                    "prompt": prompt,
                    "input_image": input_image,
                    "aspect_ratio": aspect_ratio,
                    "output_format": output_format,
                    "safety_tolerance": safety_tolerance,
                },
            )
            if not output or not isinstance(output, list) or len(output) == 0:
                raise ReplicateAPIError(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Replicate API did not return an image URL.",
                )
            return output[0]
        except Exception as e:
            logging.error(f"Replicate edit_photo failed: {e}")
            raise ReplicateAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to Replicate API failed: {e}",
            )
