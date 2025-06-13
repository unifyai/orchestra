import logging
import os
from typing import List, Optional

from fastapi import HTTPException, status


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
        It looks for ORCHESTRA_REPLICATE_API_KEY from the environment and sets the
        REPLICATE_API_TOKEN environment variable, which the replicate client uses.
        """
        try:
            # Get the API key directly from the environment
            replicate_api_key = os.getenv("ORCHESTRA_REPLICATE_API_KEY")
            if not replicate_api_key:
                raise ValueError(
                    "ORCHESTRA_REPLICATE_API_KEY environment variable is not set.",
                )

            # Set the environment variable that the 'replicate' library expects
            os.environ["REPLICATE_API_TOKEN"] = replicate_api_key

            import replicate  # Defer import until initialization

            self.client = replicate
        except ImportError:
            logging.error(
                "The 'replicate' library is not installed. Please install it with 'pip install replicate'.",
            )
            raise RuntimeError("Replicate library not found.")
        except ValueError as e:
            logging.error(f"Replicate service initialization failed: {e}")
            # Re-raise to be caught by FastAPI's dependency management
            raise e

    async def generate_photo(
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
            model_identifier = "black-forest-labs/flux-1.1-pro"
            output: Optional[List[str]] = await self.client.async_run(
                model_identifier,
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

    async def edit_photo(
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
            model_identifier = "black-forest-labs/flux-kontext-pro"
            output: Optional[List[str]] = await self.client.async_run(
                model_identifier,
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
